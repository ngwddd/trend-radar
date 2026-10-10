#!/usr/bin/env python3
"""Stage-level test harness for trend-radar.

The skill's data sources only return LIVE items, so a historical claim cannot be
injected through the fetch path. This harness therefore tests the JUDGEMENT
stages (4 精华萃取 / 5 回看) by feeding fixtures that faithfully reproduce what
the sources actually carried at the time.

Ground truth is "what is known now", which is the whole point: for these three
cases the contemporaneous consensus was WRONG, so a pipeline that merely
amplifies consensus will reproduce the error and be caught.

Usage:
  python test_historical.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass


def _find_skill_root() -> pathlib.Path:
    """Locate the skill root portably (see test_retraction.py for rationale)."""
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
WORK = pathlib.Path(tempfile.gettempdir()) / "radartest"

# Each entry mirrors a source that ACTUALLY carried the claim, using the real
# registry key so the weight table applies exactly as it would in production.
CASES = [
    {
        "id": "lk99",
        "title": "LK-99 室温常压超导（2023-07）",
        "request": "AI界和材料界的进展",
        "keywords": ["超导", "LK-99", "室温"],
        "truth_now": "全球复现全部失败，未观测到超导；铅磷灰石中的硫化亚铜杂质导致电阻假象。",
        "entries": [
            {"source_key": "arxiv-ai", "evidence_type": "preprint",
             "title": "The First Room-Temperature Ambient-Pressure Superconductor",
             "summary": "We report a modified lead-apatite structure (LK-99) exhibiting "
                        "superconductivity at room temperature and ambient pressure, with Tc above 400 K.",
             "published": "2023-07-22T00:00:00+00:00", "pickups": 1},
            {"source_key": "hn-frontpage", "evidence_type": "secondary",
             "title": "LK-99: room temperature superconductor claims go viral",
             "summary": "Huge excitement as replication attempts begin worldwide.",
             "published": "2023-07-26T00:00:00+00:00", "pickups": 1},
            {"source_key": "techreview", "evidence_type": "secondary",
             "title": "The room-temperature superconductor claim, explained",
             "summary": "Physicists are sceptical but the Korean team's preprint has not been peer reviewed.",
             "published": "2023-07-27T00:00:00+00:00", "pickups": 1},
        ],
    },
    {
        "id": "arsenic",
        "title": "砷基生命 GFAJ-1（2010-12）",
        "request": "科学界有哪些重要进展",
        "keywords": ["砷", "生命", "细菌"],
        "truth_now": "无法复现。2025 年 Science 正式撤稿；该菌实为耐砷，并未用砷替代磷构建 DNA。",
        "entries": [
            {"source_key": "crossref-science", "evidence_type": "peer-reviewed",
             # The retraction guard is exercised here: this paper WAS retracted.
             "update_notices": [{"label": "Retraction", "source": "retraction-watch"}],
             "title": "A Bacterium That Can Grow by Using Arsenic Instead of Phosphorus",
             "summary": "GFAJ-1 can substitute arsenic for phosphorus in its biomolecules "
                        "including DNA, per NASA-funded work published in Science.",
             "published": "2010-12-02T00:00:00+00:00", "pickups": 1},
            {"source_key": "science-daily", "evidence_type": "secondary",
             "title": "New life form found that uses arsenic instead of phosphorus",
             "summary": "NASA announces a microbe that rewrites the rules of life.",
             "published": "2010-12-02T00:00:00+00:00", "pickups": 1},
            {"source_key": "hn-frontpage", "evidence_type": "secondary",
             "title": "Arsenic life: NASA press conference vs the actual paper",
             "summary": "Commenters note the paper does not prove arsenic is in the DNA backbone.",
             "published": "2010-12-03T00:00:00+00:00", "pickups": 1},
        ],
    },
    {
        "id": "opera",
        "title": "超光速中微子 OPERA（2011-09）",
        "request": "物理学有什么新进展",
        "keywords": ["中微子", "光速", "OPERA"],
        "truth_now": "测量误差。光纤接头松动使计时偏差约 60 纳秒；2012 年 OPERA 撤回结论。",
        "entries": [
            {"source_key": "arxiv-ai", "evidence_type": "preprint",
             "title": "Measurement of the neutrino velocity with the OPERA detector",
             "summary": "Neutrinos were observed to arrive about 60 ns earlier than light "
                        "would, over a 730 km baseline. The collaboration states the result "
                        "is preliminary and invites scrutiny.",
             "published": "2011-09-22T00:00:00+00:00", "pickups": 1},
            {"source_key": "ars-technica", "evidence_type": "secondary",
             "title": "Faster-than-light neutrinos? Physicists urge caution",
             "summary": "Extraordinary claim requires extraordinary evidence; systematic "
                        "error is considered the most likely explanation.",
             "published": "2011-09-23T00:00:00+00:00", "pickups": 1},
            {"source_key": "hnrss", "evidence_type": "secondary",
             "title": "OPERA results and the timing question",
             "summary": "Discussion of possible clock and cable-length errors.",
             "published": "2011-09-24T00:00:00+00:00", "pickups": 1},
        ],
    },
]


def run_check(case: dict) -> dict:
    """Run stage-5 relevance re-check on the 3-item shortlist."""
    f = WORK / f"{case['id']}_shortlist.json"
    f.write_text(json.dumps({"entries": case["entries"]}, ensure_ascii=False),
                 encoding="utf-8")
    cmd = [sys.executable, str(FETCH), "--check-json", str(f)]
    for k in case["keywords"]:
        cmd += ["--keywords", k]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    text = (out.stdout or "") + (out.stderr or "")
    cov = re.search(r"覆盖\s+([\d.]+)\s*/\s*阈值", text)
    return {
        "coverage": float(cov.group(1)) if cov else None,
        "needs_second_pass": "需二次搜寻" in text,
        "raw": text.strip(),
    }


def weight_of(key: str, ev: str, notices: list | None = None) -> float:
    """Mirror the SHIPPED scoring, including the retraction short-circuit.

    Kept in sync with fetch_feeds.truth_weight on purpose: if the two diverge,
    this harness starts blessing behaviour the script no longer has.
    """
    src = {
        "crossref-science": 0.90, "crossref-nature": 0.90, "crossref-nejm": 0.90,
        "crossref-lancet": 0.90, "crossref-cell": 0.90, "crossref-natmed": 0.90,
        "crossref-natbiotech": 0.88, "crossref-sciadv": 0.86, "crossref-pnas": 0.86,
        "openai-news": 0.85, "arxiv-ai": 0.62, "arxiv-rss-ai": 0.62,
        "biorxiv": 0.60, "medrxiv": 0.58, "techreview": 0.78, "ieee-spectrum": 0.76,
        "ars-technica": 0.72, "phys-org": 0.62, "science-daily": 0.58,
        "hn-frontpage": 0.45, "hnrss": 0.45, "hn-top": 0.45, "reddit-ml": 0.40,
        "ithome": 0.52, "36kr": 0.48, "baidu-hot": 0.35, "toutiao-hot": 0.35,
        "google-trends-us": 0.35,
    }.get(key, 0.50)
    evidence = {"peer-reviewed": 0.92, "official": 0.84, "dataset": 0.78,
                "secondary": 0.55, "preprint": 0.55, "rumor": 0.25}.get(ev, 0.55)
    score = round(0.4 * src + 0.6 * evidence, 3)
    labels = " ".join(str((n or {}).get("label", "")).lower() for n in (notices or []))
    if any(k in labels for k in ("retraction", "retracted", "withdrawal", "withdrawn")):
        return 0.05
    if "concern" in labels:
        return min(score, 0.45)
    if any(k in labels for k in ("correction", "corrigendum", "erratum", "addendum")):
        score = min(score, 0.60)
    return score


def main() -> int:
    print("=" * 78)
    print("trend-radar 阶段测试 · 历史新闻回溯")
    print("考的是：当时共识是错的，管线会不会照抄共识")
    print("=" * 78)

    verdicts = []
    for case in CASES:
        print(f"\n{'─' * 78}")
        print(f"【{case['title']}】")
        print(f"  需求      : {case['request']}")
        print(f"  关键词    : {', '.join(case['keywords'])}")
        print(f"  事后真相  : {case['truth_now']}")
        print(f"\n  权重判定（按脚本规则离线复算）：")
        passed, failed = [], []
        for e in case["entries"]:
            tw = weight_of(e["source_key"], e["evidence_type"], e.get("update_notices"))
            ok = tw >= 0.6
            (passed if ok else failed).append((e["source_key"], tw))
            alert = ""
            if e.get("update_notices"):
                alert = "  ★撤稿拦截" if tw == 0.05 else "  (有更正记录)"
            print(f"    {'过' if ok else '  '} truth={tw:.3f}  {e['evidence_type']:14} "
                  f"{e['source_key']:16} {e['title'][:40]}{alert}")
        rel = run_check(case)
        print(f"\n  关联度    : {rel['coverage']}  "
              f"[{'需二次搜寻' if rel['needs_second_pass'] else 'OK'}]")
        verdicts.append({
            "case": case, "passed": passed, "failed": failed, "rel": rel,
            "trend_like": bool(passed) and not failed,
        })

    print(f"\n{'=' * 78}")
    print("测试结论")
    print("=" * 78)
    bugs = []
    for v in verdicts:
        c = v["case"]
        n_pass, n_fail = len(v["passed"]), len(v["failed"])
        print(f"\n  {c['id']:9} 过闸 {n_pass} / 未过 {n_fail}")
        if n_pass and not n_fail:
            bugs.append(
                f"{c['id']}: 全部来源都过了真值闸门，但事后真相是「{c['truth_now'][:34]}…」。"
                f"管线会把这个错误结论放进核心区。")
    print("\n  发现的漏洞：")
    for b in bugs:
        print(f"    ! {b}")
    if not bugs:
        print("    （本次未发现）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
