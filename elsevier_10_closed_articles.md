# Elsevier 10 篇闭源文章测试清单

生成时间：2026-06-20 22:42

测试逻辑：`Article Retrieval API ?view=FULL` -> 从 XML 解析 PDF `attachment-eid` -> `Content Object API /content/object/eid/{eid}` 下载出版社正式 PDF。

本次修复后的路由策略：Elsevier API 优先走 `direct`，让规则 VPN / 校园网 IP 生效；如果 direct 不通或无授权，再回退到项目配置的 `network_proxy`。

稳定指南：`docs/elsevier-api-fulltext-guide.md`

测试报告：`C:\Users\Liang\.scansci-pdf\papers\elsevier_api_rule_vpn_direct_first_20260620_224223\summary.json`

PDF 目录：`C:\Users\Liang\.scansci-pdf\papers\elsevier_api_rule_vpn_direct_first_20260620_224223`

| # | DOI | PDF object EID | 结果 | 页数 | 大小 | 本地 PDF |
|---:|---|---|---|---:|---:|---|
| 1 | `10.1016/j.jmst.2025.08.030` | `1-s2.0-S1005030225008977-main.pdf` | 成功 | 16 | 9,916,588 bytes | `01_10.1016_j.jmst.2025.08.030.pdf` |
| 2 | `10.1016/j.jmst.2025.08.056` | `1-s2.0-S1005030225009569-main.pdf` | 成功 | 10 | 3,712,591 bytes | `02_10.1016_j.jmst.2025.08.056.pdf` |
| 3 | `10.1016/j.seppur.2026.137141` | `1-s2.0-S1383586626004077-main.pdf` | 成功 | 13 | 4,627,162 bytes | `03_10.1016_j.seppur.2026.137141.pdf` |
| 4 | `10.1016/j.molstruc.2026.145682` | `1-s2.0-S0022286026004485-main.pdf` | 成功 | 8 | 4,666,334 bytes | `04_10.1016_j.molstruc.2026.145682.pdf` |
| 5 | `10.1016/j.physio.2025.101870` | `1-s2.0-S0031940625004080-main.pdf` | 成功 | 13 | 6,483,372 bytes | `05_10.1016_j.physio.2025.101870.pdf` |
| 6 | `10.1016/j.brainres.2026.150220` | `1-s2.0-S0006899326000788-main.pdf` | 成功 | 11 | 4,884,187 bytes | `06_10.1016_j.brainres.2026.150220.pdf` |
| 7 | `10.1016/j.neunet.2026.108539` | `1-s2.0-S089360802600002X-main.pdf` | 成功 | 17 | 3,129,251 bytes | `07_10.1016_j.neunet.2026.108539.pdf` |
| 8 | `10.1016/j.neunet.2026.108564` | `1-s2.0-S0893608026000274-main.pdf` | 成功 | 27 | 16,050,377 bytes | `08_10.1016_j.neunet.2026.108564.pdf` |
| 9 | `10.1016/j.neunet.2026.108617` | `1-s2.0-S0893608026000791-main.pdf` | 成功 | 15 | 4,211,724 bytes | `09_10.1016_j.neunet.2026.108617.pdf` |
| 10 | `10.1016/j.neunet.2026.108582` | `1-s2.0-S0893608026000456-main.pdf` | 成功 | 17 | 4,048,480 bytes | `10_10.1016_j.neunet.2026.108582.pdf` |

结论：10 篇全部跑通。失败根因不是 Elsevier object/eid 流程不可行，而是之前项目强制走了配置代理出口；该出口被 Elsevier 判定为 `NOT_ENTITLED`。规则 VPN 后，Python direct 路径能被 Elsevier 识别为有机构授权。
