---
name: scansci-pdf
description: 下载学术论文。支持 DOI、arXiv ID、关键词搜索、批量下载、WebVPN/CARSI 机构访问。当用户要求下载论文、搜索文献、获取引文时使用。
---

# ScanSci PDF — 学术论文下载

MCP 工具前缀：`scansci_pdf_`

## 下载单篇论文

```
scansci_pdf_download(identifier="10.1038/nature12373")
```

参数：
- `identifier`：DOI（如 `10.1038/nature12373`）、DOI URL、或 arXiv ID（如 `2301.00001`）
- `strategy`：`fastest`（默认）、`oa_first`、`scihub_only`、`legal_only`
- `use_tor`：通过 Tor 代理访问（需先启动 Tor）
- `use_vpnsci`：通过 WebVPN 机构代理访问（需先登录）
- `bibtex`：同时返回 BibTeX 引文

## 批量下载

```
scansci_pdf_batch_download(identifiers=["10.1038/a", "10.1016/b", "2301.00001"])
```

支持断点续传（相同 batch_id 自动跳过已完成项）。

## 从文件下载

```
scansci_pdf_resolve_and_download(file_path="/path/to/papers.md")
```

解析 APA/BibTeX/DOI 列表 → 补全缺失 DOI → 批量下载。

## 搜索论文

```
scansci_pdf_search(query="CRISPR gene therapy", limit=10, year_from=2020)
```

## 获取引文

```
scansci_pdf_citation(identifier="10.1038/nature12373", format="bibtex")
```

format 可选：`bibtex`、`ris`、`endnote`

## 推送到 Zotero

```
scansci_pdf_zotero_push(identifier="10.1038/nature12373")
```

要求：该论文已被下载到本地缓存。

---

## WebVPN 高校代理（付费文献）

当论文无法通过 OA 源获取时，使用用户所在高校的 WebVPN 访问。

### 设置流程（按顺序调用）

```
第1步：scansci_pdf_vpnsci_set_school(school="清华大学")
第2步：scansci_pdf_vpnsci_login          → 打开浏览器 CAS 登录
第3步：scansci_pdf_vpnsci_status          → 确认 session_valid=true
第4步：scansci_pdf_download(identifier="...", use_vpnsci=true)
```

### 关键规则

- **不要**让用户手动打开 URL、复制 cookie、检查浏览器存储。工具自动处理一切。
- `vpnsci_login` 会打开浏览器，告诉用户在浏览器中完成登录，然后等待工具返回。
- 如果 session 过期，重新调用 `vpnsci_login`。
- 搜索大学：`scansci_pdf_vpnsci_schools(query="北京")`

---

## CARSI 联邦认证

直接通过出版商的机构登录，无需 WebVPN 中转。

```
scansci_pdf_config_set(key="carsi_enabled", value="true")
scansci_pdf_config_set(key="carsi_idp_name", value="中国海洋大学")
scansci_pdf_download(identifier="...", use_vpnsci=true)  # CARSI 自动生效
```

---

## Tor 匿名代理

用于 Sci-Hub/LibGen 被封锁的网络环境。

```
scansci_pdf_tor_install     → 首次安装 Tor（~30MB）
scansci_pdf_tor_start       → 启动 SOCKS5 代理
scansci_pdf_tor_start(use_bridges=true)  → 网络受限时用桥接
scansci_pdf_download(identifier="...", use_tor=true)
```

---

## 诊断与配置

```
scansci_pdf_health_check(detailed=true)   → 检查所有数据源状态
scansci_pdf_source_scores                 → 查看各下载源健康评分
scansci_pdf_network_diagnose              → 网络诊断报告
scansci_pdf_config_get                    → 查看当前配置
scansci_pdf_config_set(key="scihub_enabled", value="true")
scansci_pdf_cache_clear                   → 清除下载缓存
```

---

## 常见场景

| 场景 | 操作 |
|------|------|
| 下载一篇 OA 论文 | `scansci_pdf_download(identifier="DOI")` |
| 下载付费论文（有高校账号） | 先设学校+登录，再 `download(use_vpnsci=true)` |
| 网络受限，Sci-Hub 不通 | `download(use_tor=true)` 或 `strategy=legal_only` |
| 批量下载 .bib 文件 | `scansci_pdf_import_bib(bib_file="refs.bib")` |
| 下载失败了 | `scansci_pdf_network_diagnose` 查看原因 |
