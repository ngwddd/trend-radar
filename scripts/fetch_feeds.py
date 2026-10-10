#!/usr/bin/env python3
"""trend-radar : fetch structured sources (RSS/Atom/API) into one JSON.

Deterministic half of the skill. The model reads the JSON and judges value;
this script only retrieves, normalizes, time-filters and dedupes.

Windows notes baked in:
  * stdout/stderr forced to UTF-8 (console is GBK by default).
  * --out writes a file; never rely on console output for CJK.

Usage:
  python fetch_feeds.py --list
  python fetch_feeds.py --source ai --days 3 --out out.json
  python fetch_feeds.py --source ai --source general --days 7 --max-per-source 30
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import threading
from concurrent.futures import (ThreadPoolExecutor, wait, as_completed,
                                FIRST_COMPLETED)
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - non-reconfigurable stream
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TIMEOUT = 25
RETRY_STATUS = {429, 500, 502, 503, 504}
# 退避曾经是 (4.0, 12.0) —— 实测代价：reddit-ml 撞 429 后单源耗 17.06s，
# 其中 16s 纯粹在 sleep。而 429 是"这个源此刻不给数据"，多等 12 秒并不会
# 让它给。缩短到 (2.0, 4.0)：仍给瞬时抖动一次机会，但不再为确定性拒绝付 16 秒。
RETRY_WAITS = (2.0, 4.0)
ARXIV_GAP = 3.0                # polite gap between arXiv API calls


def _ssl_context() -> ssl.SSLContext:
    """Prefer certifi's CA bundle.

    On this machine Python's default store is OpenSSL's own
    (C:\\Program Files\\Common Files\\SSL, 114 CAs) which is stale enough that
    technologyreview.com and news.ycombinator.com fail with
    CERTIFICATE_VERIFY_FAILED; certifi (121 CAs) verifies them fine.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # certifi missing -> stock store, still works for most hosts
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()
# 键是 (url, ua, timeout)。`_mirror_fetch` 现在用线程池并发竞速镜像，
# 所以这个 dict 会被多线程读写 —— 加锁。竞争窗口极小（只是 dict 存取），
# 但并发写 dict 在 CPython 之外不保证安全，而且"偶发损坏的缓存"
# 是最难查的一类 bug。
_HTTP_CACHE: dict[tuple[str, str, int], bytes] = {}
_CACHE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Three main domains. The pipeline picks ONE of these from the user's request,
# then routes both subagents inside it.
#   social  社会/人文  -- 热点新闻、公共事件、政策、人文
#   science 科学进步   -- 生物医学、Nature/Science 等大刊、科学进展
#   ai      AI         -- 模型、方法、基准、AI 产业
# A source may serve more than one domain.
# ---------------------------------------------------------------------------
DOMAINS = {
    "social": "社会/人文（热点新闻、公共事件、政策）",
    "science": "科学进步（生物医学、大刊、科学进展）",
    "ai": "AI（模型、方法、基准、产业）",
}

# Errata are registered with Crossref as ordinary `journal-article`, so the
# server-side type filter does NOT remove them (measured). They must be dropped
# client-side or every journal feed is polluted with corrections.
ERRATA_RE = re.compile(
    r"(?i)^\s*(publisher|author|editor)?\s*correction\b|^\s*erratum\b|"
    r"^\s*retraction\b|^\s*retracted\b|expression of concern|"
    r"^\s*corrigendum\b|^\s*addendum\b|^\s*withdrawn\b"
)
ERRATA_DROP = True

# ---------------------------------------------------------------------------
# Source credibility weights (0-1).
#
# Grounded in two established evaluation frameworks, not invented numbers:
#   SIFT  (Caulfield) - Stop, Investigate the source, Find better coverage,
#                       **Trace claims to the original context**. The "trace"
#                       move is exactly what the 真实 element measures.
#   CRAAP (Meriam Library) - Currency, Relevance, Authority, Accuracy, Purpose.
#                       "Authority" + "Accuracy" drive the base weight below.
#
# Scoring rules are deliberately SIMPLE and AUDITABLE:
#   base  = how much editorial/verification process stands behind the source
#   the authoritative-source tier tops out at 0.90, NOT 1.0, because even
#   peer review is fallible - leaving headroom means nothing ever scores a
#   perfect "verified" and the 0.6 gate stays meaningful.
#
# `primary` marks sources that ARE the origin of a claim (a paper, an official
# announcement). Everything else is a report ABOUT someone else's claim.
# ---------------------------------------------------------------------------
SOURCE_WEIGHT = {
    # -- primary: origin of the claim --
    "crossref-science":    0.90,   # peer-reviewed, top-tier
    "crossref-nature":     0.90,
    "crossref-nejm":       0.90,
    "crossref-lancet":     0.90,
    "crossref-cell":       0.90,
    "crossref-natmed":     0.90,
    "crossref-natbiotech": 0.88,
    "crossref-natmi":      0.88,
    "crossref-natcomp":    0.86,
    "crossref-sciadv":     0.86,
    "crossref-pnas":       0.86,
    # 官方口径但自我利益相关：0.85 → 0.78。
    # 实测问题：0.85 让营销内容拿到 0.844，【高于】同行评审的 PNAS 0.896 只差一点，
    # 而 8 条客户案例全靠一个固定的 −0.25 才被压下去 —— 0.844−0.25=0.594，
    # 距阈值仅差 0.006，任何微调都让 8 条整体翻转。那是精确击穿，不是稳健判定。
    # 降低源权重是诚实的做法：官方声明本来就该低于独立评审的研究。
    "openai-news":         0.78,
    "arxiv-ai":            0.62,   # preprint: moderated, not peer-reviewed
    "arxiv-rss-ai":        0.62,
    "biorxiv":             0.60,
    "medrxiv":             0.58,   # clinical preprints: weakest primary tier
    # -- secondary: verified reporting about someone else's claim --
    "techreview":          0.78,
    "ieee-spectrum":       0.76,
    "ars-technica":        0.72,
    "phys-org":            0.62,
    "science-daily":       0.58,
    "hn-frontpage":        0.45,
    "hn-top":              0.45,
    "reddit-ml":           0.40,
    "ithome":              0.52,   # fast, lightly edited
    "36kr":                0.48,   # paid placement mixed in
    # 中新社主办的中新网。**实测它原本不在本表里**，拿的是 `SOURCE_WEIGHT.get(k, 0.50)`
    # 那个兜底默认值 —— 等于让一个未声明的默认值替我做权重决定，这不可审计。
    # 显式登记并写明理由（与同级中文源对比）：
    #   ithome 0.52  —— 快，但轻编辑
    #   36kr   0.48  —— 掺付费软文
    # 中新网取 0.50：通稿社背景、有编辑流程，但仍是**二次报道**
    # （它报的是别人的一手，不是自己的发现），所以不给到通讯社级。
    # ⚠️ 这个权重意味着**单源过不了真值闸门**（0.4×0.50+0.6×0.55 = 0.53 < 0.6），
    # 只能靠多家独立印证（+0.12 印证加成）才过。
    # **这是有意的**：社会新闻天然是"多家都报"，独立印证正是这个域该用的尺子。
    # 想让单源直接过闸就得把权重抬到 0.90 一档，那是拿打分迁就结果，不做。
    "chinanews":           0.50,
    # ── 中文 AI / 科技垂媒（2026-10-10 新增，全部二次报道）─────────────
    # 都是转述别人的发布，所以压在 0.50–0.62：低于同行评审与官方一手，
    # 高于 36kr（0.48，掺付费软文）。
    "infoq-cn":            0.62,   # 工程实践向，编辑质量最高的一档
    "qbitai":              0.58,   # 原创报道比例高
    "solidot":             0.58,   # 技术社区视角，短讯为主
    "leiphone":            0.55,   # AI 产业向
    "geekpark":            0.55,   # 产品/产业向
    "tmtpost":             0.52,   # 科技商业向，软文比例略高
    "oschina":             0.50,   # 开源生态，量大但编辑轻
    # ── 中文专业媒体 ────────────────────────────────────────────────
    "jiemian":             0.74,   # 对齐已有的 yicai（第一财经）档
    "eeo":                 0.72,   # 财经深度，同档取低
    # ── 中文国际视角（立场明确，压在大众媒体档）─────────────────────
    "dw-zh":               0.55,
    "bbc-zh":              0.55,
    "nyt-zh":              0.55,
    "rfi-zh":              0.52,
    # -- trend boards: popularity signal, NOT a credibility signal --
    "toutiao-hot":         0.35,
    "google-trends-us":    0.35,
}

PRIMARY_SOURCES = {
    "crossref-science", "crossref-nature", "crossref-nejm", "crossref-lancet",
    "crossref-cell", "crossref-natmed", "crossref-natbiotech", "crossref-sciadv",
    "crossref-natmi", "crossref-natcomp",
    "crossref-pnas", "openai-news", "arxiv-ai", "arxiv-rss-ai", "biorxiv", "medrxiv",
}

# Tier base for the evidence type actually attached to a claim.
# `journalism` exists because Crossref files Nature's front-matter (books in
# brief, picture essays, briefing chats) as ordinary journal-articles. Scoring
# those as peer-reviewed put a books column at the top of the run.
EVIDENCE_WEIGHT = {
    "peer-reviewed": 0.92,   # top tier: independent review before publication
    "official":      0.84,   # first-party statement: authoritative but self-interested
    "dataset":       0.78,
    # `journalism` sits BELOW preprint/secondary, not level with them.
    # Measured inversion: at 0.55 a Nature books column scored 0.69, above a bioRxiv
    # preprint (0.57) and an IEEE empirical report (0.634) -- a piece with no claim
    # ranked above research. Source weight dominates this formula (0.4 x 0.90 for
    # Nature), so capping the evidence term was not enough; the term itself has to
    # reflect that a review/photo-essay/briefing carries nothing to verify.
    # 0.30 keeps it above `rumor` while putting it below preprints. Measured result:
    # Nature books column 0.54, below bioRxiv 0.57.
    "journalism":    0.30,
    "secondary":     0.55,   # a report about someone else's claim
    "preprint":      0.55,   # moderated screening only, no peer review
    "rumor":         0.25,
}

# An independent stream carrying the same claim is real corroboration.
CROSS_CONFIRM_BONUS = 0.12
CROSS_CONFIRM_CAP = 1.0

# The gate the pipeline uses on 真实 / 传播报道.
CREDIBILITY_THRESHOLD = 0.6

# 单源硬证据通道的合格证据类型。
#
# ★ 只有 `peer-reviewed`。理由：这是唯一**本身就含独立核验**的类型 ——
# 同行评审是第三方做的。`official` 虽然是一手，但**没有独立核验**，
# 而且官方是自我利益相关方（厂商说自己的模型好，那是自述不是证据）。
# 实测教训：一开始把 official 也放进来，结果 openai-news 那 6 条客户案例
# 被"official"这个标签护送进了核心区 —— 营销内容进了本该放最硬证据的地方。
# 收紧到只剩 peer-reviewed 之后，核心区是 1 条 Nat Comput Sci 论文。
#
# 刻意【不】用源权重阈值来判（如 source_weight>=0.9）：那会变成第二套层级实现，
# 而实测教训是「同一件事两处实现必然打架」（identity_of/entity_id 就是活例）。
HARD_EVIDENCE_TYPES = frozenset({"peer-reviewed"})

# 评分过程中发现的「登记了但没给权重」的源。见 truth_weight 里的说明。
# 用集合而不是抛异常：一次跑批不该因为一个漏登记就整体失败，
# 但这件事必须能被查出来（`--list` 会打印，测试会断言为空）。
UNWEIGHTED_SOURCES: set[str] = set()

# ---------------------------------------------------------------------------
# 问题4 修复：社会域没有权威源，无法做权威交叉核对 → 走另一套核对系统
#
# Measured: `--domain social --class authority` returns ZERO sources. The social
# domain simply has no primary/authoritative tier, so 3c cross-checking is
# impossible there. Rather than let every social claim fail the gate for lack of
# a stream that does not exist, social uses a DIFFERENT reconciliation:
#
#   真实 = 0.30·平台可信度 + 0.70·证据形式        <- inverted vs the default
#   传播 = 0.60·独立转载 + 0.40·平台热度
#
# Why the inversion: in the default path the source tier carries 0.4 weight
# because a journal's ISSN tells you something about reliability. In the social
# domain there is no such tier -- the domain is, by construction, all secondary
# reporting. What distinguishes good from bad social reporting is the FORM of
# the evidence (an official notice vs an unattributed claim), not the masthead.
# So evidence form takes the dominant weight.
#
# 热度 is load-bearing here (0.40 of 传播) precisely because the authority stream
# does not exist: without it, social 传播 could only ever come from reprints.
# ---------------------------------------------------------------------------
PLATFORM_WEIGHT = {
    # official / primary notices
    "gov-cn":       0.92, "cctv":       0.88, "xinhua":     0.88,
    # first-hand investigative reporting with named outlets
    "thepaper":     0.78, "people":     0.78, "guangming":  0.76,
    "cnr":          0.74, "chinadaily": 0.74, "yicai":     0.74,
    "stcn":         0.72, "bjnews":     0.72, "caixin":    0.80,
    # professional media, mostly secondary
    "36kr":         0.48, "ithome":     0.52, "ars-technica": 0.72,
    "techreview":   0.78, "ieee-spectrum": 0.76,
    # aggregators / community: lead only
    "toutiao":      0.35, "baidu":      0.35, "weibo":     0.35,
    "hn":           0.45, "reddit":     0.40,
    # unknown outlet: sits below the gate on purpose -- an unrecognised masthead
    # must not be able to clear 真实 just by existing
    "unknown":      0.40,
}

# Evidence FORM inside social reporting. This is the dominant axis for social.
SOCIAL_EVIDENCE_FORM = {
    "official-notice":   0.92,   # 官方通报/公告/法规原文
    "first-hand-report": 0.78,   # 一手调查，有具名记者/媒体/现场
    "named-entity":      0.68,   # 具名主体 + 具体数字，但转述
    "secondary-report":  0.52,   # 二手转述，无一手痕迹
    "unattributed":      0.30,   # 无主体、无出处、纯转述
    "rumor":             0.25,
}

# Concrete-detail regexes. Specificity is what separates reportable social news
# from vague chatter, and unlike a masthead it can be MEASURED from the text.
RE_QUANTITY = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|亿元|万元|元|人|人次|辆|例|天|小时|分钟|吨|公里|km|"
    r"亿美元|万人|个|家|所|起|倍|万|亿|"
    # SI / engineering units are common in Chinese tech reporting ("1600N 踩踏力").
    # Missing these made a concrete, number-rich story classify as 'unattributed'.
    r"N|kN|W|kW|V|kV|Hz|GHz|MHz|nm|mm|cm|m|kg|g|ms|s|A|mAh|Wh|°C|℃)")
RE_ORG = re.compile(
    r"[\u4e00-\u9fff]{2,12}(?:局|部|委|厅|办|署|院|大学|学院|医院|公司|集团|银行|"
    r"协会|中心|委员会|政府|法院|检察院|研究院|研究所)")
RE_DATE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}\s*年|\d{4}-\d{2}-\d{2}")
RE_PLACE = re.compile(
    # Negative lookahead on 值/场/民/委/政: without it, "江淮两日市值" matches as a
    # place named "江淮两日市". Measured false positive.
    r"[\u4e00-\u9fff]{2,8}(?:省|市|区|县|镇|乡|村|街道|自治区)(?![值场民委政])")

# 事故/损失类锚点词。加了它之后，真事故通报才排得到导购文上面。
# Measured failure without it: '广西贵港闪爆事故2名失联人员确认已无生命体征'
# scored 3 -> 'secondary-report', while '智界 R7 焕新版…23.98 万元起' scored 4 and
# 'AOC 16" AMOLED…' scored 6 -> 'named-entity'. Real news ranked BELOW adverts.
# These terms are highly specific and cannot be padded the way spec sheets can.
RE_CASUALTY = re.compile(
    r"(?:失联|遇难|死亡|受伤|被困|身亡|伤亡|溺亡|坠亡)[\u4e00-\u9fff]{0,2}(?:人员|人数|人)?"
    r"|闪爆|爆燃|爆炸|火灾|坍塌|垮塌|中毒|泄漏|泄露|追尾|脱轨|坠机|沉没"
    r"|召回|停产|整顿|处罚|罚款|判刑|逮捕|立案|问责|免职|停职"
    r"|直接经济损失|受灾|转移安置|撤离")

# Domain classifier tokens. DECLARED as a limitation: these are keyword lists,
# so a mis-worded request can be mis-classified. The defence is that a
# classification request is only honoured for the categories it actually names.
DOMAIN_TOKENS = {
    "social": ("社会", "热点", "新闻", "民生", "公众", "政策", "事件", "舆情"),
    "ai":     ("ai", "人工智能", "模型", "算法", "大模型", "机器学习", "深度学习"),
    "science": ("科学", "科研", "论文", "期刊", "生物", "医学", "物理", "化学",
                "nature", "science", "研究进展"),
}

# ---------------------------------------------------------------------------
# Source registry.  `status` reflects MEASURED reachability on this machine
# (2026-10-10).  Do not flip a BLOCKED source to OK without re-measuring.
# ---------------------------------------------------------------------------
SOURCES: dict[str, dict] = {
    # ---- AI / 科技 ----
    "arxiv-ai": {
        "kind": "atom",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "http://export.arxiv.org/api/query?search_query=cat:{cat}&sortBy=submittedDate&sortOrder=descending&max_results={n}",
        "cats": ["cs.AI", "cs.LG", "cs.CL", "cs.CV"],
        "label": "arXiv (cs.AI/LG/CL/CV)",
        "status": "OK",
        "note": "预印本，未经同行评审；日期用 published（首次提交）",
    },
    "arxiv-rss-ai": {
        "kind": "rss",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "https://export.arxiv.org/rss/cs.AI",
        "label": "arXiv cs.AI daily RSS",
        "status": "OK",
        "note": "只含当日新公告，量大（约 1MB）",
    },
    "techreview": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "ai",
        "url": "https://www.technologyreview.com/feed/",
        "label": "MIT Technology Review",
        "status": "OK",
        "note": "专业媒体，介于两路之间；观点文多，需过滤",
    },
    "ieee-spectrum": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "ai",
        "url": "https://spectrum.ieee.org/feeds/feed.rss",
        "label": "IEEE Spectrum",
        "status": "OK",
        "note": "专业媒体，行业风向",
    },
    "openai-news": {
        "kind": "rss",
        "domains": ["ai"],
        "class": "authority",
        "group": "ai",
        "url": "https://openai.com/news/rss.xml",
        "label": "OpenAI News",
        "status": "OK",
        "note": "官方口径：是一手来源，但不是独立验证。不能自己证明自己",
    },
    "ars-technica": {
        "kind": "rss",
        "domains": ["ai", "science", "social"],
        "class": "social",
        "group": "general",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "label": "Ars Technica",
        "status": "OK",
        "note": "科技媒体，报道较扎实但仍是二手",
    },
    "hn-frontpage": {
        "kind": "rss",
        "domains": ["ai", "science"],
        "class": "social",
        "group": "general",
        "url": "https://news.ycombinator.com/rss",
        # HN returns HTTP 419 to browser-like User-Agents (measured) but serves a
        # plain client string fine, so this source overrides the default UA.
        "ua": "curl/8.4.0",
        "label": "Hacker News front page",
        "status": "OK",
        "note": "社区热度，不等于价值；仅作线索来源。必须用 curl UA，浏览器 UA 会被 419",
    },
    # ---- 中文 / 公众热点 ----
    "36kr": {
        "kind": "rss",
        "domains": ["social", "ai"],
        "class": "social",
        "group": "general",
        "url": "https://www.36kr.com/feed",
        "label": "36氪",
        "status": "OK",
        "note": "含大量软文与融资 PR",
    },
    "ithome": {
        "kind": "rss",
        "domains": ["social", "ai"],
        "class": "social",
        "group": "general",
        "url": "https://www.ithome.com/rss/",
        "label": "IT之家",
        "status": "OK",
        "note": "快但噪声大",
    },
    # ---- 学术期刊：直连被挡，走 Crossref ----
    "crossref-science": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0036-8075",
        "label": "Science (via Crossref)",
        "status": "OK",
        "note": "只拿到元数据+摘要，拿不到正文（science.org 直连 403）",
    },
    "crossref-nature": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0028-0836",
        "label": "Nature (via Crossref)",
        "status": "OK",
        "note": "只拿到元数据+摘要（nature.com RSS 实测返回 bot 挡页）",
    },
    # ---- 科学类补充：生物医学与综合顶刊 ----
    "crossref-sciadv": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "2375-2548",
        "label": "Science Advances (via Crossref)",
        "status": "OK",
    },
    "crossref-pnas": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0027-8424",
        "label": "PNAS (via Crossref)",
        "status": "OK",
    },
    "crossref-nejm": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0028-4793",
        "label": "NEJM (via Crossref)",
        "status": "OK",
        "note": "临床医学顶刊；含大量通信与试验注册，需过滤",
    },
    "crossref-lancet": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0140-6736",
        "label": "The Lancet (via Crossref)",
        "status": "OK",
    },
    "crossref-cell": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "0092-8674",
        "label": "Cell (via Crossref)",
        "status": "OK",
    },
    "crossref-natmed": {
        "kind": "crossref",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "issn": "1078-8956",
        "label": "Nature Medicine (via Crossref)",
        "status": "OK",
    },
    "crossref-natbiotech": {
        "kind": "crossref",
        "domains": ["science", "ai"],
        "class": "authority",
        "group": "journal",
        "issn": "1087-0156",
        "label": "Nature Biotechnology (via Crossref)",
        "status": "OK",
    },
    # ---- AI-domain authoritative sources ----
    # Measured gap these fill: before them the ENTIRE `ai` authority stream was
    # arxiv-ai + arxiv-rss-ai + openai-news + crossref-natbiotech. Both arXiv
    # entries are preprints, and a preprint tops out at 0.578 < the 0.6 gate, so
    # the AI authority stream could never emit a gated item (measured: 0 of 32
    # passed, with the only truth-passers being OpenAI's own self-interested
    # posts). That is a coverage hole, not a content-quality verdict.
    "crossref-natmi": {
        "kind": "crossref",
        "domains": ["ai", "science"],
        "class": "authority",
        "group": "journal",
        "issn": "2522-5839",
        "label": "Nature Machine Intelligence (via Crossref)",
        "status": "OK",
        "note": "AI 领域少有的同行评审权威源；实测近 7 天有在线发表",
    },
    "crossref-natcomp": {
        "kind": "crossref",
        "domains": ["ai", "science"],
        "class": "authority",
        "group": "journal",
        "issn": "2662-8457",
        "label": "Nature Computational Science (via Crossref)",
        "status": "OK",
        "note": "计算方法与 AI 交叉；实测近 7 天有在线发表",
    },
    "biorxiv": {
        "kind": "rss",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=all",
        "label": "bioRxiv (全部学科)",
        "status": "OK",
        "note": "预印本。RDF 格式，<link> 是纯文本（已兼容）。30 条/次；标题常为全大写",
    },
    "medrxiv": {
        "kind": "rss",
        "domains": ["science"],
        "class": "authority",
        "group": "journal",
        "url": "https://connect.medrxiv.org/medrxiv_xml.php?subject=all",
        "label": "medRxiv (临床预印本)",
        "status": "OK",
        "note": "预印本，未经同行评审。临床结论尤其不能当已证实",
    },
    "phys-org": {
        "kind": "rss",
        "domains": ["science"],
        "class": "social",
        "group": "journal",
        "url": "https://phys.org/rss-feed/",
        "label": "Phys.org",
        "status": "OK",
        "note": "科学新闻聚合，二手报道；常把预印本写成「研究显示」，需回溯原文",
    },
    "science-daily": {
        "kind": "rss",
        "domains": ["science"],
        "class": "social",
        "group": "journal",
        "url": "https://www.sciencedaily.com/rss/all.xml",
        "label": "ScienceDaily",
        "status": "OK",
        "note": "二手科普报道，标题夸张倾向明显，必须回溯一手论文",
    },
    # ---- 热度榜：主窗口在等待两个子代理时抓这一路 ----
    # These are the ONLY sources ranked by POPULARITY rather than recency.
    # Popularity is not importance -- treat as 风向, never as 事实.
    "toutiao-hot": {
        "kind": "toutiao_hot",
        "group": "trend",
        "domains": ["social"],
        "class": "trend",
        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "label": "头条热榜 (official JSON)",
        "status": "OK",
        "note": "官方 JSON，含热度值 HotValue。按热度排序",
    },
    "hn-top": {
        "kind": "rss",
        "group": "trend",
        "domains": ["ai", "science", "social"],
        "class": "trend",
        "url": "https://news.ycombinator.com/rss",
        "ua": "curl/8.4.0",
        "label": "Hacker News (热度近似：排名序)",
        "status": "OK",
        "note": "RSS 无热度数值，靠条目顺序近似。URL 取原文链接，讨论页另存 discussion_url。必须用 curl UA",
    },
    "reddit-ml": {
        "kind": "rss",
        "group": "trend",
        "domains": ["ai"],
        "class": "trend",
        "url": "https://www.reddit.com/r/MachineLearning/top/.rss?t=day",
        "label": "r/MachineLearning 日榜",
        "status": "OK",
        "note": "真·按热度排序（top + t=day）。社区热度仅作线索。Reddit 会 429，只登记一次、别再加同 URL 的第二个 key",
    },
    "google-trends-us": {
        "kind": "google_trends",
        "group": "trend",
        "domains": ["social"],
        "class": "trend",
        "url": "https://trends.google.com/trending/rss?geo=US",
        "label": "Google Trends (US daily)",
        "status": "OK",
        "note": "英文世界搜索热度，含 approx_traffic 与原文链接；但榜单本体大量是人名/娱乐，"
                "只有 related_news 有信息量。geo=CN 实测 HTTP 400，不可用",
    },

    # ── 中文社会民生一手源 ──────────────────────────────────────────────
    # 为什么必须加：实测 `--domain social --class social` 只有 3 个源，
    # 且**全是科技媒体**（36氪创投 / IT之家 3C / Ars 英文科技）——
    # 真正的社会民生中文源一个都没有。后果是社会类每跑一次，
    # 八成内容靠 web_search 补，不是"有源可依"。
    #
    # `kind: rss_multi` 是为此新加的：中新网的 4 个版面是【不同内容】，
    # 不是同内容镜像，所以不能用 `urls`（那个字段的语义是
    # 「候选镜像，取第一个非空的」，见 L855 `urls = spec.get("urls") or [spec["url"]]`）。
    # 也不该拆成 4 个源 —— 那会污染 `scanned` 源计数与降级分母
    # （用户看到"7/11 源成功"，其实只有 8 家媒体）。
    "chinanews": {
        "kind": "rss_multi",
        "group": "general",
        "domains": ["social", "science"],
        "class": "social",
        # 实测 2026-10-10：4 个 feed 全部 HTTP 200、30 条、首条当天 21:47–21:49、带摘要。
        # `gj.xml`（国际）HTTP 404 —— 不登记猜测的 URL。
        # 注意必须用带 `www.` 的域名，裸域名会重定向。
        "feeds": [
            "https://www.chinanews.com.cn/rss/scroll-news.xml",   # 滚动（时效最强）
            "https://www.chinanews.com.cn/rss/society.xml",       # 社会
            "https://www.chinanews.com.cn/rss/china.xml",         # 国内
            "https://www.chinanews.com.cn/rss/finance.xml",       # 财经
            # 2026-10-10 实测新增（全部 200 / 首条当天）：
            "https://www.chinanews.com.cn/rss/world.xml",         # 国际
            "https://www.chinanews.com.cn/rss/health.xml",        # 健康
            "https://www.chinanews.com.cn/rss/edu.xml",           # 教育
            # ⚠️ `gj.xml` 实测 HTTP 404 —— 国际版面的正确路径是 `world.xml`。
            #    不登记猜测的 URL（这条是实测确认过的）。
        ],
        "label": "中国新闻网",
        "status": "OK",
        "note": "中新社主办，社会民生中文一手源。4 个版面【并发抓取后合并去重】。"
                "实测首条时间戳=当天，不是停更存档源"
                "（对比：人民网 RSS 返回 200+100 条但内容是 2025-06 的、"
                "新华网 200+300 条是 2022-12 的 —— 那类静默陷阱比 404 危险得多）",
    },

    # ── 中文 AI / 科技垂媒（2026-10-10 实测新增）────────────────────────
    # 背景：此前 `--domain ai` 的中文侧几乎是空的 —— 中国大模型发布、
    # 国内 AI 产业动向**连续多次跑批 0 覆盖**，只能靠 web_search 补。
    # 下面 6 个源全部实测：HTTP 200 / 首条=当天 / 有摘要。
    #
    # 权重区间 0.50–0.62：都是**二次报道**（转述别人的发布），
    # 所以低于同行评审（0.86–0.92）与官方一手（0.78–0.85），
    # 高于 36kr（0.48，掺付费软文）。
    # 单源过不了 0.6 真值闸门（0.4×0.55+0.6×0.55≈0.55）—— **这是有意的**，
    # 国内 AI 新闻天然多家都报，靠独立印证加成（+0.12）才是这个域该用的尺子。
    "qbitai": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.qbitai.com/feed",
        "label": "量子位", "status": "OK",
        "note": "中文 AI 垂媒，原创报道比例高。实测 1.1s / 10 条 / 首条当天",
    },
    "infoq-cn": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.infoq.cn/feed",
        "label": "InfoQ 中文", "status": "OK",
        "note": "工程实践向，偏架构与落地。实测 0.9s / 20 条 / 首条当天。"
                "⚠️ 正文页常需 JS 渲染，web_fetch 只拿到标题",
    },
    "oschina": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.oschina.net/news/rss",
        "label": "开源中国", "status": "OK",
        "note": "开源生态向，量大（50 条）。实测 3.3s / 首条当天",
    },
    "leiphone": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.leiphone.com/feed",
        "label": "雷锋网", "status": "OK",
        "note": "AI 产业向。实测 1.9s / 20 条 / 首条当天。"
                "⚠️ 其 AI 子站 leiphone-ai 实测 HTTP 500，不要用那个路径",
    },
    "tmtpost": {
        "kind": "rss", "group": "ai", "domains": ["ai", "social"], "class": "social",
        "url": "https://www.tmtpost.com/rss.xml",
        "label": "钛媒体", "status": "OK",
        "note": "科技商业向，兼有产业与政策。实测 3.6s / 18 条 / 首条当天",
    },
    "geekpark": {
        "kind": "rss", "group": "ai", "domains": ["ai"], "class": "social",
        "url": "https://www.geekpark.net/rss",
        "label": "极客公园", "status": "OK",
        "note": "产品与产业向。实测 3.8s / 30 条 / 首条当天",
    },
    "solidot": {
        "kind": "rss", "group": "ai", "domains": ["ai", "science"], "class": "social",
        "url": "https://www.solidot.org/index.rss",
        "label": "Solidot", "status": "OK",
        "note": "奇客资讯，科技/科学短讯，倾向技术社区视角。"
                "实测 2.6s / 20 条 / 首条当天",
    },

    # ── 中文专业媒体（2026-10-10 实测新增）─────────────────────────────
    "jiemian": {
        "kind": "rss", "group": "general", "domains": ["social", "ai"], "class": "social",
        "url": "https://a.jiemian.com/index.php?m=article&a=rss",
        "label": "界面新闻", "status": "OK",
        "note": "上海报业旗下财经/商业媒体。实测 5.1s / 30 条 / 首条当天。"
                "权重 0.74 对齐已有的 yicai（第一财经）档",
    },
    "eeo": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://www.eeo.com.cn/rss.xml",
        "label": "经济观察报", "status": "OK",
        "note": "财经深度报道。实测 1.4s / 11 条 / 首条当天（法治/社会类也进）",
    },

    # ── 中文国际视角（2026-10-10 实测新增）─────────────────────────────
    # ⚠️ 权重统一压在大众媒体档（0.52–0.55），且 note 必须写明立场属性 ——
    # 它们有编辑标准，但涉华议题立场明确，不能当不带观点的通稿源用。
    "dw-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://rss.dw.com/rdf/rss-chi-all",
        "label": "德国之声中文", "status": "OK",
        "note": "德国公共广播。实测 1.3s / 53 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "bbc-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
        "label": "BBC 中文", "status": "OK",
        "note": "实测 0.7s / 38 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "rfi-zh": {
        "kind": "rss", "group": "general", "domains": ["social"], "class": "social",
        "url": "https://www.rfi.fr/cn/rss",
        "label": "法国国际广播电台中文", "status": "OK",
        "note": "实测 0.2s / 30 条 / 首条当天。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场",
    },
    "nyt-zh": {
        "kind": "rss", "group": "general", "domains": ["social", "ai"], "class": "social",
        "url": "https://cn.nytimes.com/rss/",
        "label": "纽约时报中文网", "status": "OK",
        "note": "实测 0.3s / 20 条 / 首条 10-09。"
                "⚠️ 涉华议题立场明确，引用时须标明出处立场。"
                "⚠️ `/rss/technology/` 实测与根 feed 首条相同，疑为同一源，未单独登记",
    },
}

# Measured blockers - kept here so nobody re-discovers them the hard way.
BLOCKED = [
    ("https://www.nature.com/nature.rss", "HTTP 200 但是 Fastly bot 挡页 (text/html, /_fs-ch-)"),
    ("https://www.science.org/rss/news_current.xml", "HTTP 403"),
    ("https://www.cell.com/cell/current.rss", "HTTP 403"),
    ("https://rsshub.app/zhihu/hotlist", "HTTP 403"),
    ("https://api.semanticscholar.org/graph/v1/...", "HTTP 429 限流"),
    ("https://hnrss.org/frontpage", "超时（改用 news.ycombinator.com/rss 正常）"),
    # 热度榜类：都试过，都不通
    ("https://weibo.com/ajax/side/hotSearch", "HTTP 403（微博热搜 API）"),
    ("https://s.weibo.com/top/summary", "HTTP 200 但返回 HTML 壳，热度值靠 JS 渲染，抓不到"),
    ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total", "HTTP 401 需要授权"),
    ("https://www.douyin.com/aweme/v1/web/hot/search/list/", "HTTP 200 但返回 0 字节"),
    ("https://trends.google.com/trending/rss?geo=CN", "HTTP 400（只支持部分地区）"),
    ("https://github.com/trending", "HTTP 200 但纯 HTML，无 feed"),
    ("https://rsshub.app/weibo/search/hot", "HTTP 403（公共实例封禁）"),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    # RFC 822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # ISO 8601 (Atom)
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            return datetime.strptime(value[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _get(url: str, *, retries: int = 0, ua: str | None = None,
         timeout: int | None = None) -> bytes:
    """GET with optional retry on rate-limit / transient 5xx.

    Responses are memoized per process: several registry entries legitimately
    share one endpoint (reddit-ml and reddit-ml-top are the same URL), and
    fetching it twice got us HTTP 429 on the second call.

    `timeout` lets a single source override the global one. Measured need:
    sources differ by two orders of magnitude (ithome 14.2s vs techreview 0.13s),
    and one slow source used to delay every source after it in the same stream.
    """
    t = timeout or TIMEOUT
    cache_key = (url, ua or UA, t)
    with _CACHE_LOCK:
        if cache_key in _HTTP_CACHE:
            return _HTTP_CACHE[cache_key]
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, text/xml, */*",
        "Accept-Language": "en,zh-CN;q=0.9",
    })
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=t, context=SSL_CONTEXT) as resp:
                body = resp.read()
                _HTTP_CACHE[cache_key] = body
                return body
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= retries:
                raise
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            attempt += 1
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            attempt += 1


def _tag(elem) -> str:
    return elem.tag.rsplit("}", 1)[-1].lower()


def _find(node, *names):
    """Namespace-agnostic child lookup."""
    wanted = {n.lower() for n in names}
    for child in node:
        if _tag(child) in wanted:
            return child
    return None


def _text(node, *names) -> str | None:
    found = _find(node, *names)
    if found is None:
        return None
    if found.text:
        return found.text
    # atom <link href="..."/>
    for attr in ("href", "url"):
        if found.get(attr):
            return found.get(attr)
    return None


def _entry_link(node, prefer: str = "alternate") -> str | None:
    """Pick an entry link. `prefer='comments'` returns the discussion page
    (Hacker News item link) instead of the linked article."""
    best = None
    for child in node:
        if _tag(child) != "link":
            continue
        href = child.get("href")
        rel = (child.get("rel") or "alternate").lower()
        if not href and child.text and child.text.strip():
            href = child.text.strip()
            rel = "alternate"
        if not href:
            continue
        if prefer == "comments" and rel == "comments":
            return href
        if rel == "alternate" and best is None:
            best = href
        elif best is None:
            best = href
    return best


def parse_feed(payload: bytes, label: str, link_prefer: str = "alternate") -> list[dict]:
    """Parse RSS 2.0 or Atom into normalized entries."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"not parseable XML ({exc})") from exc

    items = [e for e in root.iter() if _tag(e) in ("item", "entry")]
    out = []
    for item in items:
        title = _text(item, "title") or ""
        summary = _text(item, "summary") or _text(item, "description") or _text(item, "content") or ""
        published = (_text(item, "published") or _text(item, "pubdate")
                     or _text(item, "updated") or _text(item, "date"))
        authors = []
        for child in item:
            if _tag(child) == "author":
                name = _find(child, "name")
                authors.append((name.text if name is not None and name.text else child.text) or "")
            elif _tag(child) == "creator" and child.text:
                authors.append(child.text)
        # `summary_truncated` 之前【只在 Crossref 路径】设置（见 fetch_crossref），
        # 于是 RSS 路径上被 _clip 截过的条目没有任何标记 —— 实测 16 条
        # （ieee-spectrum 最多，一条省了 7618 字）。同一个动作两条路径两套记账，
        # 下游就无法统一判断「这条摘要是不是完整的」。
        _sum_raw = _strip_html(summary)
        _sum_clipped = _clip(_sum_raw)
        out.append({
            "title": _strip_html(title),
            "url": _entry_link(item, link_prefer),
            "published_raw": published,
            "published": (_parse_date(published).isoformat() if _parse_date(published) else None),
            "summary": _sum_clipped,
            "summary_truncated": _is_clipped(_sum_raw),
            "authors": [a for a in authors if a][:4],
            "source_label": label,
        })
    return out


def fetch_rss(url: str, label: str, ua: str | None = None,
              link_prefer: str = "alternate", timeout: int | None = None) -> list[dict]:
    payload = _get(url, retries=2, ua=ua, timeout=timeout)
    head = payload[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or b"<html" in head:
        raise ValueError("returned HTML, not a feed (bot wall / consent page)")
    return parse_feed(payload, label, link_prefer)


def fetch_atom_arxiv(spec: dict, n: int) -> list[dict]:
    out: list[dict] = []
    # ★ 必须把 spec 上的 per-source timeout 传下去。
    # 实测 bug：这里曾经是 `_get(url, retries=2)`，不传 timeout → 回落全局 25s。
    # 而 `set_source_timeouts()` 特意给 arxiv-ai 设了 40s（因为它在 4 个分类下
    # 耗时 ≈ 9.4 + 3×3 = 18.4s，25s 余量太小会截断合法分类数据）。
    # 结果那个 40s 只作用在 arxiv-rss-ai（kind=="rss"）上 —— 而周末它是空 feed。
    # 被论证要放宽的那个源，从来没吃到过这个值。
    tmo = spec.get("timeout")
    for index, cat in enumerate(spec.get("cats", [])):
        if index:
            time.sleep(ARXIV_GAP)          # arXiv 429s on back-to-back calls
        url = spec["url"].format(cat=cat, n=n)
        for entry in parse_feed(_get(url, retries=2, timeout=tmo), f"{spec['label']} · {cat}"):
            entry["category"] = cat
            out.append(entry)
    return out


def _is_major_source(entry: dict) -> bool:
    """这条能不能享受「本周重大事件」宽窗口。

    判据刻意收窄到【领域级事件通常的发布形态】：
      ① `major_event: true` —— 由 `--extra-json` 显式标注（调用方最清楚
         哪条是诺奖这种级别，脚本不该猜）；
      ② 证据类型是 `peer-reviewed` / `official` —— 期刊与官方一手发布。

    **为什么聚合器不享受宽窗口**：实测「机构稿 09-28 → ScienceDaily 10-09
    才转发」正是这个问题的来源。让聚合器也拿宽窗口，等于奖励慢转载，
    还让「近 3 天」这个承诺悄悄失效。宽窗口只给**真正在该日发布的人**。

    ⚠️ 必须在这里**现算** evidence_type，不能读 `entry["evidence_type"]` ——
    那个字段在打分循环里才赋值，而窗口过滤发生在它**之前**。
    实测：读字段的版本让 `--major-days 7` 一条都放不进来
    （查询确实多抓回 7 条，但过滤器读到的全是 None）。
    `evidence_type_for` 是纯函数、幂等，算两次没有副作用。
    """
    if entry.get("major_event"):
        return True
    ev = entry.get("evidence_type")
    if not ev:
        key = entry.get("source_key", "")
        ev = evidence_type_for(key, SOURCES.get(key, {}).get("class", ""), entry)
    return ev in HARD_EVIDENCE_TYPES | {"official"}


def fetch_rss_multi(urls: list[str], label: str, ua: str | None = None,
                    link_prefer: str = "alternate",
                    timeout: int | None = None) -> tuple[list[dict], list[str]]:
    """Fetch several DIFFERENT feeds of one outlet, concurrently, and merge them.

    与 `_mirror_fetch` 的区别（这两个不能混用）：
      `_mirror_fetch` —— 同一内容的多个**镜像**，取**第一个非空的**就停（竞速）
      `fetch_rss_multi` —— 同一家的多个**不同版面**，**全部都要**（合并）

    实测：中新网的「滚动 / 社会 / 国内 / 财经」是四个不同版面，
    用 `urls` 只会拿到其中一个版面，另外三个的内容直接丢掉。

    并发而非串行：实测中新网单 feed 0.5–6.4s（`scroll-news` 那次 6.38s），
    串行 4 个最坏要 20s+；并发后取决于最慢的那个。
    返回 (条目, 失败的 feed 列表) —— 失败必须能被调用方看见，
    否则「4 个版面只成功 1 个」会静默地看起来像「这家今天内容少」。
    """
    out: list[dict] = []
    failed: list[str] = []
    pool = ThreadPoolExecutor(max_workers=max(1, len(urls)))
    try:
        futs = {pool.submit(fetch_rss, u, label, ua, link_prefer, timeout): u
                for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                got = fut.result()
                out.extend(got)
            except Exception as exc:                       # noqa: BLE001
                failed.append(f"{u} → {type(exc).__name__}: {str(exc)[:70]}")
    finally:
        # ★ 不能写成 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 会 join
        #   全部线程，拿到结果后仍要等慢/挂掉的那些跑完。
        #   实测（`_mirror_fetch` 那次）：拿到 51 条结果却仍耗 18.90s。
        pool.shutdown(wait=False, cancel_futures=True)
    # 同一版面可能被多个 feed 重复收录，按 URL 去重
    seen, uniq = set(), []
    for e in out:
        k = e.get("url") or e.get("title")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    # ★ 必须按时间【降序】排序后再交给下游的单源限流。
    #
    # 实测 bug（子代理用真实数据抓到）：`as_completed` 按**完成顺序**合并结果，
    # 于是先回来的那个版面独占 `--max-per-source` 的名额。
    # `chinanews` 4 个版面窗内共 **120 条**，实际只送出 **15 条，且全部来自
    # 财经版面 `/cj/`** —— 社会 `/sh/`、国内 `/gn/`、滚动版面**一条都没被送出来**。
    # 这不只是数量问题：**agent 看到的世界被"哪个 feed 先返回"决定了**，
    # 而它对每次网络抖动的结果都不一样、无法复现。
    #
    # 限流本身是对的（成本控制），错的是**限流的对象**：应该限"给多少条"，
    # 不该限"给哪个版面"。按时间降序之后，cap 拿到的是全版面里最新的一批。
    # 与 `ARCHIVE_SOURCES` 的增量截断用的是同一个办法。
    uniq.sort(key=lambda e: e.get("published") or "", reverse=True)
    return uniq, failed


def _mirror_fetch(spec: dict, label: str) -> list[dict]:
    """Fetch a mirrored source by RACING the mirrors concurrently.

    为什么必须并行：实测 `baidu-hot`（已于 2026-10-10 下架）配了 4 个镜像，
    其中 3 个已死
    （1 个 403、2 个 DNS 失败），而串行 for 循环的**最坏情况是
    4 × (3 次尝试 × TIMEOUT + 退避 2+4) = 4 × 81 = 324 秒**。
    实测确实出现过一次 94 秒的全挂（另一代理记录）。一个热度榜源不该
    主导整条管线的墙钟。

    改成并发后：墙钟 ≈ 最慢的那个镜像，而不是所有镜像之和。
    第一个返回非空结果的胜出；全部失败才报错（错误信息保留每个镜像的原因）。

    `spec["mirror_timeout"]` 控制单个镜像的超时（默认 10s）——
    热度榜是「有就有、没有就算了」的校准信号，不值得为它等 25 秒。

    ⚠️ **当前没有调用者**（唯一的 `baidu-hot` 于 2026-10-10 下架），
    但**故意保留**：
      ① 它是通用能力 —— 再登记任何带 `urls` 的镜像源立刻就能用；
      ② 上面那三条实测记录（324s 最坏 / 94s 全挂 / `with` 会 join 全部线程）
         是踩出来的，删了函数就得连教训一起重写一遍。
    `--list` 的源计数不会显示它，因为它是机制不是源。
    """
    urls = spec.get("urls") or [spec["url"]]
    per = spec.get("mirror_timeout", 10)
    box: dict = {}

    def _try(url: str) -> None:
        try:
            entries = fetch_rss(url, label, spec.get("ua"), "alternate", per)
            if not entries:
                box.setdefault("errors", []).append(f"{url}: empty")
                return
            for entry in entries:
                entry["mirror"] = url
            box["hit"] = entries
        except Exception as exc:  # noqa: BLE001 - 别的镜像可能成功
            box.setdefault("errors", []).append(f"{url}: {str(exc)[:60]}")

    # ★ 不能用 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 会 join 所有线程，
    # 于是即便第一个镜像早就成功了，仍要等失败的镜像把重试跑完。实测：
    # 拿到 51 条结果却仍耗 18.90s，等待的正是那三个已死镜像。
    # 手动管理 executor 并在拿到结果后立即返回，剩下的线程让它自生自灭
    # （进程很快结束，不值得为它们阻塞）。
    pool = ThreadPoolExecutor(max_workers=len(urls))
    try:
        pending = {pool.submit(_try, u) for u in urls}
        while pending and not box.get("hit"):
            wait(pending, return_when=FIRST_COMPLETED)
            pending = {f for f in pending if not f.done()}
    finally:
        # cancel_futures 让还没开跑的镜像直接取消；已在跑的无法中断，
        # 但不再 join 它们 —— 这正是省下时间的地方。
        pool.shutdown(wait=False, cancel_futures=True)
    if box.get("hit"):
        return box["hit"]
    raise ValueError("all mirrors failed -> " + " | ".join(box.get("errors") or []))


def fetch_google_trends(spec: dict, n: int) -> list[dict]:
    """Google Trends daily RSS.

    The top-level <link> is the feed itself, which is useless. The real payoff is
    nested <ht:news_item> children (title + url + source) plus
    <ht:approx_traffic>. A generic RSS reader throws all of that away, so this
    lifts it out: you get the search term AND the article that spiked it.
    """
    payload = _get(spec["url"], retries=1)
    root = ET.fromstring(payload)
    out = []
    for rank, item in enumerate((e for e in root.iter() if _tag(e) == "item"), 1):
        if len(out) >= n:
            break
        term = _strip_html(_text(item, "title") or "")
        if not term:
            continue
        traffic = None
        news = []
        for child in item:
            tag = _tag(child)
            if tag == "approx_traffic" and child.text:
                traffic = child.text.strip()
            elif tag == "news_item":
                news.append({
                    "title": _strip_html(_text(child, "news_item_title") or ""),
                    "url": _text(child, "news_item_url"),
                    "source": _strip_html(_text(child, "news_item_source") or ""),
                })
        top = next((x for x in news if x["url"]), None)
        out.append({
            "title": term,
            "url": (top or {}).get("url") or spec["url"],
            "published_raw": None,
            "published": None,
            "summary": (f"搜索量 {traffic or '?'}｜相关报道："
                        + (top or {}).get("title", "")[:160]),
            "authors": [],
            "source_label": spec["label"],
            "trend_rank": rank,
            "trend_heat": traffic,
            "related_news": news[:3],
        })
    return out


def fetch_toutiao_hot(spec: dict, n: int) -> list[dict]:
    """Official 头条热榜 JSON: real popularity ranking with a heat score."""
    data = json.loads(_get(spec["url"], retries=1).decode("utf-8", "replace"))
    rows = data.get("data") or []
    out = []
    for rank, row in enumerate(rows, 1):
        title = _strip_html(row.get("Title") or "")
        if not title:
            continue
        out.append({
            "title": title,
            "url": row.get("Url") or row.get("url"),
            "published_raw": None,
            "published": None,
            "summary": f"热度值 {row.get('HotValue', '?')}｜榜位 {rank}",
            "authors": [],
            "source_label": spec["label"],
            "trend_rank": rank,
            "trend_heat": row.get("HotValue"),
        })
        if len(out) >= n:
            break
    return out


_CLIP_HEAD = 1400
_CLIP_TAIL = 500
_CLIP_LIMIT = _CLIP_HEAD + _CLIP_TAIL          # 1900：超过它 _clip 才会动手


def _clip(text: str, head: int = _CLIP_HEAD, tail: int = _CLIP_TAIL) -> str:
    """Trim a long abstract WITHOUT discarding its conclusion.

    Measured loss from a plain [:900] slice: 43 of 162 summaries were cut exactly
    at the cap, 42 of them mid-word, and the dropped portion is usually the
    structured abstract's conclusion and effect sizes. Worst case seen: a Nature
    Medicine abstract lost its entire closing finding ("documentation, dependency
    specification and executable structure") and the subagent had to re-query
    Crossref to recover it.

    So: keep the head, keep the tail, and mark the omission explicitly.
    """
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]} …[省略 {omitted} 字]… {text[-tail:]}"


def _is_clipped(text: str) -> bool:
    """Was this text long enough that _clip() replaced part of it?

    ★ 判据必须是【内容长度 > _CLIP_LIMIT】，不能是 `len(raw) > len(clipped)`。
    实测 off-by-N：`_clip` 插入的 `…[省略 N 字]…` 标记【让结果变长】，
    所以 raw ∈ [1901, 1913] 时 clipped 反而比 raw 长 ——
    `len(raw) > len(clipped)` 为 False，被截过的条目被报成「没截断」。
    而 Crossref 路径当时用的是 `len(abstract) > 1900`（正确），
    RSS 路径用的是长度比较（错误）—— **同一个动作两条路径两套判据**，
    于是 full JSON 说「没截断」、compact 说「截断了」，两者对同一条给出相反答案。
    """
    return len(text or "") > _CLIP_LIMIT


def fetch_crossref(spec: dict, n: int, since: datetime) -> list[dict]:
    """Fetch a journal's recent works from Crossref.

    Sorting/filtering on `published` (the issue publication date) is WRONG for a
    "last few days" window: big journals assign an issue date months after the
    paper actually appeared online, so a 3-day window silently returned nothing.
    Measured on Nature/Science: `sort=published` with rows>=20 yielded only ~1-6
    in-window items; `sort=published-online` + `from-online-pub-date` returns them
    correctly. Always prefer the online date, and say so in the output.
    """
    url = (f"https://api.crossref.org/journals/{spec['issn']}/works"
           f"?rows={max(n, 20)}&sort=published-online&order=desc"
           f"&filter=from-online-pub-date:{since.date().isoformat()}"
           # Crossref's select whitelist: `subtype` and `reference-count` are NOT
           # selectable (both made every journal answer HTTP 400 -- measured),
           # while `is-referenced-by-count` is. Use that as the citation signal.
           f"&select=DOI,title,abstract,published,published-online,URL,author,type,"
           f"update-to,is-referenced-by-count,container-title")
    data = json.loads(_get(url, retries=2).decode("utf-8", "replace"))
    out = []
    for item in data.get("message", {}).get("items", []):
        # prefer the online date; fall back to the issue date
        for field in ("published-online", "published"):
            parts = (item.get(field) or {}).get("date-parts") or [[]]
            dt = None
            if parts and parts[0]:
                try:
                    dt = datetime(*[int(x) for x in parts[0]], tzinfo=timezone.utc)
                except Exception:
                    dt = None
            if dt is not None:
                break
        authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in (item.get("author") or [])]
        # Crossref exposes retraction / correction notices on the WORK ITSELF via
        # `update-to`, sourced from retraction-watch and publishers. Measured:
        # this is how the arsenic-life paper's retraction is discoverable.
        notices = []
        for upd in (item.get("update-to") or []):
            label = (upd.get("label") or upd.get("type") or "").strip()
            if label:
                notices.append({"label": label, "source": upd.get("source"),
                                "doi": upd.get("DOI")})
        abstract = _strip_html(item.get("abstract") or "")
        # Carry the signals that separate research papers from front-matter. The
        # DOI alone is often enough, but reference-count and a missing abstract
        # corroborate it (measured against 10.1038/d41586-026-03217-6).
        out.append({
            "title": _strip_html((item.get("title") or [""])[0]),
            "url": item.get("URL"),
            "published_raw": dt.date().isoformat() if dt else None,
            "published": dt.isoformat() if dt else None,
            "summary": _clip(abstract),
            "summary_truncated": _is_clipped(abstract),
            "authors": [a for a in authors if a][:4],
            "doi": item.get("DOI"),
            "type": item.get("type"),
            "container_title": (item.get("container-title") or [None])[0],
            "is_referenced_by_count": item.get("is-referenced-by-count"),
            "abstract_missing": not abstract,
            "update_notices": notices,
            "source_label": spec["label"],
        })
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
# Distinct-source identity. Two registry keys that front the SAME origin are one
# source, not two. Getting this wrong is the most dangerous scoring bug possible:
# it manufactures fake corroboration and lets a low-trust community mirror
# outrank a peer-reviewed journal. Measured: hn-frontpage + hnrss (same site)
# scored every HN item as "2 independent sources".
SOURCE_IDENTITY = {
    "hn-frontpage": "hackernews",
    "hn-top": "hackernews",
    # Both front the same preprint server; an arXiv paper appearing in the API
    # feed AND the daily RSS is one source, not two.
    "arxiv-ai": "arxiv",
    "arxiv-rss-ai": "arxiv",
}
# Sources whose content is a re-syndication of another source's reporting.
SYNDICATION_PARENT = {
    "science-daily": "press-release",
    "phys-org": "press-release",
}


def identity_of(source_key: str, label: str = "") -> str:
    """Resolve a registry key OR a free-text outlet name to ONE identity.

    ★ 这个函数回答的是「**是不是同一家媒体**」，不是「**属于哪一档权重**」。
    两者必须分开，因为一个权重档里装着很多家不同的媒体。

    Measured failures, in BOTH directions -- the same alias table was doing both
    jobs and got each one wrong in turn:

      假阳性（同一家算多家）: an external entry whose `also_seen_in` named its OWN
        source counted itself as an independent reprint, taking pickups 1 -> 2 and
        comm 0.35 -> 0.60, exactly across the gate.

      假阴性（多家算一家）: 修上面那个时改成返回 `platform_of()` 的结果，而那是
        **权重桶**。实测 16 组独立媒体对了 11 组：新京报 + 南方都市报 → 都是
        `bjnews`；广州日报 + 北京日报 → `bjnews`；界面新闻 + 财新 → `caixin`；
        新华社 + 中新社 → `xinhua`；民政部 + 财政部 → `gov-cn`（30 个部委全塌成
        一个）。后果相反但同样致命：**真实的多源印证被吞掉**，一条真的被多家
        报道的新闻 pickups 永远等于 1，comm 卡在 0.35 进不了核心区。

    修法：身份 = 归一化后的**媒体名**，只有 OUTLET_ALIAS 里明确列出的
    「确实是同一家的不同写法」才合并。权重桶只用于查 PLATFORM_WEIGHT。
    """
    if source_key:
        mapped = SOURCE_IDENTITY.get(source_key) or SYNDICATION_PARENT.get(source_key)
        if mapped:
            return mapped
        if source_key in SOURCES:
            return source_key
    name = (label or source_key or "").strip()
    if not name:
        return ""
    low = name.lower()
    # ★ 两轮匹配：**先精确，再子串**。
    # 实测 bug：只做子串匹配时，短别名会把长名字抢先匹配掉 ——
    # `OUTLET_ALIAS` 里既有 `上游新闻`（裸形式）又有 `上游新闻客户端`（变体），
    # 而 `上游新闻` 是 `上游新闻客户端` 的前缀，谁先谁后完全由表内顺序决定。
    # 精确轮保证「名字就是表里那一项」时用最贴切的那条；
    # 子串轮留给「名字里包含某个别名」的情形（如 `光明日报社`）。
    for alias, canon in OUTLET_ALIAS:
        if low == alias:
            return canon
    for alias, canon in OUTLET_ALIAS:
        if alias in low:
            return canon
    # 别名表没命中 → 通用归一去载体后缀。
    # ★ 已登记的品牌（如 `上游新闻`）走的是上面那条：它们的所有写法
    #   （`上游新闻客户端` / `上游新闻网` / `上游新闻新闻客户端`）都在
    #   `_OUTLET_VARIANTS` 里显式登记，所以 100% 收敛。
    #   通用规则只负责没登记的名字，因此它可以保持"激进但可预测"——
    #   这也正是这个 bug 的教训：**别把品牌知识塞进通用后缀规则**，
    #   两者的边界一模糊就会互相打架（前五版都栽在这里）。
    return normalize_identity(name)


def _register_outlet_variants() -> None:
    """把 `OUTLET_ALIAS` 里每个品牌的常见写法都登记上，形如 `X → X`。

    为什么必须显式登记（而不是靠通用后缀剥离）：
    `上游新闻` 与 `上游新闻客户端` 的差异在**品牌名内部**（「新闻」是品牌的一部分），
    任何"剥载体重后缀"的通用规则都会在某一侧多剥或少剥：
      多剥 → `上游新闻客户端` → `上游`，与裸形式 `上游新闻` 不收敛
      少剥 → `新京报网` → `新京报网`，与裸形式 `新京报` 不收敛
    实测通用规则在这组矩阵上最多只能做到 **9/15 收敛**（穷举 7 种候选规则验证过）。
    显式登记是唯一能做到 15/15 且**意图可读**的办法 —— 表里写的是什么就是什么。
    """
    seen: set[str] = set()
    for alias, canon in list(OUTLET_ALIAS):
        base = alias.split("：")[0].split(":")[0].strip()
        for form in (base + "客户端", base + "网", base + "新闻客户端"):
            if form and form not in seen:
                seen.add(form)
                OUTLET_ALIAS.append((form, canon))


# 真正的「同一家的不同写法」。**只有确实同属一个编辑部/通讯社的才进来。**
# 判据：读者会认为这两条是「同一个来源」时才算合并。
# 反例（明确不合并，虽然它们共用权重档）：新京报 ≠ 南方都市报、
# 界面新闻 ≠ 财新、民政部 ≠ 财政部 —— 同档不等于同一家。
#
# 类型是 `list`（不是 tuple）：`_register_outlet_variants()` 会往里追加
# `X客户端 / X网 / X新闻客户端` 三种变体。调用必须在**这个定义之后**。
OUTLET_ALIAS: list[tuple[str, str]] = [
    # 央视系统：一个台
    ("中央电视台", "cctv"), ("央视新闻", "cctv"), ("央视网", "cctv"),
    ("央视", "cctv"), ("cctv", "cctv"), ("新闻联播", "cctv"),
    # 新华社系统：一个通讯社
    ("新华社", "xinhua"), ("新华网", "xinhua"), ("新华每日电讯", "xinhua"),
    # 中新社系统：**另一家**通讯社。实测教训：第一版把中新社/中国新闻网/中新网
    # 也映射到 `xinhua`，于是「新华社」和「中新社」被判同一家 —— 而这恰恰是
    # 本函数 docstring 自己举的原始反例，即【过合并原封未动】。
    # 更糟的是当时的测试断言写的是 `中新社 == 中国新闻网 → 'xinhua'`，
    # 把错误固化成期望值，两个方向的断言都抓不到它。
    # 中新社与新华社是两家独立通讯社，必须分开。
    ("中新社", "chinanews"), ("中国新闻网", "chinanews"), ("中新网", "chinanews"),
    # 人民日报社：一张报纸及其网站
    ("人民日报", "people"), ("人民网", "people"),
    # 央广/国际台
    ("中央广播电视总台", "cctv"), ("央广网", "cnr"), ("央广", "cnr"),
    # 其它单点别名（同一品牌的写法差异）
    ("澎湃新闻", "thepaper"), ("澎湃", "thepaper"),
    ("光明网", "guangming"), ("光明日报", "guangming"), ("光明", "guangming"),
    ("中国日报", "chinadaily"),
    ("第一财经", "yicai"), ("证券时报", "stcn"),
    ("it之家", "ithome"), ("ithome", "ithome"),
    ("36氪", "36kr"), ("36kr", "36kr"),
    # ── 品牌名【本身】必须登记（映射到自己）。
    # ★ 这是本轮最关键的教训：曾经只登记 `X网` 变体而漏了裸品牌名，于是
    #     `极目新闻`      → 别名未命中 → 通用剥离 → `极目新闻`
    #     `极目新闻客户端` → 别名未命中 → 通用剥离 → **`极目`**
    #       （一次性正则 `^(.+?)(?:新闻客户端|客户端|网)$` 的非贪婪 `.+?`
    #        会把「新闻」吃进 `客户端` 分支，一步剥掉「新闻客户端」5 个字）
    #   → 判成两家 → pickups=2、comm=0.60 → **单一媒体伪造进核心区**。
    # 登记裸名后两条都走【精确匹配】，各自都落在 `极目新闻`，收敛。
    #
    # ★ 映射目标是【自己的身份】，不是权重桶 ——
    #   新京报与南方都市报共用 `bjnews` 权重档，但它们是两家。
    ("上游新闻", "上游新闻"), ("极目新闻", "极目新闻"), ("封面新闻", "封面新闻"),
    ("界面新闻", "界面新闻"), ("红星新闻", "红星新闻"),
    ("每日经济新闻", "每日经济新闻"), ("新京报", "新京报"),
    ("南方都市报", "南方都市报"), ("财新", "财新"),
    ("广州日报", "广州日报"), ("浙江日报", "浙江日报"), ("北京日报", "北京日报"),
    ("中国青年报", "中国青年报"), ("南方周末", "南方周末"),
    ("经济观察报", "经济观察报"), ("21世纪经济报道", "21世纪经济报道"),
    ("观察者网", "观察者网"), ("环球时报", "环球时报"),
    ("半月谈", "半月谈"), ("健康中国", "健康中国"),
    # ── 机构网站的 `X网` 变体（裸名在上，变体交给自动注册补齐；这两条是
    #    名字本身不以品牌结尾的特例，必须手写）
    ("南方周末网", "南方周末"), ("经济观察网", "经济观察报"),
    ("21世纪经济报道网", "21世纪经济报道"), ("每日经济新闻网", "每日经济新闻"),
]

# 必须在 OUTLET_ALIAS 定义【之后】调用（初版放在定义前 → NameError）。
_register_outlet_variants()


def normalize_identity(text: str) -> str:
    """Strip CARRIER suffixes so two spellings of one outlet compare equal.

    ★ 只剥**纯载体**后缀：`客户端 / 官网 / 官方网站 / 网站`。
    `新闻网` 和单字 `网` **都不剥** —— 这是踩过两次的坑：

      坑一（原始 bug，5 个版本复现，E2E 能伪造进核心区）：
        后缀表里 `新闻客户端` 剥掉 5 个字，`上游新闻客户端` → `上游`，
        而裸形式是 `上游新闻` → 判成两家 → pickups=2、comm=0.60
        → **单一媒体伪造进核心区**。13 组里 7 组中招。
        当时 docstring 明写「「新闻」不在剥离列表里」—— **注释与代码直接矛盾**。

      坑二（修坑一时引入）：为了收敛，改成反复剥到不动、并把 `网` 也剥掉。
        但 `上游新闻` 结尾的 `新闻` 里的 `闻` 不是后缀，而 `新闻网` 剥完剩
        `上游新闻`、再剥单字 `网` 又变成 `上游` —— **两条路径仍然不收敛**，
        而且 `新京报网` → `新京报` 对、`上游新闻网` → `上游` 错，规则自相矛盾。

    最终做法：`网` 一律不动，`X报网` / `X新闻客户端` 这类**品牌相关的变体**
    交给 `OUTLET_ALIAS` 显式登记 —— 别名表就是干这个的，
    而通用后缀剥离只处理与之无关的纯载体词。
    """
    out = re.sub(r"[\s·・\-_（）()【】\[\]]+", "", (text or "").lower())
    # 三个约束，缺一个就有真实品牌被判成两家（每一版都在这里翻车，全记下来）：
    #
    #   ① 必须【反复试到不动】。`新京报客户端` 一轮只剥一次：
    #      先试 `新闻客户端`（不匹配）→ 再试 `客户端` → `新京报` ✓
    #      但 `新京报新闻客户端` 先剥 `客户端` → `新京报新闻`，
    #      必须回头再试一次才能收敛。单趟 for 循环不够。
    #
    #   ② `新闻客户端` 必须排在 `客户端` 前面。否则 `上游新闻客户端`
    #      先剥 `客户端` → `上游新闻`（恰好正确，但纯属运气）；
    #      换成 `上游新闻新闻客户端` 就会剩下 `上游新闻新闻`。
    #
    #   ③ 混用「新闻」的复合后缀（如 `新闻网`）会与品牌名里的「新闻」打架：
    #      `上游新闻网` 剥 `新闻网` → `上游`，与裸形式 `上游新闻` 不收敛。
    #      所以后缀表里**只有 `新闻客户端`** 含「新闻」，它是无歧义的
    #      （没有品牌叫 `X新闻新闻客户端`）。
    #
    # 不再剥 `官网/官方网站/网站`：它们不影响任何实测用例，
    # 而每多一条规则就多一次上面这类互相打架的机会。需要时用 OUTLET_ALIAS 登记。
    out = re.sub(r"[\s·・\-_（）()【】\[\]]+", "", (text or "").lower())
    # ── 规则（前五版都在这里翻车，把推导钉死，别再靠猜）──────────────────
    # 目标：X = X客户端 = X网 = X新闻客户端，且 X新闻 = X新闻客户端
    #       收敛（绝不能把品牌名里的「新闻」剥掉）。
    #
    # ★ 核心：**只剥一个后缀就停**，用一次性正则，不用循环。
    #   循环是罪魁 —— 它一旦启动就一路剥穿品牌名：
    #     `上游新闻客户端` → 剥 `客户端` → `上游新闻` → 循环再剥 → `上游` ✗
    #   而裸形式 `上游新闻` 没有可剥的载体重后缀，停在 `上游新闻` → 判成两家。
    #   实测 6 组失败全部出在这个循环上。
    #
    # ★ 后缀按**长度降序**排列，正则交替分支**从左到右**匹配：
    #     `新闻客户端`(5) 先于 `客户端`(3) 先于 `网`(1)
    #   于是 `新京报新闻客户端` 剥 5 字 → `新京报`（一轮到位，不需要循环）。
    #
    # ★ 后缀表里**不能有 `新闻网`**：`上游新闻网` 剥 `新闻网` → `上游`，
    #   与裸形式 `上游新闻` 不收敛。单字 `网` 才是对的（→ `上游新闻`）。
    #
    # 不再剥 `官网/官方网站/网站`：不影响任何实测用例，
    # 而每多一条规则就多一次互相打架的机会。需要时用 OUTLET_ALIAS 登记。
    # 守卫 `.+` 保证剥完至少剩 1 个字符（`网` 单独出现时不会被剥成空串）。
    out = re.sub(r"^(.+?)(?:新闻客户端|客户端|网)$", r"\1", out)
    return out


# Nature's news/front-matter DOI prefix. Crossref registers these as ordinary
# `journal-article` with reference-count 0, so nothing upstream separates them
# from research papers. Measured: 12 of them were scored `peer-reviewed` at 0.912
# -- the joint-highest score in the whole run, tied with real Science papers --
# while being books-in-brief columns, photo essays and briefing chats.
RE_NEWS_DOI = re.compile(r"^10\.\d{4,}/d41586-", re.I)

# 期刊栏目标题的形态。实测漏判来源：crossref-nejm 本期 6 条**全部**是
# Perspective / 病例影像 / 随笔，DOI 不是 d41586-，所以只靠 DOI 前缀抓不到。
# 这些形态词在**栏目标题**里高频、在**研究论文标题**里几乎不出现。
RE_FRONT_TITLE = re.compile(
    r"(?i)\b(perspective|editorial|commentary|correspondence|viewpoint|"
    r"images? in clinical medicine|clinical implications|letters? to the editor|"
    r"case report|in focus|book review|obituary|in memoriam|"
    r"a missed opportunity|"
    r"flushing in|not available in your country)\b")


def evidence_type_for(source_key: str, cls: str, entry: dict | None = None) -> str:
    """Infer the evidence tier from the source itself (no guessing by the model).

    For Crossref sources the DOI is inspected, because the registry key alone
    cannot tell a research paper from front-matter. `d41586-*` is Nature's
    news/books/picture desk; `reference_count == 0` corroborates it.
    """
    if source_key in PRIMARY_SOURCES:
        if source_key.startswith("crossref-"):
            if entry:
                doi = (entry.get("doi") or "").strip()
                if doi and RE_NEWS_DOI.match(doi):
                    entry["front_matter"] = True
                    entry["front_matter_reason"] = "DOI 前缀 d41586-（Nature 新闻/书评栏目）"
                    return "journalism"
                # ★ 栏目判定必须看【内容形态】，不能看"有没有摘要"。
                #
                # 这段历史值得留着，因为两版都在同一处翻过车：
                #
                #   v1（错）：`无摘要 AND 引用数==0 → journalism`。
                #     误伤真研究论文 —— Nature Comput Sci 的 -01070-1/-01071-0/-01064-z
                #     被判 journalism 0.674，而同刊同日带摘要的 -01063-0 拿 0.896。
                #     根因：**Crossref 的摘要覆盖率是不一致的**，"没有摘要"不等于
                #     "不是论文"。整条规则已删除。
                #
                #   v2（本版）：只看 d41586- 前缀。结果反向漏判 ——
                #     实测 `crossref-nature` 本期 15 条**全部**是新闻/书评/图片栏目
                #     （DOI 确实是 d41586-，这条抓到了），但
                #     `crossref-nejm` 6 条**全部**是 Perspective / 病例影像 / 随笔
                #     （DOI 不是 d41586-，**全部漏判成 peer-reviewed**），
                #     `crossref-natcomp` 4 条里 3 条是评论/Highlight，同样漏判。
                #
                # 本版的判据只用**栏目形态**，不用"引用数"：
                #   ① d41586- 前缀（Nature 前栏目，可靠）
                #   ② 标题形态：无作者、单作者、或标题本身是栏目标题
                #   ③ 无摘要 **且** 作者数 ≤1（单作者 + 无摘要 = 评论/随笔的典型形态；
                #      真研究论文极少单作者且无摘要）
                # 三条都是"形态"证据，与摘要覆盖率无关，所以不会重演 v1 的误伤。
                if entry:
                    au = entry.get("authors") or []
                    title = (entry.get("title") or "").strip()
                    no_abs = bool(entry.get("abstract_missing"))
                    refs = entry.get("is_referenced_by_count")
                    reason = None
                    if "not available in your country" in title.lower():
                        reason = "标题为占位/栏目标题"
                    elif RE_FRONT_TITLE.search(title):
                        reason = "标题形态属栏目（随笔/影像/述评）"
                    elif no_abs and len(au) <= 1:
                        reason = f"无摘要且作者数≤1（{len(au)} 位）—— 评论/随笔形态"
                    # ★ 试过再加一条「无摘要 + 作者数≤4 + 零被引 → 述评形态」，
                    # **已删除：它误伤真研究论文。**
                    # 实测：PNAS《GTP orchestrates CTP synthase via an allosteric
                    # effector–cycle》（3 作者、Crossref 无摘要、被引数 0）被判成
                    # journalism —— 而它是一篇真的研究论文。
                    # 根因和 v1 一模一样：**新上线论文的被引数天然是 0**，
                    # 「零被引」不能区分「评论」和「刚发表」。
                    # 教训：这个位置任何基于「缺失信号」的规则都会误伤，
                    # 因为 Crossref 的元数据覆盖率是不一致的。
                    #
                    # 因此本函数只保留【形态证据】：DOI 前缀 / 标题形态 / 作者数。
                    # 已知漏判（保守代价，明说）：3 作者以上的述评/栏目仍会被标
                    # peer-reviewed，例如 10.1038/s43588-026-01070-1。
                    # 这类条目通常 body_missing → 真值封顶 0.50 → 落丢弃区，
                    # 实际危害有限；而**误伤真论文会把 0.9 分的核心证据打掉**，
                    # 代价大得多。两害相权，选漏判。
                    if reason:
                        entry["front_matter"] = True
                        entry["front_matter_reason"] = reason
                        return "journalism"
            return "peer-reviewed"
        if source_key == "openai-news":
            return "official"
        return "preprint"
    if cls == "trend":
        return "rumor"
    return "secondary"


# ---------------------------------------------------------------------------
# 问题2 修复：分类型需求下的关联度
#
# Measured failure: the request "给我社会和 AI 的信息" carries no specific intent
# keywords -- it names CATEGORIES. The relevance gate then scored everything at
# 1.0 (or 0.0) and could not tell an on-target run from a drifted one. A gate
# that silently passes everything is worse than no gate, because the report
# still claims it was checked.
#
# The fix is not better keyword extraction -- category requests genuinely have
# no keywords. It is to RECOGNISE the request type and measure the right thing:
#   keyword request    -> directional keyword coverage (unchanged)
#   classifier request -> DOMAIN SHARE: what fraction of the extracted items
#                         fall in each requested domain
# ---------------------------------------------------------------------------
def classify_request(request: str, explicit_domains: list[str] | None = None) -> dict:
    """Decide whether the request names CATEGORIES or TOPICS, and say why.

    Returns the verdict, a confidence in [0,1], and the matched tokens, so the
    classification can BE AUDITED. Measured need: a stage-1 misclassification
    propagates through every later stage and nothing downstream can detect it --
    stage 3 checks whether content is true, never whether the category was right.
    Recording the reason is the only way that mistake becomes visible.

    Rule change (was a measured bug): a single category hit is now ALWAYS a
    classifier request, whether or not --domain was passed. Before, `hits == 1`
    without --domain fell through to the keyword path, and Chinese requests then
    yielded one whole-clause "keyword" ('科学界有什么进展') that matched nothing,
    driving relevance to ~0 and firing a spurious second search.
    """
    text = (request or "").lower()
    if not text:
        return {"verdict": "keyword", "confidence": 0.0, "hits": 0,
                "matched": {}, "reason": "无 request 文本，默认按关键词型处理"}

    matched: dict[str, list[str]] = {}
    for dom, tokens in DOMAIN_TOKENS.items():
        got = [t for t in tokens if t in text]
        if got:
            matched[dom] = got
    hits = len(matched)

    if hits >= 2:
        conf, reason = 0.95, f"命中 {hits} 个类：{'/'.join(matched)}"
    elif hits == 1:
        dom = next(iter(matched))
        conf = 0.85 if explicit_domains else 0.70
        reason = (f"命中 1 个类「{dom}」（{','.join(matched[dom])}）"
                  + ("，且显式给了 --domain" if explicit_domains else
                     "，无 --domain；单类命中一律按分类型处理（否则中文整句会误触发二次搜寻）"))
    else:
        conf, reason = 0.30, "未命中任何类词，按关键词型处理"
    return {"verdict": "classifier" if hits >= 1 else "keyword",
            "confidence": conf, "hits": hits, "matched": matched, "reason": reason}


def is_classifier_request(request: str, explicit_domains: list[str] | None = None) -> bool:
    """Backwards-compatible boolean wrapper around classify_request()."""
    return classify_request(request, explicit_domains)["verdict"] == "classifier"


# 中文虚词：一句话里出现 2 个以上，基本可以断定是"整句"而不是"术语"。
# 用来把「给我社会和」「科学界有什么进展」这类从句子里剔出去。
CLAUSE_FUNCTION_CHARS = set("的了和与及或在是有为对从把被给这那什么怎么如何为什么"
                            "吗呢吧啊呀哦嗯个一二三四五六七八九十")

# 单字领域术语：单字中文关键词通常是噪声，但这些是真实可检索的术语。
# 不处理它，用户写「汞超标」里的「汞」就不算有效关键词。
SINGLE_CHAR_TERMS = {
    "汞", "砷", "铅", "镉", "铬", "镍", "锌", "铜", "硒", "碘", "氟", "氯",
    "癌", "癌", "糖", "盐", "油", "米", "茶", "酒", "药", "煤", "钢", "铝",
}


def effective_keyword_count(keywords: list[str]) -> int:
    """How many keywords can actually carry the relevance check.

    Measured problem: Chinese has no spaces, so extraction yields WHOLE CLAUSES
    ('科学界这几天有什么进展'), which can never match an article. Counting those as
    real keywords makes the gate fire a second search on every Chinese request.

    Two distinct failure shapes to reject, both measured:
      - a whole CLAUSE that happens to be 2-8 chars ('给我社会和' is 5 chars)
      - a clause containing non-CJK content
    and one to ACCEPT that a naive length rule misses:
      - a genuine single-character term ('汞' in '汞超标')
    """
    n = 0
    for k in keywords or []:
        s = (k or "").strip()
        if not s:
            continue
        cjk = re.findall(r"[\u4e00-\u9fff]", s)
        if cjk:
            # only pure-CJK strings can be Chinese terms
            if len(cjk) != len(s):
                continue
            if len(s) == 1:
                if s in SINGLE_CHAR_TERMS:
                    n += 1
                continue
            if not (2 <= len(s) <= 8):
                continue
            # Two independent clause signals, both measured:
            #   a) 2+ function characters anywhere  -> '给我社会和' (给/和)
            #   b) a function character at the START -> '的信息' (only one function
            #      char, so (a) misses it, but a real term does not begin with 的)
            if s[0] in CLAUSE_FUNCTION_CHARS:
                continue
            if sum(1 for ch in s if ch in CLAUSE_FUNCTION_CHARS) >= 2:
                continue
            n += 1
        else:
            # ASCII / mixed: length by letters, and generic words are not keywords
            letters = re.sub(r"[^A-Za-z]", "", s)
            if 2 <= len(letters) <= 30 and letters.lower() not in STOPWORD_TOKENS:
                n += 1
    return n


def domain_of_source(source_key: str) -> set:
    return set(SOURCES.get(source_key, {}).get("domains", []))


def domain_share(entries: list[dict], wanted_domains: set) -> dict:
    """Fraction of extracted items that belong to the requested domain(s).

    This is the classifier-request analogue of keyword coverage: it answers
    "did the run stay inside the categories I asked for", which is the only
    meaningful notion of relevance when the request names categories.
    """
    if not entries:
        return {"coverage": 0.0, "on_domain": 0, "off_domain": 0, "note": "无条目"}
    on, off = 0, []
    for e in entries:
        src_dom = domain_of_source(e.get("source_key", ""))
        if src_dom & wanted_domains:
            on += 1
        else:
            off.append(e.get("title", "")[:60])
    return {
        "coverage": round(on / len(entries), 3),
        "on_domain": on,
        "off_domain": len(off),
        "off_domain_examples": off[:5],
    }


def platform_of(source_key: str, label: str = "") -> str:
    """Map a registry key, or a free-text outlet name, to a platform weight key.

    Both arguments are searched, because a supplemental-search item has no
    registry key and carries its outlet only in `source_label` -- and conversely
    a registry item has a key but a generic label. Checking only one of them was
    a measured bug: a 36氪 story resolved to 'unknown' (0.40) and failed the
    truth gate even though 36kr carries 0.48.
    """
    key_s = (source_key or "").strip()
    text = (label or "").strip()

    # exact key first
    if key_s in PLATFORM_WEIGHT:
        return key_s

    # alias table: Chinese outlet names and lower-case key spellings. Checked
    # before the substring-key scan so "36氪" and "IT之家" resolve correctly.
    #
    # Government bodies are listed FIRST and checked structurally as well (below),
    # because missing them inverted the ranking: measured, a 市场监管总局 official
    # notice resolved to `unknown` (0.40) -> truth 0.628, while 光明网 merely
    # REPRINTING it scored 0.772. The primary source ranked below its own
    # syndication -- the exact opposite of what the model is for.
    alias_map = (
        ("市场监督管理总局", "gov-cn"), ("市场监管总局", "gov-cn"),
        ("国家发展改革委", "gov-cn"), ("发改委", "gov-cn"),
        ("交通运输部", "gov-cn"), ("教育部", "gov-cn"), ("工信部", "gov-cn"),
        ("工业和信息化部", "gov-cn"), ("公安部", "gov-cn"),
        ("国家卫生健康委", "gov-cn"), ("卫健委", "gov-cn"),
        ("应急管理部", "gov-cn"), ("农业农村部", "gov-cn"),
        ("生态环境部", "gov-cn"), ("民政部", "gov-cn"), ("财政部", "gov-cn"),
        ("人社部", "gov-cn"), ("人力资源社会保障部", "gov-cn"),
        ("国家医保局", "gov-cn"), ("国家疾控局", "gov-cn"),
        ("证监会", "gov-cn"), ("银保监会", "gov-cn"), ("金融监管总局", "gov-cn"),
        ("最高人民法院", "gov-cn"), ("最高法", "gov-cn"),
        ("最高人民检察院", "gov-cn"), ("最高检", "gov-cn"),
        ("住房城乡建设部", "gov-cn"), ("住建部", "gov-cn"),
        ("国家能源局", "gov-cn"), ("国家林草局", "gov-cn"),
        ("国家文物局", "gov-cn"), ("文化和旅游部", "gov-cn"),
        ("中央气象台", "gov-cn"), ("国家气候中心", "gov-cn"),
        ("中国地震台网", "gov-cn"), ("国家统计局", "gov-cn"),
        ("中国政府网", "gov-cn"), ("国务院", "gov-cn"),
        ("澎湃", "thepaper"), ("人民网", "people"), ("人民日报", "people"),
        ("光明", "guangming"),
        ("中央电视台", "cctv"), ("央视", "cctv"), ("cctv", "cctv"),
        ("新闻联播", "cctv"),
        ("新华社", "xinhua"), ("新华网", "xinhua"), ("中新社", "xinhua"),
        ("中国新闻网", "xinhua"), ("中新网", "xinhua"), ("环球时报", "xinhua"),
        ("央广", "cnr"), ("中国日报", "chinadaily"),
        ("第一财经", "yicai"), ("证券时报", "stcn"), ("新京报", "bjnews"),
        ("南方都市报", "bjnews"), ("南方周末", "bjnews"),
        ("红星新闻", "bjnews"), ("上游新闻", "bjnews"), ("极目新闻", "bjnews"),
        ("封面新闻", "bjnews"), ("北京日报", "bjnews"), ("广州日报", "bjnews"),
        ("浙江日报", "bjnews"), ("中国青年报", "bjnews"),
        ("界面新闻", "caixin"), ("财新", "caixin"), ("经济观察报", "caixin"),
        ("21世纪经济报道", "caixin"), ("每日经济新闻", "caixin"),
        ("观察者网", "caixin"), ("半月谈", "xinhua"),
        ("头条", "toutiao"), ("百度", "baidu"),
        ("36氪", "36kr"), ("36kr", "36kr"), ("it之家", "ithome"),
        ("ithome", "ithome"), ("ars", "ars-technica"),
        ("technology review", "techreview"), ("ieee", "ieee-spectrum"),
    )
    for hay in (key_s, text):
        if not hay:
            continue
        low = hay.lower()
        for alias, key in alias_map:
            if alias in low:
                return key
        # structural fallback: any *.gov.cn host, or an official-looking name.
        # Measured: 42 of 60 real institutions resolved to `unknown` before this.
        # The prefix requirement (国家/中国/中央/省市县) is what keeps 俱乐部 and
        # 干部 from being mistaken for government bodies.
        if "gov.cn" in low:
            return "gov-cn"
        if re.search(r"^(?:国家|中国|中央|中华|全国|[\u4e00-\u9fff]{2,4}[省市县区])"
                     r"[\u4e00-\u9fff]{0,12}(?:总局|总署|管理局|监督管理|委员会|"
                     r"委|部|局|署|法院|检察院|气象台|台|中心)$", hay):
            return "gov-cn"

    # last resort: a known key appearing inside the text. Guarded against the
    # empty-string trap -- `"" in anything` is True and would match the first key.
    for hay in (key_s, text):
        if len(hay) < 2:
            continue
        low = hay.lower()
        for key in PLATFORM_WEIGHT:
            if key in low:
                return key
    return "unknown"


def evidence_form_of(entry: dict) -> str:
    """Classify the FORM of evidence from the text itself.

    Deliberately measured, not declared: an entry has to show concrete detail
    (quantities, named organisations, dates, places) to earn a higher form.
    """
    blob = " ".join([entry.get("title") or "", entry.get("summary") or ""])
    n_qty = len(RE_QUANTITY.findall(blob))
    n_org = len(RE_ORG.findall(blob))
    n_date = len(RE_DATE.findall(blob))
    n_place = len(RE_PLACE.findall(blob))
    n_cas = len(RE_CASUALTY.findall(blob))
    n_anchor = n_org + n_date + n_cas
    # 事故锚点给 3 分：它比规格参数特异得多，刷不出来。
    score = n_qty * 2 + n_org * 2 + n_date + n_place + n_cas * 3
    # 合取条件从「有任一锚点」收紧为「≥3 个锚点」。实测：只有 1 个机构词的短摘要
    # （'漳州口腔医院等4家机构暂停夜间门诊，患者分流最远约12公里。'）靠数量词
    # 堆到 first-hand-report，而它并没有一手调查的密度。
    anchored = n_anchor >= 3
    label = (entry.get("source_label") or "") + " " + (entry.get("source") or "")
    if any(k in label for k in ("通报", "公告", "regulation", "政府", "gov")):
        return "official-notice"
    if score >= 10 and anchored:
        return "first-hand-report"
    if score >= 5:
        return "named-entity"
    if score >= 1:
        return "secondary-report"
    return "unattributed"


# 修饰词与感叹号：**它们是偏见检测信号，不是噪声。**
#
# skill 的硬规则是「修饰词和感叹号不要过滤掉 —— 放进 signal_words，同时不进 claim」。
# 实测问题：这条规则此前**完全落空** —— 抓取路径（RSS / Crossref / atom）
# 一个字都不往 `signal_words` 里写，该字段只由调用方的 `--extra-json` 注入。
# 实测 193 条里 0 条有值。规则写在文档里、代码里没有对应实现，等于没有。
#
# 抽的是**报道者的判断痕迹**：程度词、评价性形容词、情绪化表达、感叹号。
# 不做褒贬分类 —— 只标出「这里有主观色彩」，让下游自己去质疑。
RE_SIGNAL_ZH = re.compile(
    r"(?:极为|极其|极度|彻底|完全|根本|重大|巨大|显著|大幅|急剧|飞速|迅猛|空前"
    r"|首次|唯一|史上|最[高新强大低快慢多]|突破性|颠覆性|革命性|里程碑"
    r"|震惊|震撼|重磅|爆|惊[人呆]|罕见|惊人|可怕|恐怖|惨|悲剧"
    r"|令人|不禁|竟然|居然|万万没想到|不可思议|难以置信"
    r"|必将|势必|无疑|显然|众所周知|众所周知)"
    r"|！+|!{2,}")
RE_SIGNAL_EN = re.compile(
    r"\b(?:unprecedented|breakthrough|revolutionary|disruptive|game.?chang\w*"
    r"|groundbreaking|stunning|shocking|devastating|catastrophic|massive|huge"
    r"|dramatic\w*|drastically|dramatically|significantly|substantially|nearly"
    r"|unprecedented|definitively|unequivocally|remarkabl\w+|extraordinarily"
    r"|first-ever|record-breaking|scary|terrifying|alarming)\b"
    r"|!{2,}", re.I)


def signal_words(entry: dict, limit: int = 8) -> list[str]:
    """从标题+摘要里抽出报道者的判断痕迹，原样保留。

    返回原文片段（不改写），去重、按出现顺序，最多 `limit` 个。
    **不进 claim** —— 调用方负责把它们放在 claim 之外。

    ★ 必须**只从正文抽**，标题不算。实测两个假阳性都出在标题上：
      `drea de matteo` → `['huge']`（人名里的词）
      `I built ALHR: ... Alarming` → `['Alarming']`（技术词被当评价）
    标题天然短、缺上下文，一个孤立的形容词几乎不可能是"报道者的判断痕迹"。
    摘要有句子语境，抽出才可信。
    """
    blob = (entry.get("summary") or "")
    if not blob:
        return []                    # 没有正文就没什么可抽的
    seen, out = set(), []
    for rx in (RE_SIGNAL_ZH, RE_SIGNAL_EN):
        for m in rx.finditer(blob):
            w = m.group(0)
            k = w.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(w)
    return out[:limit]


TREND_TRUTH_CAP = 0.45
PROMO_PENALTY = 0.25
PROMO_BODY_PENALTY = 0.10

# 大归档源：整个归档都在一个 feed 里，时间窗过滤前要吃进全部条目。
# 实测 openai-news = 1287 条，是阶段 2 成本（raw 2124）的主要来源。
# 这些源按【已解析的日期】先降序截断，再进池 —— 与单源限流不同：
# 限流发生在去重之后、作用于输出；这里是解析后立刻截断，省掉的是解析与去重成本。
ARCHIVE_SOURCES = {"openai-news", "arxiv-rss-ai"}
ARCHIVE_SCAN_LIMIT = 150


def apply_trend_cap(entry: dict) -> bool:
    """Clamp a trend-board entry below the truth gate, wherever it was scored.

    Measured bug: the cap originally lived only inside `truth_weight_social`, so
    it applied ONLY to `--domain social` runs. In `--domain ai` / `--domain
    science` runs everything goes through `truth_weight` and the key
    `trend_capped` never appeared at all -- hot-search entries sat at 0.29/0.33
    merely because their source weight is low. That is safe by accident, not by
    design: a future source-weight change would silently let a celebrity headline
    into the core section. A headline has no body to verify, so its truth cannot
    clear the gate by construction.

    Called once, centrally, after scoring, so no scoring path can bypass it.
    """
    own = entry.get("source_key", "")
    is_board = SOURCES.get(own, {}).get("class") == "trend" or entry.get("is_trend")
    if not is_board:
        return False
    entry["trend_capped"] = True
    if not entry.get("evidence_form"):
        entry["evidence_form"] = "trend-board(unverifiable)"
    # ASSIGN, do not min(). Measured bug: `min(score, CAP)` is a no-op whenever the
    # score is ALREADY below the cap -- which is the normal case for these sources
    # (0.29/0.31/0.33). The result was an entry carrying `trend_capped: True`
    # alongside an uncapped score: the flag said "suppressed", the number said
    # "not suppressed". A cap that only acts when it is not needed is decoration.
    entry["truth_score"] = TREND_TRUTH_CAP
    return True


def external_truth_weight(entry: dict) -> float:
    """真实分 for a supplemental-search item (--extra-json).

    Uses the SAME 0.30/0.70 split as the in-domain social model. Measured bug: the
    two paths disagreed (0.40/0.60 here vs 0.30/0.70 there), so the same IT之家
    story scored differently depending on how it entered, and an auditor
    recomputing from the JSON's own `weights` block could not reproduce either.
    One formula, one place, used by both.
    """
    pk = platform_of(entry.get("source_key", ""), entry.get("source_label") or "")
    form = evidence_form_of(entry)
    entry["platform_key"] = pk
    entry["evidence_form"] = form
    return round(0.30 * PLATFORM_WEIGHT.get(pk, 0.40)
                 + 0.70 * SOCIAL_EVIDENCE_FORM.get(form, 0.30), 3)


def truth_weight_social(entry: dict, corroborated: bool = False) -> float:
    """社会域专用的真实分：证据形式为主，平台可信度为辅。

    Used when the entry's domain is `social` AND the run has no authority stream
    to cross-check against (問題4). The default weight model assumes an
    authoritative tier exists; in the social domain it does not, so scoring by
    masthead alone would be scoring by something the domain cannot provide.
    """
    # Trend boards are a CALIBRATION signal, never a content source. Measured bug:
    # the social domain tags them `domains:["social"]`, so this branch scored their
    # bare headlines by evidence form -- and "多家医院、卫生院暂停夜间门诊" scored
    # 0.65, putting a hot-search phrase into the core section. That directly
    # contradicts quality-bar.md ("热度榜条目默认一条都不进核心区"). A headline has
    # no body to verify, so its truth cannot clear the gate by construction.
    if SOURCES.get(entry.get("source_key", ""), {}).get("class") == "trend" or entry.get("is_trend"):
        entry["platform_key"] = platform_of(entry.get("source_key", ""),
                                            entry.get("source_label") or "")
        entry["evidence_form"] = "trend-board(unverifiable)"
        entry["trend_capped"] = True
        return 0.45          # below CREDIBILITY_THRESHOLD -> 存疑区, never core

    pk = platform_of(entry.get("source_key", ""), entry.get("source_label") or "")
    form = evidence_form_of(entry)
    entry["platform_key"] = pk
    entry["evidence_form"] = form
    score = 0.30 * PLATFORM_WEIGHT.get(pk, 0.40) + 0.70 * SOCIAL_EVIDENCE_FORM.get(form, 0.30)
    if corroborated:
        score = min(score + CROSS_CONFIRM_BONUS, CROSS_CONFIRM_CAP)
    sev = notice_severity(entry.get("update_notices") or [])
    if sev == "retracted":
        return 0.05
    if sev == "concern":
        score = min(score, 0.45)
    return round(score, 3)


def comm_weight_social(entry: dict, pickups: int, in_trend: bool) -> float:
    """社会域专用的传播分：独立转载 + 平台热度。

    热度 carries 0.40 here (vs a mere +0.08 bonus in the default path) because
    the authority stream does not exist in this domain. Without a heat term,
    social 传播 could only ever come from reprints, and a genuine public event
    that nobody has reprinted yet could not clear the gate.

    Calibration: the reprint curve is chosen so the numbers mean the SAME thing as
    the default branch. Default comm gives exactly 0.60 for 2 independent pickups,
    so the social branch must too -- otherwise it is silently stricter and good
    social items fail a gate they should clear.

    KNOWN LIMITATION, stated rather than hidden: `in_trend` requires the same
    story to be present in BOTH the social pool and the trend sample. In the
    current architecture those are separate invocations, so trend entries never
    reach this function and the heat bonus effectively never fires. It is kept as
    an additive term so that wiring the two streams together later needs no
    rescoring -- but do not rely on heat boosting anything today.
    """
    by_pickups = {1: 0.35, 2: 0.60, 3: 0.72, 4: 0.80}
    base = by_pickups.get(pickups, 0.88)
    boost = 0.30 if in_trend else 0.0
    return round(min(base + boost, 0.95), 3)


def truth_weight(source_key: str, cls: str, corroborated: bool = False,
                 notices: list | None = None, entry: dict | None = None) -> float:
    """Compute the 真实 score for one entry.

    Blends source credibility with the evidence tier, then adds a bounded
    corroboration bonus. Returns a float in [0, 1]; the pipeline passes it
    against CREDIBILITY_THRESHOLD (0.6).

    A retraction or expression of concern SHORT-CIRCUITS everything else.
    Measured failure this fixes: the arsenic-life paper scored 0.912 -- the
    highest score in the whole suite -- because the weights encode "peer-reviewed
    in Science", which says nothing about whether the finding is true. It has
    since been retracted. Editorial tier is a proxy for reliability, NOT a
    substitute for the record, so the record must be allowed to override it.
    """
    base = SOURCE_WEIGHT.get(source_key, 0.50)
    if source_key and source_key not in SOURCE_WEIGHT and source_key in SOURCES:
        # ★ 登记表里有、权重表里没有 = 静默拿默认值。
        # 实测：新加的 `chinanews` 就是这样拿到 0.50 的 —— 权重决定被一个
        # 没人看过的默认值替我做掉了，而且只在反推分数时才被发现。
        # 不抛异常（会让整跑失败），但必须留痕，让 `--list` 和测试能查出来。
        UNWEIGHTED_SOURCES.add(source_key)
    ev = EVIDENCE_WEIGHT[evidence_type_for(source_key, cls, entry)]
    score = 0.4 * base + 0.6 * ev
    if corroborated:
        score = min(score + CROSS_CONFIRM_BONUS, CROSS_CONFIRM_CAP)

    severity = notice_severity(notices or [])
    if severity == "retracted":
        return 0.05          # withdrawn: below every gate, sorted into 存疑区
    if severity == "concern":
        return min(score, 0.45)
    if severity == "correction":
        score = min(score, 0.60)
    return round(score, 3)


# Retraction / concern labels as they appear in Crossref `update-to`.
RETRACTION_LABELS = ("retraction", "retracted", "withdrawal", "withdrawn")
CONCERN_LABELS = ("expression of concern", "concern")
CORRECTION_LABELS = ("correction", "corrigendum", "erratum", "addendum")


def notice_severity(notices: list) -> str | None:
    """Map Crossref update-to labels onto a severity, worst wins."""
    found = set()
    for n in notices or []:
        label = str((n or {}).get("label") or (n or {}).get("type") or "").lower()
        if not label:
            continue
        if any(k in label for k in RETRACTION_LABELS):
            found.add("retracted")
        elif any(k in label for k in CONCERN_LABELS):
            found.add("concern")
        elif any(k in label for k in CORRECTION_LABELS):
            found.add("correction")
    for level in ("retracted", "concern", "correction"):
        if level in found:
            return level
    return None


def comm_weight(n_independent_sources: int, in_trend: bool) -> float:
    """Compute the 传播报道 score.

    Calibration note (measured): the first version gave a single source 0.20, which
    killed 770 of 1570 entries -- including every Nature paper (truth 0.91) merely
    for not being reprinted yet. That inverts the purpose of a "what's NEW" radar.
    So the scale is set by what the 0.6 gate MEANS:

      1 source  -> below gate  = exclusive; real but uncorroborated, needs checking
      2 sources -> just passes = independently picked up
      3+        -> solidly passes

    A trend-board appearance corroborates spread but is NEVER sufficient alone --
    that is how a celebrity name would otherwise pass the gate. Enforced upstream
    in estimate_pickups, which counts only NON-trend identities as corroboration,
    so the `+0.08` bump this function used to apply is GONE: it was the very thing
    that let two hot-search boards jointly clear the gate (0.60 + 0.08 = 0.68).
    """
    if n_independent_sources <= 1:
        base = 0.35 if in_trend else 0.30
    elif n_independent_sources == 2:
        base = 0.60
    elif n_independent_sources == 3:
        base = 0.72
    elif n_independent_sources == 4:
        base = 0.80
    else:
        base = 0.88
    return round(min(base, 0.95), 3)


def estimate_pickups(entry: dict, cls: str = "") -> int:
    """Distinct source IDENTITIES carrying this entry (mirrors do not count twice).

    An explicit `pickups` (supplied via --extra-json) wins: the caller counted
    outlets from a source this script cannot see, and re-deriving the number from
    `also_seen_in` would silently drop that.

    TREND BOARDS DO NOT CORROBORATE EACH OTHER. Measured bug this fixes: two
    hot-search boards (baidu-hot + toutiao-hot, 当时两个都在册) both listing the
    same celebrity story produced pickups=2 -> comm 0.68 -> the story cleared the
    gate, which directly contradicts comm_weight's own rule that a trend
    appearance "is NEVER sufficient alone -- that is how a celebrity name would
    otherwise pass the gate". Two boards tracking the same search volume is one
    signal, not two.

    ⚠️ `baidu-hot` 已于 2026-10-10 下架（4 个镜像常态 3 个已死；当天 5 次失败
    只成功 2 次；中文热度面由 `toutiao-hot` 独家覆盖，冗余已够）。
    但**这条规则必须留着** —— 只要再登记第二个中文热度榜，两榜互证的路径
    立刻就会被走通。规则的价值不随当时有几个榜单而变。
    """
    if isinstance(entry.get("pickups"), int) and entry["pickups"] > 0:
        return entry["pickups"]

    def is_trend(key: str) -> bool:
        return SOURCES.get(key, {}).get("class") == "trend"

    own = entry.get("source_key", "")
    own_label = entry.get("source_label") or entry.get("source") or ""
    own_id = identity_of(own, own_label)
    own_norm = normalize_identity(own_label)
    others = [k for k in (entry.get("also_seen_in") or []) if k]
    # Corroboration counts ONLY non-trend identities, and never the entry's own
    # source. A government notice that lists itself is not a second opinion.
    real = set()
    if own and not is_trend(own):
        real.add(own_id)
    for k in others:
        if is_trend(k):
            continue
        # `k` may be a registry key or a bare outlet name from --extra-json.
        # identity_of resolves known outlets through the alias table; unknown names
        # fall through as raw text and are compared with normalize_identity (which
        # strips generic suffixes like 网/新闻).
        #
        # Measured bug: applying normalize_identity to the RESOLVED value too broke
        # the platform keys -- "yicai" ends in i/N and became "yca", "cctv" became
        # "cc", so two unrelated outlets collapsed and real corroboration was lost.
        # Normalize only the RAW name, and only when it did not resolve to a key.
        kid = identity_of(k, k)
        if not kid or kid == own_id:
            continue
        raw_norm = normalize_identity(k)
        if raw_norm == own_norm:
            continue
        if kid == k and normalize_identity(kid) == normalize_identity(own_norm):
            continue
        real.add(kid)
    real.discard("")
    if real:
        return len(real)
    # Nothing but trend boards carried it. Count DISTINCT boards rather than
    # collapsing to 1: two different boards are a wider signal than one, but the
    # comm curve starts at 2 = 0.60, so this still cannot clear the gate alone --
    # which is the rule comm_weight states.
    return max(trend_saturation(entry), 1)


def trend_saturation(entry: dict) -> int:
    """How many DISTINCT trend boards carried this entry."""
    own = entry.get("source_key", "")
    boards = set()
    if SOURCES.get(own, {}).get("class") == "trend":
        boards.add(identity_of(own))
    for k in (entry.get("also_seen_in") or []):
        if SOURCES.get(k, {}).get("class") == "trend":
            boards.add(identity_of(k))
    return len(boards)


def title_tokens(title: str) -> set:
    """Token set for near-duplicate matching across sources.

    Latin text splits on words; CJK has no spaces so it contributes character
    bigrams (a single char is too coarse: 美国 and 中国 would look alike).
    """
    t = (title or "").lower()
    words = set(re.findall(r"[a-z0-9]{3,}", t))
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    words |= {"".join(pair) for pair in zip(cjk, cjk[1:])}
    return words


def is_same_event(a: set, b: set, threshold: float = 0.62) -> bool:
    """Jaccard similarity on title tokens.

    The absolute-overlap floor is 2, not 3. Measured bug with 3: a short title
    yields few long tokens ("Atari Falcon" -> {atari, falcon}), so the floor
    rejected EVERY pair -- including titles that were byte-identical. Three such
    duplicate pairs survived into the output, inflating `kept`.
    """
    if not a or not b:
        return False
    inter = len(a & b)
    if inter < 2:
        return False
    union = len(a | b)
    return (inter / union) >= threshold


# Tokens that are pure framing, not part of the event. Stripped before computing an
# event signature so the SAME event worded two ways gets ONE id.
# Measured failure: '广西贵港闪爆事故2名失联人员确认已无生命体征' and
# '...最新进展：2名失联人员确认已无生命体征' produced different ids even though
# is_same_event() said they were the same event -- and my test passed anyway
# because it compared two IDENTICAL strings. A test that cannot fail is not a test.
EVENT_NOISE_TOKENS = {
    "最新", "进展", "最新进展", "确认", "已", "通报", "回应", "消息", "报道",
    "情况", "相关", "记者", "来源", "视频", "图片", "组图", "快讯", "独家",
    "breaking", "update", "latest", "report", "video", "photos",
}


def event_signature(title: str) -> str:
    """Normalise a title to the token set that identifies the EVENT.

    Three normalisations, each added because a real pair of wordings diverged:

      1. Strip parenthetical attributions: （市场监管总局） / [Reuters] / (Reuters)
      2. Strip a LEADING attribution prefix: 「市场监管总局：<事件>」 and
         「<事件>（市场监管总局）」 are the same event announced by the same body,
         and both forms occur in the wild. Measured: they hashed differently.
      3. Remove framing words from the RAW STRING before tokenising.
         Chinese has no spaces, so '最新进展' lives inside cross-boundary bigrams
         ('出最', '最新', '新进', '进展', '展：'); a token-set filter removes none of
         them, and the two wordings still hashed differently.
    """
    t = (title or "")
    t = re.sub(r"[（(\[][^）)\]]{1,20}[）)\]]", " ", t)
    # The prefix strip must be RESTRICTED to institution names. A loose
    # `^\S{2,12}：` also ate '广西贵港闪爆事故2名失联人员确认' from a title whose colon
    # was content, not attribution -- measured regression.
    t = re.sub(
        r"^\s*[\u4e00-\u9fff]{2,10}(?:总局|总署|管理局|委员会|监管局|政府|法院|"
        r"检察院|气象台|统计局|局|部|委|署|台|中心|公司|集团|协会|大学|医院)"
        r"\s*[：:]\s*", "", t)
    for noise in sorted(EVENT_NOISE_TOKENS, key=len, reverse=True):
        if len(noise) >= 2:
            t = t.replace(noise, " ")
    toks = title_tokens(t)
    return "|".join(sorted(toks))


def event_id(title: str) -> str:
    """Stable id for an EVENT, derived once and reused everywhere downstream.

    Measured need: dedupe, merging, cross-checking and heat-matching each derived
    their own notion of "same event", with different tokenisation and thresholds.
    One id means one definition.

    It is a matching AID, not a hash of the exact string: framing words are
    stripped first, so rewording does not change the id. Two DIFFERENT events can
    still collide if their content tokens coincide -- use is_same_event (Jaccard)
    when you need a graded closeness rather than an identity.
    """
    sig = event_signature(title)
    if not sig:
        return ""
    return hashlib.sha1(sig[:200].encode("utf-8")).hexdigest()[:16]


def entity_id(name: str) -> str:
    """Canonical identity for an outlet/organisation name, for cross-checking.

    ★ 这个函数**必须**委托给 identity_of —— 两个函数回答的是同一个问题。
    实测教训：修 identity_of 的过归一 bug 时（它曾返回 platform_of 的**权重桶**，
    导致新京报/南方都市报被判同一家、真实多源印证被吞），只改了 identity_of，
    entity_id 留着自己的 `platform_of(...)` 副本。于是补的 7 条
    「不应合并」断言全部失败 —— **测试抓到了不完整修复，这正是它该做的。**
    修法：删掉副本，只保留一个实现。
    """
    return identity_of("", str(name or ""))


# 广告标记分两类，因为【位置】决定含义：
#   标题命中 → 这篇就是广告（导购/白皮书/赞助稿）
#   只有摘要命中 → 多半是出版社模板或正文夹带的自家推广，不是这篇的性质
# 实测：`HiPHI`（真论文）被降分 0.634→0.384，只因 summary 尾部有
# 'volume pricing' / 'download this free whitepaper' 这类 IEEE 自家 boilerplate。
# 那是本轮新加英文判据【新引入】的误伤（T0 该例 0 命中）。
PROMO_TITLE_MARKERS = (
    "whitepaper", "white paper", "download this free", "sponsored content",
    "sponsored by", "paid post", "advertisement", "promotional", "buy now",
    "shop now", "register now", "request a demo", "book a demo",
    "直达链接", "购买链接", "优惠券", "到手价", "首发价", "开售", "领券",
    "促销", "限时抢购",
)
PROMO_BODY_MARKERS = (
    "volume pricing", "contact a content specialist", "sign up for our",
    "subscribe to our newsletter", "affiliate", "京东", "天猫", "限时",
)


def is_customer_story(entry: dict) -> bool:
    """厂商官网上「X 公司用我们的产品做到了 Y」这类客户案例／成果叙事。

    与 `is_promotional` 分开，因为**判据不同**：
      `is_promotional` 抓的是**广告形式**（白皮书/导购/赞助稿/价格）
      `is_customer_story` 抓的是**营销内容但非广告形式** —— 实测
      `openai-news` 落地 9 条里 5 条是这类，全部 truth=0.816，
      与真公告（GPT-6 发布、影响行动取缔通报）**同分同区，无法区分**。
      判据用标题的广告词完全抓不到它们（标题是「Sophos cuts … by 96%」）。

    实测样本（都是真阳性）：
      Sophos cuts threat investigation time by 96% with OpenAI Daybreak
      Asana cuts model costs 76x in browser tests with GPT-6.1 Sol
      How Oracle turns days of work into minutes with ChatGPT and Codex
      Pollo AI turns creative ideas into campaigns with OpenAI
      LegalOn halves Codex costs while maintaining development speed
      Radisson Hotel Group brings hotel discovery into ChatGPT

    形态：**一个具名第三方公司 + 一个数量化的收益 + 我们的产品**。
    """
    t = (entry.get("title") or "").lower()
    s = (entry.get("summary") or "").lower()
    blob = f"{t} {s}"
    if any(k in blob for k in ("customer story", "case study", "客户案例", "客户故事")):
        return True

    # 形态：一个具名第三方 + 一个「拿去做成了什么」的动作。
    #
    # ⚠️ 只保留**明确的客户叙事引子**和**第三方动作**。曾经加过
    # `how [a-z]+ is|are` 想覆盖「How Jump Trading is scaling…」，
    # 结果把 OpenAI 自己的公告也抓了（「How OpenAI is approaching text watermarking」
    # 「How to…」）—— 判据必须能区分「我们说我们怎么做」和「客户说他们怎么做」，
    # 而 `how X is -ing` 两者都匹配。所以去掉它，宁可漏判。
    cust = re.search(
        r"(?i)\b(discover how|learn how|see how)\b"
        # ★ 主体必须是**专有名词**（大写开头），不能是小写普通名词。
        # 实测误标：`[a-z]` 起头时，arXiv 论文摘要里
        # 「OpenAI agents exploited research infrastructure」也能匹配 ——
        # 「agents」被当成客户主体 → 一篇 agent 安全预印本被判成厂商客户案例。
        # 客户案例的主体永远是具名组织（Sophos / Asana / Oracle / Radiance…），
        # 所以要求大写。这条同时把「OpenAI turns…」这类厂商自述挡在外面，
        # 因为 OpenAI 自己不是自己的客户案例。
        r"|\b[A-Z][\w.&'’-]{1,30}\s+(?:uses?|used|partnered|teams? up|"
        r"cut|cuts|turns?|brings?|advances?|advancing|scales?|scaling|"
        r"halves?|helps?|expands?)\s+"
        r"(?:openai|chatgpt|codex|gpt|its|their|days|creative|hotel|quant|enterprise)"
        r"|\bexpand(?:s|ed|ing)? (?:its |their |the )?partnership\b",
        blob)
    # 「第三方公司 + 量化收益」同现在标题里，也算。
    quant_t = re.search(r"(?i)\b\d+(?:\.\d+)?\s*(?:x|×|%|percent|times|fold)\b", t)
    gain_t = re.search(r"(?i)\b(cuts?|cutting|halves?|halving|turns?|boosts?|speeds? up|"
                       r"saves?|reduces?|doubles?|triples?|slashes?|expands?)\b", t)
    return bool(cust or (quant_t and gain_t))


def promo_position(entry: dict) -> str | None:
    """Return 'title' | 'body' | None -- where the promotional signal sits."""
    title = (entry.get("title") or "").lower()
    body = (entry.get("summary") or "").lower()
    if any(k in title for k in PROMO_TITLE_MARKERS):
        return "title"
    if any(k in body for k in PROMO_TITLE_MARKERS + PROMO_BODY_MARKERS):
        return "body"
    # 价格型导购：标题里出现价格 + 型号 → 商品宣发。
    # 实测漏网：正则曾要求 元 紧跟数字，于是 '23.98 万元起'（汽车/家电/房产）
    # 全部漏过，一轮里所有真实产品稿都没被抓到。万 必须是可选分组。
    if re.search(r"\d+(?:\.\d+)?\s*万元?\s*(?:起|起售|预售)", title):
        return "title"
    if (re.search(r"\d+(?:\.\d+)?\s*(?:万)?\s*元", title)
            and re.search(r"(?:Pro|Max|Plus|Ultra|SE|\d{2,4}\s*(?:GB|TB|寸|英寸))", title)):
        return "title"
    return None


def is_promotional(entry: dict) -> bool:
    """这一条【是不是】广告/导购/赞助/客户案例。

    实测两个教训：
      1. 不能按【源标签】判。曾对整个 "OpenAI News" 源无条件返回 True —— 而该源
         的 label 就是 "OpenAI News"，于是整个官源恒真降分，连
         'Disrupting AI-enabled false front operations'（安全治理硬新闻）和
         'Helping teens learn…'（公益教育）都被丢掉。那 8 条当时恰好全是客户案例，
         但那是碰巧，不是判据。
      2. 判据必须看【位置】。见 PROMO_TITLE_MARKERS 的注释。
    """
    return promo_position(entry) == "title"


# 正文可核验性下限。实测：`summary` 字面值 "Comments"（HN 的 RSS 就是这么给的）
# 6 个字符拿到与 1900 字符摘要同等的真值分；另有 16 条【摘要完全为空】的条目
# 过了真值闸门进待观察区，其中 PNAS 一条拿到 peer-reviewed 0.896。
# 没有正文就没有可核验的主张 —— 真值分不该过闸。
BODY_MIN_CHARS = 30
PLACEHOLDER_SUMMARIES = {"comments", "comment", "no description", "摘要", "正文",
                         "read more", "详情", "原文", "查看全文"}


def body_missing(entry: dict) -> bool:
    s = (entry.get("summary") or "").strip()
    return len(s) < BODY_MIN_CHARS or s.lower() in PLACEHOLDER_SUMMARIES


# ---------------------------------------------------------------------------
# Stage-5 relevance gate.
#
# "词汇关联度" must be DIRECTIONAL, not a symmetric similarity. Measured reason:
# if the user asks about "AI 安全" and the extraction drifts to generic AI
# capability papers, both sides still share AI/model/training, so a symmetric
# Jaccard passes 0.6 easily while the extraction is in fact off-target.
#
# So the metric is COVERAGE: of the user's intent keywords, how many show up in
# the extracted set? The denominator is the USER's keywords, never the union.
# ---------------------------------------------------------------------------
DEFAULT_RELEVANCE_MIN = 0.6

# Words too generic to prove relevance on their own. If "AI" were allowed to
# count as coverage for an "AI 安全" request, the gate would never fire.
STOPWORD_TOKENS = {
    "the", "and", "for", "with", "that", "this", "from", "最新", "进展", "新闻",
    "消息", "情况", "相关", "方面", "一个", "什么", "怎么", "如何", "为什么",
    "ai", "人工智能",           # domain marker, not an intent keyword
    "news", "update", "latest", "about", "report",
}


def intent_keywords(request: str, explicit: list[str] | None = None) -> list[str]:
    """Keywords that define what the user actually wants.

    When BOTH `--request` and `--keywords` are given, the request's own tokens are
    MERGED IN rather than discarded. Measured bug: passing a request plus a few
    explicit keywords silently dropped every keyword that appeared only in the
    request text, so a user who wrote "夜间门诊 银鳕鱼 汞超标" as the request and
    then listed two of the three saw 汞 never evaluated at all.
    """
    raw: list[str] = []
    if explicit:
        raw += [k.strip() for k in explicit if k and k.strip()]
    if request:
        raw += re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{1,}|[\u4e00-\u9fff]{2,}", request)
    out, seen = [], set()
    for k in raw:
        low = k.lower()
        if low in STOPWORD_TOKENS or low in seen:
            continue
        seen.add(low)
        out.append(k)
    return out[:12]


def keyword_coverage(keywords: list[str], entries: list[dict],
                     strict: bool = True) -> dict:
    """Fraction of the user's keywords covered by the EXTRACTED items.

    Matching is case-insensitive substring over title + summary (+ merged sibling
    titles). Any number or proper noun counts double: losing a number the user
    asked for is a worse drift than losing a topic word.

    `strict` only matches the entries' own evidence text. Do NOT loosen this to
    "did the report mention the word anywhere": measured failure -- keywords
    {安全, 对齐, 风险} scored a perfect 1.0 against a pool of generic AI papers,
    because some paper mentioned "risk" in passing. Incidental mentions must not
    count as coverage or the gate never fires.
    """
    if not keywords:
        # ★ 不能返回 coverage=1.0。实测问题：没有关键词时这里给 1.0，控制台就打
        # 「关联度 1.0 / 阈值 0.6 [OK]」—— 而 `entries_checked` 是 0，闸门根本
        # 没跑。这正是本文件自己警告过的那件事：
        #   "a gate that silently passes everything is worse than no gate,
        #    because the report still claims it checked."
        # 没有可量的东西就说「没量」，不要报一个满分。
        return {"coverage": None, "checked": False, "keywords": [], "missing": [],
                "matched": [], "entries_checked": 0,
                "note": "未提供关键词，关联度【未检查】—— 不要把它读成通过"}

    def haystack(e: dict) -> str:
        parts = [e.get("title") or "", e.get("summary") or "",
                 " ".join(e.get("also_titles") or [])]
        return " ".join(parts).lower()

    hay = [haystack(e) for e in entries]
    matched, missing, weighted_hit, weighted_total = [], [], 0.0, 0.0
    for kw in keywords:
        low = kw.lower()
        weight = 2.0 if re.search(r"\d", kw) or re.match(r"^[A-Z]", kw) else 1.0
        weighted_total += weight
        if any(low in h for h in hay):
            matched.append(kw)
            weighted_hit += weight
        else:
            missing.append(kw)
    coverage = round(weighted_hit / weighted_total, 3) if weighted_total else 1.0
    return {"coverage": coverage, "keywords": keywords, "matched": matched,
            "missing": missing, "entries_checked": len(entries)}


def compact_view(result: dict, summary_chars: int = 600) -> dict:
    """下游要读的那份：去掉推导得出的冗余，摘要按字数截断。

    实测必要性：一次三路运行写出 320 KB JSON，而**子代理要把全量读进上下文**。
    逐字段测下来，成本几乎全在 `summary`（均 1031 B/条 / 占 90%），
    其余是 `weights`（1.7 KB 常量，每条运行重复一遍）、`published_raw`、
    `also_titles` / `also_event_ids`（合并兄弟条目，判断同事件用 `event_id` 就够）。

    ⚠️ `published_raw` **不是可推导的** —— 它含时刻（`16:24:26`），而 `published`
    被归一化到午夜。删它是为了省 token 的取舍，不是因为它冗余。需要精确时刻时
    读全量那份。（第一版 docstring 把这点说反了，实测 45 条里有 15 条受影响。）

    保留原则：**凡是「判断需要」的字段一个不删** —— 分数、分区、证据类型与
    **证据形式**、事件 id、来源、独立转载、风险标记、审计理由全部保留。
    **风险标记哪怕值是 `false` 也保留**：`promotional_signal: false` 意思是
    「检查过、没有导购信号」，删掉它下游就分不清「没有」和「没查」。
    """
    keep = ("title", "url", "published", "summary", "summary_cut", "summary_truncated",
            "authors",
            "source_key", "source_label", "source_identity", "source_status",
            "evidence_type", "reconciliation", "event_id", "score_source",
            "truth_score", "comm_score", "pickups", "section", "gate_note",
            "passes_truth", "passes_comm", "passes_gate",
            "corroborated", "single_source_hard_evidence",
            "major_window", "major_window_note",
            "also_seen_in", "corroborated_by",
            "promotional_signal", "promo_body_hit", "promo_note",
            "customer_story", "customer_story_note",
            "trend_capped", "trend_rank", "trend_heat", "body_missing", "body_note",
            "record_alert", "front_matter", "front_matter_reason",
            "abstract_missing", "update_notices", "related_news",
            "doi", "is_trend", "signal_words", "elements", "in_window",
            "trend_no_date", "mirror", "type", "container_title")
    # 值为 False / 0 / 空列表 也必须保留的键。实测：省略逻辑用 falsy 通杀，于是
    # `promotional_signal: false` 被删掉 —— 230 条全部评估过、compact 里 0 条保留，
    # 下游**无法区分「检查过、没有导购信号」和「根本没做这项检查」**。
    # 「缺省」和「否」在判断上是两件事，不能合并。
    #
    # `update_notices` / `record_alert`：撤稿与更正通道。它们【在 keep 里但曾被
    # falsy 规则剔除】，因为空列表 `[]` 是 falsy —— 于是「查过、无撤稿声明」
    # 在 compact 里变成「这个字段不存在」，下游分不清「干净」和「没查」。
    # 撤稿拦截是这个 skill 的卖点之一，它的**阴性结果**同样必须可见。
    #
    # 注：`retraction` 已从 keep 移除 —— 全文件没有任何赋值处，是死键。
    KEEP_WHEN_FALSY = {"promotional_signal", "promo_body_hit", "trend_capped",
                       "body_missing", "passes_truth", "passes_comm", "passes_gate",
                       "in_window", "is_trend", "update_notices", "record_alert",
                       "front_matter", "abstract_missing"}
    out_entries = []
    for e in result.get("entries", []):
        r = {}
        for k in keep:
            # ★ 必须用 `k not in e`，不能用 `e.get(k)`。
            # 实测两个 bug 都出在这一行：
            #   ① 幽灵 null：`e.get()` 对不存在的键返回 None，而 `None in (…, False)`
            #      为真（`in` 用 == 比较，None == False 为假但 `None in` 会先比
            #      identity…… 关键是 None 落进了那个元组判断），于是
            #      record_alert / front_matter / retraction 在全量里 0/141 出现，
            #      在 compact 里却 141/141 全是 null —— 凭空造键，还让同一个文件里
            #      出现 `false` 和 `null` 两套空值语义，省幅也从 40% 掉到 24-31%。
            #   ② `evidence_form` 从来不是条目字段（是 evidence_form_of() 现算的），
            #      所以它永远 None、永远被删 —— 而它是社交域 70% 权重的那个量。
            # 只看键【在不在】；空值省略留给下面的 falsy 判断，且只处理真实存在的键。
            if k not in e:
                continue
            v = e[k]
            # `in_window` 有三态：True（在窗内）/ False（窗外）/ **None（无日期）**。
            # None 必须保留 —— 它是「这条没日期」这个**已知事实**，
            # 与「键不存在」不是一回事。趋势榜 37 条就是这样：
            # 无日期、被窗口豁免，若把 None 当空值删掉，读者会看到
            # `in_window` 键整个消失，只能猜。判据用 `is False` 而不是 falsy。
            if k == "in_window" and v is None:
                r[k] = None
                continue
            if v in (None, "", [], {}) or (v is False and k not in KEEP_WHEN_FALSY):
                continue                      # 省略空值，省的是 token
            r[k] = v
        # ★ `evidence_form` 只在【它参与打分】时才出现在全量里，而且趋势条目上
        # 有一个特殊值 `trend-board(unverifiable)`（由 apply_trend_cap 写入）。
        # 第一版这里无条件 `r["evidence_form"] = evidence_form_of(e)`，两个后果：
        #   ① 把趋势条目的 `trend-board(unverifiable)` 覆盖成 `first-hand-report`
        #      —— 实测 52/52 全部矛盾，**热搜标题在下游唯一那份文件里被标成
        #      「一手报道」**，直接违反 quality-bar「热度榜不作事实依据」；
        #   ② 给权威面条目凭空补算一个它打分时【根本没用到】的量，
        #      一条 Nature 论文会带 `unattributed`，读者可能把「文本没抽到实体锚点」
        #      误读成「这篇不可信」。
        # 正确做法：全量有就照抄，全量没有就不写。
        if "evidence_form" in e:
            r["evidence_form"] = e["evidence_form"]
        s = e.get("summary") or ""
        # ★ 先记下「入库摘要全长」和「它本身是否已被 _clip 截过」。
        # 实测双重截断陷阱：`_clip()` 的 `…[省略 N 字]…` 标记固定在【第 1401 字】，
        # 而 compact 只保留前 600 字 —— 标记被截断动作本身销毁了，
        # 16 条被 _clip 截过的条目在 compact 里【全都看不出自己残缺】。
        # 最大一条 ieee-spectrum 实际省略 7618 字。下游会把它当成完整摘要。
        r_full_len = len(s)
        src_cut = ("…[省略" in s) or bool(e.get("summary_truncated"))
        if r_full_len > summary_chars:
            r["summary"] = s[:summary_chars]
            r["summary_cut"] = r_full_len    # 入库摘要全长，便于需要时回查
            # 尾部是结论所在（_clip 特意保留 tail=500），compact 把它切掉了 ——
            # 必须标出来，否则「先给结论」这个要求在精简视图上被静默破坏。
            r["summary_tail_cut"] = True
        if src_cut:
            r["summary_source_truncated"] = True
            r["summary_note"] = (
                "本条摘要在入库前已被 _clip 截断（源文更长），"
                + ("且 compact 又截到 summary_chars="
                   f"{summary_chars} 字，尾部结论不在本视图里；"
                   if r_full_len > summary_chars else "但未再被 compact 截断；")
                + "需要完整摘要读全量 JSON。")
        out_entries.append(r)
    return {
        "schema": "trend-radar/compact-1",
        "generated_at": result.get("generated_at"),
        "since": result.get("since"),
        "window_days": result.get("window_days"),
        "requested": result.get("requested"),
        "relevance": result.get("relevance"),
        "stats": result.get("stats"),
        # 覆盖情况必须保留：下游「本次覆盖」章节要用它，缺了就没法如实报降级
        "sources_report": result.get("sources_report"),
        "source_funnel": result.get("source_funnel"),
        "degraded_streams": result.get("degraded_streams"),
        "archive_truncated": result.get("archive_truncated"),
        "summary_chars": summary_chars,
        "note": ("精简视图。删掉的键分三类，都【不影响判断】，但性质不同："
                 "① 可推导或纯记账 —— also_titles / also_event_ids / "
                 "trend_boards / _capped_in；"
                 "② 重量级常量 weights（每条运行重复 1.7 KB）；"
                 "③ ★不可推导、纯为省 token 而删的 published_raw"
                 "（含时刻 16:24，而 published 归一化到午夜）——"
                 "需要精确时刻时读全量那份。"
                 "摘要超长按 summary_chars 截断并标 summary_cut（值为原文全长）。"
                 "判断所需字段一律保留，含值为 false 的风险标记"
                 "（promotional_signal / trend_capped / body_missing / record_alert），"
                 "因为「检查过、无信号」和「没做检查」是两件事。"),
        "entries": out_entries,
    }


def set_source_timeouts() -> None:
    """Per-source HTTP timeouts, set from MEASURED maxima -- not from intuition.

    实测全部 30 个源各跑 3 次（2026-10-10）：

        源              最大      中位    说明
        arxiv-ai       11.29s    9.43s   ← 最慢，但是【合法慢】
        reddit-ml       7.35s    6.89s   ← 全在等退避，0 条
        其余 28 个       均 < 4.8s

    ⚠️ 关键结论与直觉相反：**最慢的源需要的是更大的超时，不是更小的。**
    `arxiv-ai` 慢是因为它对每个分类各发一次请求、每次之间 sleep `ARXIV_GAP=3s`
    （对 arXiv 的礼貌限流，不能去掉）。4 个分类时耗时 ≈ 9.4 + 3×3 = 18.4s，
    而全局 TIMEOUT=25s —— 余量很小，网络稍慢就会【截断合法的分类数据】。

    `reddit-ml` **故意不设**：它现在稳定 429、0 条，看起来最该压。但它一旦恢复，
    成功请求耗时未知（429 的 7.3s 里 6s 是退避，不是响应时间），
    按限流时的耗时设超时会反过来切断恢复后的正常响应。**没有可靠数字就不设。**

    对快源也【不设小超时】：`_get` 的 timeout 是每次尝试的超时，而快源会
    `retries=2`；设 2s 会在一次瞬时抖动后于 4-6s 处整体失败 —— 比等 25s 更糟。
    逐源超时的价值在于【给已知需要更久的源放宽】，不在于给快源上枷锁。
    """
    for key in ("arxiv-ai", "arxiv-rss-ai"):
        if key in SOURCES:
            SOURCES[key]["timeout"] = 40


set_source_timeouts()


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch trend-radar structured sources.")
    ap.add_argument("--source", action="append", default=None,
                    help="registry key, or a group name (ai/journal/general). repeatable")
    ap.add_argument("--class", dest="klass", action="append", default=None,
                    help="stream selector: social | authority | trend. repeatable. "
                         "social = 社会面(热度/风向/爆料) 可信度低；authority = 权威面(期刊/官方) 可核查")
    ap.add_argument("--domain", action="append", default=None,
                    help="三大类之一: social | science | ai. repeatable. "
                         "从用户需求判断出属于哪一类，两路子代理都在这一类内路由")
    ap.add_argument("--days", type=int, default=3, help="time window in days (default 3)")
    ap.add_argument("--major-days", type=int, default=0,
                    help="【本周重大事件】把窗口放宽到 N 天，但**只对重大源生效**"
                         "（期刊 / 官方发布 / 预印本）。默认 0 = 关闭。"
                         "用途：诺奖这类在窗口外一两天的领域级事件 "
                         "—— 实测化学奖 10-07 宣布，而窗口是 10-08~10-10，"
                         "按窗口纪律只能整条丢掉。"
                         "⚠️ 只有被 --extra-json 显式标注的源会走宽窗口，"
                         "聚合器（ScienceDaily / Phys.org 那类）**不**享受 —— "
                         "它们的迟到转载正是这个问题的来源，不是解法。"
                         "进来的条目带 major_window=True，必须写明「落在基础窗口外」")
    # ★ 默认 25 → 30（2026-10-10 实测）。
    # 实测 AI 域（25 源，最慢的域）：
    #   cap=12 → 41.4s / kept 221 / 超限砍掉 207
    #   cap=20 → 44.4s / kept 318 / 超限砍掉 137
    #   cap=30 → 50.6s / kept 385 / 超限砍掉 108
    # 即：+9s 换 +74% 落地条目，而「已独立印证」恒为 2 ——
    # 印证取决于【多家报道】，不取决于每源抓多少。
    # 所以提高上限是纯增益：它不是"多抓点"，是"少丢点"。
    # 墙钟上限仍由单源 timeout（最大 40s）决定，与 cap 无关。
    ap.add_argument("--max-per-source", type=int, default=30)
    ap.add_argument("--out", default=None, help="write JSON here (recommended on Windows)")
    ap.add_argument("--compact", action="store_true",
                    help="同时写一份精简视图 <out>_compact.json —— 给子代理/下游读，"
                         "省约一半读入量（去掉可推导字段 + 截断超长摘要）")
    ap.add_argument("--compact-chars", type=int, default=600,
                    help="精简视图里摘要保留的字数上限（默认 600）")
    ap.add_argument("--keywords", action="append", default=None,
                    help="阶段5 用：从用户需求里抽出的意图关键词（3-8 个）。"
                         "可重复。不传则用 --request 自动抽取")
    ap.add_argument("--request", default=None,
                    help="用户原始需求文本，用来自动抽取意图关键词")
    ap.add_argument("--relevance-min", type=float, default=DEFAULT_RELEVANCE_MIN,
                    help=f"关联度低于此值 → 触发二次搜寻（默认 {DEFAULT_RELEVANCE_MIN}）")
    ap.add_argument("--check-json", default=None,
                    help="阶段5 复检：给一个 JSON，只对里面的条目算关联度，不抓取。"
                         "量的是萃取后那几条，不是抓来的全池")
    ap.add_argument("--extra-json", default=None,
                    help="问题3 修复：把 web_search/web_fetch 补充检索来的条目注入评分。"
                         "接受 [{title,source,url,date,summary,domain?}, ...] 或 "
                         "{entries:[...]}。每条都会按 PLATFORM_WEIGHT 打真值分，"
                         "score_source 标为 'platform_table'")
    ap.add_argument("--list", action="store_true", help="show registry + measured blockers and exit")
    args = ap.parse_args()

    if args.list:
        print("DOMAINS (三大类，从用户需求判断属于哪一类)")
        print("  注：`domains` 字段是【粗粒度归属】，`class` 才是【实际路由】。")
        print("      一个源可以被标进某域、但它的 class 让它在 --domain X 时被排除。")
        print("      所以下面同时给【域内源数】和【--domain X 实际能取到的源数】——")
        print("      实测这两者曾经差一倍（social 域标 7 源，实际只取到 3 源），")
        print("      只看域内源数会高估覆盖。")
        for dom, desc in DOMAINS.items():
            members = sorted(k for k, v in SOURCES.items() if dom in v.get("domains", []))
            print(f"  {dom:8} {desc}")
            print(f"           domains 标了 {len(members)} 源: {', '.join(members)}")
            for kl in ("authority", "social", "trend"):
                hit = sorted(k for k in members if SOURCES[k].get("class") == kl)
                if hit:
                    print(f"           --domain {dom} --class {kl:9} → {len(hit)} 源"
                          + ("  ← ⚠ 交集很小" if len(hit) <= 3 else ""))
        print("\nREGISTRY  (class 决定喂给哪一路)")
        for klass in ("authority", "social", "trend"):
            n = len([k for k, v in SOURCES.items() if v.get("class") == klass])
            print(f"\n  [{klass}]  ({n} 源)")
            for key, spec in sorted(SOURCES.items()):
                if spec.get("class") != klass:
                    continue
                doms = "/".join(spec.get("domains", []))
                print(f"    {key:18} [{spec['status']}] {spec['group']:8} {doms:22} {spec['label']}")
                if spec.get("note"):
                    print(f"    {'':18}  note: {spec['note']}")
        _unweighted = sorted(k for k in SOURCES if k not in SOURCE_WEIGHT)
        if _unweighted:
            print(f"\n⚠ 未给权重的源（会静默拿默认 0.50）: {', '.join(_unweighted)}")
            print("   → 每个源都应在 SOURCE_WEIGHT 里显式登记并写明理由。")
        print("\nMEASURED BLOCKERS (do not retry blindly)")
        for url, why in BLOCKED:
            print(f"  {url}\n      {why}")
        return 0

    # ---- stage-5 relevance re-check on an already-extracted shortlist ----
    # This is the CORRECT stage-5 usage. The rule says "萃取完的内容" -- measure
    # the shortlist about to be published, NOT the raw pool. Measured failure of
    # pool-level checking: keywords {安全, 对齐, 风险} scored 1.0 against generic
    # AI papers, because one paper mentioned "risk" in passing. The gate never
    # fired, which makes it decorative.
    if args.check_json:
        try:
            # utf-8-sig, not utf-8: PowerShell's `Set-Content -Encoding UTF8`
            # writes a BOM, and json.loads chokes on it ("Unexpected UTF-8 BOM").
            # Measured; this is the normal way the file gets created on Windows.
            raw = pathlib.Path(args.check_json).read_text(encoding="utf-8-sig")
            payload = json.loads(raw)
        except Exception as exc:
            print(f"cannot read --check-json {args.check_json}: {exc}", file=sys.stderr)
            return 2
        items = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            print("--check-json must be a list, or an object with an 'entries' list",
                  file=sys.stderr)
            return 2
        kws = intent_keywords(args.request or "", args.keywords)
        rel = keyword_coverage(kws, items)
        rel["threshold"] = args.relevance_min
        # 无关键词时 coverage 是 None（不是 1.0）—— 见 keyword_coverage 的注释。
        # 这里必须显式处理，否则 `None < 0.6` 会 TypeError。
        if rel.get("checked") is False:
            rel["needs_second_pass"] = False
            print(f"关联度复检（对 {rel['entries_checked']} 条萃取结果）")
            print(f"  未检查 —— {rel.get('note','')}")
            print("  -> 要给 --keywords 才能量；不要把它读成通过。")
        else:
            rel["needs_second_pass"] = rel["coverage"] < args.relevance_min
            print(f"关联度复检（对 {rel['entries_checked']} 条萃取结果）")
            print(f"  覆盖 {rel['coverage']} / 阈值 {rel['threshold']}  "
                  f"[{'需二次搜寻' if rel['needs_second_pass'] else 'OK'}]")
            print(f"  命中  : {', '.join(rel['matched']) or '无'}")
            print(f"  未覆盖: {', '.join(rel['missing']) or '无'}")
            if rel["needs_second_pass"]:
                print("  -> 换检索角度/换关键词重跑，然后重新复核权重。不许直接出结论。")
        if args.out:
            pathlib.Path(args.out).write_text(
                json.dumps({"relevance": rel}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # --compact 在这条路径上曾是【静默失效】：check-json 提前 return 0，
            # 走不到写 compact 的那段，于是 exit 0、全量写了、_compact.json
            # 根本不存在，而文档把 --compact 列为「必加参数」。
            # 要么写出来，要么明说写不了 —— 不能什么都不做还报成功。
            if args.compact:
                print("warning: --check-json 路径不产出抓取条目，--compact 无意义，已忽略",
                      file=sys.stderr)
        elif args.compact:
            print("warning: --compact 需要配合 --out 才会写精简视图，已忽略",
                  file=sys.stderr)
        return 0

    # ---- source selection ----
    # Three orthogonal selectors: --source (explicit keys/groups), --class (which
    # pipeline stream), --domain (which of the three main categories).
    #
    # MEASURED BUG this replaces: the class filter was guarded by
    # `if args.klass and args.source`, so `--domain science --class authority`
    # silently returned the WHOLE science domain -- social-class sources and the
    # trend boards included. A stream selector that only works in one combination
    # is worse than no selector: the caller believes it restricted the run.
    # --class now ALWAYS applies.
    keys: list[str] = []
    if args.source:
        for token in args.source:
            if token in SOURCES:
                keys.append(token)
            elif token in ("social", "authority", "trend"):
                keys += [k for k, v in SOURCES.items() if v.get("class") == token]
            else:
                keys += [k for k, v in SOURCES.items() if v["group"] == token]
    elif args.klass:
        # an explicit stream selection already IS the class filter
        keys = [k for k, v in SOURCES.items() if v.get("class") in set(args.klass)]
    else:
        keys = list(SOURCES)             # narrow below

    if args.klass and args.source:
        allowed = set(args.klass)
        keys = [k for k in keys if SOURCES[k].get("class") in allowed]

    if args.domain:
        bad = [d for d in args.domain if d not in DOMAINS]
        if bad:
            print(f"unknown domain {bad}; valid: {list(DOMAINS)}", file=sys.stderr)
            return 2
        want_dom = set(args.domain)
        keys = [k for k in keys if want_dom & set(SOURCES[k].get("domains", []))]
        # Trend boards are the stage-2 路C stream: they belong to the PIPELINE,
        # not to a domain. Add them only in a domain-only run (no --class), i.e.
        # when the caller asked for the whole pipeline rather than one stream.
        if not args.klass:
            trend_keys = [k for k, v in SOURCES.items() if v.get("class") == "trend"]
            keys = list(dict.fromkeys(keys + trend_keys))
    keys = list(dict.fromkeys(keys))
    if not keys:
        print(f"no source matched source={args.source!r} class={args.klass!r} "
              f"domain={args.domain!r}; use --list", file=sys.stderr)
        return 2

    # ★ 日期边界必须用【用户本地时间】，不能用 UTC。
    #
    # 实测 bug（真实请求踩到）：北京时间 10-11 00:33 跑 `--days 1`，
    # 用户要的是「今日」= 他本地的 10-11，但当时 UTC 还是 10-10 ——
    # 于是窗口取的是 10-10，**10-11 一条都拿不到**（实测 returned 0）。
    # 对 UTC+8 的用户，每天 00:00–08:00 都会这样。
    #
    # 为什么不能改成 UTC 算、本地显示：日期边界的语义是
    # 「这一天发生了什么」，而**"这一天"是用户日历上的那一天**，
    # 不是格林尼治的那一天。日报类工具跟着读者时区走是标准做法。
    #
    # 所有下游比较都已按 `.date()` 粒度（11 处一致），所以这里换成
    # 本地日期之后，整条链路自洽，不需要动别处。
    # `generated_at` 仍写带时区的本地时间（`isoformat()` 会带 offset）。
    now = datetime.now().astimezone()
    since = now - timedelta(days=args.days)
    # 必须在这里定义，不能等到窗口过滤那一段 —— fetch 循环（更早）就要用它
    # 去放宽 Crossref 的**查询**窗口。
    major_days = max(0, int(getattr(args, "major_days", 0) or 0))
    entries: list[dict] = []
    report: list[dict] = []
    dropped_external = 0
    archive_truncated: dict[str, int] = {}
    # 版面级失败（只有 kind=rss_multi 会产生）。必须报出来 ——
    # 「4 个版面只成功 1 个」不能看起来像「这家今天内容少」。
    sub_feed_failures: dict[str, list[str]] = {}
    degraded: list[dict] = []
    stale_feeds: list[dict] = []

    for key in keys:
        spec = SOURCES[key]
        # 单源超时隔离。实测：源之间差两个数量级（ithome 14.2s / reddit 17.1s 全在
        # 等重试 / techreview 0.13s），一个慢源会拖住同路后面所有源。
        # 每个源可以用 spec["timeout"] 单独设定。
        src_timeout = spec.get("timeout", TIMEOUT)
        t0 = time.perf_counter()

        def _row(**kw) -> None:
            """Append a report row with the per-source wall time attached.

            NOTE: this APPENDS. The first version returned a dict and every call
            site forgot to use it, so `sources_report` came back empty and
            `degraded_streams` then reported "该路全部源失败" for a run where all
            seven sources had succeeded. Returning a value that the caller must
            remember to store is exactly the shape of bug that hides.

            Measured need for the timing: to find that ithome/reddit were slow I
            had to write an external timing harness. The script knows this
            already; not recording it means every future investigation repeats
            that work.
            """
            report.append({"source": key, "label": spec["label"],
                           "seconds": round(time.perf_counter() - t0, 2), **kw})

        try:
            if spec["kind"] == "rss":
                got = fetch_rss(spec["url"], spec["label"], spec.get("ua"),
                                spec.get("link_prefer", "alternate"), src_timeout)
            elif spec["kind"] == "atom":
                got = fetch_atom_arxiv(spec, args.max_per_source)
            elif spec["kind"] == "crossref":
                # ★ 查询窗口必须跟着 --major-days 放宽，否则宽窗口【根本不起作用】。
                # 实测：只改 Python 侧的过滤器时，`--major-days 7` 与 `--major-days 0`
                # 输出**完全相同** —— 因为 Crossref 的 API 请求本身就带了
                # `filter=from-online-pub-date:<since>`，窗口外的记录压根没被抓回来，
                # 过滤器看不到它们。放宽的是**查询**，收紧的仍是**过滤器**
                # （见窗口过滤处：进来的条目一律标 major_window 且 in_window=False）。
                got = fetch_crossref(
                    spec, args.max_per_source,
                    since - timedelta(days=max(0, major_days - args.days)))
            elif spec["kind"] == "rss_multi":
                got, sub_failed = fetch_rss_multi(
                    spec["feeds"], spec["label"], spec.get("ua"),
                    spec.get("link_prefer", "alternate"), src_timeout)
                if sub_failed:
                    # 版面级失败要能看见 —— 否则「4 个版面只成功 1 个」
                    # 会看起来像「这家今天内容少」，是同一类静默陷阱。
                    sub_feed_failures[key] = sub_failed
            elif spec["kind"] == "toutiao_hot":
                got = fetch_toutiao_hot(spec, args.max_per_source)
            elif spec["kind"] == "google_trends":
                got = fetch_google_trends(spec, args.max_per_source)
            else:
                raise ValueError(f"unknown kind {spec['kind']!r}")
            for entry in got:
                entry["source_key"] = key
                entry["source_status"] = spec["status"]
            # 大归档源增量采集：解析后立刻按日期降序截断。
            # 与单源限流不同 —— 限流发生在去重之后、作用于输出；这里省掉的是
            # 解析＋去重＋评分的成本。实测 openai-news 单源 1287 条。
            if key in ARCHIVE_SOURCES and len(got) > ARCHIVE_SCAN_LIMIT:
                before = len(got)
                got.sort(key=lambda e: e.get("published") or "", reverse=True)
                got = got[:ARCHIVE_SCAN_LIMIT]
                archive_truncated[key] = before - len(got)
            # 陈旧源检测。实测最危险的一类失败：人民网 RSS 返回 HTTP 200 +
            # 标准 XML + 100 条，但内容是 2025-06 的；新华网返回 200 + 300 条，
            # 内容是 2022-12 的。【比 404 危险得多】—— 直接登记会把三年前旧闻
            # 当成"近 3 天"喂进管线。
            #
            # ★ 判据必须用【日期】粒度比较，不能用 datetime。
            # 实测误杀：条件曾写成 `max(dated) < since`，而 since 是带时刻的
            # datetime（10-07 17:00），条目日期是纯 date（10-07 00:00），
            # 于是 10-07T00:00 < 10-07T17:00 成立 —— 整个 crossref-natmi 源
            # 被报告成「最新条目 2026-10-07 早于窗口起点 2026-10-07」，
            # 同一个日期被判早于自己，AI 领域唯一的同行评审权威源零覆盖。
            # 修正：同样用 date 粒度，窗口边界那天算【在内】。
            if spec.get("class") != "trend":
                dated = [d for d in (_parse_date(e.get("published")) for e in got) if d]
                if dated and max(dated).date() < since.date():
                    newest = max(dated).date().isoformat()
                    stale_feeds.append({"source": key, "label": spec["label"],
                                        "newest_item": newest,
                                        "window_start": since.date().isoformat()})
                    _row(status="stale-feed", count=0,
                     detail=f"最新条目 {newest} 早于窗口起点 "
                            f"{since.date().isoformat()}，已整源排除")
                    continue
            entries.extend(got)
            _row(status="ok", count=len(got),
                     **({"archive_truncated": archive_truncated.get(key, 0)}
                        if key in archive_truncated else {}))
        except urllib.error.HTTPError as exc:
            _row(status="http-error", detail=f"HTTP {exc.code}", count=0)
        except Exception as exc:  # noqa: BLE001 - report and continue
            detail = str(exc)[:200]
            if "timed out" in detail.lower():
                detail = "timeout"
            elif "<urlopen error" in detail:
                detail = detail.split("<urlopen error", 1)[1].split(">")[0]
            # `count` is present on failure rows too: measured, a consumer that
            # iterated the whole report and read r["count"] crashed on the first
            # failure row, which lacked the key.
            _row(status="failed", detail=detail, count=0)

    # ---- 问题3: inject supplemental-search items so they can be scored ----
    # Measured: in a real run the three most reportable items were found by
    # web_search, not by any registered source, so the whole weight system was
    # blind to them -- the gate was protecting the least valuable material.
    # This lets those items enter the same pool, tagged `external`, and be scored
    # on the platform table (the only tier information they have).
    if args.extra_json:
        try:
            payload = json.loads(pathlib.Path(args.extra_json).read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(f"cannot read --extra-json {args.extra_json}: {exc}", file=sys.stderr)
            return 2
        extra_items = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(extra_items, list):
            print("--extra-json must be a list, or an object with an 'entries' list",
                  file=sys.stderr)
            return 2
        added = 0
        for it in extra_items:
            if not isinstance(it, dict) or not (it.get("title") or "").strip():
                continue
            pub = it.get("date") or it.get("published")
            # 走 _clip 而不是 [:900] 硬切。实测问题：注入路径曾是 `[:900]`，
            # **既不插省略标记也不设 summary_truncated** —— 静默截断，正是
            # 「双重截断不可见」要消灭的那一类。改成与 RSS/Crossref 同一条路径，
            # 于是三种来源的截断行为与记账完全一致。
            _x_raw = _strip_html(it.get("summary") or it.get("claim") or "")
            entry = {
                "title": _strip_html(it.get("title") or ""),
                "url": it.get("url"),
                "summary": _clip(_x_raw),
                "summary_truncated": _is_clipped(_x_raw),
                "authors": it.get("authors") or [],
                "source_label": it.get("source") or it.get("source_label") or "补充检索",
                "source_key": it.get("source_key") or "",
                "published_raw": pub,
                "published": (_parse_date(pub).isoformat() if _parse_date(pub) else None),
                "external": True,
                "score_source": "platform_table",
                "in_window": None,
            }
            if it.get("signal_words"):
                entry["signal_words"] = it["signal_words"]
            if it.get("elements"):
                entry["elements"] = it["elements"]
            # Corroboration: a supplemental item found in several independent
            # outlets is exactly the case the caller wants counted. Accept either
            # an explicit count or the list of other outlets that carried it.
            seen_in = [x for x in (it.get("also_seen_in") or []) if x]
            entry["also_seen_in"] = seen_in
            # 身份去重后的【并集】—— 调用方给的名字可能和 source 是同一家。
            by_identity = len(
                {identity_of(x) for x in seen_in if identity_of(x)}
                | {identity_of(entry.get("source_label") or "")}
            )
            if isinstance(it.get("pickups"), int) and it["pickups"] > 0:
                # 显式计数优先（调用方可能看到了脚本看不到的渠道），
                # 但不能低于按身份算出来的并集 —— 否则等于丢掉已知来源。
                entry["_external_pickups"] = max(it["pickups"], by_identity)
            elif seen_in:
                entry["_external_pickups"] = by_identity
            # an external item is never exempt from the time window.
            # 同样用【日期】粒度 —— 注入条目带的 date 一律是 `2026-10-09` 这种纯日期，
            # 用 datetime 比较会把边界日整天误判为窗口外，与主窗口过滤口径不一致。
            dt = _parse_date(entry["published"])
            if dt is not None and dt.date() < since.date():
                dropped_external += 1
                continue
            entries.append(entry)
            added += 1
        report.append({"source": "extra-json", "label": args.extra_json,
                       "status": "ok", "count": added})

    # errata filter: Crossref registers corrections as ordinary journal-articles,
    # so the server-side type filter does not remove them (measured). Drop them here.
    errata_dropped = 0
    if ERRATA_DROP:
        remaining = []
        for entry in entries:
            if ERRATA_RE.search(entry.get("title") or ""):
                errata_dropped += 1
                continue
            remaining.append(entry)
        entries = remaining

    # time filter: keep undated entries but flag them.
    # 热度榜 (class=trend) is exempt: a hot-search list has no per-item date and
    # is by definition current, so applying the window would wrongly empty it.
    kept, undated, dropped, trend_exempt = [], 0, 0, 0
    for entry in entries:
        if SOURCES.get(entry.get("source_key", ""), {}).get("class") == "trend":
            entry["in_window"] = True
            entry["trend_no_date"] = True
            entry["is_trend"] = True
            trend_exempt += 1
            kept.append(entry)
            continue
        dt = _parse_date(entry.get("published"))
        if dt is None:
            entry["in_window"] = None
            undated += 1
            kept.append(entry)
        # ★ 必须用【日期】粒度，与 stale-feed 判据一致。
        # 实测 bug（子代理定位）：这条曾经是 `dt >= since`，而 since 是带时刻的
        # datetime（2026-10-07T11:05:46Z），Crossref 只给日期粒度
        # （一律 T00:00:00Z）→ 窗口首日的每一条都被判为"窗口外"。
        # 实测复现：crossref-natmi 2/2、crossref-nejm 1/1、crossref-nature 2/17
        # 全是 2026-10-07T00:00:00 被判 `< since` —— 权威面【100% 的"窗口外"
        # 都是这个假象，没有一条是真旧闻】。
        # 同一个文件里两个时间判据对同一天给出相反答案，是比单个 bug 更糟的状态：
        # stale 判据已经改成 .date() 了，这里没跟上。
        # `<=` 而非 `>=`：边界日整天算【在内】，与 stale 判据的边界处理对齐。
        elif dt.date() >= since.date():
            entry["in_window"] = True
            kept.append(entry)
        elif (major_days > 0
              and _is_major_source(entry)
              and dt.date() >= (since - timedelta(days=major_days - args.days)).date()):
            # ★ 重大事件宽窗口。**只对重大源生效**，其余一律照旧丢弃。
            #
            # 为什么不能全局放宽 --days：那会让聚合器的迟到转载也进来 ——
            # 而「机构稿 09-28、聚合器 10-09 才转发」正是这个问题的来源。
            # 把窗口整体放宽等于奖励慢转载，还让「近 3 天」这个承诺失效。
            #
            # 为什么不能默默放行：这条路径进来的东西**确实在基础窗口外**。
            # 所以强制打标，下游必须写明。静默放宽窗口是比漏报更糟的错。
            entry["in_window"] = False          # 它确实不在基础窗口内，不粉饰
            entry["major_window"] = True
            entry["major_window_note"] = (
                f"★ 落在基础窗口（近 {args.days} 天）之外，"
                f"经 --major-days {major_days} 作为【本周重大事件】纳入。"
                f"引用时必须写明它不在基础时间窗内。")
            kept.append(entry)
            # ⚠️ 这里【不】计 major_kept。
            # 实测 bug：原本在此 `major_kept += 1`，但后面还有**去重合并** ——
            # 被并掉的条目仍然被计了数，于是 `stats.major_events_kept` 报 10
            # 而实际发出 8 条。**报出去的数比实际多**，与 `passed_both`
            # 共用计数器那次是同一类错：计数点在管线的中段，而读者看的是末端。
            # 改成从最终 `capped` 里数（见下方 stats 构造处）。
        else:
            entry["_dropped_window"] = True
            dropped += 1

    # Dedupe by EVENT, not by exact title. Cross-source coverage of one story
    # rarely shares a title, so exact matching under-counts corroboration and
    # (worse) reports "1 source" for something three outlets carried.
    seen: list[dict] = []
    merged = 0
    for entry in kept:
        toks = title_tokens(entry.get("title") or "")
        if not toks:
            continue
        # One event id per item, assigned at the single point where the token set
        # already exists, and reused by dedupe / merge / heat-matching downstream.
        entry["event_id"] = event_id(entry.get("title") or "")
        hit = None
        for cand in seen:
            if is_same_event(toks, cand["_tokens"]):
                hit = cand
                break
        if hit is not None:
            hit.setdefault("also_seen_in", []).append(entry.get("source_key") or entry.get("source_label"))
            hit.setdefault("also_titles", []).append(entry.get("title"))
            hit.setdefault("also_event_ids", []).append(entry.get("event_id"))
            entry["_merged_away"] = True      # recorded for the per-source funnel
            merged += 1
        else:
            # Preserve any callers-supplied corroboration. Measured bug: this line
            # unconditionally reset `also_seen_in` to [], wiping the independent
            # reprints a --extra-json item declared -- so a story three outlets
            # carried scored as a single-source exclusive.
            entry.setdefault("also_seen_in", [])
            entry.setdefault("also_titles", [])
            entry["_tokens"] = toks
            seen.append(entry)
    deduped = seen
    for entry in deduped:
        entry.pop("_tokens", None)
    # Board order: highest credibility first, so the per-source cap keeps the
    # BEST entries of each source rather than the newest. Needs the scores, so the
    # weight pass below runs first -- but its COUNTERS must describe the capped
    # list, not this pre-cap list. Measured bug this ordering fixes: a run
    # reported kept=32 while its four bucket counters summed to 507, because the
    # counters ran before the cap. Subagents read those counters and drew
    # conclusions from a population that was never emitted.

    # ---- credibility weights ----
    # Computed HERE, not estimated by the model: the same input must always yield
    # the same number, otherwise the 0.6 gate is not auditable.
    retracted_count = 0
    # Which domains this run was scoped to; decides whether social uses its own
    # reconciliation path (see truth_weight_social).
    run_domains = set(args.domain or [])
    social_branch = "social" in run_domains

    for entry in deduped:
        key = entry.get("source_key", "")
        cls = SOURCES.get(key, {}).get("class", "")
        pickups = estimate_pickups(entry)
        # An explicit count from --extra-json overrides the derived one, here and
        # for every consumer below. Measured bug: comm used the override while the
        # recorded `pickups` field kept the derived value, so an entry showed
        # `pickups: 1` beside `comm_score: 0.60` -- 0.60 requires 2. An auditor
        # recomputing from the recorded field could not reproduce the score.
        if isinstance(entry.get("_external_pickups"), int) and entry["_external_pickups"] > 0:
            pickups = entry["_external_pickups"]
        entry.pop("_external_pickups", None)
        entry["evidence_type"] = evidence_type_for(key, cls, entry)
        entry["source_identity"] = identity_of(key)

        # A distinct second source is corroboration. Mirrors of one origin
        # (hn-frontpage vs hnrss) collapse to a single identity and do NOT count;
        # trend boards never corroborate truth, only spread.
        others = {identity_of(k) for k in (entry.get("also_seen_in") or []) if k}
        others.discard(entry["source_identity"])
        corroborated = bool(others) and cls != "trend"

        if entry.get("external"):
            # 问题3: supplemental-search items. They have no registry key, so the
            # source-tier model cannot score them -- but leaving them unscored is
            # exactly the failure this fixes: the BEST items in a real run came
            # from supplemental search and were invisible to the gate. Score them
            # on the platform table, which is the only tier information they have.
            entry["reconciliation"] = "platform-table(external)"
            entry["truth_score"] = external_truth_weight(entry)
            # honour an explicit pickup count when the caller supplied one
            entry["comm_score"] = comm_weight_social(
                entry, pickups, bool(entry.get("is_trend")))
            entry["score_source"] = "platform_table"
        elif bool(domain_of_source(key) & run_domains) and social_branch:
            # 问题4: no authority tier exists in this domain, so cross-checking
            # is impossible. Use the evidence-form model instead of the default
            # source-tier model, and say so on the entry.
            entry["reconciliation"] = "social-evidence-form"
            entry["truth_score"] = truth_weight_social(entry, corroborated=corroborated)
            entry["comm_score"] = comm_weight_social(
                entry, pickups, bool(entry.get("is_trend")))
        else:
            entry["reconciliation"] = "source-tier"
            entry["truth_score"] = truth_weight(key, cls, corroborated=corroborated,
                                                notices=entry.get("update_notices"),
                                                entry=entry)
            entry["comm_score"] = comm_weight(pickups, bool(entry.get("is_trend")))
        # Applied centrally, AFTER every scoring path, so no branch can skip it.
        # Previously the cap lived inside truth_weight_social and therefore only
        # ran for --domain social (see apply_trend_cap docstring).
        apply_trend_cap(entry)

        # 导购/广告/客户案例降分（信号，不是删除）。实测：详情密度高 ≠ 编辑核验，
        # 36氪/IT之家的产品导购靠价格与型号刷满 evidence_form，被判 first-hand-report
        # 0.78 过闸。降 25% 让它落在合理区间，同时把信号写进审计轨迹供模型推翻。
        # 广告降分：标题命中 → 全罚；只有摘要命中 → 半罚。
        # 半罚的理由见 PROMO_TITLE_MARKERS 注释（出版社 boilerplate 夹带自家推广）。
        promo = promo_position(entry)
        entry["promotional_signal"] = promo == "title"
        if promo == "title":
            entry["truth_score"] = round(max(entry["truth_score"] - PROMO_PENALTY, 0.0), 3)
            entry["promo_note"] = (f"标题即广告/导购/赞助/白皮书：真值分降 {PROMO_PENALTY}"
                                   "（信号，可被推翻）")
        elif promo == "body":
            entry["promo_body_hit"] = True
            entry["truth_score"] = round(max(entry["truth_score"] - PROMO_BODY_PENALTY, 0.0), 3)
            entry["promo_note"] = (f"仅摘要含推广措辞（多为出版社模板）：真值分降 "
                                   f"{PROMO_BODY_PENALTY}，不视为该条性质")

        # 无正文 → 真值分压到闸门下。实测：`summary` = "Comments" 的 6 字符条目拿到
        # 与 1900 字符摘要同等的分；16 条空摘要条目过了真值闸门。没有正文就没有
        # 可核验的主张，真值分不该过闸。
        # ⚠️ 文案必须与实际一致。第一版写「保留在待观察区」「压到 0.50 以下」，
        # 实测两条都不对：36 条 body_missing **100% 落在 section=丢弃**
        # （0.50 仍低于 0.6 闸门），且分值恒为 **0.500**（min 的结果不是"以下"）。
        # 一条自称"保留在待观察区"的注释，会让下游以为还能引用它。
        if body_missing(entry) and not entry.get("trend_capped"):
            entry["body_missing"] = True
            entry["truth_score"] = min(entry["truth_score"], 0.50)
            entry["body_note"] = ("无可核验正文（摘要为空或占位符）→ 真值分封顶 0.50，"
                                  "低于 0.6 闸门，因此落在【丢弃】区，不作事实依据；"
                                  "条目本身仍保留在 JSON 里可查")

        # 修饰词与感叹号：抓取路径以前一字不写，实测 193 条里 0 条有值 ——
        # skill 的硬规则「修饰词和感叹号不要过滤掉」在代码里没有对应实现。
        # 在这里统一补上（所有来源路径都经过这个循环）。
        # 调用方通过 --extra-json 已经给过的，尊重它，不覆盖。
        if not entry.get("signal_words"):
            sw = signal_words(entry)
            if sw:
                entry["signal_words"] = sw

        # 客户案例／成果叙事：营销内容但**不是广告形式**，所以 is_promotional
        # 抓不到。单独标记，让下游能把它与真公告区分开。
        # 不是删除，是标注 —— 厂商自述仍有信息价值，只是不能当独立验证。
        cs = is_customer_story(entry)
        entry["customer_story"] = cs
        if cs:
            entry["customer_story_note"] = (
                "厂商官网的客户案例/成果叙事：量化收益由厂商与客户共同提供，"
                "**不是独立验证**。已单独标记，不要与真公告同权重看待。")

        entry["pickups"] = pickups
        entry["trend_boards"] = trend_saturation(entry)
        entry["corroborated_by"] = sorted(others)

    # ★ 排序键必须【完全确定】，不能只按分数。
    #
    # 实测稳定性 bug：原来只按 `(truth_score, comm_score)` 降序。而同一个源里
    # 大量条目**分数完全相同**（实测 `chinanews` 一次跑批 162 条全是 0.53/0.30），
    # Python 的 sort 是稳定的 —— 于是同分条目**保持摄入顺序**。
    # 摄入顺序取决于哪个 feed 先返回、`as_completed` 谁先完成，
    # **每次跑都可能不同**。
    # 后果：`--max-per-source 30` 从 162 条里挑 30 条时，
    # **挑中哪 30 条是不确定的** —— 同一份数据、同一套参数，两次跑出的
    # 报告内容不一样。这比"少抓几条"严重得多，因为结论会漂。
    #
    # 加入【发布时间】作次级键：同分时新的优先。这把不确定性换成了明确规则，
    # 而且"同分取更新"本身就是这个场景该有的取舍。
    # 再兜一层 title 保证全序（极端情况下连时间都相同）。
    deduped.sort(
        key=lambda e: (e["truth_score"], e["comm_score"],
                       str(e.get("published") or ""), str(e.get("title") or "")),
        reverse=True)

    # per-source cap: arXiv daily RSS can dump 470+ entries into a 3-day window;
    # uncapped that swamps every other source in the JSON the model has to read.
    capped: list[dict] = []
    per_source_count: dict[str, int] = {}
    overflow = 0
    for entry in deduped:
        key = entry.get("source_key") or entry.get("source_label") or "?"
        if per_source_count.get(key, 0) >= args.max_per_source:
            overflow += 1
            continue
        per_source_count[key] = per_source_count.get(key, 0) + 1
        entry["_capped_in"] = True        # dicts are unhashable; flag instead
        capped.append(entry)

    # ---- gate accounting: on `capped`, i.e. exactly what gets emitted ----
    # ★ `passed_both` 与 `passed_single` 必须【分开计数】。
    # 实测问题（子代理用真实数据抓到）：两者曾共用 `passed_both`，
    # 于是 stdout 打出「双项达标 25」，而逐条字段是
    # `passes_truth=25 / passes_comm=0`（comm 全 0.3）—— 那 25 条**全是**
    # 单源硬证据通道，没有一条经过独立印证。
    # 后果比少报更糟：**引用「双项达标 25」会严重高估印证程度**，
    # 而"有多少条真的被多家印证过"正是这个数字唯一该回答的问题。
    passed_truth = passed_comm = passed_both = passed_single = failed = 0
    for entry in capped:
        sev = notice_severity(entry.get("update_notices") or [])
        if sev:
            entry["record_alert"] = sev
            if sev == "retracted":
                # Short-circuit: a retracted paper must never reach any pass list,
                # whatever its source tier says.
                entry["truth_score"] = 0.0
                entry["passes_truth"] = False
                entry["passes_comm"] = entry["comm_score"] >= CREDIBILITY_THRESHOLD
                entry["passes_gate"] = False
                entry["gate_note"] = "★ 已被撤稿 —— 作废，不得作为事实依据（进存疑区）"
                retracted_count += 1
                failed += 1
                continue

        # 新 is NOT weight-gated: a fresh item has no coverage yet, so scoring
        # novelty with the same gate would systematically kill the newest news.
        t_ok = entry["truth_score"] >= CREDIBILITY_THRESHOLD
        # A trend-only item is a calibration signal, not content. Enforced here as
        # a hard gate rather than relying on trend_capped truth being low: measured,
        # "宋佳集齐三大奖" reached comm=0.60 (two hot-search boards merging into
        # pickups=2) and landed in 存疑区 -- it must occupy no list budget at all.
        if entry.get("trend_capped"):
            c_ok = False
        else:
            c_ok = entry["comm_score"] >= CREDIBILITY_THRESHOLD
        entry["passes_truth"] = t_ok
        entry["passes_comm"] = c_ok
        # ★ 单源硬证据通道。闸门本身不改（comm<0.6 确实=未经独立印证），
        # 改的是【不让它与不可信混为一谈】。
        #
        # 实测问题（三个域都复现）：真值 0.896–0.912 的同行评审论文、0.816 的厂商
        # 官方发布，全部因 comm=0.30 卡在待观察区 → 核心区结构性为空。
        # 根因：`comm>=0.6 需 >=2 家独立转载` 是给【社会面】设计的规则，
        # 而学术首发与官方发布天然是单源 —— 新论文上线第一时间只有一家报。
        # 把这两种情况都归入"未印证"，等于让最硬的证据永远进不了核心区。
        #
        # 判据刻意收得很窄：【证据类型】已是 peer-reviewed 或 official
        # （而不是靠源权重阈值，避免又出现两套可能打架的实现），
        # 且真值达标。趋势榜已被上面的 trend_capped 硬闸挡在外面，不会受益。
        #
        # ⚠️ 这类条目**仍然是单源**，所以下游必须能看到这一点 ——
        # 靠 `corroborated=False` + gate_note 明写，而不是悄悄放行。
        entry["corroborated"] = bool(c_ok)
        entry["single_source_hard_evidence"] = bool(
            t_ok and not c_ok and not entry.get("trend_capped")
            # ★ 客户案例不得走这条通道。实测：`official` 是来源标签，
            # 而 openai-news 的客户案例也带这个标签 —— 不排除的话，
            # 「某公司用我们的产品省了 96%」这种营销内容会被护送进核心区。
            and not entry.get("customer_story")
            and entry.get("evidence_type") in HARD_EVIDENCE_TYPES)
        entry["passes_gate"] = bool(t_ok and (c_ok or entry["single_source_hard_evidence"]))
        # `section` is written for EVERY entry, including passers. Measured bug:
        # `gate_note` existed only on non-passers, so consumers hit
        # KeyError: 'gate_note' on exactly the successful items. A field that
        # appears only on failures is a trap for whoever reads the successes.
        if entry["passes_gate"] and entry["single_source_hard_evidence"]:
            entry["section"] = "核心区"
            entry["gate_note"] = (
                "★ 单源硬证据通道：真值达标 + 证据类型为 "
                f"{entry.get('evidence_type')}，但【未经独立印证】"
                f"（comm={entry['comm_score']}）。可引用，必须标注单源。")
            passed_single += 1
        elif entry["passes_gate"]:
            entry["section"] = "核心区"
            entry["gate_note"] = "双项达标 → 核心区"
            passed_both += 1
        elif t_ok and not c_ok:
            # verifiable but not yet widely reported -> 待观察, NOT discarded
            entry["section"] = "待观察区"
            entry["gate_note"] = "真实达标/传播未达标 → 待观察区"
            passed_truth += 1
        elif c_ok and not t_ok:
            entry["section"] = "存疑区"
            entry["gate_note"] = "传播达标/真实未达标 → 存疑区"
            passed_comm += 1
        elif entry.get("customer_story") and not c_ok:
            # 厂商官网的客户案例/成果叙事：真值分高（官方一手）但【不是独立验证】，
            # 而且它是营销内容。实测：不单独归类的话，6 条「某公司用我们的产品
            # 省了 96%」会与真公告一起堆在待观察区顶部、分数完全相同。
            # 放进存疑区 —— 语义准确（声称可信但未经独立核验），
            # 同时不占待观察区的名额。
            entry["section"] = "存疑区"
            entry["gate_note"] = ("厂商客户案例/成果叙事 → 存疑区：官方一手但**不是独立验证**，"
                                  "且属营销内容。可作厂商口径样本，不可作事实依据。")
            passed_comm += 1
        elif entry.get("trend_capped"):
            # 热度榜条目不是"没达标"，它是【刻意不占预算的校准信号】。
            # 实测：45 条热度榜被写成 section=丢弃，语义完全错 —— 它们既没失败
            # 也不该被丢，它们的作用是校准传播度和发现遗漏。
            entry["section"] = "校准信号"
            entry["gate_note"] = ("热度榜：只有标题无正文，真值分在构造上不可能过闸。"
                                  "用作传播度校准与发现遗漏，不占列表预算。")
            failed += 1
        else:
            entry["section"] = "丢弃"
            entry["gate_note"] = "双项未达标 → 丢弃"
            failed += 1

    # Emit in board order: the model reads top-down, and position in context is
    # itself a signal (see the lost-in-the-middle stage).
    capped.sort(key=lambda e: (e["passes_gate"], e["truth_score"], e["comm_score"]),
                reverse=True)

    # Surface a structural warning: if nothing at all passed truth, the domain is
    # probably served only by preprints (which top out at 0.578 < 0.6).
    truth_passers = passed_both + passed_single + passed_truth
    if capped and truth_passers == 0:
        # Only meaningful when an AUTHORITY stream was actually selected. Measured
        # bug: a pure `--class trend` run printed the "本域权威面只有预印本" warning
        # even though no authority source was queried -- the trigger never checked
        # what was selected. A warning that fires on unrelated runs trains the
        # reader to ignore it.
        selected_classes = {SOURCES[k].get("class") for k in keys}
        if "authority" in selected_classes:
            preprint_only = {e.get("evidence_type") for e in capped} <= {"preprint", "rumor"}
            print("  WARNING: 0 条通过真实闸门。"
                  + ("本域权威面只有预印本/传言类证据，而预印本上限 0.578 < 0.6 —— "
                     "这是结构性错配，不是内容质量问题。"
                     if preprint_only else "检查阈值或源覆盖。"))
        else:
            print("  NOTE: 0 条通过真实闸门，但本次未选任何权威面源 —— "
                  "这不是权威面覆盖问题。")

    # ---- stage-5 relevance gate ----
    # Measures whether the extraction still answers the ORIGINAL request. A low
    # score means the run drifted, which triggers a second search pass rather
    # than shipping an off-target briefing.
    #
    # 问题2: two request types need two different measures. A classifier request
    # ("给我社会和 AI 的信息") has no intent keywords to cover, so keyword
    # coverage reports a meaningless 1.0. Detect the type and measure domain
    # share instead.
    req_text = args.request or ""
    verdict = classify_request(req_text, args.domain)
    classifier = verdict["verdict"] == "classifier"
    kws = intent_keywords(req_text, args.keywords)
    eff_kw = effective_keyword_count(kws)

    if classifier and not kws:
        rel = domain_share(capped, run_domains or set(DOMAINS))
        rel["mode"] = "classifier(domain-share)"
        rel["threshold"] = args.relevance_min
        rel["needs_second_pass"] = rel["coverage"] < args.relevance_min
        if rel["needs_second_pass"]:
            rel["instruction"] = (
                "分类型需求下的域内占比低于阈值 → 大部分内容不在你指定的类里。"
                "检查分类是否判错，或该域源覆盖是否不足。")
    elif classifier:
        # A classifier request that ALSO yielded keywords: report both, but do NOT
        # let the keyword metric veto the result. Measured: the request
        # "给我社会和AI的信息" tokenises to ['给我社会和', '的信息'] -- Chinese has no
        # spaces, so the tokenizer produces whole clauses, which match nothing and
        # drive coverage to 0.0 even though the run was perfectly on-domain
        # (domain share 1.0). Category requests are generic by nature; keyword
        # recall is not a meaningful precision measure for them.
        kw_rel = keyword_coverage(kws, capped)
        dom_rel = domain_share(capped, run_domains or set(DOMAINS))
        rel = dict(dom_rel)
        rel["mode"] = "classifier(domain-share; keyword advisory)"
        rel["threshold"] = args.relevance_min
        rel["keyword_advisory"] = {
            "coverage": kw_rel["coverage"],
            "keywords": kw_rel["keywords"],
            "matched": kw_rel["matched"],
            "missing": kw_rel["missing"],
            "note": "仅供参考，不参与判定 —— 分类型需求的关键词是整句，无匹配意义",
        }
        rel["needs_second_pass"] = dom_rel["coverage"] < args.relevance_min
        if rel["needs_second_pass"]:
            rel["instruction"] = (
                "域内占比低于阈值 → 大部分内容不在你指定的类里。"
                "检查阶段1 分类是否判错，或该域的源覆盖是否不足。"
                "注意：此判定只看域内占比，不看关键词。")
    else:
        rel = keyword_coverage(kws, capped)
        rel["mode"] = "keyword"
        rel["threshold"] = args.relevance_min
        if eff_kw < 2:
            # Measured: the boundary must be `< 2`, not `<= 2`. With exactly 2 real
            # terms (e.g. ['夜间门诊','汞']) the coverage denominator is legitimate
            # and the gate should act; the old `<= 2` silently disabled it there,
            # so the field only ever fired at 0 keywords.
            rel["needs_second_pass"] = False
            rel["weak_validation"] = True
            rel["weak_reason"] = (
                f"有效关键词只有 {eff_kw} 个（<2），单次未命中就会越过阈值。"
                "本轮不据此触发二次搜寻，关键词覆盖仅作记录。")
        else:
            rel["needs_second_pass"] = rel["coverage"] < args.relevance_min
            if rel["needs_second_pass"]:
                rel["instruction"] = (
                    "关联度低于阈值 → 执行二次搜寻：换检索角度/换关键词重跑，"
                    "然后重新复核权重。不许直接出结论。")
    rel["request_type"] = verdict["verdict"]
    rel["effective_keywords"] = eff_kw
    # 这个键必须【两种需求类型都出现】。实测：classifier 分支不带它，
    # 于是同一份 JSON 里 eff_kw=0 却没有 weak_validation，消费者读键会 KeyError。
    rel.setdefault("weak_validation", eff_kw < 2)
    if rel["weak_validation"] and not rel.get("weak_reason"):
        rel["weak_reason"] = (
            f"有效关键词只有 {eff_kw} 个（<2）："
            + ("本需求为分类型，判定走域内占比，关键词仅供参考"
               if classifier else "覆盖度不可靠，本轮不据此触发二次搜寻"))
    # The audit trail. Stage-1 misclassification is invisible downstream, so the
    # reason and confidence are recorded here for a human to overrule.
    rel["classification"] = verdict
    # 告警必须覆盖【两种】低置信情形。实测这行曾是死代码：
    # classifier 只产出 0.70/0.85/0.95，`< 0.7` 恒假；而 keyword 的真低分
    # 0.0/0.3 又被 `verdict == "classifier"` 排除。两个条件一起把分支锁死了。
    if verdict["confidence"] <= 0.70:
        rel["classification_warning"] = (
            f"分类置信度仅 {verdict['confidence']}（{verdict['verdict']}）："
            + ("建议人工确认这确实属于分类型需求"
               if verdict["verdict"] == "classifier"
               else "未命中任何类词；若这其实是分类需求，请显式给 --domain"))

    # Per-source funnel accounting. Measured need: `crossref-natmi` reported
    # 2 items fetched but ZERO landed, with no field anywhere explaining it -- and
    # "is the authoritative tier big enough?" is exactly the question that needs
    # answering. Without this an item can vanish (merged as a duplicate, dropped
    # out of window) and look like a silent bug.
    #
    # Note: `dropped` (time window) is counted BEFORE the funnel exists, so it is
    # tracked per entry via the `_dropped_window` marker set in that loop.
    funnel: dict[str, dict] = {}

    def _f(key: str | None) -> dict:
        return funnel.setdefault(
            key or "?",
            {"fetched": 0, "dropped_window": 0, "merged_into_other": 0,
             "capped_out": 0, "emitted": 0})

    # ★ 为【每一个被请求的源】预建漏斗键，即使它返回 0 条。
    # 实测 bug：漏斗只在有条目时才建键，于是返回 0 条的源在 `source_funnel` 里
    # **连键都没有** —— 本次实测 crossref-cell / lancet / natbiotech / sciadv 四个源
    # 全部如此。后果是「0 条」这个事实在结构上【双重不可见】：
    #   ① 进不了 stale-feed（判据要求 dated 非空）
    #   ② 也进不了漏斗表（没有键）
    # 于是权威面 13 个源里 4 个空转，而任何一张表都看不出来。
    # 预建键之后，"这个源被请求了、返回 0 条"本身就是一个可读的事实。
    for _k in keys:
        _f(_k)

    for e in entries:
        rec = _f(e.get("source_key") or e.get("source_label"))
        rec["fetched"] += 1
        if e.get("_dropped_window"):
            rec["dropped_window"] += 1
        if e.get("_merged_away"):
            rec["merged_into_other"] += 1
    for e in capped:
        _f(e.get("source_key") or e.get("source_label"))["emitted"] += 1
    for e in deduped:
        if not e.get("_capped_in"):
            _f(e.get("source_key") or e.get("source_label"))["capped_out"] += 1
    for rec in funnel.values():
        rec["unaccounted"] = max(
            rec["fetched"] - rec["emitted"] - rec["merged_into_other"]
            - rec["capped_out"] - rec["dropped_window"], 0)
        # 0 条要能被读出来，且带上耗时 —— 0 条 + 极短耗时是"空 feed"的典型形态，
    # 与"抓到了但全被过滤"是两回事。
    for r in report:
        rec = funnel.get(r["source"])
        if rec is None:
            continue
        sec = r.get("seconds")
        if sec is not None:
            rec["seconds"] = sec
        if r.get("status") == "ok" and not (r.get("count") or 0):
            rec["empty_feed"] = True
            # 实测对比：arxiv-rss-ai 0 条时耗时 0.03s（空 feed 立即返回），
            # 而 arxiv-ai 正常抓取 11.66s。0 条 + <1s 基本可以断定 feed 是空的，
            # 不是"窗口内没有新条目"。但这只是可疑标记，不是判决。
            if sec is not None and sec < 1.0:
                rec["empty_feed_suspect"] = True

    # ---- 异常降级：某一路全挂必须显式标记，不能让报告看起来像完整覆盖 ----
    # 实测：某一路全部源失败时，输出里只是少了那一份数据，没有任何字段说明，
    # 而报告仍然会声称"三路全开"。
    for cls_name in ("authority", "social", "trend"):
        want = [k for k in keys if SOURCES[k].get("class") == cls_name]
        if not want:
            continue
        rows = [r for r in report if r["source"] in want]
        ok_rows = [r for r in rows if r["status"] == "ok"]
        got_n = sum(r.get("count", 0) for r in ok_rows)
        # ★ extra-json 的注入条目也算这一路的来源。
        # 实测口径问题：分母只含注册表键，于是「2/3 源成功，50 条」实际上
        # 是 3/4 个来源、56 条 —— 注入那么重要（社会面的主要来源），
        # 在覆盖率口径里却不计入，等于把最有用的一路报小了。
        ext_rows = [r for r in report
                    if r["source"] == "extra-json" and (r.get("count") or 0) > 0]
        if ext_rows:
            got_n += sum(r["count"] for r in ext_rows)
        n_total = len(want) + (1 if ext_rows else 0)
        n_ok = len(ok_rows) + (1 if ext_rows else 0)
        if not ok_rows and not ext_rows:
            degraded.append({"stream": cls_name, "reason": "该路全部源失败",
                             "attempted": n_total, "succeeded": 0, "entries": 0})
        elif got_n == 0:
            degraded.append({"stream": cls_name, "reason": "该路全部源返回 0 条",
                             "attempted": n_total, "succeeded": n_ok, "entries": 0})
        else:
            # 零产出源单独点名。实测：hnrss 抓 20 条、被 hn-frontpage 全额判重合并、
            # emitted=0 —— 一个源等于不存在，但 status 是 ok，所以既不进 failed
            # 也不进 degraded，只有逐源 funnel 看得出来。下游读的是 degraded_streams。
            dead = sorted(k for k in want
                          if (funnel.get(k) or {}).get("fetched", 0) > 0
                          and (funnel.get(k) or {}).get("emitted", 0) == 0)
            # ★ 「返回 0 条」与「零产出」是两回事，但都属于覆盖缺口。
            # 实测：crossref-sciadv / lancet / cell / natbiotech 四个源返回 0 条，
            # 既不进 degraded（status=ok）也不进 zero_yield（要求 fetched>0），
            # 于是权威面 13 个源里 4 个空转而「本次覆盖」看不出来。
            # 0 条有两种原因（期刊本周没新文章 vs 接口坏了），报告只需如实列出，
            # 由人判断 —— 隐去才是最坏的选择。
            empty = sorted(r["source"] for r in ok_rows if not (r.get("count") or 0))
            failing = sorted(r["source"] for r in rows if r["status"] != "ok")
            gaps = {}
            if failing:
                gaps["failed_sources"] = failing
            if dead:
                gaps["zero_yield_sources"] = dead
            if empty:
                gaps["returned_zero_sources"] = empty
            if len(ok_rows) < len(want):
                degraded.append({"stream": cls_name,
                                 "reason": f"该路有源失败（{n_ok}/{n_total} 来源可用）",
                                 "attempted": n_total, "succeeded": n_ok,
                                 "entries": got_n,
                                 **({"injected_extras": len(ext_rows)} if ext_rows else {}),
                                 **gaps})
            elif dead or empty:
                why = []
                if dead:
                    why.append(f"抓到了但一条没落地：{','.join(dead)}")
                if empty:
                    why.append(f"返回 0 条：{','.join(empty)}")
                degraded.append({"stream": cls_name,
                                 "reason": "该路有源无贡献（" + "；".join(why) + "）",
                                 "attempted": n_total, "succeeded": n_ok,
                                 "entries": got_n, **gaps})

    result = {
        "generated_at": now.isoformat(),
        "window_days": args.days,
        "since": since.isoformat(),
        "requested": {"source": args.source, "class": args.klass, "domain": args.domain,
                      "resolved_sources": keys},
        "relevance": rel,
        "sources_report": report,
        "source_funnel": funnel,
        "degraded_streams": degraded,
        "archive_truncated": archive_truncated,
        "sub_feed_failures": sub_feed_failures,
        "stats": {
            # `raw` counts entries BEFORE the errata drop, and each per-source row
            # in sources_report also reports pre-drop counts. Measured: an auditor
            # summing the per-source table got 418 while `raw` said 413, the gap
            # being exactly errata_dropped. Both numbers are now stated so the two
            # views reconcile instead of looking like an accounting error.
            "raw": len(entries) + errata_dropped,
            "raw_after_errata": len(entries),
            "kept": len(capped),
            "dropped_out_of_window": dropped,
            "undated": undated,
            "duplicates_merged": merged,
            "capped_per_source": overflow,
            "max_per_source": args.max_per_source,
            "trend_exempt_from_window": trend_exempt,
            "errata_dropped": errata_dropped,
            "gate_threshold": CREDIBILITY_THRESHOLD,
            "passed_both": passed_both,
            "passed_single_source": passed_single,
            "truth_only": passed_truth,
            "comm_only": passed_comm,
            "failed_gate": failed,
            "retracted_total": retracted_count,
            "external_injected": sum(r["count"] for r in report if r["source"] == "extra-json"),
            "external_dropped_out_of_window": dropped_external,
            # 宽窗口纳入数。与 dropped_out_of_window 并列 —— 它描述的是同一件事
            # 的另一个方向（放行而非丢弃），放在顶层会被读 stats 的下游漏掉。
            # 从【最终发出的集合】里数，不在中段数 —— 见窗口过滤处的说明。
            # 这样这个数字与读者实际看到的条数永远一致。
            "major_events_kept": sum(1 for e in capped if e.get("major_window")),
            # ★ 数据指纹。用途：让「这次跑批内容变了」可归因。
            #
            # 实测（同参数连跑 3 次）：25 个源里 **22 个逐条完全一致**，
            # 只有 3 个会变 —— `toutiao-hot`（实时热度榜）、`hn-frontpage`
            # （实时首页）、`36kr`（滚动 feed）。**那是数据真的在变，不是缺陷。**
            #
            # 但"变了"和"我这次拿到的和上次不一样"从报告里看不出来。
            # 有了指纹就能回答：「同样的查询，两次跑批拿到的是同一批东西吗？」
            # 变了 = 源更新了；没变 = 可复现。两种都有用，混在一起就没用。
            #
            # 只哈希标题序列，不含时间戳 —— 否则每次跑都不同、失去意义。
            "content_fingerprint": hashlib.sha256(
                "|".join(e.get("title") or "" for e in capped).encode("utf-8")
            ).hexdigest()[:16],
            # 逐条数对不上的源：不是错误，是"这些源在实时更新"。
            "volatile_sources": sorted({
                k for k, v in funnel.items()
                if v.get("fetched") and k in ("toutiao-hot", "hn-frontpage", "36kr",
                                              "reddit-ml", "hn-top", "google-trends-us")
            }),
        },
        "weights": {
            "source_weight": SOURCE_WEIGHT,
            "evidence_weight": EVIDENCE_WEIGHT,
            "cross_confirm_bonus": CROSS_CONFIRM_BONUS,
            "threshold": CREDIBILITY_THRESHOLD,
            # The social branch uses DIFFERENT tables and a different split. Both
            # are exported so an auditor can actually reproduce every score --
            # measured: exporting only the default tables made the social-domain
            # numbers impossible to recompute from the JSON.
            "social_platform_weight": PLATFORM_WEIGHT,
            "social_evidence_form": SOCIAL_EVIDENCE_FORM,
            "trend_truth_cap": TREND_TRUTH_CAP,
            "note": "source-tier: truth = 0.4*source_weight + 0.6*evidence_weight "
                    "(+bonus if corroborated). social-evidence-form / external: "
                    "truth = 0.30*social_platform_weight + 0.70*social_evidence_form "
                    "(+bonus if corroborated). trend boards: truth forced to "
                    "trend_truth_cap. All computed deterministically, not judged.",
        },
        "entries": capped,
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        # 方案A：同时写一份精简视图给【子代理/主窗口读】。
        # 实测收益：全量 320 KB → 精简 158 KB（三路合计），省约一半读入量。
        # 成本几乎全在 summary（均 1031 B/条），所以按字数截断是主要手段；
        # 另外 `weights`（1.7 KB 常量，每次重复）、`published_raw`、`also_titles`、
        # `also_event_ids` 都是可推导的冗余，直接不写。
        if args.compact:
            cpath = pathlib.Path(args.out).with_name(
                pathlib.Path(args.out).stem + "_compact.json")
            cpath.write_text(json.dumps(compact_view(result, args.compact_chars),
                                        ensure_ascii=False, indent=1),
                             encoding="utf-8")
            print(f"wrote {cpath}  (精简视图，给下游读)")
        ok = [r for r in report if r["status"] == "ok"]
        bad = [r for r in report if r["status"] != "ok"]
        print(f"wrote {args.out}")
        print(f"  sources ok   : {len(ok)}/{len(report)}")
        for r in ok:
            print(f"    [ok]   {r['source']:22} {r['count']} 条")
        for r in bad:
            print(f"    [FAIL] {r['source']:22} {r['status']} {r.get('detail','')}")

        # 逐源耗时汇总。实测需要：为了查出哪个源慢，我写了一次性外部计时脚本；
        # 而脚本自己就知道。不打印出来，下次调查还要重做一遍。
        timed = sorted((r for r in report if r.get("seconds") is not None),
                       key=lambda r: r["seconds"], reverse=True)
        if timed:
            slow = [r for r in timed if r["seconds"] >= 5.0]
            print(f"  耗时         : 合计 {sum(r['seconds'] for r in timed):.1f}s"
                  f" | 最慢 {timed[0]['source']} {timed[0]['seconds']}s"
                  + (f" | ≥5s 的源: " +
                     ", ".join(f"{r['source']}={r['seconds']}s" for r in slow)
                     if slow else ""))
        # external 的窗口丢弃数必须一起报。实测：反向测试里控制台显示 0，
        # 而 JSON 里 `external_dropped_out_of_window` 是 3 —— 控制台与 JSON 对
        # 同一件事给出不同答案，读控制台的人会以为一条没丢。
        _ext_drop = result["stats"].get("external_dropped_out_of_window") or 0
        _mj = result["stats"].get("major_events_kept") or 0
        print(f"  entries      : {result['stats']['kept']} kept / {result['stats']['raw']} raw "
              f"({dropped} out-of-window"
              # ★ 宽窗口纳入的数必须与 dropped 一起报。实测：控制台显示
              # `0 out-of-window` 而实际有 7 条是窗口外的 —— 因为它们走的是
              # 「宽窗口放行」路径，不增加 dropped。不报出来就成了反向的
              # 静默放宽：读控制台的人会以为这 7 条本来就在窗口内。
              + (f", {_mj} 条由 --major-days 从窗口外纳入" if _mj else "")
              + (f" + {_ext_drop} external out-of-window" if _ext_drop else "")
              + f", {undated} undated, "
              f"{result['stats']['duplicates_merged']} duplicates merged, "
              f"{errata_dropped} errata dropped, "
              f"{overflow} over per-source cap)")
        s = result["stats"]
        # ★ 「双项达标」必须与「单源硬证据通道」分开报。
        # 实测：两者曾共用一个计数器，于是打出「双项达标 25」而 passes_comm=0 ——
        # 那 25 条**没有一条经过独立印证**。这个数字唯一该回答的问题是
        # 「多少条真的被多家印证过」，混在一起就等于虚报。
        print(f"  权重闸门     : 阈值 {CREDIBILITY_THRESHOLD} | "
              f"双项达标（已独立印证）{s['passed_both']} | "
              f"单源硬证据（未印证）{s.get('passed_single_source', 0)} | "
              f"真实达标/传播未达标 {s['truth_only']} | "
              f"传播达标/真实未达标 {s['comm_only']} | 双项未达标 {s['failed_gate']}")
        # Read relevance keys defensively. Measured crash: the classifier branch
        # nests keywords under `keyword_advisory` instead of at the top level, so
        # `rel["keywords"]` raised KeyError -- AFTER the JSON had already been
        # written, exiting 1. Any orchestrator checking the exit code then treated
        # a successful run as a failure.
        adv = rel.get("keyword_advisory") or {}
        kws_shown = rel.get("keywords") or adv.get("keywords") or []
        missing_shown = rel.get("missing") or adv.get("missing") or []
        # 没量过就说没量过。曾经这里无条件打印 `coverage / 阈值 [OK]`，
        # 而没有关键词时 coverage 是伪 1.0、entries_checked 是 0 —— 输出宣称
        # 「关联度检查通过」，实际一个字都没检查。
        if rel.get("checked") is False or rel.get("coverage") is None:
            print(f"  关联度       : 未检查（无关键词可量）  模式={rel.get('mode')}")
            print(f"    说明      : {rel.get('note','')}")
        else:
            flag = "⚠ 需二次搜寻" if rel.get("needs_second_pass") else "OK"
            print(f"  关联度       : {rel.get('coverage')} / 阈值 {rel.get('threshold')}  "
                  f"[{flag}]  模式={rel.get('mode')}")
            if kws_shown:
                print(f"    关键词    : {', '.join(kws_shown)}"
                      + ("（仅供参考，不参与判定）" if adv else ""))
            if missing_shown:
                print(f"    未覆盖    : {', '.join(missing_shown)}")
        if retracted_count:
            print(f"  ★ 撤稿拦截   : {retracted_count} 条已被撤稿，已作废并压入存疑区")
        promo_n = sum(1 for e in capped if e.get("promotional_signal"))
        if promo_n:
            print(f"  导购/广告降分 : {promo_n} 条（真值分 -{PROMO_PENALTY}，信号可被推翻）")
        if archive_truncated:
            for k, n in archive_truncated.items():
                print(f"  大归档截断   : {k} 丢弃 {n} 条旧条目（只保留最新 {ARCHIVE_SCAN_LIMIT}）")
        if sub_feed_failures:
            for k, fails in sub_feed_failures.items():
                print(f"  ★ 版面降级   : {k} 有 {len(fails)} 个版面失败，"
                      f"本条覆盖【不完整】")
                for f_ in fails:
                    print(f"        - {f_}")
        if _mj:
            print(f"  ★ 重大事件窗口: {_mj} 条落在近 {args.days} 天之外，"
                  f"由 --major-days {args.major_days} 纳入 —— **引用时必须写明**")
        if degraded:
            print("  ★ 降级告警   :")
            for d in degraded:
                print(f"      [{d['stream']}] {d['reason']} "
                      f"（{d['succeeded']}/{d['attempted']} 源成功，{d['entries']} 条）")
            print("      → 「本次覆盖」必须写明这条降级，不许当成完整覆盖")
        if result["stats"]["kept"] == 0:
            print("  WARNING: zero entries in window. Widen --days or add sources; do NOT pad with old news.")
        if len(bad) >= max(2, len(report) // 3):
            print("  WARNING: a third or more of the sources failed. Say so in the coverage section; "
                  "do not present a partial sweep as complete.")
    else:
        print(text)
        # --compact 依赖 --out 才有地方写精简视图。之前是静默什么都不做，
        # 而文档把 --compact 列为「必加参数」—— 调用方以为拿到了精简视图，实际没有。
        if args.compact:
            print("warning: --compact 需要配合 --out 才会写精简视图；"
                  "本次没有 --out，已忽略 --compact。"
                  "（全量 JSON 走的是 stdout，没有精简版可写）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
