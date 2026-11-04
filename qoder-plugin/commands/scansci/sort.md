---
description: 大清单嗅探分类摸底（开放获取 / 补充来源 / 需机构权限），产出分类报告与分桶下载队列
---
按 scansci-sort 技能的完整流程，对文献清单执行嗅探分类摸底。

输入：$ARGUMENTS（Excel / CSV / DOI 文件路径；为空时先向用户索取文件）

要求：

1. 数据卫生：去重、规范化 DOI、分离缺 DOI 的行单独处理。
2. 开放获取判定：优先 OpenAlex（被限流则降级 Unpaywall 单点并发），全程断点续传。
3. 补充来源嗅探：仅对非开放获取子集执行，多镜像四分类（hit / miss / turnstile / blocked）。
4. 合法补捞：对 miss 子集跑 OpenAIRE 与 DOAJ 接口。
5. 产出：分类结果写回原 Excel 的新列，另存 UTF-8 BOM 的 CSV 报告、分桶文件（`oa.txt` / `scihub.txt` / `repo.txt` / `institution.txt`）与渠道统计表。

只嗅探、不下载。最后汇报各桶数量与建议路由（L1 直链 → L2 批量补充来源 → L3 出版商 API → L4 机构通道），并等用户确认后再进入下载。
