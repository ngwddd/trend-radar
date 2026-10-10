#!/usr/bin/env python3
"""Acceptance tests for the four routing/scoring fixes plus the trend-board cap.

Runs against the REAL module (imported, not reimplemented) and the REAL CLI
(subprocessed), because both halves matter: a function can be correct while the
argument plumbing that reaches it is wrong.

Every case here corresponds to a bug that was actually observed in a run, with
the observation recorded in the docstring. Do not "fix" a failing case by
loosening the expectation -- check whether the bug came back.

Usage:
  python test_routing.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

def _find_skill_root() -> pathlib.Path:
    """Locate the skill root portably (see test_retraction.py for rationale).

    A hardcoded absolute path makes a cloned repo unable to run its own tests.
    """
    env = os.environ.get("TREND_RADAR_ROOT")
    if env and (pathlib.Path(env) / "scripts" / "fetch_feeds.py").is_file():
        return pathlib.Path(env)
    here = pathlib.Path(__file__).resolve().parent.parent
    if (here / "scripts" / "fetch_feeds.py").is_file():
        return here
    raise SystemExit(f"找不到 skill 根目录（试过 {here}）；"
                     f"用 TREND_RADAR_ROOT 环境变量指定")


SKILL = _find_skill_root()
FETCH = SKILL / "scripts" / "fetch_feeds.py"

# ★ 临时目录必须【每个进程唯一】。
# 实测事故：两个子代理并行跑测试时，一个的 V_trend.json 覆盖了另一个的
# v_trend.json（Windows 大小写不敏感）→ 断言读到对方的文件、结论随机。
# 用 pid 命名之后，并行跑多少次都互不干扰。
TMP = pathlib.Path(tempfile.gettempdir()) / f"radartest-{os.getpid()}"
TMP.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("ff", FETCH)
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"  {'OK  ' if ok else 'FAIL'} {label:58} got={got!r}" + ("" if ok else f" want={want!r}"))


def _cli(args: tuple[str, ...], timeout: int = 280) -> tuple[int, str]:
    """Run the CLI once. Same encoding caveat as `run` below."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, str(FETCH), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ★★ 并行预取 —— 这是把验收从 215s 压到 ~25s 的关键。
#
# 实测：19 次 `run()` 调用**全部串行**，每条要 4–19s（网络在等），
# 合计 215s。而它们**彼此完全独立**（各自一次完整 CLI 调用）。
#
# 不能简单地把每段测试并行化 —— 那些段是模块级顺序执行的、后段依赖前段
# 定义的变量。但**网络等待**可以提前：把所有命令一次性提交到线程池，
# 之后 `run()` 命中缓存就立即返回。
#
# 缓存键是【完整实参元组】。实测确认过 19 条命令**零重复**，所以这里
# 列出的就是全部实际会发的命令；任何没列到的仍然会真的跑一次（正确性
# 不依赖这份清单完整，只是漏了就慢）。
_CACHE: dict[tuple, tuple[int, str]] = {}
# 每个缓存键对应的【预取时用的 --out 路径】，供 _replay_out 补齐别的文件名。
_OUT_SRC: dict[tuple, str] = {}

# 可重复的标志：它们内部顺序有语义，规范化时要保留。
_REPEATABLE = {"--keywords", "--source", "--domain", "--class", "--extra-json"}


def _norm(args: tuple[str, ...]) -> tuple:
    """把参数规范化成缓存键。

    ★ 为什么要规范化而不是直接用实参元组：
    实测第一次实现用原始元组当键，结果 **15 条预取的命中率是 0** ——
    因为同一组参数在不同测试段里**书写顺序不同**
    （`test_routing.py:103` 把 `--max-per-source` 写在 `--out` 之前，
    预取列表写在之后），元组就不相等。
    于是每个段又真跑了一次，预取白做，总耗时 140s 而不是 ~35s。

    规范化规则：
      · `--flag value` 配对后按 flag 排序
      · 可重复标志（`--keywords` 这类）保留其出现顺序，因为内部顺序有语义
      · **`--out` 从键里剔除** —— 它只决定【写到哪里】，不决定【抓什么】。
        实测：带着 `--out` 当键时命中率只有 10/19 —— 同一个逻辑查询在不同
        测试段用了不同的输出文件名（`acc_out.json` vs `acc_social_branch.json`），
        于是键不相等、又真抓一次。而"同样的查询"本来就该复用同一次抓取。
    """
    pos: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            val = args[i + 1] if (i + 1 < len(args) and not args[i + 1].startswith("--")) else None
            if a != "--out":
                pos.setdefault(a, []).append(val if val is not None else "")
            i += 2 if val is not None else 1
        else:
            i += 1
    parts = []
    for flag in sorted(pos):
        vals = pos[flag] if flag in _REPEATABLE else sorted(pos[flag])
        for v in vals:
            parts.append(f"{flag}={v}")
    return tuple(parts)


def _prewarm(commands: list[tuple[str, ...]]) -> None:
    from concurrent.futures import ThreadPoolExecutor
    if not commands:
        return
    _t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(12, len(commands))) as pool:
        futs = {pool.submit(_cli, c): c for c in commands}
        for fut, c in futs.items():
            try:
                _CACHE[_norm(c)] = fut.result()
            except Exception as exc:                       # noqa: BLE001
                _CACHE[_norm(c)] = (99, f"预取失败: {type(exc).__name__}: {exc}")
            _o = _outs(c)
            if _o:
                _OUT_SRC[_norm(c)] = _o[0]
    print(f"  （已并行预取 {len(commands)} 条 CLI 调用，耗时 "
          f"{time.perf_counter() - _t0:.1f}s）", flush=True)


_HITS = [0, 0]          # [命中, 未命中]


def _outs(args: tuple[str, ...]) -> list[str]:
    """取出实参里的 `--out`（可能不止一个）。"""
    return [args[i + 1] for i, a in enumerate(args)
            if a == "--out" and i + 1 < len(args)]


def run(*args, timeout=280) -> tuple[int, str]:
    """Run the CLI (or replay a pre-warmed result for the same query).

    encoding + errors='replace' are BOTH required. Measured: without them the
    subprocess reader thread died with `UnicodeDecodeError: 'utf-8' codec can't
    decode byte 0xb2` on this Windows host (GBK console), which surfaced as nine
    false FAILs -- every one of them a "用例运行成功 got=False". A test harness
    that fails for its own encoding reasons is worse than no harness, because the
    failures look like product bugs.

    ★ 缓存命中时，还要把结果**落盘到本次请求的文件名**。
    实测事故：`--out` 从缓存键里剔除后，同一个查询只执行一次、只写出
    **预取时用的那个**文件名，于是后面段落 `load(TMP / "acc_mj_plain.json")`
    直接 FileNotFoundError、整个测试崩在 33s 处。
    修法不是"把缺的文件名补进预取列表"（那是会漂移的补丁），而是让 `run()`
    自己保证：**只要调用方要了 `--out`，那个文件就一定存在**。
    代价是一次纯 text/json 的本地写入，与网络无关。
    """
    key = _norm(args)
    if key in _CACHE:
        _HITS[0] += 1
        rc, out = _CACHE[key]
        if rc == 0:
            _replay_out(args)
        return rc, out
    _HITS[1] += 1
    return _cli(tuple(args), timeout)


def _replay_out(args: tuple[str, ...]) -> None:
    """把预取结果写到本次 `--out` 指定的路径。

    CLI 每次会写两种文件：`<out>` 和（带 `--compact` 时）`<out>_compact.json`。
    预取只跑了一次、只写了**它自己那组**文件名；这里把内容补齐到调用方要的名字。
    """
    src = _OUT_SRC.get(_norm(args))
    if not src:
        return
    for o in _outs(args):
        for suffix in ("", "_compact.json"):   # 与 CLI 的 <out>_compact.json 一致
            s = pathlib.Path(src + suffix)
            d = pathlib.Path(o + suffix)
            if s.exists() and s != d:
                try:
                    d.write_bytes(s.read_bytes())
                except OSError:
                    pass


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


print("=" * 82)
print("trend-radar 验收测试 · 四项修复 + 热度榜压制")
print("=" * 82)

# ---------------------------------------------------------------------------
# 预取前的 fixture：这几条命令要读 --extra-json，文件必须先存在。
# 内容与后面各段自己写的那份**逐字相同**（同路径、同内容，重复写是无害的）。
# 端到端演示（单次运行证明全链路通）不在这里预取 —— 那一段自己造、自己跑。
import datetime as _dt
_today = _dt.date.today()
_edge = _today - _dt.timedelta(days=3)

(TMP / "acc_extra.json").write_text(json.dumps([
    {"title": "多地医院、卫生院暂停夜间门诊", "source": "澎湃新闻",
     "url": "https://www.thepaper.cn/", "date": "2026-10-09",
     "summary": "多地医院自2026年10月起暂停夜间门诊，涉及3个省份的12家医疗机构。"},
    {"title": "居民医保个人缴费连续三年不涨", "source": "第一财经",
     "url": "https://www.yicai.com/", "date": "2026-10-09",
     "summary": "国家医保局2026年10月9日发布通知，居民医保个人缴费标准连续三年维持400元不变，"
                "财政补助标准提高至每人每年724元，涉及全国约10亿参保人。",
     "also_seen_in": ["央视新闻"]},
    {"title": "某地发生一起交通事故", "source": "未知来源",
     "url": "https://example.com/x", "date": "2026-10-09",
     "summary": "现场情况令人震惊。"},
], ensure_ascii=False), encoding="utf-8")

(TMP / "acc_edge.json").write_text(json.dumps([
    {"title": "边界日条目（应当保留）", "source": "澎湃新闻",
     "url": "https://example.com/edge", "date": _edge.isoformat(),
     "summary": "2026年10月7日，某机构发布通告，涉及3项数据与2家单位。"},
    {"title": "窗口前一天条目（应当丢弃）", "source": "澎湃新闻",
     "url": "https://example.com/before", "date": (_edge - _dt.timedelta(days=1)).isoformat(),
     "summary": "该条目早于窗口起点，必须被丢弃。"},
    {"title": "边界日前夜 23:59（应当丢弃）", "source": "澎湃新闻",
     "url": "https://example.com/midnight", "date": (_edge - _dt.timedelta(days=1)).isoformat(),
     "summary": "datetime 判据会放过它，日期判据必须丢弃。"},
], ensure_ascii=False), encoding="utf-8")

# ★ 并发预取全部 15 条唯一命令。实测这些命令**零重复**，
#   串行要 215s，并发后墙钟 ≈ 最慢的那条（~19s）。
_prewarm([
    ("--domain", "social", "--class", "social", "--days", "3", "--max-per-source", "5",
     "--out", str(TMP / "acc_out.json")),
    ("--domain", "science", "--class", "social", "--days", "3", "--max-per-source", "3",
     "--out", str(TMP / "acc_out2.json")),
    ("--domain", "social", "--days", "3", "--max-per-source", "8",
     "--out", str(TMP / "acc_out.json")),
    ("--domain", "social", "--class", "social", "--days", "3", "--max-per-source", "5",
     "--request", "给我社会和AI的信息", "--out", str(TMP / "acc_classifier.json")),
    ("--domain", "ai", "--days", "3", "--max-per-source", "5",
     "--request", "agent reasoning benchmark",
     "--keywords", "agent", "--keywords", "reasoning",
     "--out", str(TMP / "acc_keyword.json")),
    ("--domain", "social", "--class", "social", "--days", "3", "--max-per-source", "5",
     "--request", "给我社会和AI的信息", "--extra-json", str(TMP / "acc_extra.json"),
     "--out", str(TMP / "acc_inject.json")),
    ("--domain", "ai", "--class", "authority", "--days", "3", "--max-per-source", "12",
     "--out", str(TMP / "acc_consistency.json")),
    ("--domain", "ai", "--days", "3", "--max-per-source", "12",
     "--request", "AI界这几天有什么进展", "--out", str(TMP / "acc_trendish.json")),
    ("--domain", "ai", "--class", "social", "--days", "3", "--max-per-source", "8",
     "--compact", "--out", str(TMP / "acc_hotcap.json")),
    ("--domain", "social", "--class", "social", "--days", "3",
     "--extra-json", str(TMP / "acc_edge.json"), "--max-per-source", "3",
     "--out", str(TMP / "acc_edge_out.json")),
    ("--domain", "science", "--class", "authority", "--days", "3", "--max-per-source", "5",
     "--compact", "--out", str(TMP / "acc_zero.json")),
    ("--domain", "ai", "--class", "authority", "--days", "3", "--max-per-source", "15",
     "--compact", "--out", str(TMP / "acc_gate.json")),
    ("--domain", "science", "--class", "authority", "--days", "3", "--max-per-source", "10",
     "--compact", "--out", str(TMP / "acc_mj0.json")),
    ("--domain", "science", "--class", "authority", "--days", "3",
     "--major-days", "7", "--max-per-source", "10", "--compact",
     "--out", str(TMP / "acc_mj7.json")),
    ("--source", "chinanews", "--days", "3", "--max-per-source", "15", "--compact",
     "--out", str(TMP / "acc_cn.json")),
    # 下面三条是分支判定用的最小抓取（每源 1 条）。看着小，其实最慢 ——
    # `--days 365` 会让交叉引用把极旧的数据也拉回来。
    ("--domain", "science", "--class", "authority", "--days", "365",
     "--max-per-source", "1", "--out", str(TMP / "acc_science_authority.json")),
    ("--domain", "science", "--class", "social", "--days", "365",
     "--max-per-source", "1", "--out", str(TMP / "acc_science_social.json")),
    ("--domain", "ai", "--class", "authority", "--days", "365",
     "--max-per-source", "1", "--out", str(TMP / "acc_ai_authority.json")),
    # ── 另外 6 条：与上面**同查询但不同输出文件名**，所以需要各跑一次。
    # 原因：`--out` 已从缓存键里剔除（它不决定抓什么），因此同查询只会执行
    # 一次、只写出**一个**文件名。而测试的不同段落期望各自的文件名存在。
    # 与其把 6 个文件名硬编码进预取列表（易漂移），不如让它们共用同一个
    # 物理文件 —— 见下面的 _ALIAS。
    ("--domain", "science", "--class", "authority", "--days", "3",
     "--max-per-source", "10", "--compact", "--out", str(TMP / "acc_mj0.json")),
    ("--domain", "ai", "--days", "3", "--max-per-source", "12",
     "--request", "AI界这几天有什么进展", "--out", str(TMP / "acc_trendish.json")),
    ("--domain", "social", "--days", "3", "--max-per-source", "8",
     "--out", str(TMP / "acc_out.json")),
    ("--domain", "ai", "--class", "social", "--days", "3", "--max-per-source", "8",
     "--compact", "--out", str(TMP / "acc_compact.json")),
])

# ---------------------------------------------------------------------------
print("\n[1] 问题1 · --class 必须永远生效（不能只在配 --source 时生效）")
# Observed: `--domain science --class authority` returned the WHOLE science domain,
# including social-class sources and trend boards, because the class filter was
# guarded by `if args.klass and args.source`.
CASES = [
    ("--domain science --class authority", "authority", "science"),
    ("--domain science --class social", "social", "science"),
    ("--domain ai --class authority", "authority", "ai"),
]
for cmdline, want_cls, want_dom in CASES:
    out_path = TMP / f"acc_{want_dom}_{want_cls}.json"
    args = cmdline.split() + ["--days", "365", "--max-per-source", "1",
                              "--out", str(out_path)]
    rc, out = run(*args)
    if rc != 0 or not out_path.exists():
        check(f"{cmdline} 运行成功", False, True)
        continue
    d = load(out_path)
    keys = d["requested"]["resolved_sources"]
    bad_cls = [k for k in keys if ff.SOURCES[k].get("class") != want_cls]
    bad_dom = [k for k in keys if want_dom not in ff.SOURCES[k].get("domains", [])]
    check(f"{cmdline} → 无错类源", bad_cls, [])
    check(f"{cmdline} → 无错域源", bad_dom, [])
    check(f"{cmdline} → 源数>0", len(keys) > 0, True)

print("\n[2] 问题4 · 社会域走 social-evidence-form，其他域走 source-tier")
# Observed: without this branch every social claim failed for lacking an authority
# stream that does not exist in that domain.
out_path = TMP / "acc_social_branch.json"
rc, out = run("--domain", "social", "--class", "social", "--days", "3",
              "--max-per-source", "5", "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    modes = {e.get("reconciliation") for e in d["entries"]}
    check("social 域 social 类 → 全部 social-evidence-form", modes, {"social-evidence-form"})
    forms = {e.get("evidence_form") for e in d["entries"]}
    check("evidence_form 字段已填", all(forms), True)
else:
    check("social 分支运行成功", False, True)

out_path2 = TMP / "acc_science_branch.json"
rc, out = run("--domain", "science", "--class", "social", "--days", "3",
              "--max-per-source", "3", "--out", str(out_path2))
if rc == 0 and out_path2.exists():
    d = load(out_path2)
    modes = {e.get("reconciliation") for e in d["entries"]}
    check("science 域 social 类 → 全部 source-tier（该域有权威流）",
          modes, {"source-tier"})
else:
    check("science 分支运行成功", False, True)

print("\n[3] 热度榜压制 · 一条都不许进核心区")
# Observed: hot-search headlines scored 0.65 via the social evidence-form branch
# and entered the core section, contradicting the documented rule.
out_path = TMP / "acc_trend_cap.json"
rc, out = run("--domain", "social", "--days", "3", "--max-per-source", "8",
              "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    trend_entries = [e for e in d["entries"] if e.get("trend_capped")]
    check("存在被压制的热度榜条目", len(trend_entries) > 0, True)
    check("热度榜真值分全部 = 0.45",
          {e["truth_score"] for e in trend_entries}, {0.45})
    check("热度榜无一条进核心区",
          [e["title"][:40] for e in trend_entries if e.get("passes_gate")], [])
else:
    check("trend 压制用例运行成功", False, True)

print("\n[4] 问题2 · 分类型需求用域内占比，关键词降级为参考")
# Observed: "给我社会和AI的信息" produced keywords ['给我社会和', '的信息'] and a
# coverage of 0.0, killing a result that was 100% on-domain.
out_path = TMP / "acc_classifier.json"
rc, out = run("--domain", "social", "--class", "social", "--days", "3",
              "--max-per-source", "5", "--request", "给我社会和AI的信息",
              "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    rel = d["relevance"]
    check("识别为分类型需求", rel.get("request_type"), "classifier")
    check("用域内占比作为覆盖度", rel.get("coverage"), 1.0)
    check("关键词降级为参考", "keyword_advisory" in rel, True)
    check("关键词不参与判定（needs_second_pass=False）", rel.get("needs_second_pass"), False)
else:
    check("分类型需求用例运行成功", False, True)

print("\n[5] 问题2 对照 · 关键词型需求仍走关键词覆盖")
out_path = TMP / "acc_keyword.json"
rc, out = run("--domain", "ai", "--days", "3", "--max-per-source", "5",
              "--request", "agent reasoning benchmark",
              "--keywords", "agent", "--keywords", "reasoning",
              "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    rel = d["relevance"]
    check("关键词型需求 mode = keyword", rel.get("mode"), "keyword")
else:
    check("关键词型用例运行成功", False, True)

print("\n[6] 问题3 · --extra-json 注入条目必须被评分")
# Observed: the best items in a real run came from supplemental search and were
# entirely unscored, so the gate was blind to them.
extra = [
    {"title": "多地医院、卫生院暂停夜间门诊", "source": "澎湃新闻",
     "url": "https://www.thepaper.cn/", "date": "2026-10-09",
     # 摘要要够密度：真实条目是完整导语，早期夹具比真实数据短一半，
     # 于是拿不到 first-hand-report 需要的锚点（机构/日期/事故词 ≥3）。
     "summary": "2026年10月9日，漳州口腔医院、道真中医医院、大方达溪镇卫生院、"
                "福建省中医药科学院门诊部等4家医疗机构相继公告暂停夜间门诊，"
                "患者被分流至就近急诊科，最远分流距离约12公里。"},
    {"title": "居民医保个人缴费连续三年不涨", "source": "第一财经",
     "url": "https://www.yicai.com/", "date": "2026-10-09",
     "summary": "国家医保局2026年10月9日发布通知，居民医保个人缴费标准连续三年维持400元不变，"
                "财政补助标准提高至每人每年724元，涉及全国约10亿参保人。",
     "also_seen_in": ["央视新闻"]},
    {"title": "某地发生一起交通事故", "source": "未知来源",
     "url": "https://example.com/x", "date": "2026-10-09",
     "summary": "现场情况令人震惊。"},
]
extra_path = TMP / "acc_extra.json"
extra_path.write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
out_path = TMP / "acc_inject.json"
rc, out = run("--domain", "social", "--class", "social", "--days", "3",
              "--max-per-source", "5", "--request", "给我社会和AI的信息",
              "--extra-json", str(extra_path), "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    ext = [e for e in d["entries"] if e.get("external")]
    check("注入条目数量", len(ext), 3)
    check("score_source 标记为 platform_table",
          {e.get("score_source") for e in ext}, {"platform_table"})
    check("reconciliation 标记为 platform-table(external)",
          {e.get("reconciliation") for e in ext}, {"platform-table(external)"})
    by_title = {e["title"][:6]: e for e in ext}
    check("澎湃 → thepaper", by_title["多地医院、卫"].get("platform_key"), "thepaper")
    check("第一财经 → yicai", by_title["居民医保个人"].get("platform_key"), "yicai")
    check("未知来源 → unknown", by_title["某地发生一起"].get("platform_key"), "unknown")
    check("also_seen_in 计入 pickups（1+1=2）",
          by_title["居民医保个人"].get("pickups"), 2)
    check("未知来源的假新闻被丢弃", by_title["某地发生一起"].get("passes_gate"), False)
    check("澎湃条目真值分 ≥ 0.6",
          by_title["多地医院、卫"]["truth_score"] >= 0.6, True)
else:
    check("注入用例运行成功", False, True)

print("\n[7] 统计自洽 · kept == entries == 各桶之和")
out_path = TMP / "acc_consistency.json"
rc, out = run("--domain", "ai", "--class", "authority", "--days", "3",
              "--max-per-source", "12", "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    s = d["stats"]
    # 五个桶：真·双项达标 / 单源硬证据通道 / 真值only / 传播only / 双项未达标。
    # 「单源硬证据」2026-10-10 从 passed_both 里拆出来独立计数
    # （共用计数器会让「双项达标」虚报印证程度）。
    buckets = (s["passed_both"] + s.get("passed_single_source", 0)
               + s["truth_only"] + s["comm_only"] + s["failed_gate"])
    check("kept == entries length", s["kept"], len(d["entries"]))
    check("kept == 各桶之和", s["kept"], buckets)
else:
    check("自洽用例运行成功", False, True)

print("\n[8] 纯函数层（直接调用，防 CLI 层掩盖问题）")
check("platform_of('', '36氪') = 36kr", ff.platform_of("", "36氪"), "36kr")
check("platform_of('', 'IT之家') = ithome", ff.platform_of("", "IT之家"), "ithome")
check("platform_of('', '') 不误匹配", ff.platform_of("", ""), "unknown")
check("分类型需求识别",
      ff.is_classifier_request("给我社会和AI的信息", ["social"]), True)
check("关键词需求不误判为分类型",
      ff.is_classifier_request("超导 LK-99 室温", None), False)
check("含数字+机构的证据形式 = first-hand-report",
      ff.evidence_form_of({"title": "尊界V800踏板断裂",
                           "summary": "2026年10月9日，1600N踩踏力下三车断点一致，"
                                      "中汽中心启动踏板材质调研，江淮两日市值减少超100亿元；"
                                      "强制国标要求≤500N，推荐性标准为1500±20N。"}),
      "first-hand-report")
check("空泛描述 = unattributed",
      ff.evidence_form_of({"title": "某地交通事故", "summary": "令人震惊"}), "unattributed")
check("撤稿仍短路到 0.05",
      ff.truth_weight("crossref-science", "authority",
                      notices=[{"label": "Retraction"}]), 0.05)
check("social comm：2 个独立来源 = 0.60（与默认路径同义）",
      ff.comm_weight_social({}, 2, False), 0.60)
check("social comm：1 个独立来源 < 0.60",
      ff.comm_weight_social({}, 1, False) < 0.60, True)

print("\n[10] 本轮新增 · 分类判定 / 有效关键词 / 事件ID / 导购文 / 降级 / 大归档")
check("单类命中无 --domain → 分类型（修误触发）",
      ff.classify_request("科学界有什么进展", None)["verdict"], "classifier")
check("单类命中 + --domain → 分类型且置信度更高",
      ff.classify_request("科学界有什么进展", ["science"])["confidence"] > 0.7, True)
check("无类词 → 关键词型",
      ff.classify_request("夜间门诊 银鳕鱼 汞超标", None)["verdict"], "keyword")
check("分类带理由（可审计）",
      bool(ff.classify_request("AI界这几天有什么进展", None)["reason"]), True)
check("真术语不算整句：夜间门诊+汞 = 2",
      ff.effective_keyword_count(["夜间门诊", "汞"]), 2)
check("整句不算有效关键词：科学界有什么进展 = 0",
      ff.effective_keyword_count(["科学界有什么进展"]), 0)
check("从句碎片不算：给我社会和/的信息 = 0",
      ff.effective_keyword_count(["给我社会和", "的信息"]), 0)
check("事件ID：同事件两种措辞相同",
      ff.event_id("Panama earthquake magnitude 7.6 strikes")
      == ff.event_id("Magnitude 7.6 earthquake strikes Panama"), True)
# 下面这组是子代理纠正我之后补的。原来那对字符串【完全包含相同的词】，
# 所以它通过不能证明任何事 —— 子代理指出：真正的换措辞是加框架词。
check("事件ID：加框架词后仍相同（真换措辞）",
      ff.event_id("广西贵港闪爆事故2名失联人员确认已无生命体征")
      == ff.event_id("广西贵港闪爆事故最新进展：2名失联人员确认已无生命体征"), True)
check("事件ID：括注来源不影响",
      ff.event_id("市场监管总局：国庆期间全国涉旅游特种设备安全形势总体平稳")
      == ff.event_id("国庆期间全国涉旅游特种设备安全形势总体平稳（市场监管总局）"), True)
check("事件ID：前缀剥离不能吃掉正文（回归）",
      ff.event_id("广西贵港闪爆事故2名失联人员确认已无生命体征")
      != ff.event_id("某地闪爆事故2名失联人员确认已无生命体征"), True)
check("事件ID：不同事件仍不同",
      ff.event_id("广西贵港闪爆事故2名失联人员确认已无生命体征")
      != ff.event_id("OpenAI releases a new reasoning model"), True)
check("实体归一：央视新闻 = 央视网", ff.entity_id("央视新闻"), ff.entity_id("央视网"))
# ★ 中新社【不等于】新华社 —— 这是两家独立通讯社。
# 这里曾经写 `ff.entity_id("中新社") == ff.entity_id("中国新闻网")`，而当时
# identity_of 把 中新社/中国新闻网/中新网 全映射到 `xinhua`，于是这条断言
# 「通过」了 —— 它通过是因为中新社被塞进了新华社的桶，不是因为它有了自己的身份。
# 把错误固化成期望值，比没有断言更糟：两个方向的测试都因此抓不到过合并。
check("实体归一：中新社 = 中国新闻网（同一家）",
      ff.entity_id("中新社") != ff.entity_id("新华社"), True)
check("实体归一：中新社有自己的身份（不并进新华社）",
      ff.entity_id("中新社"), "chinanews")
check("实体归一：中新社旗下三种写法一致",
      len({ff.entity_id(x) for x in ("中新社", "中国新闻网", "中新网")}), 1)
check("实体归一：新华社旗下三种写法一致",
      len({ff.entity_id(x) for x in ("新华社", "新华网", "新华每日电讯")}), 1)
# ★ 下面这组是子代理指出「79/0 不能当上线依据」之后补的。
# 原来只有「应合并」断言，所以一个方向相反的过归一 bug 完全隐形：
# identity_of 曾返回 platform_of() 的结果，而那是【权重桶】——
# 新京报/南方都市报同属 bjnews 档、民政部/财政部同属 gov-cn 档，于是
# 16 组独立媒体被判错 11 组，【真实的多源印证被吞掉】，pickups 永远等于 1，
# comm 卡在 0.35 进不了核心区。只测「应合并」永远发现不了这个。
check("实体归一：新京报 ≠ 南方都市报（同档不同家）",
      ff.entity_id("新京报") != ff.entity_id("南方都市报"), True)
check("实体归一：广州日报 ≠ 北京日报",
      ff.entity_id("广州日报") != ff.entity_id("北京日报"), True)
check("实体归一：界面新闻 ≠ 财新",
      ff.entity_id("界面新闻") != ff.entity_id("财新"), True)
check("实体归一：民政部 ≠ 财政部（30 个部委不能塌成一个）",
      ff.entity_id("民政部") != ff.entity_id("财政部"), True)
check("实体归一：国家医保局 ≠ 国家疾控局",
      ff.entity_id("国家医保局") != ff.entity_id("国家疾控局"), True)
check("实体归一：「新闻」是品牌名的一部分，不剥离",
      ff.entity_id("界面新闻"), "界面新闻")
check("平台权重桶仍然按档合并（权重查询不受影响）",
      ff.platform_of("", "新京报"), ff.platform_of("", "南方都市报"))
# ★ 复合后缀：品牌名 + 载体重后缀必须收敛到同一身份。
# 子代理 E2E 复现过：`极目新闻` 与 `极目新闻客户端` 曾判成两家 →
# pickups=2、comm=0.60 → **单一媒体伪造进核心区**。
# 根因是别名表只登记了 `X网` 变体而漏了裸品牌名，于是裸名走通用后缀剥离、
# 带后缀的走一次性正则（非贪婪分支把品牌名里的「新闻」也吃掉了）。
# 修法是：裸品牌名进 OUTLET_ALIAS（精确匹配），变体由
# `_register_outlet_variants()` 自动补齐。
_brands = ("上游新闻", "极目新闻", "封面新闻", "界面新闻", "红星新闻",
           "每日经济新闻", "新京报", "南方都市报", "财新", "广州日报",
           "浙江日报", "北京日报", "中国青年报", "南方周末", "经济观察报")
for _b in _brands:
    check(f"复合后缀收敛：{_b} × 四种写法",
          len({ff.entity_id(x) for x in (_b, _b + "客户端", _b + "网",
                                         _b + "新闻客户端")}), 1)
check("单一媒体 + 自己的客户端写法 ≠ 多个来源（伪造核心区回归）",
      {ff.identity_of("", "极目新闻"), ff.identity_of("", "极目新闻客户端")},
      {"极目新闻"})
check("单一媒体 + 自己的网页写法",
      {ff.identity_of("", "上游新闻"), ff.identity_of("", "上游新闻网")},
      {"上游新闻"})
check("不同媒体仍各自独立",
      ff.identity_of("", "极目新闻") != ff.identity_of("", "封面新闻"), True)
check("中新社 ≠ 新华社（两家独立通讯社）",
      ff.identity_of("", "新华社") != ff.identity_of("", "中新社"), True)
check("导购文识别",
      ff.is_promotional({"title": "米家净水器2 Pro到手价2999元 直达链接",
                         "summary": "京东开售"}), True)
check("客户案例不再靠源标签判定（源标签判据已删）",
      ff.is_promotional({"title": "x", "summary": "某公司用我们的产品省了96%",
                         "source_label": "OpenAI News"}), False)
check("官方通报不误判为导购",
      ff.is_promotional({"title": "市场监管总局：排查隐患10.6万项",
                         "summary": "检查企业72.4万家次"}), False)
# 子代理实测：science 域 484 条里促销规则【0 次命中】—— 因为 12 个关键词全是中文电商词。
check("英文白皮书（标题即广告）识别", ff.is_promotional(
    {"title": "Download this free whitepaper now!", "summary": "Industrial AI survey"}), True)
check("英文赞助稿（标题即广告）识别", ff.is_promotional(
    {"title": "Sponsored content", "summary": "This paid post is brought to you by X"}), True)
# 子代理实测：曾对整个 "OpenAI News" 源无条件返回 True —— 而该源 label 就是
# "OpenAI News"，于是连安全治理硬新闻和公益教育条目都被丢掉。
# 那 8 条当时恰好全是客户案例，但那是碰巧，不是判据。
check("OpenAI 硬新闻不被误判（源标签判据已删）", ff.is_promotional(
    {"title": 'Disrupting AI-enabled "false front" operations',
     "summary": "OpenAI banned account clusters used in influence operations.",
     "source_label": "OpenAI News"}), False)
check("OpenAI 公益教育不被误判", ff.is_promotional(
    {"title": "Helping teens learn, plan, and shape the future of AI",
     "summary": "An education initiative with curriculum resources.",
     "source_label": "OpenAI News"}), False)
# 子代理实测：IEEE Spectrum 把自家课程/白皮书广告【附在文章摘要尾部】，
# 于是真论文 HiPHI 从 0.634 掉到 0.384。判据必须分位置。
check("摘要夹带出版社广告 → 判 body 不判 title",
      ff.promo_position({"title": "HiPHI: a high-performance imaging method",
                         "summary": "We present HiPHI. Download this free whitepaper "
                                    "now! contact a content specialist for volume pricing."}),
      "body")
check("摘要夹带广告不触发全罚", ff.is_promotional(
    {"title": "HiPHI: a high-performance imaging method",
     "summary": "We present HiPHI. Download this free whitepaper now!"}), False)
check("真英文新闻不误判", ff.is_promotional(
    {"title": "PC shipments fall 20.1 percent",
     "summary": "Global PC shipments fell 20.1 percent year over year."}), False)
# 子代理实测：summary 字面值 "Comments"（6 字符）拿到与 1900 字符摘要同等的分
check("占位摘要被识别为无正文", ff.body_missing({"summary": "Comments"}), True)
check("空摘要被识别为无正文", ff.body_missing({"summary": ""}), True)
check("实质摘要不算无正文", ff.body_missing(
    {"summary": "市场监管总局通报：排查整改隐患10.6万项，检查企业72.4万家次。"}), False)
# 子代理实测：Nature 书评 0.69 > bioRxiv 预印本 0.57 —— 无主张的东西排在研究之上
check("书评低于预印本（倒挂已修）",
      ff.truth_weight("crossref-nature", "authority",
                      entry={"doi": "10.1038/d41586-026-03217-6"})
      < ff.truth_weight("biorxiv", "authority", entry={"doi": "10.1101/x"}), True)
check("研究论文仍高于预印本",
      ff.truth_weight("crossref-nature", "authority",
                      entry={"doi": "10.1038/s41586-026-x"})
      > ff.truth_weight("biorxiv", "authority", entry={"doi": "10.1101/x"}), True)
check("栏目判定不再靠「引用数 0」兜底（v1 的误伤来源）",
      ff.evidence_type_for("crossref-natcomp", "authority",
                           {"doi": "10.1038/s43588-026-01070-1",
                            "title": "Epidemiological modeling in the age of AI",
                            "authors": ["A B", "C D"],
                            "abstract_missing": True,
                            "is_referenced_by_count": 0}), "peer-reviewed")

out_path = TMP / "acc_new.json"
rc, out = run("--domain", "ai", "--days", "3", "--max-per-source", "12",
              "--request", "AI界这几天有什么进展", "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    check("分类理由写进 JSON", "classification" in d["relevance"], True)
    check("promotional_signal 字段存在于条目",
          all("promotional_signal" in e for e in d["entries"]), True)
    check("degraded_streams 字段存在", "degraded_streams" in d, True)
    check("archive_truncated 字段存在", "archive_truncated" in d, True)
    check("大归档源被截断（openai-news 或 arxiv-rss-ai）",
          len(d.get("archive_truncated") or {}) > 0, True)
    check("event_id 存在于条目",
          all(e.get("event_id") for e in d["entries"]), True)

# ---- 精简视图（--compact）：验「判断所需字段一个未删」不是一句空话 ----
out_c = TMP / "acc_compact.json"
rc, _ = run("--domain", "ai", "--class", "social", "--days", "3",
            "--max-per-source", "8", "--compact", "--out", str(out_c))
comp_path = out_c.with_name(out_c.stem + "_compact.json")
if rc == 0 and comp_path.exists():
    cf = load(out_c)
    cc = load(comp_path)
    check("compact 视图生成", comp_path.exists(), True)
    check("compact 体积小于全量", comp_path.stat().st_size < out_c.stat().st_size, True)
    check("compact 保留全部条目数",
          len(cc["entries"]), len(cf["entries"]))
    # 风险标记【键存在但值为 false 时也必须保留】—— 「检查过、无信号」和
    # 「没做检查」是两件事。实测 bug：省略逻辑用 falsy 通杀，promotional_signal
    # 被删光，230 条全部评估过、compact 里 0 条保留。
    #
    # ★ 判据必须拿【全量】当基准，不能要求 compact 里每条都有这个键：
    #   契约是「全量里有的键，值为 falsy 也要保留」，不是「凭空造出全量里没有的键」。
    #   第一版断言写成 `all(key in e for e in compact)`，于是只要某个标记是
    #   【条件赋值】（trend_capped 只打在热度榜条目上、body_missing 只打在无正文
    #   条目上），非该类条目就会 FAIL —— 那是测试写错了，不是代码错了。
    #   反过来，「为不存在的键写 null」是另一个真 bug（幽灵 null），
    #   所以两个方向都要测。
    full_eval = {key: sum(1 for e in cf["entries"] if key in e)
                 for key in ("promotional_signal", "trend_capped", "body_missing")}
    ck_all = set()
    for e in cf["entries"]:
        ck_all |= set(e.keys())
    # 这些字段【每条都该有】—— 由所有打分路径无条件写入。
    # `evidence_form` 不在这个列表里：它**只在参与打分时**才存在
    # （社交域 truth_weight_social / external_truth_weight 写它，
    #  权威面 truth_weight 从不写）。要求「每条都有」是把两种语义混了 ——
    # 而更早一版为了满足这条断言，在 compact_view 里无条件重算它，
    # 结果把热度榜的 `trend-board(unverifiable)` 覆盖成 `first-hand-report`。
    for key in ("section", "truth_score", "comm_score", "event_id"):
        check(f"compact 每条都有 {key}", all(key in e for e in cc["entries"]), True)
    check("compact 的 evidence_form 与全量逐条一致（不得重算覆盖）",
          {e["title"]: e.get("evidence_form") for e in cc["entries"]},
          {e["title"]: e.get("evidence_form") for e in cf["entries"]})
    for key, n in full_eval.items():
        check(f"compact 保留 {key}（全量有 {n} 条，值为 falsy 也要留）",
              sum(1 for e in cc["entries"] if key in e), n)
    # 例外名单 = compact 视图【有意新增】的派生字段（都有实际内容，不是 null）。
    # 幽灵 null 的定义是「全量里没有、compact 里凭空写成 None」，所以下面单独测 null。
    derived = {"summary_cut", "summary_tail_cut", "summary_source_truncated",
               "summary_note", "evidence_form"}
    stray = [k for e in cc["entries"] for k in e
             if k not in ck_all and k not in derived]
    check("compact 不凭空造键（幽灵 null 回归）", stray, [])
    # `in_window` 是唯一允许为 null 的字段：三态 True/False/None，
    # 而 None = 「这条没有日期」这个【已知事实】，与「键不存在」不是一回事。
    # 趋势榜 37 条就是这样（无日期、被窗口豁免）。
    tri_state = {"in_window"}
    bad_null = [k for e in cc["entries"] for k, v in e.items()
                if v is None and k not in tri_state]
    check("compact 里除 in_window 外无 null 值字段", bad_null, [])
    check("compact 保留 in_window=None（无日期是已知事实，不是缺省）",
          all("in_window" in e for e in cc["entries"]
              if e.get("in_window") is None), True)
    check("compact 保留覆盖情况（degraded_streams / source_funnel / sources_report）",
          all(k in cc for k in ("degraded_streams", "source_funnel", "sources_report")), True)
    # 摘要截断必须标出处，否则下游会以为拿到全文
    cut = [e for e in cc["entries"] if e.get("summary_cut")]
    full_by_title = {e["title"]: len(e.get("summary") or "") for e in cf["entries"]}
    check("summary_cut 的值 = 全量摘要长度（区别于来源原文长度）",
          all(full_by_title.get(e["title"]) == e["summary_cut"] for e in cut), True)
    check("每个被截断的条目都有 summary_cut",
          all("summary_cut" in e for e in cc["entries"]
              if len(e.get("summary") or "") >= 600), True)
    # 双重截断必须可见。实测陷阱：`_clip()` 的 `…[省略 N 字]…` 标记固定在
    # 第 1401 字，而 compact 只留 600 字 —— 标记被截断动作本身销毁，
    # 16 条残缺摘要全都看不出自己残缺，下游会当成完整摘要用。
    fs_n = sum(1 for e in cf["entries"] if e.get("summary_truncated"))
    cs_n = sum(1 for e in cc["entries"] if e.get("summary_source_truncated"))
    check(f"入库被 _clip 截过的条目在 compact 里可见（全量 {fs_n} 条）", cs_n, fs_n)
    check("被 compact 再截的条目标了 summary_tail_cut（尾部结论不在本视图）",
          all(e.get("summary_tail_cut") for e in cc["entries"] if e.get("summary_cut")), True)
    check("compact 摘要是全量的前缀",
          all(e["summary"] == (next(x for x in cf["entries"]
                                    if x["title"] == e["title"]).get("summary") or "")[:len(e["summary"])]
              for e in cc["entries"]), True)
else:
    check("精简视图用例运行成功", False, True)

print("\n[11] 窗口粒度 · 边界日必须在内（本轮最大改动，之前零覆盖）")
# 三个独立复核代理都指出：窗口是这轮最大的改动，但 test_routing 里
# grep `since|window|dropped|\.date\(\)` **0 匹配** —— 没有护栏。
# 原来的 bug：主窗口用 `dt >= since`（datetime 粒度），而 Crossref 只给日期
# 粒度（一律 T00:00:00Z），于是窗口首日的每一条都被判「窗口外」。
# 这里造边界日与前一天各一条，断言「前一天丢、当天留」。
import datetime as _dt
_today = _dt.date.today()
_edge = _today - _dt.timedelta(days=3)        # --days 3 的窗口起点（日期粒度）
_json_edge = TMP / "acc_edge.json"
_json_edge.write_text(json.dumps([
    {"title": "边界日条目（应当保留）", "source": "澎湃新闻",
     "url": "https://example.com/edge", "date": _edge.isoformat(),
     "summary": "2026年10月7日，某机构发布通告，涉及3项数据与2家单位。"},
    {"title": "窗口前一天条目（应当丢弃）", "source": "澎湃新闻",
     "url": "https://example.com/before", "date": (_edge - _dt.timedelta(days=1)).isoformat(),
     "summary": "该条目早于窗口起点，必须被丢弃。"},
    {"title": "边界日前夜 23:59（应当丢弃）", "source": "澎湃新闻",
     "url": "https://example.com/midnight", "date": (_edge - _dt.timedelta(days=1)).isoformat(),
     "summary": "datetime 判据会放过它，日期判据必须丢弃。"},
], ensure_ascii=False), encoding="utf-8")
_edge_out = TMP / "acc_edge_out.json"
rc, _ = run("--domain", "social", "--class", "social", "--days", "3",
            "--extra-json", str(_json_edge), "--max-per-source", "3",
            "--out", str(_edge_out))
if rc == 0 and _edge_out.exists():
    de = load(_edge_out)
    titles = [e.get("title") or "" for e in de["entries"]]
    check("窗口边界日条目被保留", any("边界日条目" in t for t in titles), True)
    check("窗口前一天条目被丢弃", any("前一天条目" in t for t in titles), False)
    check("边界日前夜条目被丢弃（datetime 判据会放过它）",
          any("前夜" in t for t in titles), False)
    check("external_dropped_out_of_window 计到 2 条",
          de["stats"].get("external_dropped_out_of_window"), 2)
else:
    check("窗口粒度用例运行成功", False, True)

print("\n[12] 期刊栏目判定 / 0 条源漏斗 / 信号词 / 客户案例 / --list 口径")
# ── 这一节全部来自真实运行的漏判，每一条都对应一次实际错误 ──────────────
# 判据：**只测「应判对」不够，每个都要测反方向**（子代理第一轮抓到的教训：
# 只测「应合并」的断言，让一个方向相反的过归一 bug 完整活了下来）。

# ① 期刊「评论栏目」曾被标成 peer-reviewed。
#    实测 crossref-nature 15 条全是 d41586- 前缀（这条本来就对），
#    但 crossref-nejm 6 条全是 Perspective/病例影像（DOI 不是 d41586-）→ 全漏判。
check("栏目判定：NEJM Perspective → journalism",
      ff.evidence_type_for("crossref-nejm", "authority",
                           {"doi": "10.1056/NEJMp2601234",
                            "title": "Who Counts as Infertile? A Missed Opportunity",
                            "authors": ["A B"], "abstract_missing": True}),
      "journalism")
check("栏目判定：病例影像 → journalism",
      ff.evidence_type_for("crossref-nejm", "authority",
                           {"doi": "10.1056/NEJMicm2601234",
                            "title": "Flushing in Carcinoid Syndrome",
                            "authors": ["A B"], "abstract_missing": True}),
      "journalism")
check("栏目判定：无作者 → journalism",
      ff.evidence_type_for("crossref-natcomp", "authority",
                           {"doi": "10.1038/s43588-026-01071-0",
                            "title": "Photonic circuits become trainable AI hardware",
                            "authors": [], "abstract_missing": True}),
      "journalism")
check("栏目判定：d41586- 前缀仍判 journalism",
      ff.evidence_type_for("crossref-nature", "authority",
                           {"doi": "10.1038/d41586-026-03217-6",
                            "title": "Books in brief", "authors": [], "abstract_missing": True}),
      "journalism")
# ★ 反方向：真研究论文**不能**被误判成栏目。
#   这条是上一版（v1 兜底规则）翻车的地方，必须守住。
check("栏目判定不误伤：有摘要的多作者研究论文 → peer-reviewed",
      ff.evidence_type_for("crossref-nature", "authority",
                           {"doi": "10.1038/s41586-026-10910-z",
                            "title": "Psychedelics align brain activity with context",
                            "authors": ["A B", "C D", "E F"], "abstract_missing": False}),
      "peer-reviewed")
check("栏目判定不误伤：无摘要但多作者的研究论文 → peer-reviewed",
      ff.evidence_type_for("crossref-pnas", "authority",
                           {"doi": "10.1073/pnas.2619925123",
                            "title": "GTP orchestrates CTP synthase",
                            "authors": ["A B", "C D", "E F"], "abstract_missing": True}),
      "peer-reviewed")

# ② 信号词只从【正文】抽。标题里的孤立形容词是假阳性来源。
check("信号词：无正文不抽（标题人名里有 huge）",
      ff.signal_words({"title": "drea de matteo", "summary": ""}), [])
check("信号词：无正文不抽（技术词 Alarming）",
      ff.signal_words({"title": "I built ALHR: Alarming latency", "summary": ""}), [])
check("信号词：有正文才抽",
      ff.signal_words({"title": "x", "summary": "The decline was unprecedented and dramatic."}),
      ["unprecedented", "dramatic"])
check("信号词：中文程度词",
      ff.signal_words({"title": "x", "summary": "有关部门高度重视，彻底排查隐患。"}),
      ["彻底"])

# ③ 客户案例：营销内容但**不是广告形式**，`is_promotional` 抓不到，要单独判。
#    实测 openai-news 落地 9 条里 5 条是这类，全部 truth=0.816 与真公告同分。
for _t, _s in (
    ("Sophos cuts threat investigation time by 96% with OpenAI Daybreak",
     "Discover how Sophos uses OpenAI's Daybreak to cut investigation time by 96%."),
    ("Asana cuts model costs 76x in browser tests with GPT-6.1 Sol",
     "Using GPT-6 Astra in Codex, Asana made its browser agent 76x cheaper."),
    ("How Oracle turns days of work into minutes with ChatGPT and Codex",
     "Oracle turns specialist knowledge into workflows with ChatGPT Work."),
    ("Radisson Hotel Group brings hotel discovery into ChatGPT",
     "Radisson partnered with Accenture to build a ChatGPT plugin."),
    ("How Jump Trading is scaling quant research with ChatGPT",
     "Jump Trading uses OpenAI to expand quantitative research."),
):
    check(f"客户案例识别：{_t[:26]}", ff.is_customer_story({"title": _t, "summary": _s}), True)
# ★ 反方向：真公告与研究论文**不能**被误判（这是收紧判据的原因 ——
#   `how X is -ing` 会连 OpenAI 自己的公告一起抓）。
for _t, _s in (
    ("GPT-6 and Intelligent UI for everyone", "GPT-6 is rolling out globally in ChatGPT."),
    ("Disrupting AI-enabled false front operations",
     "OpenAI disrupted two influence operations."),
    ("Our approach to EU text provenance rules",
     "How OpenAI is approaching text watermarking under EU rules."),
    ("A DNA Typewriter records the cell lineage history of a mouse",
     "We applied DNA Typewriter to record the cell lineage."),
    ("Safeguarding global terrestrial vertebrate species from sea-level rise",
     "We project species exposed to SLR will increase."),
    # ★★ 实测误标（子代理用真实数据抓到、我复现并修）：主体必须是大写专有名词。
    #   曾经写成 `[a-z]` 起头，于是 arXiv 论文摘要里
    #   「OpenAI agents exploited research infrastructure」也匹配 ——
    #   「agents」被当成客户主体，一篇 agent 安全预印本被判成厂商客户案例。
    ("From Reactive Containment to Proactive Assurance: Lessons from OpenAI, "
     "Anthropic, and Google Agent Security Incidents",
     "In 2026, cybersecurity evaluations involving OpenAI, Anthropic, and Google "
     "agents reached real systems outside their authorized test scope. "
     "OpenAI agents exploited research infrastructure."),
):
    check(f"客户案例不误伤：{_t[:26]}", ff.is_customer_story({"title": _t, "summary": _s}), False)

# ④ 0 条源必须有 funnel 键（否则「0 条」在结构上双重不可见：
#    进不了 stale-feed，也进不了漏斗表）。
_z = TMP / "acc_zero.json"
_rc, _ = run("--domain", "science", "--class", "authority", "--days", "3",
             "--max-per-source", "5", "--compact", "--out", str(_z))
if _rc == 0 and _z.exists():
    _dz = load(_z)
    _fn = _dz.get("source_funnel") or {}
    _req = set((_dz.get("requested") or {}).get("resolved_sources") or [])
    check("每个被请求的源都有 funnel 键（含返回 0 条的）",
          sorted(_req - set(_fn)), [])
    _zero = [k for k, v in _fn.items() if v.get("fetched") == 0 and v.get("emitted") == 0]
    check("0 条源被标 empty_feed", all(_fn[k].get("empty_feed") for k in _zero), True)
else:
    check("0 条源漏斗用例运行成功", False, True)

print("\n[13] 单源硬证据通道 / 客户案例不得进核心区")
# 背景：三域实测一致 —— 真值 0.896–0.912 的同行评审论文、0.816 的厂商官方发布，
# 全部因 comm=0.30 卡在待观察区 → **核心区结构性为空**。
# 根因：`comm>=0.6 需 >=2 家独立转载` 是给社会面设计的，而学术首发天然单源。
# 修法：只给【peer-reviewed】开一条显式单源通道（含独立核验的类型），
# 且必须标 corroborated=False。
#
# ⚠️ 这一节的核心是【反向断言】—— 试过把 official 也放进来，
#    结果 openai-news 那 6 条客户案例被 "official" 标签护送进了核心区。
_gt = TMP / "acc_gate.json"
_rc, _ = run("--domain", "ai", "--class", "authority", "--days", "3",
             "--max-per-source", "15", "--compact", "--out", str(_gt))
if _rc == 0 and _gt.exists():
    _dg = load(_gt)
    _core = [e for e in _dg["entries"] if e["section"] == "核心区"]
    _cs_core = [e for e in _core if e.get("customer_story")]
    check("客户案例【不得】进核心区", _cs_core, [])
    check("核心区里的单源条目都是 peer-reviewed",
          sorted({e.get("evidence_type") for e in _core
                  if e.get("single_source_hard_evidence")}) in ([], ["peer-reviewed"]), True)
    check("单源通道条目必须标 corroborated=False",
          all(e.get("corroborated") is False for e in _core
              if e.get("single_source_hard_evidence")), True)
    check("official 类型不得走单源通道进核心区",
          [e.get("title") for e in _core if e.get("evidence_type") == "official"], [])
    # 自洽性：kept 必须等于各桶之和（实测曾出现 kept=32 而桶和=507）
    _s = _dg["stats"]
    check("闸门自洽：kept == len(entries) == 各桶之和",
          (_s["kept"], len(_dg["entries"]),
           _s["passed_both"] + _s.get("passed_single_source", 0)
           + _s["truth_only"] + _s["comm_only"] + _s["failed_gate"]),
          (_s["kept"], _s["kept"], _s["kept"]))
else:
    check("单源通道用例运行成功", False, True)

print("\n[14] 源与权重表一致性（新源不得静默拿默认权重）")
# 实测：新加的 `chinanews` 当时不在 SOURCE_WEIGHT 里，拿的是
# `SOURCE_WEIGHT.get(k, 0.50)` 兜底默认值 —— 一个没人看过的默认值替我做掉了
# 权重决定，而且只在反推分数时才被发现。这条断言把那类事故变成会响的警报。
_unw = sorted(k for k in ff.SOURCES if k not in ff.SOURCE_WEIGHT)
check("每个注册源都在 SOURCE_WEIGHT 里有显式权重", _unw, [])
check("权重表里没有已下架源的死条目",
      sorted(k for k in ff.SOURCE_WEIGHT if k not in ff.SOURCES), [])

# 中文社会民生源
check("chinanews 已注册", "chinanews" in ff.SOURCES, True)
if "chinanews" in ff.SOURCES:
    _cn = ff.SOURCES["chinanews"]
    check("chinanews 是多版面源（rss_multi 而非 urls 镜像）", _cn.get("kind"), "rss_multi")
    check("chinanews 的 feeds 数量 ≥3", len(_cn.get("feeds") or []) >= 3, True)
    check("chinanews 归社交域", "social" in _cn.get("domains", []), True)
    check("chinanews 走 social 路（否则社会域依旧是 3 个科技媒体）",
          _cn.get("class"), "social")
    # 权重必须"有意登记"，不能靠默认值。同时它应当是**二次报道**档位，
    # 不该被抬到通讯社级（那等于让单源直接过闸，是拿打分迁就结果）。
    check("chinanews 权重在二次报道档（0.45–0.60）",
          0.45 <= ff.SOURCE_WEIGHT.get("chinanews", -1) <= 0.60, True)
    # 单源过不了真值闸门是【有意的】—— 社会新闻靠多家独立印证才该过。
    check("chinanews 单源真值分低于闸门（需印证才过，有意）",
          ff.truth_weight("chinanews", "social") < ff.CREDIBILITY_THRESHOLD, True)

# hnrss 已下架（抓 20 条全部并入 hn-frontpage，落地 0，两次运行都是）
check("hnrss 已从注册表移除", "hnrss" in ff.SOURCES, False)

print("\n[15] 本周重大事件宽窗口（--major-days）")
# 用途：诺奖这类领域级事件常在窗口外一两天宣布（实测化学奖 10-07，
# 而窗口是 10-08~10-10），按窗口纪律只能整条丢掉。
# 设计要点：**只对重大源生效**，且进来的条目必须被显式打标。
_m0, _m7 = TMP / "acc_mj0.json", TMP / "acc_mj7.json"
_r0, _ = run("--domain", "science", "--class", "authority", "--days", "3",
             "--max-per-source", "10", "--compact", "--out", str(_m0))
_r7, _ = run("--domain", "science", "--class", "authority", "--days", "3",
             "--major-days", "7", "--max-per-source", "10", "--compact", "--out", str(_m7))
if _r0 == 0 and _r7 == 0 and _m0.exists() and _m7.exists():
    _d0, _d7 = load(_m0), load(_m7)
    check("关闭时 major_events_kept 为 0", _d0.get("stats", {}).get("major_events_kept"), 0)
    _mj = [e for e in _d7["entries"] if e.get("major_window")]
    check("开启后确有窗口外条目被纳入", len(_mj) > 0, True)
    # ★ 反向断言：进来的必须如实标 in_window=False —— 它们**确实**不在基础窗口内。
    #   粉饰这一点就等于静默放宽窗口，比漏报更糟。
    check("宽窗口条目如实标 in_window=False",
          all(e.get("in_window") is False for e in _mj), True)
    check("宽窗口条目带 major_window_note（强制下游写明）",
          all(e.get("major_window_note") for e in _mj), True)
    check("stats 里报了纳入数（与 dropped 并列，不被漏读）",
          _d7["stats"].get("major_events_kept"), len(_mj))
    # ★ 「双项达标」与「单源硬证据」必须分开计数。
    # 实测问题（子代理用真实数据抓到）：两者曾共用 `passed_both`，于是 stdout
    # 打出「双项达标 25」而逐条是 passes_comm=0 —— 那 25 条**没有一条经过
    # 独立印证**。这个数字唯一该回答的问题是「多少条真的被多家印证过」。
    _s7 = _d7["stats"]
    check("单源硬证据与双项达标分开计数（不得共用计数器）",
          "passed_single_source" in _s7 and _s7["passed_single_source"] > 0, True)
    check("科学权威面：真·双项达标应为 0（comm 全 0.3，无独立印证）",
          _s7["passed_both"], 0)
    check("分开计数后总量仍守恒",
          _s7["passed_both"] + _s7["passed_single_source"] + _s7["truth_only"]
          + _s7["comm_only"] + _s7["failed_gate"], _s7["kept"])
    # ★ 反向断言：聚合器不得享受宽窗口。
    #   实测「机构稿 09-28 → ScienceDaily 10-09 才转发」正是问题的来源；
    #   让聚合器也拿宽窗口等于奖励慢转载。
    check("聚合器不得进入宽窗口",
          [e.get("source_key") for e in _mj
           if e.get("source_key") in ("science-daily", "phys-org",
                                      "hn-frontpage", "reddit-ml")], [])
    # 关掉时必须与不加参数完全一致（默认关闭、无副作用）
    _r_plain, _ = run("--domain", "science", "--class", "authority", "--days", "3",
                      "--max-per-source", "10", "--compact",
                      "--out", str(TMP / "acc_mj_plain.json"))
    if _r_plain == 0:
        _dp = load(TMP / "acc_mj_plain.json")
        check("不传 --major-days 与传 0 结果一致（默认关闭、无副作用）",
              (_dp["stats"]["kept"], _dp["stats"]["raw"],
               _dp["stats"].get("major_events_kept")),
              (_d0["stats"]["kept"], _d0["stats"]["raw"], 0))
else:
    check("宽窗口用例运行成功", False, True)

# baidu-hot 已下架（4 镜像常态 3 个已死；当天 5 次失败只成功 2 次）
check("baidu-hot 已从注册表移除", "baidu-hot" in ff.SOURCES, False)
check("百度仍在 PLATFORM_WEIGHT 里（内容提到百度热榜时权重仍可解析）",
      "baidu" in ff.PLATFORM_WEIGHT, True)
check("_mirror_fetch 机制保留（通用能力，非死代码）",
      callable(getattr(ff, "_mirror_fetch", None)), True)

# ── 多版面源的 cap 不得按"哪个 feed 先返回"挑选版面 ──────────────────
# 实测 bug（子代理用真实数据抓到，我复现）：`fetch_rss_multi` 用
# `as_completed` 按**完成顺序**合并，先回来的版面独占 `--max-per-source` 名额。
# chinanews 4 个版面窗内共 120 条，实际只送出 15 条且**全部来自财经版面**，
# 社会/国内/滚动一条都没送出来。
# 后果不只是数量：**看到的世界被"哪个 feed 先返回"决定了**，
# 且随每次网络抖动变化、无法复现。
import re as _re
import collections as _collections
_cn_j = TMP / "acc_cn.json"
_rcn, _ = run("--source", "chinanews", "--days", "3", "--max-per-source", "15",
              "--compact", "--out", str(_cn_j))
if _rcn == 0 and _cn_j.exists():
    _dcn = load(_cn_j)
    _cn_e = [e for e in _dcn["entries"] if e.get("source_key") == "chinanews"]
    _sections = _collections.Counter(
        (_re.search(r"chinanews\.com\.cn/(\w+)/", e.get("url") or "") or [None, "?"])[1]
        if _re.search(r"chinanews\.com\.cn/(\w+)/", e.get("url") or "") else "?"
        for e in _cn_e)
    check("多版面源不只吐出一个版面（cap 不得按完成顺序挑版面）",
          len(_sections) >= 3, True)
    _dates = [str(e.get("published") or "") for e in _cn_e]
    check("多版面源结果按时间降序（cap 取的是全版面最新一批）",
          _dates == sorted(_dates, reverse=True), True)
else:
    check("多版面源用例运行成功", False, True)

print("\n[16] 中文源覆盖（2026-10-10 扩充，实测可达 + 首条=当天）")
# 背景：扩充前 29 源里中文只有 2 个（中新网、头条热榜），后果是
# 「社会」类八成内容靠 web_search 补、中国大模型发布 0 覆盖。
# 这批全部经【可达性实测 + 新鲜度校验】两道关才登记 ——
# 因为人民网/新华网那种「200 + 几百条 + 内容是三五年前」的静默陷阱比 404 危险。
_CN = ("chinanews", "qbitai", "infoq-cn", "oschina", "leiphone", "tmtpost",
       "geekpark", "solidot", "jiemian", "eeo", "dw-zh", "bbc-zh", "rfi-zh", "nyt-zh")
for _s in _CN:
    check(f"中文源已登记：{_s}", _s in ff.SOURCES, True)
check("新中文源全部有显式权重（不得静默拿默认 0.50）",
      [s for s in _CN if s not in ff.SOURCE_WEIGHT], [])
check("chinanews 版面扩到 ≥7 个（含 world/health/edu）",
      len(ff.SOURCES["chinanews"].get("feeds") or []) >= 7, True)
check("chinanews 未登记 404 的 gj.xml（实测确认不可用）",
      any("gj.xml" in u for u in ff.SOURCES["chinanews"]["feeds"]), False)
check("中文 AI 垂媒权重都在二次报道档（≤0.62）",
      all(ff.SOURCE_WEIGHT.get(s, 1) <= 0.62
          for s in ("qbitai", "infoq-cn", "oschina", "leiphone", "tmtpost",
                    "geekpark", "solidot")), True)
check("外媒中文站权重压在大众媒体档（≤0.55）",
      all(ff.SOURCE_WEIGHT.get(s, 1) <= 0.55
          for s in ("dw-zh", "bbc-zh", "rfi-zh", "nyt-zh")), True)
check("外媒中文站的 note 都写明了立场属性",
      all("立场" in (ff.SOURCES[s].get("note") or "")
          for s in ("dw-zh", "bbc-zh", "rfi-zh", "nyt-zh")), True)
_ai_social = [k for k, v in ff.SOURCES.items()
              if "ai" in v.get("domains", []) and v.get("class") == "social"]
check("ai 域可取到的社交源 ≥12（扩充前为 6）", len(_ai_social) >= 12, True)

print("\n[9] 回归 · 已有两套测试仍通过")
print(f"  （缓存命中 {_HITS[0]} / 未命中 {_HITS[1]} —— 未命中会真的再抓一次）")
for name in ("test_retraction.py", "test_historical.py"):
    p = subprocess.run([sys.executable, str(SKILL / "tests" / name)],
                       capture_output=True, text=True, encoding="utf-8", timeout=280)
    ok = p.returncode == 0
    (PASS if ok else FAIL).append(f"回归 {name}")
    print(f"  {'OK  ' if ok else 'FAIL'} 回归 {name} (exit={p.returncode})")

print("\n" + "=" * 82)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  !", f)
print("=" * 82)
sys.exit(1 if FAIL else 0)
