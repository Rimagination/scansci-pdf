---
name: scansci-pdf
description: 下载学术论文。支持 DOI、arXiv ID、关键词搜索、批量下载、Elsevier API、WebVPN/CARSI 机构访问、下载失败排障。当用户要求下载论文(单篇或批量)、搜索文献、获取引文、配置 Elsevier/ScienceDirect API、或下载遇到 Cloudflare/验证页/代理问题时使用。
---

# ScanSci PDF — 学术论文下载与检索

13+ 数据源并行竞速,首个成功立即返回。数百篇以上的清单需要先分类摸底(OA/Sci-Hub/需机构)时,转用 `scansci-sort` skill。

## 环境检查

工具列表含 `scansci_pdf_*` → 用 MCP 工具;否则 CLI 兜底,先 `scansci-pdf check` 确认依赖。

## 策略选择规则(必读)

**用户指定了来源 = 只用那个来源:**

| 用户说的是 | 策略 | 说明 |
|-----------|------|------|
| "从 Sci-Hub 下载" | `scihub_only` | 只走 Sci-Hub |
| "优先 scihub" | `scihub_first` | OA 仍竞速——OA 更快时最终来源可能是 OA |
| "只要免费合法的" | `legal_only` | 排除 Sci-Hub / LibGen |
| 没指定 | `fastest`(默认) | 全源并行竞速 |

```bash
scansci-pdf config-cmd download_strategy scihub_only
```

## 单篇下载

```bash
scansci-pdf get <DOI>                    # 零配置竞速
scansci-pdf fetch <DOI> [--output DIR]   # 7 步机构级联
```

竞速分层:Tier1 出版商直链(4s) → Tier2 OpenAlex/Unpaywall(5s) → Tier3 EuropePMC/PMC/arXiv(8s) → Tier4 Sci-Hub/LibGen 无头浏览器(25s) → Tier5 WebVPN/CARSI(20s)。国内 Tor 常失败,直接不用。

**换源重下必须清缓存**:`rm -f <out>/.doi_index.json && rm -rf ~/.scansci-pdf/cache/*`。

## 批量下载

```bash
scansci-pdf batch dois.txt --scihub --output <dir> --format json   # 灰源竞速
scansci-pdf batch dois.txt                                          # 机构级联
scansci-pdf publisher-batch f.txt --publisher elsevier
```

⚠️ **>300 篇必须分批**——校验阶段并发 validate 会 TimeoutError 崩溃,一个都不下。断点续传:重跑同文件自动跳过已完成;MCP 用相同 `batch_id`。连续 Cloudflare 拦截时停下来排障(见下),不要硬冲。

## 检索与引文

```bash
scansci-pdf search "关键词" --limit 10 --sort cited_by_count   # 13源引擎,失败降级三源
```

搜作者优先用 `--author "Dabo Guan"` 或 OpenAlex `--author-id`,别把人名放 query(全文匹配会混入同名/被引提及)。引文格式(citation: bibtex/ris/endnote)仅 MCP 支持。发现层:CLI `plan → estimate → find --out <dir>`,再 `build-queue <dir> --out queue.txt` 接 batch。OpenAlex 配额按 IP 每日计(429 就降级 Unpaywall 单点并发);Unpaywall 批量端点常 500,自写单点并发脚本更稳。

## 机构渠道(按 DOI 前缀路由,先快后慢)

1. **`10.1016`(Elsevier)→ Elsevier API**:key + 校园网 IP 即可,**无需 insttoken**,1–2 s/篇(需机构桶里常占一半以上)。
   ```bash
   scansci-pdf elsevier-setup --api-key YOUR_KEY --validate
   ```
2. **其余出版商 → WebVPN/CARSI**:`scansci-pdf schools 清华` → `setup 清华大学` → `login`(浏览器 CAS,cookie 自动保存,勿让用户手动复制)→ 正常 `get`。CARSI:`config-cmd carsi_idp_name <学校>` + `federated-login elsevier`。卡登录页 = session 过期,重新 login。

## 排障速查

| 症状 | 修复 |
|---|---|
| Sci-Hub 返回 Cloudflare/Turnstile 页 | `browser-status` 查后端;Turnstile widget 空白是 patchright 已知 bug(1.56~1.61)→ `config-cmd browser_backend cloakbrowser`,或改走机构路径 |
| cloakbrowser 内核过老 | `config-cmd browser_executable "C:\Program Files\Google\Chrome\Application\chrome.exe"` |
| 所有源超时 | 查 `network_proxy`;本机代理可能没启动,先测端口 |
| Elsevier 只回 1 页预览 | key 无效/无权限,重新 setup --validate |
| Agent 坚持要 Elsevier insttoken | 不需要:API key + 校园网出口即可;NOT_ENTITLED=未连校园网或学校未订阅,连网重试或转其他渠道 |
| MCP 无 `scansci_pdf_*` 工具 | plugin 未启用 → 走 CLI 兜底 |
| 403/超时 ≠ 无全文 | 多为反爬或网络受限,留给浏览器/代理轮 |

自写嗅探脚本时:HTTP 200 ≠ 成功(验证页也是 200),按内容分类 hit/miss/turnstile/blocked;验证页按 IP 频率随机插入,假 DOI 也会触发——重试 + 轮换域名(sci-hub.vg/al/ee 国内可达,se/ru/st 不通;LibGen 全镜像国内不可达且 scimag 与 Sci-Hub 同库,不用重复测)。
