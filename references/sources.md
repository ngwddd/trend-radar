# 源注册表与可达性实测

## 新增源之前必做两件事（都是踩过的坑）

1. **可达性实测** —— 不要凭「这家很权威」就加。
2. **新鲜度校验** —— 看**最新一条有多新**，不是看返回了多少条。

> 人民网 RSS 返回 `200` + **100 条** + 标准 XML，内容是 **2025-06** 的；
> 新华网返回 `200` + **300 条**，内容是 **2022-12** 的。
> **这比 404 危险得多** —— 404 你会知道它坏了，而这个会静默把三年前旧闻
> 当成「近 3 天」喂进管线。

## 源的三个字段别混用

| 字段 | 语义 | 取法 |
|---|---|---|
| `url` | 单个 feed | 直接用 |
| `urls` | **同内容镜像**（如 baidu-hot 的 4 个 rsshub 实例） | **竞速，取第一个非空的** |
| `feeds` | **同一家的多个不同版面**（如中新网的滚动/社会/国内/财经） | **全部并发抓取后合并去重** |

`urls` 和 `feeds` **不能混用**：实测把中新网的 4 个版面写成 `urls`，
只会拿到其中一个版面，另外三个的内容**直接丢掉**（`urls = spec.get("urls") or [spec["url"]]`）。

`feeds` 由 `kind: "rss_multi"` 驱动（`fetch_rss_multi`）。它同时返回**失败版面列表**，
并记进 `sub_feed_failures` → 控制台打 `★ 版面降级`。
**没有这个的话，「4 个版面只成功 1 个」会看起来像「这家今天内容少」** —— 同一类静默陷阱。

## 新增源必须显式登记权重

`SOURCE_WEIGHT.get(source_key, 0.50)` 有个兜底默认值。实测新加的 `chinanews`
就静默拿了 `0.50` —— **一个没人看过的默认值替我做掉了权重决定**，
而且只在反推分数时才被发现。

现在 `--list` 会打印「⚠ 未给权重的源」，测试也断言这个集合为空。
**每个源都要在 `SOURCE_WEIGHT` 里写上数字和理由。**

> **实测环境**：Windows 11 · Python 3.14.5 · **2026-10-10**
> **可达性是会变的。** 这张表是"当时测出来是这样"，不是永久事实。
> 用 `python scripts/fetch_feeds.py --list` 看脚本里当前注册的内容。

**当前状态：30 个源全部可用，11 条已知死路。**

---

## 〇、三大类（`domains` 字段）—— 比 `class` 更上层的路由

需求进来**先归到一类**，然后三路都在这一类内路由：

| 类 | `--domain` | 覆盖 | 域内源数 |
|---|---|---|---|
| **社会** | `social` | 热点新闻、公共事件、政策、人文 | 7 |
| **科学进步** | `science` | 生物医学、大刊、科学进展 | 21 |
| **AI** | `ai` | 模型、方法、基准、产业 | 15 |

```bash
python scripts/fetch_feeds.py --list                                   # 看全部三类归属
python scripts/fetch_feeds.py --domain science --class authority --days 3 --compact --out "$env:TEMP\a.json"
python scripts/fetch_feeds.py --domain ai --days 3 --compact --out "$env:TEMP\a.json"     # 整类
```

一个源可属于多类（`ars-technica` 三类都算），所以三个数加起来大于总数。

> ⚠️ **注意两个口径不同，不是笔误**：
> 本节按 **`domains`** 字段统计（一个源可属多类）；
> 下面第一节按 **`class`** 字段统计（一个源只有一个 class）。`--list` 里两处都标了。
>
> ⚠️ **`--domain` 单独用时会把热度榜一起带上**（因为 `class=trend` 的源 `domains` 也含
> `social`/`ai`），这是刻意的 —— 热度榜是阶段 2 的路 C，属于管线而不属于某一类。
> 加了 `--class` 就不会带（那时你在明确指定某一路）。

---

## 一、三路分流（`class` 字段）

`class` 决定一个源喂给哪个执行阶段：

| class | 归谁 | 含义 | 源数 |
|---|---|---|---|
| `authority` | 子代理 B（权威面） | 期刊、官方、可核查。慢但准 | 16 |
| `social` | 子代理 A（社会面） | 热度、风向、爆料、社区。快但不可信 | 9 |
| `trend` | **主窗口**（热度榜） | 按热度排序。**不是内容源，是校准工具** | 5 |

```bash
python scripts/fetch_feeds.py --class authority --domain science --days 3 --compact --out "$env:TEMP\auth.json"
python scripts/fetch_feeds.py --class social    --domain ai      --days 3 --compact --out "$env:TEMP\social.json"
python scripts/fetch_feeds.py --class trend --compact                                 --out "$env:TEMP\trend.json"
```

> **实测教训（这个 bug 很隐蔽）**：`social` **既是域名也是类名**。
> 早期版本用 `wanted = list(DOMAINS)` 再逐 token 匹配，`"social"` 会**先命中类名分支**，
> 于是 `--domain social` 静默解析成 `class=social`（9 源）而不是 `domains∋social`（7 源），
> 再被 domain 过滤砍到 **只剩 3 个源**，而且 `sources ok: 3/3` 看着完全正常。
> **修法**：`--domain` 直接走 domain 过滤，不再经过 token 匹配。修完 3 源 → 8 源。

---

## 二、权威面（`class=authority`）

| key | 来源 | 类型 | 特点与陷阱 |
|---|---|---|---|
| `arxiv-ai` | arXiv API（cs.AI / cs.LG / cs.CL / cs.CV） | Atom API | **预印本**。按 `submittedDate` 降序。连发请求会 **429**，脚本内置 3 秒间隔 + 重试 |
| `arxiv-rss-ai` | arXiv cs.AI 每日 RSS | RSS | **单日量极大**（约 1 MB / 470+ 条）。只含当日公告 |
| `openai-news` | OpenAI 官方 News | RSS | **整个归档都在这一个 feed 里**（1200+ 条），靠时间窗过滤。**官方口径 ≠ 独立验证** |
| `crossref-science` | Science `0036-8075` | API | **只有元数据 + 摘要** |
| `crossref-nature` | Nature `0028-0836` | API | 同上 |
| `crossref-sciadv` | Science Advances `2375-2548` | API | 开放获取，摘要通常可用 |
| `crossref-pnas` | PNAS `0027-8424` | API | |
| `crossref-nejm` | NEJM `0028-4793` | API | 临床顶刊；含大量通信与试验注册 |
| `crossref-lancet` | The Lancet `0140-6736` | API | **实测近 3 天常常 0 条** —— 它的在线发表不是每天都有，不是 bug |
| `crossref-cell` | Cell `0092-8674` | API | **同上，实测常 0 条** |
| `crossref-natmed` | Nature Medicine `1078-8956` | API | 生物医学 |
| `crossref-natbiotech` | Nature Biotechnology `1087-0156` | API | 生物技术；**兼 ai 域** |
| `crossref-natmi` | Nature Machine Intelligence `2522-5839` | API | **AI 域少有的同行评审源**。实测近 7 天有在线发表 |
| `crossref-natcomp` | Nature Computational Science `2662-8457` | API | 计算方法与 AI 交叉。实测近 7 天有在线发表 |
| `biorxiv` | bioRxiv 全学科 | RSS(RDF) | **预印本**。30 条/次。RDF 格式，`<link>` 是纯文本；标题常为**全大写** |
| `medrxiv` | medRxiv 临床预印本 | RSS(RDF) | **预印本**。临床结论尤其不能当已证实 |
| `techreview` | MIT Technology Review | RSS | 兼作权威与社会面；观点文多 |

**两个必须知道的 Crossref 陷阱**（都踩过）：

1. **日期字段**：必须用 `published-online`。用 `published`（期号日期）会让大刊
   **3 天窗口内一条都返回不了** —— 它们的期号日期滞后在线发表数月。
2. **勘误过滤不掉**：`Publisher Correction` / `Author Correction` 在 Crossref 里
   注册为普通 `journal-article`，服务端 `filter=type:journal-article` **完全不管用**（实测）。
   脚本只能在**客户端按标题正则**丢弃（本次运行丢掉 11 条）。

   另外 `d41586-*` 这类是 **Nature 的新闻稿**而非研究论文。
   **已修（2026-10-10 实测）**：`evidence_type_for()` 现在检查 DOI。
   12–15 条 `d41586-*` 曾拿到 `peer-reviewed` + **0.912**（与真 Science 论文并列全轮最高），
   现已标为 `journalism` → 0.690（低于研究论文，高于二手报道）。
   判据：DOI 前缀 + `is-referenced-by-count == 0` 且无摘要。
   **另一种作废的尝试**：`select` 里加 `subtype` 或 `reference-count` 会让 Crossref
   对**每个**期刊返回 HTTP 400（实测 11 个源全挂）；合法字段是 `is-referenced-by-count`。

3. **撤稿记录在 `update-to` 字段里**（这个能用，且重要）：
   ```bash
   select=...,update-to
   ```
   返回的 `update-to` 数组带 `label`（`Retraction` / `Expression of Concern` /
   `Correction` …）、`source`（`retraction-watch` 或 `publisher`）、`doi`。
   脚本据此把撤稿条目真值分强制压到 **0.05**（见 `quality-bar.md`）。

   **实测覆盖的边界**：近 7 天的窗口抓取**一条撤稿记录都没有**（0/242）——
   因为撤稿总是发生在**更早**的论文上。所以这个机制在函数级测试里验证（36 项全过），
   但在日常运行中**不会触发**。要测它必须用历史数据。

---

## 三、社会面（`class=social`）

| key | 来源 | 特点与陷阱 |
|---|---|---|
| `techreview` | MIT Technology Review | 专业媒体，介于两路之间；**观点文多，需过滤** |
| `ieee-spectrum` | IEEE Spectrum | 偏工程与产业，更新较慢 |
| `ars-technica` | Ars Technica | 科技新闻，较扎实但仍是二手 |
| `hn-frontpage` | Hacker News 首页 | **必须用 curl UA**，浏览器 UA 会被 419 |
| `hnrss` | hnrss.org 镜像 | 与 `hn-frontpage` **内容重复**，二选一 |
| `36kr` | 36氪 | **含大量软文与融资 PR** |
| `ithome` | IT之家 | 快、量大；**消费电子与汽车噪声多** |

**科学类的社会面**（二手科普报道，标题夸张倾向明显，必须回溯一手论文）：

| key | 来源 | 陷阱 |
|---|---|---|
| `phys-org` | Phys.org | 常把**预印本**写成「研究显示」 |
| `science-daily` | ScienceDaily | 标题夸张，必须回溯原文 |

> `reddit-ml` 原本在这条流里，但它与热度榜的 Reddit 源**是同一个 URL**，
> 抓两次会 429，所以只保留在 `trend` 里。**不要再往 social 加同 URL 的 key。**

---

## 四、热度榜（`class=trend`）—— 只用于校准，不用作内容

| key | 来源 | 是否真按热度排序 | 陷阱 |
|---|---|---|---|
| `baidu-hot` | 百度热搜（RSSHub 镜像，**逐个试 4 个**） | ✅ 是 | URL 是百度搜索页，不是新闻原文；含政治与娱乐 |
| `toutiao-hot` | 头条热榜官方 JSON | ✅ 是，且**带 HotValue 热度数值** | 娱乐/体育占多数 |
| `hn-top` | Hacker News 首页 | ⚠️ 近似（条目顺序） | RSS 无热度数值 |
| `reddit-ml` | r/MachineLearning `top?t=day` | ✅ 是 | Reddit 会 429 |
| `google-trends-us` | Google Trends 每日 | ✅ 是，且**带搜索量 + 原文链接 + 来源** | **榜单本体大量是人名**；有价值的是 `related_news` |

**Google Trends 的特别处理**：它的顶层 `<link>` 指向 feed 自身（没用）。
真正的信息藏在嵌套的 `<ht:news_item>` 里 —— 通用 RSS 解析器会全部丢掉。
脚本专门把这层挖出来，所以每个条目能拿到：搜索量 + 触发它上涨的那篇报道 + 报道来源。

**热度榜的铁律**：实测同一时刻，百度热搜是「宋佳集齐三大奖」，头条第 1 是 WTT 乒乓球，
Google Trends 前 10 全是人名。**这些一条都不该进核心区。**
热榜只有两个用途：**校准传播度**、**发现遗漏**。

**热榜豁免时间窗**：热榜条目没有发布日期，套时间窗会被清空。
脚本对 `class=trend` 跳过时间过滤并标 `trend_no_date: true`。

---

## 五、实测被挡（**不要再盲试**）

### 期刊：不是全都值得加

探过但**不加**的 AI 相关期刊（实测原因）：

| 目标 | ISSN | 实测 | 结论 |
|---|---|---|---|
| JMLR | `1532-4435` | Crossref **0 条记录** | **不在 Crossref 注册**，加了也没用 |
| NeurIPS proceedings | `1049-5258` | Crossref **0 条记录** | 会议论文集不走 Crossref |
| IEEE TPAMI | `0162-8828` | 12,488 条，**近 7 天在线发表 0 条** | 有记录但更新频率低，3 天窗口常空 |
| Artificial Intelligence (Elsevier) | `0004-3702` | 4,860 条，近 7 天 0 条 | 同上 |
| Patterns (Cell Press) | `2666-3899` | 995 条，近 7 天 0 条 | 同上 |

**教训**：「这个期刊很权威」不等于「它适合当源」。加源前必须查
**近 7 天在线发表条数**，否则加进去只会永远返回 0 条、看着像坏了。

### 期刊直连

| 目标 | 症状 | 原因 / 对策 |
|---|---|---|
| `nature.com/*.rss` | **HTTP 200，但返回 `text/html` 3 KB**，含 `/_fs-ch-` | Fastly bot 挡页。**最容易骗人的失败：状态码是 200**。脚本主动检查 `<!doctype html>` 并报错 |
| `science.org/rss/news_current.xml` | HTTP 403 | 直接封 → 走 Crossref |
| `cell.com/cell/current.rss` | HTTP 403 | 同上 |

### 热度榜

| 目标 | 症状 |
|---|---|
| `weibo.com/ajax/side/hotSearch` | HTTP 403 |
| `s.weibo.com/top/summary` | HTTP 200 但返回 HTML 壳，**热度值靠 JS 渲染，抓不到** |
| `zhihu.com/api/v3/feed/topstory/hot-lists/total` | HTTP 401 需授权 |
| `douyin.com/aweme/v1/web/hot/search/list/` | HTTP 200 但 **0 字节** |
| `trends.google.com/trending/rss?geo=CN` | HTTP 400（只支持部分地区） |
| `github.com/trending` | HTTP 200 但纯 HTML，无 feed |
| `rsshub.app/weibo/search/hot`、`rsshub.app/zhihu/hotlist` | HTTP 403（公共实例封禁） |

### 其它

| 目标 | 症状 |
|---|---|
| `api.semanticscholar.org` | HTTP 429 严重限流。要替代品用 **OpenAlex**（本次未测） |
| `hnrss.org` | 间歇超时（脚本已加 2 次重试） |
| `news.ycombinator.com/rss` + **浏览器 UA** | **HTTP 419**，换 `curl/8.4.0` 即 200 |

**核心教训**：`HTTP 200` 不等于成功。必须**同时校验 `Content-Type` 和内容开头**。

---

## 六、本机三个环境坑（一定会踩）

### 1. Python 的 CA 库是旧的 → SSL 验证失败

```
SSL: CERTIFICATE_VERIFY_FAILED  certificate has expired
  www.technologyreview.com  ✗
  news.ycombinator.com      ✗
```

**但证书其实没过期**（实测 technologyreview 到 2026-12-25，HN 到 2026-12-19）。

真因：Python 3.14 默认验证路径指向 **OpenSSL 自带库**
`C:\Program Files\Common Files\SSL\cert.pem`（**114** 个 CA），
而 `certifi` 在 `%APPDATA%\Python\Python314\site-packages\certifi\cacert.pem`（**121** 个 CA），
差的那几个正好是这两家站点的签发链。

**对策（脚本已内置）**：显式用 `ssl.create_default_context(cafile=certifi.where())`，
certifi 缺失时自动退回系统库。

> 这个坑最阴的地方：报「证书过期」时你会先去改系统时间 —— **但系统时间是准的**（2026-10-10）。
> 先核对**是谁在验证**，不要 `verify=False`。

### 2. 控制台是 GBK → 中文乱码

所有 CJK 输出必须 `sys.stdout.reconfigure(encoding="utf-8")`，
且**结果写文件再用 `read` 读**，不要靠控制台打印中文。

### 3. 限流：同 URL 抓两次会 429

`reddit-ml` 与「热度榜的 Reddit」是同一个 URL，先后抓取时第二次被 429。
脚本做了**进程内 URL 缓存**（按 `(url, ua)` 键），并在注册表里只登记一次。
**加新源时先检查 URL 是否与已有源重复。**

---

## 七、加新源的正确姿势

1. **先裸测**，别直接写进注册表：
   ```powershell
   $r = Invoke-WebRequest -Uri "<候选URL>" -UseBasicParsing -Headers @{ "User-Agent"="curl/8.4.0" }
   $r.StatusCode; $r.Headers["Content-Type"]; $r.Content.Substring(0,300)
   ```
2. **三看**：状态码、`Content-Type`、内容开头像不像 XML/JSON。`text/html` 一律视为被挡。
3. 在 `SOURCES` 加一项，**四个字段如实填**：`kind` / `class` / `group` / `status`，
   并在 `note` 里写清陷阱。
4. 跑 `--source <新key> --days 3`，确认条目数与日期合理。
5. **把失败也记进本文档** —— 知道哪条路不通，和知道哪条路通，一样值钱。

---

## 八、`source_funnel` —— 排查「抓到了但没落地」的唯一工具

**实测需要**：`crossref-natmi` 报「抓到 2 条、落地 0 条」，**没有任何字段解释为什么**，
而这直接决定「权威面够不够用」能否被复核。

加了分源漏斗后当场查清：

```
源                    抓取   窗口丢   被合并   超限   落地   未解释
crossref-natmi          2       2       0      0      0       0   ← 窗口外，不是 bug
crossref-nejm           1       1       0      0      0       0   ← 同上
crossref-nature        17       2       0      0     15       0
biorxiv                30       0       0     10     20       0
```

**`unaccounted` 不为 0 才是真丢了东西。** 每个源六个字段：
`fetched / dropped_window / merged_into_other / capped_out / emitted / unaccounted`。

> 顺带修掉的会计不一致：`stats.raw` 与逐源 count 之和对不上，差值恒等于 `errata_dropped`
> —— 因为勘误在 raw 统计前就被剔除，而逐源 count 是剔除前的。
> 现在同时给 `raw` 与 `raw_after_errata`，两张表能对上。

---

## 九、真实跑批抓到的坑（**只在实战中暴露，单测没覆盖**）

下面每一条都是**跑真实请求时才发现**的，都不是构造出来的场景。留着它们的原因是：
**它们都属于「输出看起来正常、但结论已经错了」那一类**，靠读代码不容易想到。

### 1. 陈旧源判据的日期粒度 → 同一个日期被判「早于自己」

**症状**：`--domain ai --class authority` 报

```
[FAIL] crossref-natmi  stale-feed
       最新条目 2026-10-07 早于窗口起点 2026-10-07
```

**两个日期是同一个。** 根因是拿 `datetime` 和 `date` 比：

```
since          = 2026-10-07 17:00   （datetime，带时刻）
条目 published  = 2026-10-07 00:00   （纯 date）
10-07T00:00 < 10-07T17:00  →  成立  →  整源被排除
```

**代价**：`crossref-natmi`（Nature Machine Intelligence）是 AI 领域少有的同行评审权威源，
本次**零覆盖**，而 `degraded_streams` 只报「有源失败」，**不会告诉你这是误杀**。

**修法**：两边都降到 `date` 粒度，且窗口边界那天算**在内**。

```python
if dated and max(dated).date() < since.date():   # 不是 max(dated) < since
```

**教训**：窗口边界永远是最容易错的地方。**比较两个时间之前，先确认粒度一致。**

### 2. 零产出源不告警 → 一个源等于不存在，但报告说「全部成功」

**症状**：路 A 报 `sources ok: 7/7`、`hnrss 20 条`，但 `source_funnel` 里

```
hnrss    fetched=20  merged=20  emitted=0     ← 抓到了，一条没落地
degraded_streams: []                          ← 却是空的
```

`hnrss` 的 20 条**全部被 `hn-frontpage` 判重合并**（两者内容本来就重复，
`--list` 的 note 里也写了"二选一即可"）。于是这一路**实际只有 6 个有效源**，
但每个字段都显示正常 —— `status=ok`、`count=20`。

**修法**：`degraded_streams` 现在会点名零产出源：

```
★ 降级告警 : [social] 该路有源零产出（抓到了但一条没落地：hnrss）（7/7 源成功，200 条）
```

**教训**：`status=ok` 和 `count>0` **都不代表这个源对结果有贡献**。
判断一个源是否真的有效，只看 `emitted`。

### 3. 窗口外的量远大于预期 —— 不是所有"抓到"都能用

同一批实测：

| 源 | 抓到 | 窗口内 | 说明 |
|---|---|---|---|
| `ieee-spectrum` | 30 | **5** | 25 条落在 3 天窗外 |
| `hnrss` | 20 | **0** | 全部判重 |
| `ithome` | 60 | 15 | 其余是手机/汽车/家电 |
| `crossref-natmi` | 2 | **0** | 最新条目恰在窗口边界 |

**`--max-per-source 15` 限制的是"落地条数"，不是"可用条数"。**
一个源抓到 30 条不代表你有 30 条素材 —— 先看 `source_funnel` 的 `emitted`。

### 4. 摘要长度极不均衡 → 直接决定「保留详细过程」能否做到

实测各源摘要长度中位数（AI 域一次跑批）：

```
ieee-spectrum   1915 字
36kr            1332
ithome           433
techreview       328
ars-technica      79   ← 只有一行
hn-frontpage       8   ← 只有标题
```

`hn-frontpage` 全部 15 条命中 `body_missing`（摘要 <30 字符），真值分被压到 0.50。
**这是设计意图（没有正文就没有可核验的主张），但意味着 HN 作为源，
对「保留详细过程/机制/数字」这个要求是结构性不达标的 —— 它只能当线索，不能当事实依据。**

### 5. 中文权威媒体**未注册** → 社会面中文素材一律走低权重通道

实测：`--domain ai --class social` 那一批 **12 条的 `comm_score` 全是 0.30**，
`pickups` 几乎全是 1。不是漏抄，是**源配置的结构性后果**：

```
已注册且权重高：caixin 0.80 · thepaper 0.78 · gov-cn 0.92   ← 一个都没在 registry 里
实际在用的：    36kr 0.48 · ithome 0.52 · hn 0.45 · reddit 0.40
```

**后果**：社会面的中文素材，传播分永远到不了 0.6 门槛，**核心区结构性为空**。

**要修就得真加源**，但加之前必须做两件事（都踩过坑）：
1. **可达性实测** —— 不要凭"这家媒体很权威"就加。
2. **新鲜度校验** —— 见第 6 条，这是最阴的一类。

### 6. 陈旧 RSS 是**静默陷阱**：返回 200 + 标准 XML + 条目数漂亮，内容是几年前的

已实测的候选源（**不要直接加**）：

| 目标 | 状态 | 实测首条日期 | 结论 |
|---|---|---|---|
| `people.com.cn/rss/politics.xml` | 200 / 100 条 | **2025-06-05** | 停更存档 |
| `people.com.cn/rss/society.xml` | 200 / 100 条 | **2025-06-05** | 停更存档 |
| `xinhuanet.com/politics/news_politics.xml` | 200 / **300 条** | **2022-12-14** | 3 年前存档 |

**这三个返回 200、返回标准 XML、条目数还很多。** 直接登记进注册表，
会把 2022/2025 年的旧闻**当成"近 3 天"喂进管线** —— 比 404 危险得多。
脚本现在有 `stale-feed` 检测兜底（见第 1 条），但**加源时仍应手工核一遍首条日期**。

**唯一验证可行的中文社会民生新增源**：

```
chinanews.com.cn/rss/scroll-news.xml
→ HTTP 200 / text/xml / 30 条 / 首条时间戳与当天一致
→ 标准 <item>/<title>/<link>/<pubDate>/<description>，description 带正文摘要
→ 域名已在 identity 别名表（→ xinhua 0.88）
```

> **判断一个源能不能加，看的是「最新一条有多新」，不是「返回了多少条」。**

