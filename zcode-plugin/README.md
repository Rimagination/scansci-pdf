# scansci-pdf

![logo](icon.png)

ScanSci 学术文献全家桶 plugin:大清单嗅探分类 + 全场景下载检索排障,携带 scansci-pdf MCP server(stdio)。

## 组件

**Skills(2 个,按意图拆分):**

| Skill | 场景 |
|---|---|
| `scansci-sort` | 大清单(数百篇+)分类摸底:OA / Sci-Hub / 需机构,嗅探优先不下载,产出分桶队列 |
| `scansci-pdf` | 其余一切:单篇/批量下载、检索引文、机构渠道(Elsevier API/WebVPN/CARSI)、排障(随 `~/.agents/skills/scansci-pdf` 分发,用户目录优先) |

**Commands(4 个,快捷入口,不占上下文):**

| Command | 用途 |
|---|---|
| `/scansci:sort <文件>` | 嗅探分类摸底,产出报告+分桶队列 |
| `/scansci:paper <DOI> [策略]` | 单篇下载 |
| `/scansci:batch <文件>` | 批量下载(自动分批) |
| `/scansci:doctor <症状>` | 诊断排障 |

## 依赖

- `scansci-pdf` CLI 在 PATH(`pip install -e <scansci-pdf 源码目录>`)
- MCP server 由 plugin 自动注册:`scansci-pdf run`(stdio),无需手动配置

## 安装

Settings → Plugin Management → Discover → 添加本地目录(本目录)→ 启用 scansci-pdf。

## 跨 agent 共享

Claude Code / Codex 等不支持 ZCode plugin,用 shared-skill-installer 把 `skills/scansci-sort/` 同步到 `~/.agents/skills/`(scansci-pdf 主 skill 已在用户目录);commands 与 MCP 为 ZCode 专属。
