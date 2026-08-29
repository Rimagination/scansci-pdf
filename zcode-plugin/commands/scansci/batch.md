---
description: 批量下载论文清单(自动分批/断点续传/失败汇总)
---
按 scansci-pdf skill 的批量下载规则执行。

输入:$ARGUMENTS(DOI 文件路径,每行一个;可带 --scihub 标记)

要求:
1. 清单超过 300 篇必须先分批(batch 校验阶段大清单会 TimeoutError 崩溃)
2. 用户要求灰源或清单来自 sort 的 scihub.txt 时加 `--scihub --format json --output <dir>`
3. 逐批执行,批间检查成功率;连续 Cloudflare 拦截时停下按排障速查表诊断
4. 失败 DOI 汇总写 failed.txt 并给出下一步建议(机构渠道/单篇重试/换源清缓存)
5. 汇报:成功数/失败数/输出目录/实际来源分布
