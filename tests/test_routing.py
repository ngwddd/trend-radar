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
TMP = pathlib.Path(tempfile.gettempdir())

spec = importlib.util.spec_from_file_location("ff", FETCH)
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"  {'OK  ' if ok else 'FAIL'} {label:58} got={got!r}" + ("" if ok else f" want={want!r}"))


def run(*args, timeout=280) -> tuple[int, str]:
    """Run the CLI.

    encoding + errors='replace' are BOTH required. Measured: without them the
    subprocess reader thread died with `UnicodeDecodeError: 'utf-8' codec can't
    decode byte 0xb2` on this Windows host (GBK console), which surfaced as nine
    false FAILs -- every one of them a "用例运行成功 got=False". A test harness
    that fails for its own encoding reasons is worse than no harness, because the
    failures look like product bugs.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, str(FETCH), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


print("=" * 82)
print("trend-radar 验收测试 · 四项修复 + 热度榜压制")
print("=" * 82)

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

print("\n[7] 统计自洽 · kept == entries == 四桶之和")
out_path = TMP / "acc_consistency.json"
rc, out = run("--domain", "ai", "--class", "authority", "--days", "3",
              "--max-per-source", "12", "--out", str(out_path))
if rc == 0 and out_path.exists():
    d = load(out_path)
    s = d["stats"]
    buckets = s["passed_both"] + s["truth_only"] + s["comm_only"] + s["failed_gate"]
    check("kept == entries length", s["kept"], len(d["entries"]))
    check("kept == 四桶之和", s["kept"], buckets)
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
check("实体归一：中新社 = 中国新闻网", ff.entity_id("中新社"), ff.entity_id("中国新闻网"))
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
check("journalism 只认 d41586- 前缀（兜底规则已删）",
      ff.evidence_type_for("crossref-natcomp", "authority",
                           {"doi": "10.1038/s43588-026-01070-1",
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
    for key in ("section", "truth_score", "comm_score", "event_id", "evidence_form"):
        check(f"compact 每条都有 {key}", all(key in e for e in cc["entries"]), True)
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
    check("compact 里没有 null 值字段",
          sum(1 for e in cc["entries"] for v in e.values() if v is None), 0)
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

print("\n[9] 回归 · 已有两套测试仍通过")
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
