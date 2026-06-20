# Elsevier API 全文下载成功经验：可迁移到 InstSci

更新时间：2026-06-20

这份文档记录 scansci-pdf 在 Elsevier/ScienceDirect 闭源文章上跑通的经验，并整理成 InstSci 可以复用的配置与实现 checklist。核心前提是：用户拥有合法机构访问条件，例如校园网、学校 VPN、CARSI/图书馆出口或 Elsevier institutional token。

## 一句话结论

Elsevier 的网页 PDF 路线和 API 对象路线是两条路径。稳定成功的做法不是直接请求 Article Retrieval API 的 PDF，而是：

```text
Article Retrieval API ?view=FULL
  -> 获取全文 XML
  -> 解析 MAIN PDF 的 attachment-eid / object-eid
  -> Content Object API /content/object/eid/{eid}
  -> 下载出版社正式 PDF
```

这条路径不依赖 ScienceDirect 网页、`/pdfft`、Cloudflare 或浏览器验证码；它依赖 Elsevier API Key 与机构 entitlement。

## 本次为什么成功

之前失败不是因为 Elsevier object/eid 路线不可行，而是网络路由错了：

- 项目配置了 `network_proxy`，Python 请求优先走普通代理出口。
- Elsevier 对这个出口返回 `NOT_ENTITLED` / object 401。
- 切换到规则 VPN 后，Python 的 direct route 能让 `api.elsevier.com` 走机构出口。
- 修复策略改为 Elsevier API direct-first：先走系统路由/规则 VPN；direct 不通或无授权时，再 fallback 到配置代理。
- 同一批 10 篇闭源 Elsevier 文章全部通过 XML -> object/eid 下载成功。

## 必备配置

### 1. Elsevier API Key

用户自己申请：

1. 打开 <https://dev.elsevier.com/>
2. 注册或登录 Elsevier 账号。
3. 进入 `My API Key` / API Key Settings。
4. 创建 API Key；若页面要求选择产品/API，选择 ScienceDirect / Article Retrieval 相关权限。
5. 保存到本地配置，不要写入公开日志、文档或仓库。

建议 InstSci 配置项：

```text
elsevier_api_key = "..."
elsevier_inst_token = ""   # 可选；仅图书馆明确提供时配置
```

多数用户不需要 `elsevier_inst_token`。校园网、规则 VPN、CARSI/VPN 或图书馆出口 IP 也可能提供 entitlement。

### 2. 网络路由

Elsevier 的闭源全文授权与请求 IP 强相关。InstSci 应该优先让 `api.elsevier.com` 走机构出口：

- 校园网直连
- 学校 VPN / 规则 VPN / TUN 模式
- 图书馆认可的机构出口

不要让普通 `network_proxy` 覆盖机构出口。推荐策略：

```text
route_options = [
  direct,
  configured_proxy,
]
```

只有 direct 请求失败、超时或无授权时，再尝试 configured proxy。

## InstSci 迁移 checklist

### 实现顺序

1. 在 InstSci 的 Elsevier API 源里先请求 XML：
   - URL：`https://api.elsevier.com/content/article/doi/{doi}`
   - Headers：`X-ELS-APIKey: ...`, `Accept: application/xml`
   - Params：`view=FULL`
2. 解析 XML 中的 PDF object EID：
   - 优先 `attachment-eid` / `object-eid`
   - 识别 `web-pdf`、`attachment`、`MAIN`、`full-text`
   - 排除 `supplement`、`mmc`、`appendix`、`graphical` 等补充材料
   - 若只有文章 EID `1-s2.0-...`，可兜底推断 `1-s2.0-...-main.pdf`
3. 请求 Content Object API：
   - URL：`https://api.elsevier.com/content/object/eid/{urlencoded-eid}`
   - Headers：`X-ELS-APIKey: ...`, `Accept: application/pdf`
4. 验证 PDF：
   - 响应以 `%PDF-` 开头，或 `Content-Type` 包含 PDF
   - 文件大小大于最小阈值
   - 页数大于 1，拒绝一页预览
   - 可选检查 DOI/标题文本
5. direct-first，proxy fallback。
6. 失败时记录 route、HTTP status、`X-ELS-Status`、content-type、bytes；不要记录 API Key。

### 伪代码

```python
def fetch_elsevier_pdf(doi, config):
    routes = [direct_route()]
    if config.network_proxy:
        routes.append(proxy_route(config.network_proxy))

    for route in routes:
        xml = get(
            f"https://api.elsevier.com/content/article/doi/{quote(doi)}",
            headers={
                "X-ELS-APIKey": config.elsevier_api_key,
                "Accept": "application/xml",
            },
            params={"view": "FULL"},
            route=route,
        )
        if xml.status_code != 200:
            continue

        eids = extract_main_pdf_eids(xml.text)
        for eid in eids:
            pdf = get(
                f"https://api.elsevier.com/content/object/eid/{quote(eid)}",
                headers={
                    "X-ELS-APIKey": config.elsevier_api_key,
                    "Accept": "application/pdf",
                },
                route=route,
            )
            if is_valid_full_pdf(pdf.content):
                return pdf.content

    return None
```

## 验收标准

最小验收：

- 一个 OA Elsevier DOI 能拿到 `view=FULL` XML。
- 一个有机构订阅的闭源 Elsevier DOI 能从 XML 解析到 `*-main.pdf` EID。
- object/eid 返回正式 PDF，不是一页预览。
- 配置了普通代理时，日志显示先尝试 direct route。
- direct 成功时不再使用 configured proxy。
- direct `NOT_ENTITLED` 时才尝试 configured proxy。

推荐回归测试：

- XML 中明确 `attachment-eid` 的样例。
- XML 只有文章 EID 时推断 `-main.pdf`。
- MAIN PDF 与 supplementary PDF 同时出现时优先 MAIN。
- object/eid 返回 401/403 时不写入成功。
- 直接 PDF 返回 1 页时拒绝。

## 常见失败判断

| 现象 | 含义 | 下一步 |
|---|---|---|
| `view=FULL` 返回 400 / view invalid | 当前 route 无 FULL entitlement | 检查是否走机构出口 |
| ENTITLED 返回 `NOT_ENTITLED` | Elsevier 不认可当前 IP/授权上下文 | 关闭普通代理，使用校园网/规则 VPN |
| object/eid 返回 401/403 | 对象存在，但当前授权不能下载 | 换机构出口或确认订阅范围 |
| 直接 PDF 只有 1 页 | 预览，不是正式全文 | 拒绝并走 XML/object-eid |
| OA 成功、闭源失败 | API Key 有效，但缺机构 entitlement | 需要机构网络或机构 token |

## 与 InstSci browser evidence 的关系

InstSci 的项目规则中，publisher PDF capability 或 closed-access 最终 verdict 可能要求 visible CloakBrowser 证据。Elsevier API 路线可以作为合法授权的快速获取通道和工程实现路径，但在报告能力矩阵或最终 publisher verdict 时，仍要遵守 InstSci 的 `AGENTS.md`：

- HTTP/API 结果可标为 API route / HTTP preflight evidence。
- 如果任务要求 browser-verified verdict，仍需 visible CloakBrowser 截图与下载证据。
- 不要用 API 成功替代项目规则要求的浏览器证据，除非 InstSci 明确更新 evidence standard。

## 可复制到 InstSci 的用户引导

```text
Elsevier/ScienceDirect 论文建议先配置 Elsevier API Key：
1. 打开 https://dev.elsevier.com/
2. 注册/登录 Elsevier 账号
3. My API Key / API Key Settings -> 创建 API Key
4. 若需要选择 API，选择 ScienceDirect / Article Retrieval
5. 在 InstSci 配置 elsevier_api_key
6. 使用校园网、学校 VPN 或规则 VPN，确保 api.elsevier.com 走机构出口

下载时 InstSci 将优先：
view=FULL XML -> 解析 MAIN PDF attachment-eid -> object/eid 下载正式 PDF。
如果返回 NOT_ENTITLED，优先检查网络出口是否被普通代理覆盖。
```

## 实测记录

2026-06-20，规则 VPN 环境下，scansci-pdf 使用 direct-first + XML/object-eid 路线，10 篇闭源 Elsevier 文章全部成功下载。记录见：

- scansci-pdf：`elsevier_10_closed_articles.md`
- PDF 输出目录：`C:\Users\Liang\.scansci-pdf\papers\elsevier_api_rule_vpn_direct_first_20260620_224223`

