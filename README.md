<div align="center">
  <img src="./assets/brand/scansci-pdf-icon.png" alt="ScanSci PDF" width="96" />
  <h1>ScanSci PDF</h1>

  <a href="https://pypi.org/project/scansci-pdf/"><img alt="PyPI" src="https://img.shields.io/badge/PyPI-scansci--pdf-2563EB?style=flat-square" /></a>
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-0F766E?style=flat-square" /></a>
  <a href="./.codex-plugin/plugin.json"><img alt="Plugin" src="https://img.shields.io/badge/plugin-Codex%20%7C%20ZCode-2563EB?style=flat-square" /></a>
  <a href="./.mcp.json"><img alt="MCP" src="https://img.shields.io/badge/MCP-stdio-111827?style=flat-square" /></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-45%20tools-111827?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square" />

  让 Agent 替你下载学术论文 —— 13+ 数据源并行竞速，100+ 高校机构通道，一句自然语言拿下整份文献清单。

  [能力](#能力) · [快速开始](#快速开始) · [怎么用](#怎么用) · [机构通道](#机构通道) · [社区](#交流群--community) · [致谢](#致谢)
</div>

## 能力

给出 DOI、arXiv 号或一份文献清单，ScanSci PDF 会自动挑最快能下的那条路：OA 直链、预印本、出版商 API、你的学校通道都试一遍，付费墙自动路由，结果落地成规整命名的 PDF 文件。

- **单篇，一句话的事** — DOI / arXiv 进，`作者_年份_标题.pdf` 出；BibTeX / RIS / EndNote 引文顺手导出，可直推 Zotero。
- **清单，整批拿下** — APA / BibTeX / DOI 列表直接喂，自动补全缺失 DOI；上千篇先分「OA / 灰色源 / 需机构」三桶再分批下载，不瞎跑不浪费。
- **付费墙，走你的学校** — 100+ 高校 WebVPN、CARSI 联邦认证、EZProxy、Elsevier API 快速通道（1–2 秒/篇）；登录在你自己的浏览器完成，密码不经过工具。
- **对抗与自愈全自动** — Cloudflare / CAPTCHA / SSO 分层处理；出版商封 IP 自动停损；机构会话过期自动重登，登录一次全程复用。
- **Agent 原生** — 标准 MCP 服务器、45 个工具即装即用；Codex App、ZCode、Claude Code 都有现成的插件或配置。

## 快速开始

### 安装：按你的 Agent 选一条

**Codex App / Codex CLI**（Windows 一键：克隆 + 装环境 + 注册插件）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command '$p=Join-Path $HOME "plugins\scansci-pdf"; if(Test-Path $p){ git -C $p pull --ff-only } else { git clone --branch main --single-branch https://github.com/Rimagination/scansci-pdf.git $p }; & (Join-Path $p "scripts\install-scansci-pdf.ps1")'
```

**ZCode** — 插件市场搜 `scansci-pdf` 一键安装，MCP、skills、快捷命令一次带齐。

**其他 MCP 客户端**（Claude Desktop / Claude Code / Cursor / Windsurf / Cline / Cherry Studio…）：

```bash
pip install scansci-pdf
```

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

<details>
<summary><strong>插件里有什么 / 手动安装</strong></summary>

仓库为各家 Agent 提供了现成的插件化入口，装完即被自动识别：

| Agent | 入口 |
|---|---|
| Codex App / CLI | `.codex-plugin/plugin.json` + `.mcp.json`（自动注册 MCP server）+ `skills/` |
| ZCode | [`zcode-plugin/`](./zcode-plugin/)（marketplace 包：skills + `/scansci` 快捷命令） |
| Claude Code | `.claude/skills/` + 上面的标准 MCP 配置 |

手动安装（Codex）：把仓库放到 `%USERPROFILE%\plugins\scansci-pdf` → `python -m pip install -e .` → 在插件管理页刷新个人 marketplace；Codex CLI 可用时执行 `codex plugin add scansci-pdf@local-plugins`。

</details>

<details>
<summary><strong>其他部署方式</strong>（HTTP 远程 / Docker / Web UI）</summary>

HTTP 模式（远程部署或不支持 stdio 的场景，默认只监听本机）：

```bash
scansci-pdf run --mode streamable_http --host 127.0.0.1 --port 8000
```

Web UI：

```bash
scansci-pdf web --port 8080   # 浏览器打开 http://localhost:8080
```

Docker（内置 MCP 服务器 + Tor，数据卷持久化）：

```bash
docker compose up -d
```

</details>

## 怎么用

装好后，对着 Agent 说一句话就行。这些例子覆盖最常见的日常场景：

```text
帮我下载 10.1038/nature12373 这篇论文

这个清单里有 3000 篇文献，先分类哪些能直接下、哪些要走机构权限，然后分批下载

搜一下 "urban heat island" 2023 年以来被引最高的 10 篇论文，并下载

用北大的 WebVPN 登录，把这篇 Elsevier 的论文下下来

把刚才那批论文的 BibTeX 导出，再推送到 Zotero

这篇下载失败了，帮我诊断一下网络问题
```

不动 Agent、直接命令行也行：

```bash
scansci-pdf get 10.1038/nature12373            # 单篇下载
scansci-pdf batch 文献清单.xlsx --lanes        # 批量下载（xlsx/csv/队列通用，四车道调度）
scansci-pdf search "carbon cycle" --out q.txt  # 检索结果落成队列文件，直接接 batch
scansci-pdf check                             # 依赖与环境体检
```

<!-- mcp-tools:start -->
<details>
<summary><strong>MCP 工具全表</strong>（17 个）</summary>

| 工具 | 用途 |
|---|---|
| `scansci_pdf_batch_download` | Download multiple papers: pass identifier list, or one file (txt/DOI list, csv/xlsx table, BibTeX, APA) with auto DOI resolution and resumable batches. |
| `scansci_pdf_cache_clear` | Clear paper download cache. |
| `scansci_pdf_channel_status` | Status of an institutional channel, the browser backend, or a WebVPN connectivity test. |
| `scansci_pdf_citation` | Citation (bibtex/ris/endnote) or structured metadata for a paper. |
| `scansci_pdf_config` | Get (sensitive values masked) or set scansci-pdf configuration. |
| `scansci_pdf_diagnostics` | Diagnostics: source health, network fixes, adaptive scores, environment, one-click setup. |
| `scansci_pdf_download` | Download a single academic paper by DOI or arXiv ID. |
| `scansci_pdf_elsevier_setup` | Setup Elsevier API key for ScienceDirect fast-track access. |
| `scansci_pdf_expand_citations` | Search plus backward/forward citation chasing (Semantic Scholar / OpenCitations). |
| `scansci_pdf_find` | ScanSci Find discovery engine: plan, estimate, smoke-test, or calibrate a search. |
| `scansci_pdf_login` | Unified institutional login — paywalled PDFs need this once; cookies are reused. |
| `scansci_pdf_parse_list` | Parse a paper list file (APA references, BibTeX, or DOI list) and extract metadata. |
| `scansci_pdf_prepare_queue` | Prepare a download queue: verify identifiers, resolve OA locations, or build from a search. |
| `scansci_pdf_schools` | Search or set supported WebVPN universities (100+ Chinese universities). |
| `scansci_pdf_search` | Search for academic papers by keyword or author using OpenAlex API. |
| `scansci_pdf_tor` | Embedded Tor SOCKS5 proxy: install, start, or stop. |
| `scansci_pdf_zotero_push` | Push a downloaded paper to Zotero. |

</details>
<!-- mcp-tools:end -->

## 机构通道

付费墙论文优先走这四条机构通道。登录都在你自己的浏览器里完成，密码不经过本工具。

<!-- publisher-table:start -->
| 策略 | DOI 前缀 | 域名 |
|---|---|---|
| ACM | `10.1145/` | `dl.acm.org`, `acm.org` |
| ACS | `10.1021/` | `pubs.acs.org` |
| AIP | `10.1063/` | `pubs.aip.org`, `aip.org` |
| APS | `10.1103/` | `journals.aps.org`, `aps.org` |
| ASCE | `10.1061/` | `ascelibrary.org` |
| Copernicus | `10.5194/` | `copernicus.org` |
| Elsevier | `10.1016/`, `10.1016/j.`, `10.1016/j.cell`, `10.1016/j.oneear`, `10.1016/j.cels`, `10.1016/j.cub`, `10.1016/j.neuron`, `10.1016/j.molcel`, `10.1016/j.devcel`, `10.1016/j.immuni`, `10.1016/j.chom`, `10.1016/j.cmet`, `10.1016/j.stem`, `10.1016/j.celrep`, `10.1016/j.isci`, `10.1016/j.xcr`, `10.1016/j.heliyon`, `10.1016/j.ajhg` | `sciencedirect.com`, `linkinghub.elsevier.com` |
| Generic | — | — |
| IEEE | `10.1109/` | `ieeexplore.ieee.org`, `ieee.org` |
| IOP | `10.1088/` | `iopscience.iop.org`, `iop.org` |
| Nature | `10.1038/` | `nature.com` |
| Oxford | `10.1093/` | `academic.oup.com`, `oup.com` |
| Royal Society | `10.1098/` | `royalsocietypublishing.org` |
| RSC | `10.1039/` | `pubs.rsc.org`, `rsc.org` |
| SAGE | `10.1177/` | `journals.sagepub.com`, `sagepub.com` |
| Science | `10.1126/` | `science.org`, `sciencemag.org` |
| Springer | `10.1007/`, `10.1023/` | `link.springer.com`, `springer.com` |
| Tandfonline | `10.1080/` | `tandfonline.com` |
| Wiley | `10.1002/`, `10.1111/` | `onlinelibrary.wiley.com`, `wiley.com` |
<!-- publisher-table:end -->

### Elsevier API（推荐，无需浏览器）

ScienceDirect / Cell Press 等 Elsevier 论文走 API 直接下载，速度从 15–30 秒降到 1–2 秒。Key 申请免费、个人邮箱即可：

1. 访问 [Elsevier Developer Portal](https://dev.elsevier.com/) 创建应用，勾选 **ScienceDirect Article Retrieval**
2. 配置 Key：`scansci_pdf_elsevier_setup`（MCP 会打开浏览器引导）或 `scansci_pdf_config(key="elsevier_api_key", value="...")`

### WebVPN（高校代理）

```text
1. scansci_pdf_schools(action="search", query="北京")  → 搜你的学校
2. scansci_pdf_schools(action="set", school="你的学校")
3. scansci_pdf_login(kind="webvpn")                    → 浏览器完成 CAS 认证
4. scansci_pdf_channel_status(kind="webvpn_test")      → 确认连接正常
```

支持 100+ 所中国高校，命令行等价：`scansci-pdf schools 北京` / `scansci-pdf setup 北京航空航天大学`。

### CARSI（出版商联邦认证）

```text
1. scansci_pdf_config(key="carsi_enabled", value="true")
2. scansci_pdf_config(key="carsi_idp_name", value="你的学校名称")
3. scansci_pdf_login(kind="carsi", publisher="sciencedirect")
```

支持 sciencedirect、springer、wiley、ieee、tandfonline、nature 等。

### EZProxy（图书馆代理）

```text
1. scansci_pdf_config(key="ezproxy_enabled", value="true")
2. scansci_pdf_config(key="ezproxy_login_url", value="https://libproxy.你的学校.edu.cn/login?url={url}")
3. scansci_pdf_login(kind="ezproxy")
```

## 配置与策略

<details>
<summary><strong>配置参考</strong>（完整表）</summary>

通过 `scansci_pdf_config` 或 `scansci-pdf config-cmd` 修改：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `output_dir` | `~/.scansci-pdf/papers` | PDF 保存目录 |
| `auto_rename` | `true` | 自动按作者/标题重命名 |
| `download_strategy` | `fastest` | 下载策略 |
| `scihub_enabled` | `true` | 启用 Sci-Hub/LibGen 类来源 |
| `network_proxy` | 空 | HTTP/SOCKS 代理地址 |
| `proxy_pool` | 空 | 逗号分隔的代理列表；非空时批量下载按代理轮换出口 IP |
| `batch_workers` | `10` | 批量下载并发数（被封 IP 时建议调低到 2） |
| `request_delay_min` | `2.0` | 请求间随机延迟下限（秒） |
| `request_delay_max` | `5.0` | 请求间随机延迟上限（秒） |
| `vpnsci_enabled` / `instsci_enabled` | `false` | 启用 WebVPN |
| `vpnsci_school` / `instsci_school` | 空 | WebVPN 学校名称 |
| `carsi_enabled` | `false` | 启用 CARSI |
| `carsi_idp_name` | 空 | CARSI 机构名称 |
| `auto_relogin` | `true` | 机构会话自愈：下载前自动校验 WebVPN 会话，明确过期才弹浏览器重登 |
| `cache_ttl_hours` | `168` | 下载缓存 TTL（小时），设 0 禁用 |
| `elsevier_api_key` | 空 | Elsevier / ScienceDirect API Key |
| `elsevier_insttoken` | 空 | Elsevier institutional token，可选 |
| `use_tor_for_scihub` | `false` | Sci-Hub 走 Tor |
| `flaresolverr_url` | `http://localhost:8191/v1` | FlareSolverr 服务地址 |
| `browser_headless` | `false` | 浏览器是否无头运行 |
| `browser_humanize` | `true` | 浏览器人性化操作 |

</details>

<details>
<summary><strong>下载策略与缓存语义</strong></summary>

| 策略 | 描述 |
|------|------|
| `fastest`（默认） | 多数据源并行，最快获胜 |
| `oa_first` | 优先开放获取，Sci-Hub 兜底 |
| `scihub_only` / `scihub_first` | 仅用 / 优先 Sci-Hub |
| `legal_only` | 仅使用合法数据源（不含 Sci-Hub/LibGen） |

- 每个输出目录的 `.doi_index.json` 记录 `{file, source, strategy, ts}`；超过 `cache_ttl_hours` 视为过期自动重下。
- 显式策略与缓存记录的策略不符时视为未命中重新下载，无需手动删缓存。
- 下载进入机构阶段前自动校验 WebVPN 会话（HTTP 探测重定向），判定过期且 `auto_relogin=true` 时自动重登；CARSI 自带 24 小时新鲜度校验。
- 与 ScanSci Find 闭环：`find` 产出候选 → `manifest` / `batch` 下载并写 `download_results.json` → `scansci-find reconcile` 回写候选状态。（Find 系命令需要可选的 scansci-find CLI：`pip install scansci-find`，未安装时用 search/verify/resolve-oa/build-queue 本地降级链。）

</details>

<details>
<summary><strong>批量调优：代理池与封 IP 停损</strong></summary>

**自动停损**（默认开启）：批量任务连续 3 次检测到 IP 被封（ACS 封锁页 / 403 / 429）会自动取消剩余下载，终端提示：

```
⚠ 已自动停止：连续检测到 IP 被出版商封禁（N 篇返回 ip_blocked），剩余任务已取消。
```

**降低被封概率** —— 让请求像人：

```bash
scansci-pdf config-cmd batch_workers 2          # 调低并发（默认 10）
scansci-pdf config-cmd request_delay_min 5      # 拉大随机延迟下限（默认 2）
scansci-pdf config-cmd request_delay_max 12     # 拉大随机延迟上限（默认 5）
```

**代理池轮换（进阶）**：多个代理轮换出口 IP，每个代理一个独立浏览器上下文，登录一次 cookies 全复用；连续 3 次被封的代理自动剔除：

```bash
scansci-pdf config-cmd proxy_pool "socks5://1.1.1.1:1080,http://2.2.2.2:8080"
```

> ⚠ **权衡**：同一登录态从多个 IP 并发访问，少数出版商可能视为异常。机构对这种检测敏感的话，保持 `proxy_pool` 为空即可。

</details>

## 故障排查

| 现象 | 先做 |
|---|---|
| 下载失败 | `scansci-pdf check`，会话问题再跑 `scansci-pdf session-doctor` |
| Agent 说 Elsevier 需要 insttoken | 不需要：API key + 校园网出口即可；NOT_ENTITLED=未连校园网或学校未订阅，连网重试或转其他渠道 |
| 数据源全红 / 打不开 | Agent 里调 `scansci_pdf_diagnostics(check="network")`，给出针对性修复建议 |
| 以前能下的站点突然 403 / 弹 Cloudflare | 大概率 cloakbrowser 过旧：`pip install -U cloakbrowser`（`scansci-pdf browser-doctor` 会标出 outdated） |
| WebVPN / CARSI 登录失败 | `pip install "scansci-pdf[cloakbrowser,instsci]"`，在可见浏览器完成登录后重试 |
| Sci-Hub 连不上 | 内嵌 Tor：`scansci_pdf_tor(action="start", use_bridges=true)`，或配置 `network_proxy` |
| 下载速度慢 | 配置 Elsevier API Key；调 `batch_workers`；数据源延迟用 `scansci_pdf_diagnostics(check="health")` 查看 |

<details>
<summary><strong>ACS 提示 IP Address Blocked 怎么办</strong></summary>

批量下载 ACS（`pubs.acs.org`）等出版商时可能遇到整页报错：

> IP Address Blocked — Your IP address has been blocked automatically due to unusual behavior.

这是出版商的自动反爬。机构出口 IP（如校园网）是共享的，一个人触发就可能让整段 IP 被封。

**立即解除**（封禁在出版商侧，代码改不了）：

- 邮件 `ipblock@acs.org` 申诉，附上被封 IP，通常 1–3 个工作日解封
- 换出口 IP（代理 / 手机热点）可绕过，但会失去机构订阅授权，只能下 OA 论文
- 部分封锁会在 24–48 小时后自动解除

**预防**：见上方「批量调优」折叠块 —— 调低并发、拉大延迟、启用代理池。

</details>

<details>
<summary><strong>工作原理：五层竞速</strong></summary>

下载一篇论文时，ScanSci PDF 同时启动多个数据源，按优先级分层竞速，第一个成功立即返回：

```
Tier 1 (4s)  ─ 出版商直链（OA/机构访问）
Tier 2 (5s)  ─ OpenAlex / Unpaywall / DOAJ
Tier 3 (8s)  ─ EuropePMC / CORE / PMC / arXiv
Tier 4 (25s) ─ LibGen / Sci-Hub（带 FlareSolverr 绕过）
Tier 5 (20s) ─ WebVPN / CARSI 机构代理
```

架构说明：公开层（`.py` 源码、配置、文档）Apache 2.0；`_core/*.pyx`（Cython 源码）为专有层不公开；PyPI 安装自带编译二进制（`.pyd`），GitHub 克隆使用纯 Python 回退实现（功能相同，性能略低）。

</details>

## 交流群 / Community

扫码加入微信交流群，一起聊 **AI for Science** —— 偏 AI 应用与科研工具，也欢迎讨论 ScanSci PDF 的用法、bug 和需求。

<table>
  <tr>
    <td width="250" align="center">
      <img src="assets/brand/wechat-group-qr.jpg" alt="微信群二维码" width="200">
      <br>
      <sub><strong>微信交流群</strong> / WeChat Group<br>群码 7 天有效，过期会更新</sub>
    </td>
    <td width="250" align="center">
      <img src="assets/brand/wechat-contact-qr.jpg" alt="群主微信" width="200">
      <br>
      <sub><strong>加群主微信</strong> / Contact<br>群码失效？扫码备注 <em>scansci</em> 拉你进群</sub>
    </td>
    <td valign="middle">
      <p><strong>群聊方向</strong></p>
      <ul>
        <li>AI 在科研场景的落地与工具链</li>
        <li>论文检索、下载、阅读、整理的工作流</li>
        <li>ScanSci PDF 使用问题与改进建议</li>
      </ul>
      <p><sub>二维码过期会更新；着急进群可直接加群主微信。</sub></p>
    </td>
  </tr>
</table>

更偏好异步交流？欢迎直接在 [Issues](https://github.com/Rimagination/scansci-pdf/issues) 或 [Discussions](https://github.com/Rimagination/scansci-pdf/discussions) 区开贴。

## 赞助者

<a href="https://github.com/qwlei328-maker"><img src="https://avatars.githubusercontent.com/u/257463305?v=4" width="50" height="50" alt="qwlei328-maker" title="Natasha"/></a>
<a href="https://github.com/jingqingqiu1"><img src="https://avatars.githubusercontent.com/u/87510394?v=4" width="50" height="50" alt="jingqingqiu1" title="jingqingqiu1"/></a>
<a href="https://github.com/minqifeng"><img src="https://avatars.githubusercontent.com/u/61303605?v=4" width="50" height="50" alt="minqifeng" title="minqifeng"/></a>

## 致谢

本项目在开发过程中参考和借鉴了以下开源项目：

- **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** — 早期反 bot 绕过架构设计
- **[ref-downloader](https://github.com/ltczding-gif/ref-downloader)** — Publisher 专用下载策略（Elsevier crasolve 检测、Wiley PDFDirect、AIP loading page 等）
- **[paper-fetch-skill](https://github.com/Dictation354/paper-fetch-skill)** — 论文获取 Agent Skill 设计
- **[paper-fetcher](https://github.com/fermionoid/paper-fetcher)** — 论文下载流程参考
- **[cloakbrowser](https://github.com/CloakHQ/CloakBrowser)** — Chromium stealth 浏览器引擎

感谢以上项目作者的开源贡献。

## 许可证

[Apache License 2.0](LICENSE)

例外：`src/scansci_pdf/_core/` 中的 Cython 编译扩展（`.pyd`/`.so`）为预编译二进制，仅通过 PyPI 分发。其 Cython 源码（`.pyx`）为专有代码，不包含在本仓库中。

## Star History

<p align="center">
  <a href="https://www.star-history.com/?repos=Rimagination%2Fscansci-pdf&type=date&legend=top-left">
    <img alt="Star History Chart" src="scansci-pdf-star-history-crop.png" width="600" />
  </a>
</p>

> Interactive/live chart: [Star History](https://www.star-history.com/?repos=Rimagination%2Fscansci-pdf&type=date&legend=top-left)
