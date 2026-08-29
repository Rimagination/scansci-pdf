---
description: ScanSci 诊断排障(下载失败/Cloudflare/浏览器后端/网络/会话)
---
按 scansci-pdf skill 的排障速查表对问题做系统诊断。

问题描述:$ARGUMENTS(如"sci-hub 一直返回验证页"、"批量下载全失败"、"MCP 工具不见了")

执行:
1. `scansci-pdf check` + `browser-status` + `session-doctor` 三件套
2. 按排障速查表逐项匹配症状,给出具体修复命令
3. 涉及 Cloudflare/Turnstile 的说明 patchright↔cloakbrowser 切换权衡
4. 修复后用单个 DOI 验证下载,再建议恢复批量任务
