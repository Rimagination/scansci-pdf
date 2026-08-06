# ScanSci PDF

[![PyPI version](https://img.shields.io/pypi/v/scansci-pdf)](https://pypi.org/project/scansci-pdf/)
[![Python](https://img.shields.io/pypi/pyversions/scansci-pdf)](https://pypi.org/project/scansci-pdf/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)

> 学术论文下载 MCP 服务器 — 13+ 数据源、100+ 高校 WebVPN、并行竞速下载引擎

---

## 为什么选择 ScanSci PDF？

- **一个工具，13+ 数据源** — arXiv、Sci-Hub、LibGen、Unpaywall、OpenAlex、Semantic Scholar、DOAJ、EuropePMC、CORE、PMC、出版商直链等，自动选择最快可用源
- **100+ 高校 WebVPN** — 通过中国高校机构代理访问付费论文全文，CAS 认证，密码不经过工具
- **CARSI 联邦认证** — 支持 ScienceDirect、Springer、Wiley、IEEE、Taylor & Francis、Nature 等出版商的机构登录
- **Cloudflare 绕过** — CloakBrowser 反检测浏览器（Chromium 指纹修补）+ curl_cffi TLS 指纹模拟 + FlareSolverr 浏览器引擎
- **并行竞速引擎** — 多数据源同时尝试，首个成功立即返回，无需逐个等待
- **智能列表解析** — 支持 APA 引文、BibTeX、DOI 列表，自动补全缺失 DOI 后批量下载
- **自动重命名** — PDF 自动重命名为 `作者年份_标题.pdf` 格式
- **引文导出** — 一键获取 BibTeX、RIS、EndNote 格式引文
- **网络诊断** — 自动检测 DNS 封锁、代理配置、Tor 连接问题，给出针对性修复建议

---

## 前置准备（可选但推荐）

### Elsevier API Key — ScienceDirect 直接下载

ScienceDirect/Elsevier 是最大的学术出版商。配置 API Key 后可直接通过 API 下载 PDF，**无需浏览器登录**，速度从 15-30 秒降到 1-2 秒。

**申请步骤：**

1. 访问 [Elsevier Developer Portal](https://dev.elsevier.com/) 注册账号（已有 Elsevier 账号可直接登录）
2. 点击 **"My API Key"** → 创建新应用 → 选择 **"ScienceDirect Article Retrieval"** API
3. 复制生成的 API Key，运行配置命令：

```bash
# 方式一：通过 MCP 工具（推荐，会自动打开浏览器引导）
scansci_pdf_elsevier_setup

# 方式二：直接配置
scansci_pdf_config_set(key="elsevier_api_key", value="你的APIKey")
```

> **提示：** 申请完全免费，无需机构邮箱，个人邮箱即可。配置后所有 Elsevier/ScienceDirect/Cell Press 论文自动走 API 快速通道。

---

## 快速开始

### 安装

```bash
pip install scansci-pdf
```

### MCP 配置

在任何支持 MCP 的 Agent 中添加以下配置即可使用：

```json
{
  "mcpServers": {
    "scansci-pdf": {
      "command": "scansci-pdf",
      "args": ["run"]
    }
  }
}
```

支持 MCP 的 Agent 和客户端：

| 客户端 | 说明 |
|--------|------|
| Claude Desktop | Anthropic 官方桌面客户端 |
| Claude Code | Anthropic 命令行 Agent |
| Cursor | AI 代码编辑器 |
| Windsurf | AI 代码编辑器 |
| Cline | VS Code 插件 Agent |
| Cherry Studio | 多模型桌面客户端 |
| OpenClaw | MCP 客户端 |
| 任何 MCP 兼容客户端 | MCP 是开放协议，任何实现均可接入 |

<details>
<summary>HTTP 模式（远程/Web 调用）</summary>

适用于远程部署或不支持 stdio 的场景：

```bash
scansci-pdf run --mode streamable_http --host 0.0.0.0 --port 8000
```
</details>

常用 MCP 工具：

| 工具 | 用途 |
|---|---|
| `scansci_pdf_download` | 下载单篇 DOI/arXiv |
| `scansci_pdf_batch_download` | 批量下载 |
| `scansci_pdf_search` | 搜索论文 |
| `scansci_pdf_citation` | 获取 BibTeX/RIS/EndNote |
| `scansci_pdf_login` | 打开浏览器完成机构登录 |
| `scansci_pdf_elsevier_setup` | 引导配置 Elsevier API Key |
| `scansci_pdf_network_diagnose` | 网络诊断 |

---

## 配置参考

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `output_dir` | `~/.scansci-pdf/papers` | PDF 保存目录 |
| `auto_rename` | `true` | 自动按作者/标题重命名 |
| `scihub_enabled` | `true` | 启用 Sci-Hub/LibGen 类来源 |
| `network_proxy` | 空 | HTTP/SOCKS 代理地址 |
| `proxy_pool` | 空 | 逗号分隔的代理列表；非空时批量下载按代理轮换出口 IP |
| `batch_workers` | `10` | 批量下载并发数（被封 IP 时建议调低到 2） |
| `request_delay_min` | `2.0` | 请求间随机延迟下限（秒） |
| `request_delay_max` | `5.0` | 请求间随机延迟上限（秒） |
| `instsci_enabled` | `false` | 启用 WebVPN |
| `instsci_school` | 空 | WebVPN 学校名称 |
| `carsi_enabled` | `false` | 启用 CARSI |
| `carsi_idp_name` | 空 | CARSI 机构名称 |
| `auto_relogin` | `true` | 机构会话自愈：下载走机构渠道前自动校验 WebVPN 会话，已过期时打开浏览器重新登录。仅当存在历史 cookie 且校验明确判定过期才触发；新用户或网络不可达绝不弹浏览器 |
| `cache_ttl_hours` | `168` | 全局下载缓存与 `.doi_index.json` 的 TTL（小时）；设为 0 禁用过期 |
| `elsevier_api_key` | 空 | Elsevier / ScienceDirect API Key |
| `elsevier_insttoken` | 空 | Elsevier institutional token，可选 |
| `browser_headless` | `false` | 浏览器是否无头运行 |
| `browser_humanize` | `true` | 浏览器人性化操作 |

查看全部配置：

```bash
scansci-pdf config-cmd
```

---

## 可选高级功能

### Web UI

```bash
scansci-pdf web --port 8080
```

浏览器打开：

```text
http://localhost:8080
```

### Docker

适合长期运行服务或远程 MCP。

```bash
docker compose up -d
```

| 服务 | 端口 | 说明 |
|---|---:|---|
| `scansci-pdf` | `8000` | streamable HTTP MCP 服务 |
| `tor` | `1080` | Tor SOCKS5 代理 |

### Tor

如果你的网络无法访问某些来源，可以配置代理或使用 Tor。Docker 模式会提供 Tor 服务；本地模式可按诊断提示启用。

### CloakBrowser

遇到 Cloudflare、CAPTCHA、SSO 或出版商浏览器下载时，安装可选浏览器依赖：

```bash
pip install "scansci-pdf[cloakbrowser]"
scansci-pdf browser-doctor
```

CloakBrowser 是反爬检测对抗库，**指纹补丁和检测规则更新频繁**。如果以前能下载的站点突然开始被 Cloudflare/CAPTCHA 拦截、出现 403 或空响应，大概率是本地 cloakbrowser 版本过期，建议定期升级：

```bash
pip install -U cloakbrowser
```

可以用以下任一命令检查当前版本是否过旧（离线比对，不会联网上报）：

```bash
scansci-pdf doctor          # 表格中 package: cloakbrowser 行，过旧会标黄
scansci-pdf browser-doctor  # JSON 中 cloakbrowser_version.status = outdated
```

---

## 故障排查

### 下载失败

先运行：

```bash
scansci-pdf check
```

---

## 工作原理

下载一篇论文时，ScanSci PDF 会同时启动多个数据源，按优先级分层竞速：

```
Tier 1 (4s)  ─ 出版商直链（OA/机构访问）
Tier 2 (5s)  ─ OpenAlex / Unpaywall / DOAJ
Tier 3 (8s)  ─ EuropePMC / CORE / PMC / arXiv
Tier 4 (25s) ─ LibGen / Sci-Hub（带 FlareSolverr 绕过）
Tier 5 (20s) ─ WebVPN / CARSI 机构代理
```

### Elsevier 返回 `NOT_ENTITLED`

常见原因是当前请求没有走机构出口。请检查：

- 是否已经连接校园网、学校 VPN 或规则 VPN
- `api.elsevier.com` 是否被普通代理转发到非机构 IP
- 学校是否订阅了目标期刊和年份

### WebVPN 或机构登录失败

- 确认安装了完整依赖：`pip install "scansci-pdf[cloakbrowser,instsci]"`
- 在可见浏览器中完成登录、验证码或二次验证
- 登录后重新运行下载命令

### 浏览器下载以前能用，现在被拦

出版商和 Cloudflare 会持续更新反爬检测，cloakbrowser 也需要跟着升级来应对。如果以前能正常下载的出版商站点突然返回 403、长时间空白、跳到 CAPTCHA 或 Cloudflare 验证页，先检查 cloakbrowser 是否过旧：

```bash
scansci-pdf doctor
```

如果 `package: cloakbrowser` 标黄并提示 `outdated`，升级即可：

```bash
pip install -U cloakbrowser
```

### 被 ACS 等出版商封 IP（IP Address Blocked）

批量下载 ACS（`pubs.acs.org`）等出版商时，可能遇到整页报错：

> IP Address Blocked — Your IP address has been blocked automatically due to unusual behavior. Contact ipblock@acs.org.

这是出版商的自动反爬封锁。机构出口 IP（如校园网 `166.111.x.x`）是共享的，**一个人触发就可能让整段 IP 被封**，所以容易"经常有人遇到"。

**立即解除**（封禁在出版商侧，代码改不了）：

- 邮件 `ipblock@acs.org` 申诉，附上被封 IP，通常 1–3 个工作日解封
- 换出口 IP（代理 / 手机热点）可绕过，但会失去机构订阅授权，只能下 OA 论文
- 部分封锁会在 24–48 小时后自动解除

**预防**（降低被封概率）——让请求看起来像人在浏览，而不是 10 线程同时打：

```bash
scansci-pdf config-cmd batch_workers 2          # 调低并发（默认 10）
scansci-pdf config-cmd request_delay_min 5      # 拉大随机延迟下限（默认 2）
scansci-pdf config-cmd request_delay_max 12     # 拉大随机延迟上限（默认 5）
```

**自动停损**：从 v1.9.0 起，批量任务一旦**连续 3 次**检测到 IP 被封（ACS 封锁页 / HTTP 403 / 429），会自动取消剩余下载，避免越踩越深。无需配置，默认开启。触发时终端会显示：

```
⚠ 已自动停止：连续检测到 IP 被出版商封禁（N 篇返回 ip_blocked），剩余任务已取消。
```

**代理池轮换（进阶）**：如果有多个代理可用，可以配置代理池让批量下载轮换出口 IP，从源头降低单 IP 被盯上的概率：

```bash
scansci-pdf config --proxy-pool "socks5://1.1.1.1:1080,http://2.2.2.2:8080,socks5://3.3.3.3:1080"
```

启用后，每个代理启动一个独立浏览器上下文，记录按 round-robin 分配到各代理。某个代理连续被封（3 次）会被自动剔除，剩余记录转到其他代理；所有代理都被封才会整体停止。登录只需完成一次，cookies 会复用到各代理上下文。

> ⚠ **权衡**：同一登录态从多个 IP 并发访问，少数出版商可能视为异常（账号共享/被盗）。这比被整体封 IP 更可接受，但如果你的机构对这种检测敏感，保持 `proxy_pool` 为空即可回到单上下文模式。

### 下载速度慢

- 配置 Elsevier API Key 可显著改善 ScienceDirect 论文下载速度
- 批量任务可调整 `batch_workers`
- 网络受限时配置 `network_proxy`

---

## 交流群 / Community

扫码加入微信交流群，一起聊 **AI for Science** —— 偏 AI 应用与科研工具，也欢迎讨论 ScanSci PDF 的用法、bug 和需求。

<table>
  <tr>
    <td width="280" align="center">
      <img src="assets/brand/wechat-group-qr.jpg" alt="微信群二维码" width="220">
      <br>
      <sub>微信群 / WeChat Group</sub>
    </td>
    <td valign="middle">
      <p><strong>群聊方向</strong></p>
      <ul>
        <li>AI 在科研场景的落地与工具链</li>
        <li>论文检索、下载、阅读、整理的工作流</li>
        <li>ScanSci PDF 使用问题与改进建议</li>
      </ul>
      <p><sub>二维码过期会更新，若扫码失效请在 issue 区留言。</sub></p>
    </td>
  </tr>
</table>

更偏好异步交流？欢迎直接在 [Issues](https://github.com/Rimagination/scansci-pdf/issues) 或 [Discussions](https://github.com/Rimagination/scansci-pdf/discussions) 区开贴。

---

## 更多资料

### 论文下载

| 工具 | 描述 |
|------|------|
| `scansci_pdf_smart_download` | **推荐** 零配置下载，自动尝试所有源 + Tor |
| `scansci_pdf_download` | 下载单篇论文（完整参数控制） |
| `scansci_pdf_batch_download` | 批量下载多篇论文 |
| `scansci_pdf_resolve_and_download` | 解析列表 → 补全 DOI → 批量下载 |

### 付费墙登录

| 工具 | 描述 |
|------|------|
| `scansci_pdf_login` | **推荐** 统一登录：输入 DOI 自动识别出版商并打开浏览器 SSO |
| `scansci_pdf_camofox_login` | camofox 持久化浏览器登录 |
| `scansci_pdf_camofox_status` | 检查 camofox-browser 状态 |
| `scansci_pdf_camofox_import_cookies` | 导入 Netscape cookie 到 camofox |
| `scansci_pdf_import_browser_cookies` | 打开浏览器捕获登录 cookie |

### 搜索与解析

| 工具 | 描述 |
|------|------|
| `scansci_pdf_search` | 按关键词搜索论文（OpenAlex） |
| `scansci_pdf_parse_list` | 解析 APA/BibTeX/DOI 列表文件 |

### 引文管理

| 工具 | 描述 |
|------|------|
| `scansci_pdf_citation` | 获取论文引文（BibTeX/RIS/EndNote） |
| `scansci_pdf_import_bib` | 导入 .bib 文件并下载全部论文 |

### 机构代理（WebVPN / CARSI / EZProxy）

| 工具 | 描述 |
|------|------|
| `scansci_pdf_vpnsci_set_school` | 设置 WebVPN 学校 |
| `scansci_pdf_vpnsci_login` | WebVPN 浏览器 CAS 认证 |
| `scansci_pdf_vpnsci_status` | WebVPN 登录状态 |
| `scansci_pdf_vpnsci_schools` | 搜索支持的大学 |
| `scansci_pdf_vpnsci_test` | 测试 WebVPN 连接性 |
| `scansci_pdf_carsi_login` | CARSI 出版商机构登录 |
| `scansci_pdf_carsi_status` | CARSI 状态与 cookie 检查 |
| `scansci_pdf_ezproxy_login` | EZProxy 图书馆代理登录 |
| `scansci_pdf_ezproxy_status` | EZProxy 状态检查 |

### 系统管理

| 工具 | 描述 |
|------|------|
| `scansci_pdf_auto_setup` | 一键环境检测与自动配置 |
| `scansci_pdf_setup_check` | 检测系统环境并给出安装建议 |
| `scansci_pdf_health_check` | 检查所有数据源可用性与延迟 |
| `scansci_pdf_network_diagnose` | 网络诊断 + 修复建议 |
| `scansci_pdf_source_scores` | 各数据源历史成功率排名 |
| `scansci_pdf_config_get` / `config_set` | 查看/修改配置 |
| `scansci_pdf_cache_clear` | 清除下载缓存 |

### Tor 管理

| 工具 | 描述 |
|------|------|
| `scansci_pdf_tor_install` | 自动下载安装 Tor Expert Bundle |
| `scansci_pdf_tor_start` | 启动内嵌 Tor SOCKS5 代理 |
| `scansci_pdf_tor_stop` | 停止 Tor 代理 |

---

## 下载策略

| 策略 | 描述 |
|------|------|
| `fastest`（默认） | 多数据源并行，最快获胜 |
| `oa_first` | 优先开放获取，Sci-Hub 兜底 |
| `scihub_only` | 仅使用 Sci-Hub |
| `legal_only` | 仅使用合法数据源（不含 Sci-Hub/LibGen） |

### 缓存与重下语义

- 每个输出目录的 `.doi_index.json` 记录 `{file, source, strategy, ts}`；条目超过 `cache_ttl_hours` 视为过期，自动重新下载（旧版纯路径格式自动兼容升级）。
- 显式策略（`scihub_only` / `legal_only` / `scihub_first`）与缓存记录策略不符时视为未命中重新下载，无需手动 `rm` 缓存；默认 `fastest` / `oa_first` 命中缓存。
- 全局下载缓存（`cache_ttl_hours`，默认 168 小时）按 mtime 过期，`scansci_pdf_cache_clear` 可整体清空。

### 机构会话自愈

下载进入机构阶段前会自动校验 WebVPN 会话（HTTP 探测登录页重定向）。判定过期且 `auto_relogin=true`（默认）时自动打开浏览器重新登录后再继续；新用户（无 cookie）或网络不可达时不会弹浏览器。CARSI 会话在 `login()` 内自带 24 小时新鲜度校验与自动重登。

---

## 付费墙：机构登录

### 统一登录（推荐）

只需一行，自动识别出版商、打开浏览器、引导完成 SSO 登录，cookie 跨所有下载复用：

```
scansci_pdf_login(identifier="10.1126/science.aec6396")
```

`identifier` 可以是 DOI 或出版商名（`elsevier`, `wiley`, `nature`, `springer`, `ieee`, `science`, `tandfonline`, `acs`, `rsc`, `aip`, `aps`, `iop`, `oxford`, `acm`）。

### WebVPN（高校代理）

通过中国高校机构代理访问论文全文：

```
1. scansci_pdf_vpnsci_schools(query="北京")      → 搜索学校
2. scansci_pdf_vpnsci_set_school(school="你的学校")
3. scansci_pdf_vpnsci_login                     → 浏览器 CAS 认证
4. scansci_pdf_vpnsci_test                      → 确认连接正常
```

支持 100+ 所高校。

### CARSI（出版商联邦认证）

直接通过出版商机构登录页面认证，无需 WebVPN 中转：

```
1. scansci_pdf_config_set(key="carsi_enabled", value="true")
2. scansci_pdf_config_set(key="carsi_idp_name", value="你的学校名称")
3. scansci_pdf_carsi_login(publisher="sciencedirect")
```

支持：sciencedirect, springer, wiley, ieee, tandfonline, nature

### EZProxy（图书馆代理）

通过学校图书馆 EZProxy 服务访问：

```
1. scansci_pdf_config_set(key="ezproxy_enabled", value="true")
2. scansci_pdf_config_set(key="ezproxy_login_url", value="https://libproxy.你的学校.edu.cn/login?url={url}")
3. scansci_pdf_ezproxy_login
```

---

## 配置参考

通过 `scansci_pdf_config_set` 修改：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `scihub_enabled` | `true` | 启用 Sci-Hub |
| `download_strategy` | `fastest` | 下载策略 |
| `output_dir` | `~/.scansci-pdf/papers` | PDF 输出目录 |
| `auto_rename` | `true` | 自动重命名 PDF |
| `network_proxy` | （空） | HTTP/SOCKS 代理地址 |
| `batch_workers` | `10` | 批量下载并发数 |
| `vpnsci_enabled` | `false` | 启用 WebVPN |
| `vpnsci_school` | （空） | WebVPN 学校名称 |
| `carsi_enabled` | `false` | 启用 CARSI 联邦认证 |
| `carsi_idp_name` | （空） | CARSI 机构名称 |
| `flaresolverr_url` | `http://localhost:8191/v1` | FlareSolverr 服务地址 |
| `use_tor_for_scihub` | `false` | Sci-Hub 使用 Tor |

---

## 高级功能（可选）

以下功能为可选项，适用于特定网络环境或高级需求。

### Docker 部署

适用于需要将 scansci-pdf 作为长期运行服务的场景，或不想在本机安装 Python 环境的用户。Docker 容器内置 MCP 服务器和 Tor 代理，数据通过 Docker 卷持久化。

```bash
docker compose up -d
```

| 服务 | 说明 | 端口 |
|------|------|------|
| `scansci-pdf` | MCP 服务器 | 8000 |
| `tor` | Tor SOCKS5 代理 | 1080 |

Docker 配置方式：

```json
{
  "mcpServers": {
    "scansci-pdf": {
      "command": "docker",
      "args": ["compose", "-f", "path/to/docker-compose.yml", "run", "--rm", "scansci-pdf"]
    }
  }
}
```

### Tor 匿名代理

Tor 用于在 Sci-Hub、LibGen 等网站被网络封锁的地区匿名访问。如果你的网络可以直连 Sci-Hub，则不需要 Tor。内嵌 Tor 会自动下载 Tor Expert Bundle（约 30MB），无需 Docker 或系统级安装。

```bash
# 首次使用：自动下载安装 Tor
scansci_pdf_tor_install

# 启动 Tor SOCKS5 代理
scansci_pdf_tor_start

# 如果 Tor 本身也被封锁（连接超时），启用 obfs4 桥接绕过
scansci_pdf_tor_start(use_bridges=true)

# 下载时通过 Tor 访问
scansci_pdf_download(identifier="10.1038/nature12373", use_tor=true)
```

二进制文件存储在 `~/.scansci-pdf/tor/`，不污染系统环境。

### FlareSolverr（Cloudflare 绕过）

当 Sci-Hub、LibGen 等站点触发 Cloudflare 防护时，FlareSolverr 可以自动绕过。需要 Docker 运行 FlareSolverr 服务：

```bash
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr
```

ScanSci PDF 会自动检测 Cloudflare 并回退到 FlareSolverr，无需手动配置。如果已安装 curl_cffi，会优先使用 TLS 指纹模拟（更快，无需 Docker）。

---

## 故障排查

**Sci-Hub 下载失败** — 运行 `scansci_pdf_health_check(detailed=true)` 查看数据源状态。域名轮换自动处理。如果遇到 Cloudflare 防护，安装 FlareSolverr 或 curl_cffi。

**Tor 连接失败** — 确认 Tor 运行在 `socks5h://127.0.0.1:1080`。如 Tor 也被封锁，使用 `scansci_pdf_tor_start(use_bridges=true)` 启用桥接。

**WebVPN 登录失败** — 需要 CloakBrowser（自动安装）。登录在你的浏览器中完成，密码不经过本工具。

**下载速度慢** — 运行 `scansci_pdf_health_check(detailed=true)` 检查数据源延迟。如 Sci-Hub 在你的网络被封锁，尝试 `legal_only` 策略或配置代理。

**网络问题** — 运行 `scansci_pdf_network_diagnose` 获取全面的连接诊断报告和针对性修复建议。

---

## 架构说明

本项目采用分层架构：

| 层级 | 内容 | 许可 |
|------|------|------|
| 公开层 | 所有 `.py` 源码、配置、文档 | Apache 2.0 |
| 保护层 | `_core/*.pyx`（Cython 源码） | 专有，不公开 |
| 分发层 | `_core/*.pyd`（编译二进制） | 随 PyPI 包分发 |

从 GitHub 克隆的用户使用纯 Python 回退实现（功能相同，性能略低）。从 PyPI 安装的用户自动获得编译版本。

---

## 赞助者

<a href="https://github.com/qwlei328-maker"><img src="https://avatars.githubusercontent.com/u/257463305?v=4" width="50" height="50" alt="qwlei328-maker" title="Natasha"/></a>
<a href="https://github.com/jingqingqiu1"><img src="https://avatars.githubusercontent.com/u/87510394?v=4" width="50" height="50" alt="jingqingqiu1" title="jingqingqiu1"/></a>
<a href="https://github.com/minqifeng"><img src="https://avatars.githubusercontent.com/u/61303605?v=4" width="50" height="50" alt="minqifeng" title="minqifeng"/></a>

---

## 致谢

本项目在开发过程中参考和借鉴了以下开源项目：

- **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** — 早期反 bot 绕过架构设计
- **[ref-downloader](https://github.com/ltczding-gif/ref-downloader)** — Publisher 专用下载策略（Elsevier crasolve 检测、Wiley PDFDirect、AIP loading page 等）
- **[paper-fetch-skill](https://github.com/Dictation354/paper-fetch-skill)** — 论文获取 Agent Skill 设计
- **[paper-fetcher](https://github.com/fermionoid/paper-fetcher)** — 论文下载流程参考
- **[cloakbrowser](https://github.com/CloakHQ/CloakBrowser)** — Chromium stealth 浏览器引擎

感谢以上项目作者的开源贡献。

---

## 许可证

[Apache License 2.0](LICENSE)

例外：`src/scansci_pdf/_core/` 中的 Cython 编译扩展（`.pyd`/`.so`）为预编译二进制，仅通过 PyPI 分发。其 Cython 源码（`.pyx`）为专有代码，不包含在本仓库中。
