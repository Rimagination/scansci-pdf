---
name: scansci-pdf
description: >
  Use this skill whenever the user wants to download academic papers, search for research literature,
  get citations (BibTeX/RIS/EndNote), manage WebVPN institutional proxy for paper access,
  import .bib files, or batch-download papers. This skill orchestrates the scansci-pdf MCP server
  which exposes 17 high-level tools covering download (OA/grey/institutional channels),
  discovery, queue preparation, diagnostics, and login.
  TRIGGER when: user mentions downloading papers, DOI, arXiv ID, Sci-Hub, paper search,
  literature review, citation export, WebVPN, institutional access, "帮我下载论文", "搜索文献",
  "批量下载", "论文下载", "文献检索", or provides a list of DOIs/arXiv IDs.
  SKIP: user is only discussing papers conceptually without intent to download/search/cite,
  or user asks about non-academic PDFs (invoices, reports, etc.).
---

# scansci-pdf — 学术论文下载 MCP 服务

## 概述

scansci-pdf 是一个 MCP 服务器，提供 **17 个高层工具**，覆盖学术论文的搜索、下载（开放获取 / 灰色源 / 机构渠道）、发现与队列准备、引文导出和诊断排障。下载引擎支持 13+ 数据源并行竞速、100+ 中国高校 WebVPN。

## MCP 工具参考（17 个）

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
| `scansci_pdf_search` | 关键词/作者搜索（OpenAlex） | `query`/`author`/`author_id`、`limit`、`year_from`、`year_to`、`sort` |
| `scansci_pdf_parse_list` | 解析论文列表文件（APA/BibTeX/DOI 列表/表格） | `file_path`（必需） |
| `scansci_pdf_citation` | 导出引文 | `identifier`（必需）、`format`（bibtex/ris/endnote/metadata） |
| `scansci_pdf_zotero_push` | 推送已下载论文到 Zotero | `identifier`（必需，需先下载） |

**重要**：`verify`/`resolve_oa` 是轻量操作，预算 45 秒内返回；超时会返回结构化错误（`stage`/`timeout_seconds`/`retryable`），不会无反馈地等 180 秒。`resolve_oa` 依赖 Unpaywall 邮箱配置（`scansci_pdf_config(key='email', ...)`），缺失时直接返回 `blocked_configuration` 而不是逐条失败。

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

### 流程 6：内嵌 Tor（灰色源被墙时）

```
1. scansci_pdf_tor(action="install")   # 首次下载 Tor Expert Bundle
2. scansci_pdf_tor(action="start")     # 受限网络可 use_bridges=true
3. scansci_pdf_download(identifier="...", strategy="scihub_only")
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