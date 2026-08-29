---
description: 下载单篇论文(支持指定来源策略)
---
按 scansci-pdf skill 下载单篇论文。

输入:$ARGUMENTS(DOI/论文标题/URL;可带策略关键词如"从sci-hub"、"只要合法OA")

要求:
1. 解析出 DOI;只有标题时先搜索拿 DOI
2. 按用户措辞映射策略:"从 Sci-Hub"→scihub_only;"优先scihub"→scihub_first;"只要合法"→legal_only;默认 fastest
3. CLI:`scansci-pdf config-cmd download_strategy <策略>` 后 `scansci-pdf get <DOI>`
4. 失败时按 scansci-pdf skill 的排障速查表处理(Cloudflare/代理/换源清缓存),不要盲目重试
5. 汇报文件路径与实际来源(注意 scihub_first 下最终来源可能是 OA)
