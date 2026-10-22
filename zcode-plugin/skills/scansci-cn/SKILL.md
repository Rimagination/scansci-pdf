---
name: scansci-cn
description: 中文文献获取与核验。当用户要检索/下载中文期刊论文(知网/万方/维普/NCPSSD)，或文献清单里混有中文标题、中文 DOI(10.3969 等)、.caj 文件，需要规范化标识、走机构订阅渠道、或做下载后核验时使用。
---

# ScanSci CN — 中文文献获取与核验

扫描中文文献的合法通道，并给中文清单做标识规范化与下载后校验。不涉及任何镜像绕过或第三方共享账号。

## 通道矩阵(按可达性排序)

| 通道 | 覆盖 | 前提 | 脚本化程度 |
|---|---|---|---|
| **机构订阅(IP/CARSI)** | 知网、万方、维普、超星 | 用户自己的校园网/单位认证 | 浏览器登录态；`scansci-pdf login --kind carsi` 同理扩展 |
| **NCPSSD 国家哲学社会科学文献中心** | 人文社科期刊/论文 | 免费注册 | 页面为 JS 渲染，**目前只能人工检索下载** |
| **期刊官网** | 大量中文刊官网提供免费 PDF | 无 | 常见路径 `/CN/article/downloadArticleFile.do`、`/CN/Y{年}/V{卷}/I{期}/{页}` |
| **预印本/机构库** | 中国科技论文在线、各高校机构库 | 无 | 部分可脚本化 |
| **中文 DOI 系统** | chinadoi 注册的中文刊 | 无 | 仅用于标识解析(见 scripts/cn_resolve.py) |

⚠️ 知网/万方的站内检索接口带轮换 JS 加密(crypto-js)，不可脚本化——检索与下载一律走机构登录态下的浏览器流程，不做逆向。

## 中文 DOI 与标识

- 中文刊常见 DOI 族：`10.3969/j.issn.1000-xxxx.yyyy.nn.nnn`(万方系)、`10.11897/…`、`10.13324/…` 及大量自有前缀。
- 校验用 `scripts/cn_resolve.py`：全角→半角、空白清理、DOI 提取、ISSN/年份/期号/篇号拆解。
- 知网导出多为 `CAJ` 格式：需 CAJViewer，或机构版页面选 PDF 下载；批量任务应把 `.caj` 单独成桶。

## 下载后核验(必做)

`scripts/verify_pdf.py`：用 PDF 首页文本对比 Crossref 标题(EN 用词元覆盖率，CJK 用字符二元组 Jaccard)，并检查
撤稿状态(Crossref `update-to` + OpenAlex `is_retracted`)。

```bash
python scripts/verify_pdf.py --doi 10.1007/s11356-020-11742-8 --pdf paper.pdf
python scripts/verify_pdf.py --dir ./pdfs          # 文件名以 DOI 开头的批量核验
```

建议在 sort 落库前对整批下载跑一次，输出 JSON 里：
- `match` 低于阈值 → 疑似下错文章
- `retracted: true` → 从综述证据链剔除

## 与 sort 流程的衔接

1. 清单卫生：中文标题做全角/空白归一(见 cn_resolve.py)
2. 分诊：中文 DOI 走机构桶；无 DOI 的中文文献单独成桶(缺DOI)
3. 下载：机构登录态 / NCPSSD 人工 / 期刊官网直链
4. 核验：verify_pdf.py 全量跑
5. 报告：按"来源/许可"分桶(O A/机构/官网/人工)，便于发表偏倚分析

## 反爬与礼貌

- 知网/万方/维普对高频请求敏感：串行、间隔 ≥2s，单账号单日控制在个人阅读量级。
- 机构 cookie 有时效：批量任务前先用一次小请求探测登录态是否有效。
