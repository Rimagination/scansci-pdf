<div align="center">
  <img src="./assets/brand/scansci-pdf-icon.png" alt="ScanSci PDF" width="96" />
  <h1>ScanSci PDF</h1>

  <a href="https://pypi.org/project/scansci-pdf/"><img alt="PyPI" src="https://img.shields.io/pypi/v/scansci-pdf?style=flat-square" /></a>
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-0F766E?style=flat-square" /></a>

  不用懂代码，也不用记命令。把论文题目、链接或一份清单交给 Agent，它帮你找、帮你下、帮你整理。

  [这是什么](#这是什么) · [能做什么](#能做什么) · [开始使用](#开始使用) · [常见问题](#常见问题) · [使用边界](#使用边界) · [社区](#交流群--community) · [进阶参考](docs/REFERENCE.md)
</div>

## 这是什么

ScanSci PDF 是一个论文助手。你告诉它想下哪篇论文，或者想做哪个主题，它会自己去试：

- 先找免费的版本、预印本；
- 再试你学校订阅的渠道；
- 找到后自动下载、按“作者_年份_标题”命名；
- 需要的话，顺手导出引用、抓补充材料、整理成可读的 Markdown。

**你不需要懂代码，也不需要记命令。** 装好之后，对 Agent 说一句话就行——Claude Code、Codex、Cursor、ZCode、DeepSeek Harness 都可以。

> 不知道 DOI 也没关系。DOI 就是论文页面上以 `10.` 开头的那串编号；不知道的话，直接发论文标题或链接即可。

### 适合谁

- 需要下论文、下整批清单的大学生、研究生、老师和科研人员。
- 不想在十几个网站之间来回切换的人。
- 需要学校权限、中文文献、引用整理或综述取证的人。
- 已经在用 AI Agent，想让它顺手把论文也下了的人。

### 不适合谁

- 只需要 PDF 阅读器或文献管理器——请用 Zotero、ReadPaper 等专门工具。
- 需要 OCR 扫描版 PDF 全文识别——本工具不内置 OCR。
- 没有合法访问权限、且不愿意自行确认授权与合规性的人。

## 能做什么

| 你想要 | 你可以这样说 | 它会做什么 |
|---|---|---|
| **下这一篇** | “帮我下 10.1038/nature12373 这篇论文” | 自动找最快能下的来源，得到命名规整的 PDF |
| **下一整批** | “这个 3000 篇的 xlsx，先分类哪些能直接下、哪些要学校权限，再分批下载” | 去重、补全信息、分批下载，最后给你成功/失败清单 |
| **先搜再下** | “搜 2023 年以来‘城市热岛’被引最高的 10 篇，然后下载” | 多个学术搜索引擎一起查，结果让你确认后再下 |
| **用学校权限** | “用北大的学校权限下这篇 Elsevier 论文” | 打开浏览器让你登录一次学校账号，之后自动复用 |
| **中文论文** | “下这篇 10.3969/j.issn.1674-4993.2024.02.008” | 自动切到中文文献通道（国家哲社文献中心 / 期刊官网 / 知网 / 万方 / 维普） |
| **找原文证据** | “找几段讨论石墨烯衰减机制的原文，要页码” | 返回带论文、页码和原文位置的证据片段 |
| **整理引用** | “把这批论文的引用导出来，推到 Zotero” | 导出常用引用格式，或直接推送 Zotero |
| **下不下来** | “这篇下不下来，帮我看看卡在哪” | 告诉你每个渠道为什么失败，以及下一步怎么做 |

> 没有说“下载”的请求，它不会自动批量下载；检索结果需要你确认后再保存。

## 开始使用

### DeepSeek Harness（DSH）

DSH 可以直接从插件管理界面安装，不需要手动配置：

1. 侧边栏 **Plugins** → **Add plugin**
2. 输入 **`scansci-pdf`**
3. 安装源保持默认，或按网络情况选 **中国大陆镜像源**
4. 点击 **Install**，确认插件列表中 bundle 为 **On**

安装后会自动注册论文下载和中文清单整理两个技能，通常不需要重启。

### ZCode

打开插件市场，搜索 **`scansci-pdf`**，点安装即可。

### Claude Code / Codex / Cursor

对 Agent 说：

> 帮我安装或更新这个插件：https://github.com/Rimagination/scansci-pdf

Agent 会自己完成安装和注册。装好后，先让它检查一次环境：

> 帮我检查一下论文下载环境

它会告诉你还缺什么、哪些功能需要补装。

## 你会得到什么

- **PDF**：默认按“作者_年份_标题”命名，直接归档。
- **引用**：常用引用格式，可推送 Zotero。
- **补充材料**：需要时一起下载。
- **Markdown**：需要时把 PDF 转成更适合 AI 阅读的文本。
- **清单**：批量任务结束后，给你一份成功/失败结果。
- **失败原因**：不会只说“失败”，会告诉你每个来源为什么没成功。

## 需要学校权限时

有些论文需要学校订阅才能下。遇到这种情况：

1. 工具会打开浏览器，让你登录学校账号；
2. 登录一次后，后续下载会自动复用；
3. 没有学校账号时，只能获取免费版本、预印本或开放获取版本。

中文的知网、万方、维普通常需要校园网或学校账号；没有权限时，工具会明确告诉你。

## 可选增强（不配置也能用）

- **常用出版商的官方接口**：常下 Elsevier / Springer 论文的话，配置官方 Key 后速度更快，通常 1–2 秒一篇。
- **更强的语义检索**：开启 Sciverse 后，可以直接找“哪篇论文的哪一段说了什么”，适合写综述和找证据。

这两项都是可选的，不影响基础下载。具体怎么配置见[进阶参考](docs/REFERENCE.md)。

## 常见问题

| 问题 | 回答 |
|---|---|
| 要付费吗？ | 软件本身免费、开源（MIT）。Elsevier / Springer / Sciverse 是可选第三方服务，有各自的配额和条款。 |
| 我不会找 DOI 怎么办？ | 直接发论文标题、网页链接或 PDF 所在页面即可。 |
| 没有学校账号能下吗？ | 开放获取、arXiv、部分中文免费源可以；付费墙论文需要学校订阅。 |
| 会不会一下下载很多？ | 不会。批量任务开始前会让你确认清单和范围。 |
| 下不下来怎么办？ | 让 Agent 诊断；它会告诉你卡在登录、网络、反爬还是权限。 |
| 支持中文论文吗？ | 支持，包括 NCPSSD、中文期刊官网、知网、万方、维普。 |
| 会用盗版来源吗？ | 默认配置包含灰色来源；你可以要求“只用合法来源”，它就会只用开放获取和机构渠道。 |
| 安装后没反应？ | 重启 Agent 或插件；仍然不行就看[进阶参考](docs/REFERENCE.md)里的排障。 |

## 使用边界

- 请在你有权访问的范围内使用。学校订阅论文需要你自己的学校账号或机构网络。
- 没有订阅或合法开放副本时，付费墙全文无法凭空获得。
- 灰色来源（Sci-Hub / LibGen）默认按配置启用；如果你只想使用合法来源，直接告诉 Agent“只用合法来源”。
- 扫描版 PDF 可能没有文本层，Markdown 导出或语义检索效果会受影响。

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

<details>
<summary><strong>贡献者与致谢</strong></summary>

<a href="https://github.com/annsyun"><img src="https://avatars.githubusercontent.com/u/151621087?v=4" width="50" height="50" alt="annsyun" title="annsyun"/></a>

- **[annsyun](https://github.com/annsyun)** — SAGE 中国站机构授权下载通道
- 感谢 FlareSolverr、ref-downloader、paper-fetch-skill、paper-fetcher、cloakbrowser 等开源项目

</details>

## 许可证

[MIT License](LICENSE)

## 进阶参考

- [完整技术参考](docs/REFERENCE.md)：更细的配置、支持的数据源、机构通道和排障。
- [反爬与镜像维护手册](docs/PLAYBOOK.md)：给维护者和高级用户。
