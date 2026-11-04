---
description: 下载单篇论文（本版本固定 legal-only）
---
按 scansci-pdf 技能下载单篇论文。

输入：$ARGUMENTS（DOI / 论文标题 / URL）

要求：

1. 解析出 DOI；只有标题时先检索拿到 DOI，不要凭标题猜。
2. 本版本固定 `legal_only`，不接受 Sci-Hub / 灰色源策略；如果用户要求灰色源，说明本版本不支持。
3. 执行：`scansci-pdf get <DOI>`。
4. 失败时按技能的排障速查表处理（反爬拦截 / 代理 / 换源并清缓存），不要盲目重试。
5. 汇报文件路径与实际命中的来源。
