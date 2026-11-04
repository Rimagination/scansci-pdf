---
description: 诊断排障（下载失败 / 反爬拦截 / 浏览器后端 / 网络 / 会话）
---
按 scansci-pdf 技能的排障速查表，对问题做系统诊断。

问题描述：$ARGUMENTS（例如「Sci-Hub 一直返回验证页」「批量下载全失败」「MCP 工具不见了」）

执行：

1. 三件套：`scansci-pdf check`、`browser-status`、`session-doctor`。
2. 按排障速查表逐项匹配症状，给出可直接执行的具体修复命令。
3. 涉及反爬与人机验证时，说明 `patchright` 与 `cloakbrowser` 后端之间的切换权衡，并给出推荐。
4. 修复后用单个 DOI 验证下载通路，再建议恢复批量任务。
5. 汇报区分三类原因：依赖缺失、网络/代理、权限不足——不要笼统说「下载失败」。
