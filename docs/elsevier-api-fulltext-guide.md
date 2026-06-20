# Elsevier API 全文 PDF 稳定路径

更新时间：2026-06-20

这份指南记录已验证的 Elsevier/ScienceDirect 全文 PDF 下载路径。它适用于有 Elsevier API Key，并且当前网络/IP 对目标文章有机构订阅权限的用户。

如果要把这套经验迁移到 InstSci，见 `docs/elsevier-api-success-experience-for-instsci.md`。

## 核心结论

不要把 Article Retrieval API 的直接 PDF 响应当作唯一入口。对闭源文章，直接请求 PDF 可能只返回预览或被授权拦截。稳定路径是：

1. 请求 Article Retrieval API 的全文 XML：
   `GET https://api.elsevier.com/content/article/doi/{doi}?view=FULL`
2. 从 XML 中解析正式 PDF 附件 ID，例如：
   `1-s2.0-S0006320725007013-main.pdf`
3. 请求 Content Object API：
   `GET https://api.elsevier.com/content/object/eid/{attachment-eid}`
4. 验证响应：
   - `Content-Type` 为 PDF，或内容以 `%PDF-` 开头
   - 页数大于 1
   - 文件大小合理
   - 若可得，`X-ELS-Status: OK`

## 初次使用配置

### 1. 申请 Elsevier API Key

1. 打开 Elsevier Developer Portal：<https://dev.elsevier.com/>
2. 注册或登录 Elsevier 账号。
3. 进入 `My API Key` / API Key Settings。
4. 创建新的 API Key；如果页面要求选择产品/API，选择 ScienceDirect / Article Retrieval 相关权限。
5. 复制生成的 API Key，不要把 key 写进公开文档、日志或仓库。

说明：
- Elsevier 官方说明，API Key 可申请；非商业学术、公共机构、慈善等使用场景可免费使用多数 API。
- 完整全文访问仍取决于机构订阅、API Key 权限，以及请求 IP/授权 token 的 entitlement。

### 2. 配置 scansci-pdf

MCP：

```text
scansci_pdf_config_set(key="elsevier_api_key", value="YOUR_KEY")
scansci_pdf_elsevier_setup(test=true)
```

CLI：

```bash
scansci-pdf elsevier-setup --api-key YOUR_KEY --validate
```

可选：如果图书馆明确提供 Elsevier institutional token，再配置：

```text
scansci_pdf_config_set(key="elsevier_insttoken", value="YOUR_INST_TOKEN")
```

多数用户不需要 `elsevier_insttoken`；校园网、CARSI/VPN、图书馆出口 IP 已经可能构成授权上下文。

### 3. 配置网络

Elsevier 的全文 entitlement 与请求 IP 强相关。建议优先使用能让 `api.elsevier.com` 走机构出口的方式：

- 校园网直连
- 学校 VPN / 规则 VPN / TUN 模式
- 图书馆认可的机构代理

不要让 scansci-pdf 的 `network_proxy` 强制覆盖 Elsevier API 到非机构出口。当前实现对 Elsevier API 使用 direct-first 策略：先走系统路由，让规则 VPN 或校园网 IP 生效；direct 不通或无授权时，再回退到配置代理。

## 程序实现规则

Agent 或开发者维护 Elsevier 路径时遵守以下规则：

1. 对 `10.1016/`、ScienceDirect、Elsevier、Cell Press 论文，配置了 `elsevier_api_key` 时优先尝试 Elsevier API。
2. 第一个请求必须优先拿 XML：`Accept: application/xml` + `view=FULL`。
3. 从 XML 中按优先级解析 PDF object EID：
   - 明确的 `attachment-eid` / `object-eid`
   - `web-pdf`、`attachment` 节点中的 MAIN/full-text PDF
   - 兜底把文章 EID `1-s2.0-...` 推断为 `1-s2.0-...-main.pdf`
4. 下载 PDF 时使用：
   `https://api.elsevier.com/content/object/eid/{urlencoded-eid}`
5. 拒绝一页预览 PDF；不要把预览当成功。
6. direct route 优先，configured proxy 只作为 fallback。
7. 失败时记录状态码、`X-ELS-Status` 和路由，但不要记录 API Key。

## 常见返回与处理

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `view=FULL` 返回 400，提示 view invalid | 当前 IP/API Key 没有 FULL entitlement | 检查是否走机构出口；改用规则 VPN/TUN；确认机构订阅 |
| `ENTITLED` 返回 `NOT_ENTITLED` | Elsevier 不认可当前请求 IP/授权上下文，或目标期刊不在订阅内 | 先确认 `api.elsevier.com` 没走普通代理；再换机构网络 |
| object/eid 返回 401/403 | 附件存在，但当前授权无法下载对象 PDF | 同上，优先检查出口 IP 与机构订阅 |
| 直接 PDF 返回 1 页 | 预览 PDF，不是正式全文 | 必须改走 XML -> object/eid |
| OA 文章成功、闭源文章失败 | Key 有效，但无订阅 entitlement | 接入机构网络或机构 token |

## 验证样例

2026-06-20 规则 VPN 环境下，以下 10 篇闭源 Elsevier 文章通过 direct-first + XML/object-eid 全部成功：

- `10.1016/j.jmst.2025.08.030`
- `10.1016/j.jmst.2025.08.056`
- `10.1016/j.seppur.2026.137141`
- `10.1016/j.molstruc.2026.145682`
- `10.1016/j.physio.2025.101870`
- `10.1016/j.brainres.2026.150220`
- `10.1016/j.neunet.2026.108539`
- `10.1016/j.neunet.2026.108564`
- `10.1016/j.neunet.2026.108617`
- `10.1016/j.neunet.2026.108582`

详细记录见仓库根目录 `elsevier_10_closed_articles.md`。
