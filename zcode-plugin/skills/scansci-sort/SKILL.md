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
- 二选 **Semantic Scholar 批量**:`POST /graph/v1/paper/batch?fields=isOpenAccess,openAccessPdf&id=DOI:...`,500 篇/请求、无需邮箱、429 退避 10–20s 可续;`openAccessPdf` 直链质量不错。5645 篇 12 批 ≈3 min(2026-09 实测)。
- 兜底 **Unpaywall 单点并发**:10 并发、3 次重试、每 500 条落盘 JSON 断点续传,~17 min/4000 篇。⚠️ 批量端点 `POST /v2/dois` 经常 500,不可依赖。
- ⚠️ Unpaywall 拒绝占位邮箱(422 `Please use your own email address`,错误在响应 body):**必须传真实用户邮箱**,启动前先拿 1 条 DOI 验证;`config.py` 的默认 `scansci-pdf@example.invalid` 不可用于 Unpaywall。
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
- **灰色层默认启用**(`config` 的 `scihub_enabled` 默认 `true`),L2 嗅探是核心流程,不要跳过。仅当用户明确要求合规模式或环境(如严格代理)确实无法访问灰色源时才跳过 L2,此时保持分类值一致:`Sci-Hub有` 桶并入 `需机构权限` 并在报告标注,不要自造非标准桶名;环境恢复后应补跑 L2 把桶拆回来。
- 写回原 Excel 加两列(按 DOI 匹配):`获取分诊`(分桶值+配色)和 `已下载/编号`(人工主键,可选);输出 UTF-8 BOM CSV 报告;生成分桶文件 `oa.txt` / `scihub.txt` / `repo.txt` / `institution.txt`。用 `scripts/sort_finalize_writeback.py`。
- 课题组用户通常用自己的编号做主键(WOS 筛选表的 `Serial No.ID`):回写列填该编号、下载文件重命名加同前缀(`{前缀}{编号}_{doi规范化}.pdf`),比裸 DOI 文件名更符合他们的工作流。

## 下载路由(分类完成后移交)

| 层 | 对象 | 渠道 | 速度 | 详见 skill |
|---|---|---|---|---|
| L1 | OA-开源 | Unpaywall `best_oa_location.pdf_url` 直链 | <1s/篇 | — |
| L1.5 | OA-开源 MDPI | `scripts/sort_mdpi_res_batch.py`(mdpi-res CDN 规律直连) | ~1.5s/篇 | — |
| L2 | Sci-Hub有 | `scansci-pdf batch --scihub` | 竞速 10–30s/篇 | scansci-batch |
| L3 | 需机构∩DOI前缀`10.1016` | Elsevier API(key+校园网,无需 insttoken) | 1–2s/篇 | scansci-institution |
| L4 | 需机构其余 | WebVPN/CARSI | 10–30s/篇 | scansci-institution |
| — | 仓库有全文 | 报告里仓储直链 | <1s/篇 | — |

路由原则:**按 DOI 前缀把 L3 插到 L4 之前**(实测需机构桶 Elsevier 占 56%)。

### L1.5 MDPI CDN 直连(5645 篇清单实测 268/285=94%)

`www.mdpi.com/pdf` 对脚本 403(Cloudflare 按 TLS 指纹拦:python-ssl 必挂,curl 走代理也挂),但 CDN `mdpi-res.com` 开放且 URL 可构造:

```
https://mdpi-res.com/d_attachment/{slug}/{slug}-{vol:02d}-{art:05d}/article_deploy/{同名}[-v2|-v3|-v4].pdf
```

- **slug = 期刊全名,不是 DOI 前缀**:`su`→`sustainability`、`atmos`→`atmosphere`、`w`→`water`、`f`→`forests`(ijerph/toxics/molecules 等本来就是全名的例外)。
- **文件名里没有期号**:DOI 数字 = 卷 + 期(2位) + 文号(4–5位);卷补零到 2、文号补零到 5(`toxics10100577` → `toxics-10-00577`,`min9030139` → `minerals-09-00139`,卷 1 位的老刊要用 `vol:1` 再补零拆)。
- 直链 404 就换 `-v2/-v3/-v4` 后缀;**连打会触发临时 IP 封锁(SSL EOF,~10min 自愈)**,串行 + 0.3–0.5s 间隔,429/EOF 就冷却别硬冲。
- mdpi.com 主站(非 CDN)无论代理还是直连都可能 403 挑战页——不要依赖它,只打 mdpi-res.com。

## 关键坑速查

| 坑 | 处理 |
|---|---|
| OpenAlex 429 配额耗尽 | 切 Semantic Scholar 批量 → Unpaywall 单点并发 |
| Unpaywall 批量端点 500 / 占位邮箱 422 | 单点 + 并发 + 断点续传;真实用户邮箱(启动先用 1 条 DOI 验证) |
| Turnstile 混入探测 | 四分类 + 重试 + 轮换域名 |
| 本机代理 127.0.0.1:7890 可能未启动 | 探测前先测代理连通 |
| 代理开着但出口 IP 被 Cloudflare 记账 | **双路径预检**:同一 URL 各测直连(`curl --noproxy "*"`)/代理,分域名记住可用路径(实测同站"代理挂/直连通"与反之都存在) |
| mdpi-res SSL EOF 突发 | IP 被临时记账,冷却 ~10min 自愈;串行节流预防 |
| 无效 DOI(Crossref 404) | 以 Sci-Hub 探测结果为准归类 |
| 后台 Python"卡死"假象 | `python -u` + 日志落文件(stdout 全缓冲看不见进度) |
| Windows 批量改名 WinError 32 | 重命名步骤放在下载层全部结束后单独跑,跳过占用文件重试 |

## 交付物兼容性

- 打 zip 发给用户**不要用 `tar -a -cf x.zip`**(Windows 资源管理器判"无效",中央目录不完整);用 `python zipfile`(PDF 已压缩,`ZIP_STORED` 即可,500 文件/1.3GB 秒级)。
- CSV 报告保持 UTF-8 BOM(Excel 直开不乱码)。
- 断点续传统一 `<layer>_log.jsonl` 约定,层内脚本重启自动跳过已完成项。

## 耗时基线(4000 篇实测)

嗅探全程 ~30 min(OpenAlex 可用时);L1 ~10 min;L2 1–3 h(并发);L3 ~15 min/500 篇;L4 每篇需浏览器登录态。
