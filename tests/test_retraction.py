#!/usr/bin/env python3
"""Invoke the REAL functions from fetch_feeds.py to verify the retraction guard.

Deliberately not a reimplementation: a mirrored copy of the scoring logic would
pass even if the shipped script were broken. We import the module itself.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

def _find_script() -> pathlib.Path:
    """Locate fetch_feeds.py portably.

    Resolution order:
      1. <skill>/scripts/            -- the normal layout, and what a clone has
      2. $TREND_RADAR_SCRIPT          -- explicit override
    Never hardcode an absolute path: a cloned repo must be able to run its own
    tests, and a machine-specific path silently breaks that for everyone else.
    """
    env = os.environ.get("TREND_RADAR_SCRIPT")
    if env and pathlib.Path(env).is_file():
        return pathlib.Path(env)
    here = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "fetch_feeds.py"
    if here.is_file():
        return here
    raise SystemExit(f"找不到 fetch_feeds.py（试过 {here}）；"
                     f"用 TREND_RADAR_SCRIPT 环境变量指定")


FETCH = _find_script()

spec = importlib.util.spec_from_file_location("ff", FETCH)
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)

PASS, FAIL = [], []


def check(label: str, got, want) -> None:
    ok = got == want
    (PASS if ok else FAIL).append(label)
    print(f"  {'OK  ' if ok else 'FAIL'} {label:52} got={got!r} want={want!r}")


print("=" * 78)
print("撤稿拦截 · 直接调用实际发货的函数")
print("=" * 78)

print("\n[1] notice_severity —— Crossref update-to 标签分级")
check("Retraction -> retracted", ff.notice_severity([{"label": "Retraction"}]), "retracted")
check("Retracted -> retracted", ff.notice_severity([{"label": "Retracted"}]), "retracted")
check("Withdrawal -> retracted", ff.notice_severity([{"label": "Withdrawal"}]), "retracted")
check("Expression of Concern -> concern",
      ff.notice_severity([{"label": "Expression of Concern"}]), "concern")
check("Correction -> correction",
      ff.notice_severity([{"label": "Correction"}]), "correction")
check("Erratum -> correction", ff.notice_severity([{"label": "Erratum"}]), "correction")
check("Addendum -> correction", ff.notice_severity([{"label": "Addendum"}]), "correction")
check("empty -> None", ff.notice_severity([]), None)
check("None -> None", ff.notice_severity(None), None)
check("worst wins (correction+retraction)",
      ff.notice_severity([{"label": "Correction"}, {"label": "Retraction"}]), "retracted")

print("\n[2] truth_weight —— 撤稿必须压到 0.05，低于任何闸门")
check("Science 论文 + 撤稿 = 0.05",
      ff.truth_weight("crossref-science", "authority", notices=[{"label": "Retraction"}]), 0.05)
check("Science 论文 + 无撤稿 = 0.912",
      ff.truth_weight("crossref-science", "authority"), 0.912)
check("Science 论文 + 关注声明 <= 0.45",
      ff.truth_weight("crossref-science", "authority",
                      notices=[{"label": "Expression of Concern"}]), 0.45)
check("Science 论文 + 更正 <= 0.60",
      ff.truth_weight("crossref-science", "authority", notices=[{"label": "Correction"}]), 0.6)
check("撤稿压过佐证加成",
      ff.truth_weight("crossref-science", "authority", corroborated=True,
                      notices=[{"label": "Retraction"}]), 0.05)

print("\n[3] 历史案例的实测分数（用真实权重表重算）")
cases = [
    ("砷基生命 · Science 论文（后被撤稿）", "crossref-science", "authority", None, 0.912),
    ("砷基生命 · ScienceDaily 转述", "science-daily", "social", None, 0.562),
    ("LK-99 · arXiv 预印本", "arxiv-ai", "authority", None, 0.578),
    ("LK-99 · Tech Review 报道", "techreview", "social", None, 0.642),
    ("OPERA · arXiv 预印本", "arxiv-ai", "authority", None, 0.578),
    ("OPERA · Ars Technica 报道", "ars-technica", "social", None, 0.618),
]
for label, key, cls, notices, want in cases:
    got = ff.truth_weight(key, cls, notices=notices)
    check(label, got, want)

print("\n[4] 同一来源的不同入口必须合并（防虚假佐证）")
# ⚠️ `hnrss` 已于 2026-10-10 从注册表移除 —— 它抓 20 条但全部被
# `hn-frontpage` 判重合并，落地 0 条，占着名额不产出（实测两次运行都是）。
# 所以这里不再断言 hnrss 的身份，改为断言【仍在册的】同源入口。
check("hn-top 与 hn-frontpage 同源",
      ff.identity_of("hn-top") == ff.identity_of("hn-frontpage"), True)
check("arxiv-ai 与 arxiv-rss-ai 同源",
      ff.identity_of("arxiv-ai") == ff.identity_of("arxiv-rss-ai"), True)
check("hn-top 也归 hackernews", ff.identity_of("hn-top"), "hackernews")
check("不同源不合并",
      ff.identity_of("crossref-nature") != ff.identity_of("techreview"), True)

print("\n[5] 关联度 —— 方向性，不是对称相似度")
items_off = [{"title": "Scaling laws for dense language models", "summary": "parameter scaling"},
             {"title": "Efficient attention kernels", "summary": "kernel optimization"}]
r = ff.keyword_coverage(["安全", "对齐", "风险"], items_off)
check("跑偏内容 + 安全关键词 -> 0.0", r["coverage"], 0.0)
check("并标出未覆盖关键词", r["missing"], ["安全", "对齐", "风险"])
items_on = [{"title": "AgentBench: evaluating LLM agents", "summary": "A benchmark"},
            {"title": "Chain-of-thought reasoning", "summary": "reasoning traces"}]
r2 = ff.keyword_coverage(["agent", "reasoning", "benchmark"], items_on)
check("贴题内容 -> 1.0", r2["coverage"], 1.0)

print("\n[6] 标题近似去重（跨源同一事件）")
a = ff.title_tokens("Panama earthquake magnitude 7.6 strikes")
b = ff.title_tokens("Magnitude 7.6 earthquake strikes Panama")
check("同事件不同措辞 -> 判为同一事件", ff.is_same_event(a, b), True)
c = ff.title_tokens("OpenAI releases a new reasoning model")
check("不同事件 -> 不合并", ff.is_same_event(a, c), False)

print("\n[7] 勘误标题过滤")
for t, want in [("Publisher Correction: Sleep chart", True),
                ("Author Correction: NK cells", True),
                ("Retraction: Arsenic life", True),
                ("Expression of Concern: X", True),
                ("A Bacterium That Can Grow by Using Arsenic", False),
                ("Five movements from the edge of the singularity", False)]:
    check(f"勘误正则 {t[:40]}", bool(ff.ERRATA_RE.search(t)), want)

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  !", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
