# ScanSci 文献获取 — Qoder 插件

把学术文献的「找、下、引、整理」收进一个工作流。你只需要说人话——「帮我下这篇」「这批清单先分类」「搜 2023 年以来被引最高的 10 篇」——插件负责选路、取文、命名、导出与兜底诊断。

## 能力

| 你想要 | 它会做什么 |
| --- | --- |
| 下这一篇 | 按你已授权的渠道并行获取，产出「作者_年份_标题」规整命名的 PDF |
| 下一整批 | 去重、规范化标识符、分批调度、断点续传，最后给出成功/失败清单 |
| 先搜再下 | 多源检索与引文追踪，结果交你确认后再进入下载队列 |
| 用学校权限 | 打开浏览器让你登录一次，之后自动复用会话（WebVPN / CARSI / EZproxy） |
| 中文论文 | 自动切换到中文文献通道（国家哲社文献中心 / 期刊官网 / 知网 / 万方 / 维普） |
| 整理引用 | 导出 BibTeX / RIS / EndNote，可直接推送 Zotero |
| 下不下来 | 报告每个渠道的失败原因与下一步动作，而不是只说「失败」 |

## 组成

| 组件 | 内容 |
| --- | --- |
| Skills | `scansci-pdf`（主工作流，附 `references/` 两份进阶文档）、`scansci-sort`（大清单分类摸底） |
| Commands | `/scansci:paper`、`/scansci:batch`、`/scansci:sort`、`/scansci:doctor` |
| Connector | `.mcp.json` 声明本地 stdio MCP 服务器 `scansci-pdf`，提供 20 个工具 |
| 前置条件 | 见 `CONNECTORS.md`：本机需要 Python 3.11+ 与 `scansci-pdf` 包 |

## 安装后自检

在 Qoder 里说一句「帮我检查一下论文下载环境」。它会调用 `scansci_pdf_diagnostics` 与 `scansci_pdf_channel_status`，报告依赖、浏览器后端、机构通道与可选 Key 的实际状态，并告诉你还缺什么。

## 数据与凭据

插件不内置任何账号、Cookie 或密钥。API Key 保存在你本机的 `~/.scansci-pdf/config.json`，机构登录在你的浏览器里完成，会话凭据只留在本机。可选的 Elsevier / Springer / Sciverse 是第三方服务，各有自己的配额与条款。

## 使用边界

请在你有权访问的范围内使用。付费墙论文需要你本人的机构订阅或校园网出口；没有权限时，插件只返回开放获取、预印本或合法免费版本，并说明原因。

## 许可与源码

MIT，见 `LICENSE`。源码：https://github.com/Rimagination/scansci-pdf
