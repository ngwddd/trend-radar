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
    # 自洽性：kept 必须等于四桶之和（实测曾出现 kept=32 而四桶和=507）
    _s = _dg["stats"]
    check("闸门自洽：kept == len(entries) == 四桶之和",
          (_s["kept"], len(_dg["entries"]),
           _s["passed_both"] + _s["truth_only"] + _s["comm_only"] + _s["failed_gate"]),
          (_s["kept"], _s["kept"], _s["kept"]))
else:
    check("单源通道用例运行成功", False, True)

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
