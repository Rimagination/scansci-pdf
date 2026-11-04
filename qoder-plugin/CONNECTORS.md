# 连接器依赖说明

本插件通过 `.mcp.json` 声明一个**本地 stdio** MCP 连接器 `scansci-pdf`。它不是远程 HTTPS 服务，而是启动本机已安装的 Python 包，因此安装插件本身不会自动装依赖——需要先完成下面两步。

连接器固定使用 `scansci-pdf run --profile qoder`。该 profile 强制 `legal_only`：即使调用方传入 `scihub_enabled=true` 或灰色源 strategy，服务端也会拒绝。这个限制只收紧、不会放宽任何渠道。

## 安装顺序

```bash
# 1. 安装 Python 包（建议带上机构通道所需的加密依赖）
python -m pip install -U "scansci-pdf[fast,vpnsci]"

# 2. 校验运行环境
scansci-pdf check
```

> 本插件使用 `--profile qoder`，需要包含该参数的 scansci-pdf 版本。如果启动时报 unknown option，先执行上面的 `pip install -U` 升级。

装好后在 Qoder 的「扩展管理 → 连接器」里应能看到 `scansci-pdf`，状态为就绪、20 个工具。若显示启动失败，通常是 CLI 不在 PATH：把 `command` 改成绝对路径即可，例如 Windows 上形如

```text
C:\Users\<用户名>\AppData\Local\Programs\Python\Python311\Scripts\scansci-pdf.exe
```

## 依赖与可选能力

| 想做什么 | 需要装什么 |
| --- | --- |
| 开放获取、预印本、出版商 API 下载 | 基础安装即可 |
| 机构通道（WebVPN / CARSI / EZproxy） | `vpnsci` extra（pycryptodome），URL 加密依赖它 |
| 机构登录与反爬较强的站点 | 可选 `cloakbrowser` / `camoufox` / `patchright` 浏览器后端 |
| Elsevier / Springer 官方接口提速 | 需你自行申请 API Key（机构邮箱 + 校园网出口创建） |
| Sciverse 语义证据与元数据 | 可选 Token，见 https://sciverse.space/tokens |

以上第三方服务各有配额与条款，不配置不影响基础检索与下载。

## 隐私与凭据

插件不内置任何账号、Cookie、密钥或机构凭据。API Key 由用户在本机 `~/.scansci-pdf/config.json` 中保存（可通过 `scansci_pdf_config` 或 `scansci-pdf config-cmd` 写入）；机构登录在用户自己的浏览器中完成，会话 Cookie 仅保存在本机，不上传任何第三方。

## 使用边界

请在用户有权访问的范围内使用。付费墙内容需要用户本人的机构订阅或校园网出口；无权限时工具只返回开放获取、预印本或合法免费版本，并明确报告失败原因。
