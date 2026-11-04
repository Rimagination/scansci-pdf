---
description: 下载单篇论文（可按措辞指定来源策略）
---
按 scansci-pdf 技能下载单篇论文。

输入：$ARGUMENTS（DOI / 论文标题 / URL；可带策略措辞，如「从 Sci-Hub」「只要合法 OA」）

要求：

1. 解析出 DOI；只有标题时先检索拿到 DOI，不要凭标题猜。
2. 按用户措辞映射策略：「从 Sci-Hub」→ `scihub_only`；「优先 Sci-Hub」→ `scihub_first`；「只要合法」→ `legal_only`；默认 `fastest`。
3. 执行：`scansci-pdf config-cmd download_strategy <策略>`，再 `scansci-pdf get <DOI>`。
4. 失败时按技能的排障速查表处理（反爬拦截 / 代理 / 换源并清缓存），不要盲目重试。
5. 汇报文件路径与实际命中的来源（注意 `scihub_first` 下最终命中的可能是开放获取渠道）。
