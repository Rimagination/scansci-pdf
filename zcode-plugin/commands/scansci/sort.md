---
description: 大清单文献嗅探分类摸底(OA/Sci-Hub/需机构),产出分类报告与分桶下载队列
---
按 scansci-sort skill 的完整流程,对文献清单执行嗅探分类摸底:

输入:$ARGUMENTS(Excel/CSV/DOI 文件路径;为空时请向用户要文件)

要求:
1. 数据卫生:去重、规范化 DOI、分离缺 DOI 行
2. OA 判定:OpenAlex 优先(429 则降级 Unpaywall 单点并发),断点续传
3. 灰色源嗅探:仅对非 OA 子集,sci-hub.vg/al/ee 三镜像四分类(hit/miss/turnstile/blocked)
4. 合法补捞:对 miss 子集跑 OpenAIRE + DOAJ API
5. 产出:分类写回原 Excel 新列 + UTF-8 BOM CSV 报告 + 分桶文件(oa.txt/scihub.txt/repo.txt/institution.txt)+ 渠道统计表

只嗅探不下载。最后汇报各桶数量与建议路由(L1 直链/L2 batch --scihub/L3 Elsevier API/L4 WebVPN)。
