---
name: scansci-sort
description: 大型文献清单分类摸底(嗅探优先)。当用户拿到数百篇以上的文献清单(WOS导出/Excel/DOI列表),要求分类、摸底、区分"哪些是OA开源、哪些Sci-Hub/灰色源有、哪些需机构权限"、为批量下载做路由规划时使用。先30分钟嗅探全分类,再分桶下载,不对全清单跑慢速竞速。
---

# ScanSci Sort — 大清单文献分类摸底

核心原则:**先嗅探、后下载;分类一次、路由到底**。4000 篇实测嗅探 ≈30 分钟,之后每层下载只处理属于自己的桶。

## 环境检查

工具列表有 `scansci_pdf_*` → 可用 MCP(嗅探主要靠自写并发脚本,MCP 非必需);否则用 CLI `scansci-pdf check` 确认依赖。

## 嗅探阶段(只查不载)

### 0. 数据卫生
- 去重、去空、规范化 DOI:剥 `https://doi.org/` 前缀、小写、trim;正则 `^10\.\d{4,9}/.+` 校验。
- WOS 导出约 5–6% 行缺 DOI → 提前分离单独文件,不进主流程。

### 1. OA 判定(全量,不碰灰色源)
- 首选 **OpenAlex**:50 DOI/请求、10 并发、~3 min/4000 篇。⚠️ 免费配额按 IP 每日计,共享 IP 可能 429(`$0 remaining`)。
- 兜底 **Unpaywall 单点并发**:10 并发、3 次重试、每 500 条落盘 JSON 断点续传,~17 min/4000 篇。⚠️ 批量端点 `POST /v2/dois` 经常 500,不可依赖。
- 查不到的 DOI 用 Crossref `api.crossref.org/works/{doi}` 验证:404 = 未注册(中文刊常见),**不代表 Sci-Hub 没有**。

### 2. 灰色源嗅探(只对非 OA 子集,请求量天然减半)
- Sci-Hub 三镜像轮测:`sci-hub.vg` / `sci-hub.al` / `sci-hub.ee`(国内可达性 2026-08 实测;se/ru/st/ws/wf 不通,ru 返回反爬页)。
- 15 并发、每篇 3 次重试轮换域名。**四分类**:hit(HTML 含 `(embed|iframe) src="...pdf`,命中页 ~8KB)/ miss / turnstile(含 `challenges.cloudflare.com/turnstile` 或 `Verification - Sci-Hub`,按 IP 频率随机插入,假 DOI 也会触发)/ blocked(403/503)。
- ⚠️ HTTP 200 ≠ 成功,验证页也是 200,必须看内容。
- **LibGen 不用测**:scimag 库与 Sci-Hub 同源,全部镜像国内不可达(16 域名实测)。
- 三镜像全 miss = 灰色源确认无。

### 3. 合法 OA 补捞(对 Sci-Hub miss 子集)
- **OpenAIRE**:`api.openaire.eu/search/publications?doi={doi}&format=json`,8 并发,递归提取 `webresource.url` 过滤 `doi.org`。实测 837 篇 miss 捞回 136 条记录、5 篇确定直链。
- 链接分型:`.pdf` 直链→命中;PubMed 页→NCBI elink 查 PMC(多数无);Scopus/Lens→无用;机构库 landing→GET 页面找 `.pdf` 链接。
- **DOAJ API**:`doaj.org/api/search/articles/{doi}` 补期刊官网直链。
- 403/超时 ≠ 无全文(反爬/网络),标注"记录存在但连通受限"。

### 4. 分类落库
- 分类值:`OA-开源` / `Sci-Hub有` / `仓库有全文` / `需机构权限` / `缺DOI`。
- 写回原 Excel 加一列(按 DOI 匹配);输出 UTF-8 BOM CSV 报告;生成分桶文件 `oa.txt` / `scihub.txt` / `repo.txt` / `institution.txt`。

## 下载路由(分类完成后移交)

| 层 | 对象 | 渠道 | 速度 | 详见 skill |
|---|---|---|---|---|
| L1 | OA-开源 | Unpaywall `best_oa_location.pdf_url` 直链 | <1s/篇 | — |
| L2 | Sci-Hub有 | `scansci-pdf batch --scihub` | 竞速 10–30s/篇 | scansci-batch |
| L3 | 需机构∩DOI前缀`10.1016` | Elsevier API(key+校园网,无需 insttoken) | 1–2s/篇 | scansci-institution |
| L4 | 需机构其余 | WebVPN/CARSI | 10–30s/篇 | scansci-institution |
| — | 仓库有全文 | 报告里仓储直链 | <1s/篇 | — |

路由原则:**按 DOI 前缀把 L3 插到 L4 之前**(实测需机构桶 Elsevier 占 56%)。

## 关键坑速查

| 坑 | 处理 |
|---|---|
| OpenAlex 429 配额耗尽 | 切 Unpaywall 单点并发 |
| Unpaywall 批量端点 500 | 单点 + 并发 + 断点续传 |
| Turnstile 混入探测 | 四分类 + 重试 + 轮换域名 |
| 本机代理 127.0.0.1:7890 可能未启动 | 探测前先测代理连通 |
| 无效 DOI(Crossref 404) | 以 Sci-Hub 探测结果为准归类 |

## 耗时基线(4000 篇实测)

嗅探全程 ~30 min(OpenAlex 可用时);L1 ~10 min;L2 1–3 h(并发);L3 ~15 min/500 篇;L4 每篇需浏览器登录态。
