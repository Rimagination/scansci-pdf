---
name: scansci-pdf
description: >
  Use this skill whenever the user wants to download academic papers, search for research literature,
  get citations (BibTeX/RIS/EndNote), manage WebVPN institutional proxy for paper access,
  import .bib files, or batch-download papers. This skill orchestrates the scansci-pdf MCP server
  which exposes 18 high-level tools covering download (OA/grey/institutional channels),
  discovery, queue preparation, diagnostics, and login.
  TRIGGER when: user mentions downloading papers, DOI, arXiv ID, Sci-Hub, paper search,
  literature review, citation export, WebVPN, institutional access, "帮我下载论文", "搜索文献",
  "批量下载", "论文下载", "文献检索", or provides a list of DOIs/arXiv IDs.
  SKIP: user is only discussing papers conceptually without intent to download/search/cite,
  or user asks about non-academic PDFs (invoices, reports, etc.).
---

# scansci-pdf — 学术论文下载 MCP 服务


## ⚠️ 修改反爬/嗅探/镜像相关代码前必读

先读 [`docs/PLAYBOOK.md`](../docs/PLAYBOOK.md)：镜像健康唯一存储（domain_db wall_state 表）、
结构性失败签名表、节奏参数、已知死镜像清单、以及六条铁律（含"造轮子前先 grep 家底"——
镜像健康存储曾被重复造过三代）。
## 概述

scansci-pdf 是一个 MCP 服务器，提供 **20 个高层工具**，覆盖学术论文的搜索、下载（开放获取 / 灰色源 / 机构渠道）、Sciverse 语义检索、发现与队列准备、引文导出和诊断排障。下载引擎支持 20+ 数据源并行竞速、100+ 中国高校 WebVPN。

> 工具名可能带宿主的 MCP 命名空间前缀（`mcp__<server>__<tool>`）；以当前工具列表中实际注册的名称为准。

## MCP 工具参考（20 个）

### 下载

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| `scansci_pdf_download` | 单篇下载（OA/灰色源/机构级联） | `identifier`（必需）、`strategy`（可覆盖全局策略）、`markdown`/`bibtex`/`download_si`（可选） |
| `scansci_pdf_batch_download` | 批量下载 | `identifiers`（必需）、`scihub_enabled`、`batch_id`（断点续传）、`resume`（默认 true） |
| `scansci_pdf_cache_clear` | 清理下载缓存 | `identifier`（可选，省略清全部） |

**identifier 格式**：DOI（`10.1038/nature12373`）、DOI URL、arXiv ID（`2301.00001`）。

**下载策略（`download_strategy`，全局配置或单次 `strategy=` 覆盖）**：

| 策略 | 含义 |
|------|------|
| `fastest`（默认） | 全部源并行竞速（含灰色源，若 `scihub_enabled=true`） |
| `oa_first` | OA 源优先，失败后回退灰色源 |
| `scihub_first` | 灰色源优先，失败后回退合法源 |
| `scihub_only` | 仅 Sci-Hub |
| `legal_only` | 仅合法源（Unpaywall/出版商/OpenAIRE 等），不碰 Sci-Hub/LibGen |

**来源授权不可被调度扩张**：用户显式设置 `scihub_enabled=false` 或 `legal_only` 时，任何批量/车道调度都不会重新启用灰色源；只有显式 `--scihub`/`scihub_enabled=true` 或全局策略允许时才启用。

### 发现与队列准备

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| `scansci_pdf_find` | ScanSci Find 引擎：`action=plan\|estimate\|smoke\|calibrate` 系统综述搜索协议 | `action`、`query`、`domain`、`depth`、`sample_size` |
| `scansci_pdf_expand_citations` | 引文追踪（Semantic Scholar/OpenCitations） | `query`、`rounds`（≤5）、`citation_source` |
| `scansci_pdf_prepare_queue` | 准备下载队列：`action=verify\|resolve_oa\|build\|full` | `candidates_json`、`query`、`limit` |
| `scansci_pdf_search` | 关键词/作者搜索（OpenAlex；中文并入 NCPSSD；配置 Key 后并入 Sciverse） | `query`/`author`/`author_id`、`limit`、`year_from`、`year_to`、`sort` |
| `scansci_pdf_sciverse` | Sciverse 全能力分发：`search`（agentic 片段）/ `content`（读全文）/ `meta_search`（元数据）/ `relations`（引文图谱）/ `catalog`（字段目录）/ `resource`（图表附件） | `query`、`doc_id`、`unique_id`、`relation`、`top_k`、`page`/`page_size`、`offset`/`limit`、`file_name`、`filters_json`、`sort_json`、`fields`、`enrich_dois`、年份/语言/期刊/主题/作者/DOI/引用数字段 |
| `scansci_pdf_parse_list` | 解析论文列表文件（APA/BibTeX/DOI 列表/表格） | `file_path`（必需） |
| `scansci_pdf_citation` | 导出引文 | `identifier`（必需）、`format`（bibtex/ris/endnote/metadata） |
| `scansci_pdf_zotero_push` | 推送已下载论文到 Zotero | `identifier`（必需，需先下载） |

**重要**：`verify`/`resolve_oa` 是轻量操作，预算 45 秒内返回；超时会返回结构化错误（`stage`/`timeout_seconds`/`retryable`），不会无反馈地等 180 秒。`resolve_oa` 依赖 Unpaywall 邮箱配置（`scansci_pdf_config(key='email', ...)`），缺失时直接返回 `blocked_configuration` 而不是逐条失败。

**Sciverse 能力**：配置 `sciverse_api_key`（https://sciverse.space/tokens）后，`scansci_pdf_search` 会自动并入 agentic-search 证据片段和 meta-search 元数据（记录带 `doc_id`/`unique_id`/`evidence`/页码）；需要原始 chunk、精确元数据、引文图谱或图表时用 `scansci_pdf_sciverse`。**分场景**：统一 `search` 默认补 DOI（多 1 次 doc_id 定向 meta-search）；单独 `action="search"` 默认不补，需要下载时传 `enrich_dois=true`。`action="relations"` 的 `REFERENCES` 可能只返回 OpenAlex ID、标题为空；实测这些 ID 在 OpenAlex 不可解析，保留 ID 由调用方处理。`meta-search` 的 `total_count` 到 10000 是上限，返回 `total_count_capped=true`。agentic 的 `filters_json` 是对象（字段间 AND、同字段列表内 OR，`doc_ids` 硬范围 ≤1000）；meta-search 的 `filters_json` 是 `{field, operator, value}` 数组，算子用 `FILTER_OP_*`。`sciverse_meta_search_enabled=false` 可省掉每次 search 的第二路 Sciverse 配额（同时关闭统一 search 的 DOI 补全）。

### 配置、通道与诊断

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| `scansci_pdf_config` | 读取/设置配置（值会脱敏） | `key`（可选）、`value`（可选） |
| `scansci_pdf_channel_status` | 机构渠道状态：`kind=webvpn\|carsi\|ezproxy\|browser\|webvpn_test` | `kind`、`doi`（仅测试） |
| `scansci_pdf_schools` | 搜索/设置 WebVPN 高校 | `action=search\|set`、`query`、`school` |
| `scansci_pdf_diagnostics` | 健康/网络/来源/设置诊断 | `check=health\|network\|sources\|setup` |
| `scansci_pdf_tor` | 内嵌 Tor：`action=install\|start\|stop` | `action`、`use_bridges` |

### 登录（机构渠道）

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| `scansci_pdf_login` | 统一登录：`kind=publisher\|webvpn\|carsi\|ezproxy\|custom\|cookie_import` | `kind`（默认 publisher）、`identifier`、`publisher`、`custom_url`、`cookie_file` |
| `scansci_pdf_elsevier_setup` | Elsevier API Key 配置向导（免费，无需机构邮箱） | `test`（验证 key） |
| `scansci_pdf_springer_setup` | Springer TDM 全文 API Key 配置与权益探测 | `test`（验证 key / 机构订阅） |
| `scansci_pdf_sciverse_setup` | Sciverse API Token 配置向导（打开控制台、保存并验证；启用语义证据/元数据/引文图谱/图表） | `token`、`test` |

## 工作流编排

### 流程 1：模糊研究查询 → 下载

```
1. scansci_pdf_find(action="plan"|"estimate", query="植物功能性状 气候变化")
   或 scansci_pdf_search(query="plant functional traits climate change", limit=20, sort="cited_by_count")
2. scansci_pdf_prepare_queue(action="full", candidates_json=<candidates>)
   → 验证 DOI + 解析 OA 位置（45s 预算，超时给出结构化错误）
3. scansci_pdf_batch_download(identifiers=[...])
```

**关键点**：搜索后必须让用户确认，不要自动下载所有结果。

### 流程 2：论文列表全文下载

```
1. scansci_pdf_parse_list(file_path="papers.md") → 查看解析结果
2. scansci_pdf_batch_download(identifiers=entries)（或 resolve_and_download 的等价组合）
```

### 流程 2.5：先定机构认证方式与浏览器（流程 3/4/5 的前置判断）

机构失败大多不是"下不下来"，而是这两件事没定：

**① 浏览器**：`scansci_pdf_login` 打开的是工具自带的可见反检测浏览器，**cookie 是从那个上下文抓取并落盘的**——用户在自带的 Safari/Edge/日常 Chrome 里登录、或用无头模式，登录态都拿不到。要求：`browser_headless=false`（机构登录/CAPTCHA/Cloudflare 都需要可见窗口）；后端用 `camoufox` 或 `cloakbrowser`（`patchright` 1.56~1.61 有 Turnstile 空白页 bug）。用户坚持用自己的浏览器时不必硬拦，两条兜底：`config-cmd browser_executable "<他的 Chrome 路径>"` 把内核指过去；或 `scansci_pdf_login(kind="cookie_import", cookie_file="cookies.txt")` 导入 Netscape cookie（CARSI 另会自动读本机 Chrome 的 cookie 库）。

**② 出口 IP**：出版商（尤其 Elsevier）按请求源 IP 判权益。机器一旦开系统/全局代理，**工具与浏览器的出口就变成代理出口，机构授权当场失效**——典型症状是 `pdf.sciencedirectassets.com` 返回 "There was a problem providing the content you requested"（带 `CPE` 码，页面上会打印它看到的 IP）。**先拿那个 IP 核对是否属于机构网段，再判断"学校没订阅"。** 工具只认 `network_proxy`/`SCANSCI_PDF_PROXY`，**忽略** `HTTP_PROXY`/`HTTPS_PROXY`。

| 你的情况 | 走哪条 | 怎么配 |
|---|---|---|
| 在校内（IP 认证） | 直连 | `is_campus_network=true` |
| 校外 + 出版商支持联邦认证 | **CARSI 优先于 WebVPN** | `config-cmd carsi_idp_name <学校>` |
| 校外 + 学校有 WebVPN 网关 | WebVPN | 流程 3 |
| 学校只给 EasyConnect/aTrust | SOCKS5 连接器 | `network_proxy=socks5://…`；自签名证书需 `institutional_allow_self_signed` |
| 学校给图书馆代理 | EZProxy | `ezproxy_login_url`（前缀式 / 域名重写式） |
| 有 Elsevier/Springer key | API 车道，1–2 s/篇且无需浏览器 | 流程 5 / Springer TDM |

**不要替用户猜学校名**——`scansci_pdf_schools(action="search")` 让他确认；项目策略明确"不要默认任何学校"。

### 流程 3：WebVPN 机构代理

```
1. scansci_pdf_schools(action="search", query="清华") → 找到学校
2. scansci_pdf_schools(action="set", school="清华大学")
3. scansci_pdf_login(kind="webvpn") → 浏览器 CAS 认证
4. scansci_pdf_channel_status(kind="webvpn_test", doi="10.1038/nature12373") → 确认连通
5. scansci_pdf_download(identifier="...") → 自动走 WebVPN 渠道
```

### 流程 4：付费论文登录下载（出版社 SSO）

当下载返回 `error_type="paywall"` 和 `action="login_required"` 时：

```
1. scansci_pdf_download(identifier="10.1126/science.aec6396")
   → 返回 {"error_type": "paywall", "action": "login_required", ...}
2. scansci_pdf_login(identifier="10.1126/science.aec6396")  # kind=publisher 默认
   → 打开浏览器到论文页 → 用户点 "Access through your institution" → 选择机构 → SSO → 关闭浏览器
3. scansci_pdf_download(identifier="10.1126/science.aec6396") → 用已保存 cookies 成功下载
```

**要点**：
- 任何有机构账号的用户都能用，无需预配置 WebVPN/CARSI
- Cookies 持久化，同一出版商登录一次即可
- 浏览器引擎为 **CloakBrowser**（Playwright 兼容反检测浏览器），能通过 Cloudflare Turnstile；出版社检测 TLS 指纹，Python HTTP 客户端即使带 cookies 也可能 403

### 流程 5：Elsevier API 快速通道（1-2 秒下载）

```
1. scansci_pdf_elsevier_setup → 浏览器注册指引 → 复制 API Key
2. scansci_pdf_config(key="elsevier_api_key", value="...")
3. scansci_pdf_elsevier_setup(test=true) → 验证
4. 后续 10.1016/ 开头 DOI 自动走 API 直接下载（无需 insttoken：校园网出口 + API key 即可）
```

**NOT_ENTITLED 含义**：未连校园网或学校未订阅该刊，不是缺 insttoken。

### 流程 6：Sciverse 语义检索与元数据（可选，推荐）

```
1. scansci_pdf_sciverse_setup → 打开 https://sciverse.space/tokens，创建 API Token
2. scansci_pdf_sciverse_setup(token="...", test=true) → 保存并验证
3. scansci_pdf_search(...) → 自动并入语义证据片段 + meta-search 元数据
4. 需要原始片段/元数据/引文图谱/图表：scansci_pdf_sciverse(action="search|meta_search|relations|resource", ...)
```

**要点**：不配置不影响现有检索与下载；检索时若未配置，结果会带 `config_hint` 提示。每次 search 默认跑 agentic + meta 两路，配额紧张时 `scansci_pdf_config(key="sciverse_meta_search_enabled", value="false")`。

### 流程 7：内嵌 Tor（灰色源被墙时）

```
1. scansci_pdf_tor(action="install")   # 首次下载 Tor Expert Bundle
2. scansci_pdf_tor(action="start")     # 受限网络可 use_bridges=true
3. scansci_pdf_download(identifier="...", strategy="scihub_only")
```

### 流程 7：某学者的 TOP-N 被引论文

```
1. 解析作者 ID（不要用 author=姓名 直接搜，见「已知坑位」）
   GET https://api.openalex.org/authors?search=<姓名>
   → 取被引最高、同被引再看 works_count 最大的那条，核对 last_known_institutions 是否含目标单位
2. 取代表作（务必加 type:article，否则数据集/paratext 会混进榜）
   GET https://api.openalex.org/works?filter=author.id:<ID>,type:article
       &sort=cited_by_count:desc&per_page=25
3. scansci_pdf_batch_download(identifiers=[DOI...], output_dir=<宿主已授权的目录>)
4. 校验 + 去重（下载器会为每篇留下多份源标记副本，见「已知坑位」）
```

## 能力边界

| 请求 | 处理方式 |
|------|----------|
| 阅读/理解论文内容 | 不支持——只下载 PDF（可用 `markdown=true` 得 AI 可读文本层） |
| 翻译论文 | 需要其他工具 |
| 生成文献综述/摘要 | 需要 LLM 读取 PDF 后生成 |
| 下载非学术 PDF | 不支持 |
| 用户给了标题没有 DOI | 先 `search` 获取 DOI 再下载 |
| 批量 100+ 篇 | `batch_download`，并发数由 `batch_workers` 配置控制 |
| 需要机构权限的论文 | `login` 登录后重试下载 |
| 下载失败 | 结果含 `error_type`；`paywall`→登录流程，`rate_limited`→稍后重试，`cloudflare_blocked`→启动 CloakBrowser/配置代理 |

## 已知坑位（2026-10-22 实测，agent 必读）

- **`search` 的 `author=` 曾经会命中同名假档案（2026-10-22 已修）**。传 `author="Shilong Piao"` 时旧版只返回 1 条 figshare 数据集，命中 `A5072538516`（1 篇 / 0 被引）：权威档案 `A5000033522`（999 篇 / 125 224 被引）因为带中间名缩写 `L.` 不算"精确匹配"而被排除，`max()` 于是在假档案里选。现已改为名字 token 兼容（容忍中间名缩写、紧凑缩写 `L.Piao`、姓在前；Jr/Sr/III/IV 不同会被拒绝）后取最大，并在 `_author_match` 里返回 `exact`、`fallback` 与 `alternatives`。**旧版仍会选错**，此时先解析 `author_id` 再用 `author_id=` 调；看到 `exact=false`、`fallback=true` 或 `alternatives` 非空时，核对一下选中的是不是目标学者。
- **`batch_download` 的 `failed` 计数曾经会骗人（2026-10-22 已修）**。车道模式下重试成功的 DOI 会留两行（先 failed 后 success），旧版按行计数，于是 `total` 大于去重条数（实测 24 > 20）、`failed: 4` 与 `succeeded: 20` 同时出现。现已由 `server._merge_lane_results` 按论文合并（成功覆盖失败，新增 `retried` 计数）。**旧版或其它宿主的返回仍可能这样**，此时以 `failed_dois` 和每条的 `success` 为准。
- **每篇会落 2–4 个源标记副本**。除自动改名的 `Piao2009_Carbon.pdf` 外，还可能留 `10.xxxx_Sci-Hub.pdf`、`_CrossrefPage.pdf`、`_NatureDirect.pdf`、`_PublisherDirect.pdf`、`_OpenAIRE.pdf`；其中一份与最终改名文件逐字节相同（实测 20 篇留下 34 个多余副本）。**交付前必须去重**，否则文件夹里全是同一篇的多份副本。
- **`output_dir` 不要指向宿主运行时新建的子目录**。在 DSH 这类沙箱宿主里，由非沙箱进程（MCP server 自己）创建的子目录拿不到沙箱写权限：`write` 工具能写，但 `pwsh` 之后在同一目录里新建或删除任何文件都报 Access denied，把"删几个副本"拖成一轮权限排查。**先让宿主在自己的目录里建好目标文件夹，再下到那里**。
- **校验用轻量手段**：大小 ≥ `min_pdf_size_bytes`（默认 10 000）+ 头 5 字节 `%PDF-` 即可判定；要页数就用 `pymupdf` 一次跑完。实测 `_check.py` 第一版把 `doc.close()` 写在 `doc.page_count` 之前，20 篇全报 "document closed"，白跑一轮。
- **非正式版本要点出来**：机构库存档稿（"Accepted Article"、"archived peer-reviewed author-version"）与 early-online-release 都是合法 PDF，但页数和排版与正式版差别很大（实测 71 页 vs 正式版约 20 页），交付时要说明版本性质。
- **合作者不等于通讯作者**：OpenAlex 按署名计入，大团队论文（Crowther / Luyssaert / Janssens 领衔）会出现在"某人的高被引"列表里；报告时要区分本人主导与合作署名。

## 常见边界情况

- 科学符号（版权 ©、重音姓名）在 Markdown 导出中可能有质量告警（`markdown_warnings`）——文本可读，罕见字符需人工核对。
- 批量任务无论成功、失败或超时都会在预算内退出并写 `batch_results.json`（原子写入）；挂住的源会被放弃而不是拖住整批。
- `scan_find` 未安装时，`find`/`search`/`expand_citations` 会给出安装指引；`verify`/`resolve_oa` 超时返回结构化错误。

## 环境安装引导

```
1. scansci_pdf_diagnostics(check="setup") → 环境状态
2. 按 readiness 处理：ready / partial / limited
3. 缺少组件时按建议安装（pymupdf4llm 用于 markdown、cloakbrowser 用于反爬登录）
```

## 快速安装

```
pip install -U scansci-pdf
pip install -U cloakbrowser    # 反爬浏览器（Chromium 内核）
scansci-pdf doctor             # 或 MCP: scansci_pdf_diagnostics(check="health")
```