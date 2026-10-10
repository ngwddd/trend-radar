# trend-radar · 扫一下

三路情报研判 skill。用户给一个需求，产出**有出处、可核查、不注水**的简报。

**触发词：「扫一下」**（以及「扫扫」「扫一遍」「扫描一下」）。

> **已安装。** 位置 `~/.dsh/skills/trend-radar/`，DSH 会自动发现并加载。
> 源模板在 `Desktop\DeepSeek 产出\trend-radar.skill.模板\` —— 改哪边都行，
> 但改完记得同步，两处应逐字节一致（`tests\_*` 子代理产物除外）。

---

## 文件

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 入口。三大类分流、硬约束、萃取规则、边界。**DSH 加载器只读这一个** |
| `references/pipeline.md` | 八个阶段的完整执行规格 + 两个子代理的提示词模板 |
| `references/quality-bar.md` | 权重规则与依据、社会域核对系统、三大类合格线、自检清单 |
| `references/sources.md` | 30 个源的注册表、可达性实测、11 条已知死路、本机环境坑 |
| `scripts/fetch_feeds.py` | 抓取 + 权重 + 闸门。**约 1900 行，是本 skill 的核心** |
| `tests/` | 4 套测试，见下 |

---

## 八阶段管线

```
1 判断三大类（social/science/ai）+ 解析参数        [主窗口]
2 三路并行 ─ 路A 社会面 / 路B 权威面（子代理）+ 路C 热度榜（主窗口）
3 验证：三要素 + 三个分区 + 交叉核对               [主窗口]
4 精华萃取：过滤 → 按事件合并 → 文字编排 → 上限5条  [主窗口]
5 回看：先算关联度（<0.6 强制二次搜寻）→ 再补漏     [主窗口]
6 主体结论 + 横向结论                              [主窗口]
7 对照校验：两组结论互照                            [主窗口]
8 总结论 → 只输出到聊天，不落文件
```

**只有阶段 2 用子代理**，因为验证和回看依赖全局视野。
**子代理只交素材、不交结论** —— 否则阶段 3 没东西可验。

---

## 快速上手

```bash
S="scripts/fetch_feeds.py"

python $S --list                                   # 三大类 + 注册表 + 死路
python $S --domain ai --days 3 --max-per-source 12 --out "$env:TEMP\r.json"
python $S --domain social --class social --days 3 --out "$env:TEMP\r.json"
python $S --domain science --class authority --days 3 --out "$env:TEMP\r.json"
python $S --class trend --max-per-source 12 --out "$env:TEMP\r.json"

# 补充检索来的条目注入评分
python $S --domain social --class social --days 3 --extra-json "$env:TEMP\extra.json" --out "$env:TEMP\r.json"

# 阶段5 关联度复检（量萃取后那几条，不是全池）
python $S --check-json "$env:TEMP\extracted.json" --keywords 安全 --keywords 对齐
```

**结果必须写 `--out`** —— Windows 控制台是 GBK，中文会乱码。

### 选择器（三者正交）

| 选择器 | 含义 |
|---|---|
| `--domain` | 三大类之一：`social` / `science` / `ai` |
| `--class` | 哪一路：`authority` / `social` / `trend` |
| `--source` | 具体源 key 或组名 |

- `--class` **永远生效**（曾经只在配 `--source` 时生效，是个 bug）
- `--domain` 单独用时**会带上热度榜**（路C 属于管线不属于某一类）；加 `--class` 就不带
- `--domain science --class authority` 出 13 源；`--domain social --class authority` **出 0 源**（真实漏洞）

---

## 测试

```bash
python tests/test_retraction.py    # 36 项：撤稿拦截、平台识别、关联度方向性、去重
python tests/test_historical.py    # 三篇"当时共识是错的"历史新闻回溯
python tests/test_routing.py       # 验收：四项修复 + 热度榜压制 + 统计自洽 + 回归
```

`test_retraction.py` **import 实际发货的模块**，不复制评分逻辑 ——
镜像一份逻辑的测试，即使脚本坏了也会通过。

`test_routing.py` 会发起真实网络请求，**不要和其他抓取任务同时跑**（arXiv 会 429）。

---

## 六处已知限制（都已写进代码注释，不是口头承诺）

| # | 限制 | 位置 |
|---|---|---|
| 1 | **同行评审 ≠ 事实正确**。权重编码的是"编辑流程多严"，不是"结论对不对"。一篇被撤稿的 Science 论文曾拿到全测试集最高的 0.912 | `quality-bar.md` 第〇节 |
| 2 | 撤稿拦截**只在撤稿记录存在时生效**。从未被撤稿、只是没人复现的结论（LK-99、OPERA）**没有任何警告** | 同上 |
| 3 | **热度加成实际不会触发**。`in_trend` 要求同一事件同时出现在社会池和热度样本里，但两者是分开调用的 | `comm_weight_social` docstring |
| 4 | `DOMAIN_TOKENS` 是**关键词表不是分类模型**，需求措辞不同可能判错类型 | `quality-bar.md` 第〇·四节 |
| 5 | `input_n`/`output_n` 是实测的，但 `gain`/`error_prevented` 是**人工判定** —— 所以阶段权重只能用排序、不能用绝对值 | `阶段权重分析/方法说明.md` |
| 6 | **付费墙正文读不到**（Science/Cell 实测被挡），只能凭摘要，必须标「仅凭摘要」 | `SKILL.md` 边界节 |

---

## JSON 输出里的关键字段（消费这份 JSON 时看这些）

| 字段 | 含义 |
|---|---|
| `truth_score` / `comm_score` | 两个独立分数，**别合成一个** |
| `section` | 核心区 / 待观察区 / 存疑区 / 丢弃 |
| `event_id` | 事件的稳定 id，跨条目比对用它不用标题 |
| `reconciliation` | 走了哪条打分路径：`source-tier` / `social-evidence-form` / `platform-table(external)` |
| `evidence_type` | `peer-reviewed` / `official` / `journalism` / `preprint` / `secondary` / `rumor` |
| `promotional_signal` | 疑似导购/广告/客户案例，真值分已 −0.25。**是信号不是判决，可推翻** |
| `trend_capped` | 热度榜条目，真值分被强制 0.45，**一条都不进核心区** |
| `record_alert` | `retracted` / `concern` / `correction` —— 撤稿记录命中 |
| `front_matter_reason` | 为什么被判 journalism（如 `DOI 前缀 d41586-`） |
| `source_funnel` | 每个源的漏斗：`fetched / dropped_window / merged_into_other / capped_out / emitted / unaccounted` |
| `degraded_streams` | **非空 = 某一路数据不完整，「本次覆盖」必须写明** |
| `archive_truncated` | 大归档源被截断的条数 |
| `relevance.classification` | 分类判定 + 置信度 + **理由**（分类错下游发现不了，这是唯一线索） |
| `relevance.weak_validation` | `true` = 有效关键词 ≤2，覆盖度不可靠，本轮不据此触发二次搜寻 |
| `relevance.effective_keywords` | 有几个关键词真能用于关联度校验（中文整句不算） |


---

## 本机环境坑（脚本已内置修复，别拆）

| 坑 | 症状 | 修法 |
|---|---|---|
| **Python CA 库旧** | `technologyreview.com`/`news.ycombinator.com` 报 `certificate has expired`，**但证书没过期** | 强制用 `certifi`（121 CA）而非 OpenSSL 自带库（114 CA） |
| **HN 反爬** | 浏览器 UA → HTTP **419** | 该源单独用 `curl/8.4.0` |
| **arXiv 限流** | 连发请求 → 429 | 3 秒间隔 + 退避重试 |
| **Reddit 限流** | 同 URL 抓两次 → 429 | 进程内 URL 缓存 + 注册表只登记一次 |
| **热度榜无日期** | 套时间窗会被清空 | `class=trend` 豁免窗口过滤 |
| **Crossref 日期** | 用 `published` 会让大刊**一条都返回不了**（期号日期滞后数月） | 改用 `published-online` |
| **Crossref 勘误** | `Publisher Correction` 注册为普通 `journal-article`，服务端 `type:` 过滤**不管用** | 客户端按标题正则丢弃 |
| **BOM** | PowerShell `Set-Content -Encoding UTF8` 写 BOM，`json.loads` 报 "Unexpected UTF-8 BOM" | 读文件用 `utf-8-sig` |
| **控制台 GBK** | 中文乱码 | `sys.stdout.reconfigure(encoding="utf-8")` + 结果写文件 |

**最阴的一条**：`nature.com/*.rss` 返回 **HTTP 200**，但内容是 3 KB 的 `text/html`
bot 挡页。**状态码骗人** —— 所以脚本会同时校验 `Content-Type` 和内容开头。
`HTTP 200` 不等于成功。

---

## 相关但不在本目录

- `Desktop\DeepSeek 产出\阶段权重分析\` —— 阶段贡献/效率分析，**独立工具，不属于本 skill**，
  `fetch_feeds.py` 永远不会调用它
- `Desktop\DeepSeek 产出\视频背景替换_项目经验\` —— 另一个任务的经验文档
