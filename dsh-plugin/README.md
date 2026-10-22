# ScanSci PDF for DeepSeek Harness

把 ScanSci PDF 接入 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）的 bundle。安装一次即可同时获得：

- **MCP server**：`scansci-pdf run`（stdio），18 个 `scansci_pdf_*` 工具，在 DSH 中呈现为 `mcp__scansci__scansci_pdf_*`。
- **两个 skill**：`scansci-pdf`（下载、检索、引文、机构渠道、排障）和 `scansci-sort`（大清单分桶嗅探）。

DSH 与 Claude Code、Codex、ZCode 共用同一份 Python 包与同一个 MCP server；本目录只负责 DSH 的注册层。

## 前置条件

需要 Python 3.11+，并在同一个 Python 环境里安装 ScanSci PDF：

```bash
python -m pip install "scansci-pdf[cloakbrowser,vpnsci]"
# 从本仓库开发时：
python -m pip install -e <本仓库路径>
```

确认 CLI 可用：

```bash
scansci-pdf run --help
```

> MCP 由 DSH 启动，不需要手动常驻。`scansci-pdf` 必须在 DSH 进程的 `PATH` 上；若使用虚拟环境，见下方“CLI 不在 PATH”。npm 只安装 DSH 注册层，不会安装 Python CLI。

## 从 npm 安装（推荐）

DSH 的 **Add plugin** 接受 npm 包名。本包的发布名是 `scansci-pdf`（与 PyPI 的 Python 包同名，两个生态互不冲突）：

1. 侧边栏 **Plugins** → **Add plugin**
2. 输入 `scansci-pdf`（或固定版本 `scansci-pdf@1.18.2`）
3. **Install**，安装完成后确认 bundle 为 **On**

等价命令：

```bash
dsh plugin add scansci-pdf
```

升级时先 **Remove** 再安装新版本；**Update** 只出现在 DSH 官方 bundle 上。

如果 registry 上还没有该版本（例如刚改完包名、尚未发布），或者你的网络访问不了 npmjs.com，可以先用下面的本地目录方式。另一个不依赖 npm 账号的办法：`npm pack` 生成 `scansci-pdf-1.18.2.tgz`，上传到 GitHub Release，然后在 **Add plugin** 里直接填 tarball 的 URL（DSH 支持 package、Git、tarball、本地目录四种来源）。

## 发布到 npm（维护者）

发布名固定为 `scansci-pdf`，版本与仓库的 `pyproject.toml` 保持一致。

```bash
cd dsh-plugin
npm publish --access public
```

`prepack` 会把 `../skill/SKILL.md` 和 `../zcode-plugin/skills/scansci-sort/SKILL.md` 复制进 tarball，因此 npm 包是自包含的。发布前可先用 `npm pack --dry-run` 检查文件清单（应包含 `cordis.patch.yml`、`index.js`、`icon.png`、`locale/`、`skills/`、`README.md`、`LICENSE`）。

也可以走 GitHub Actions：**Publish DSH bundle to npm** → Run workflow → 填版本号。仓库需要配置 secret `NPM_TOKEN`（npm granular access token，至少对 `scansci-pdf` 有 publish 权限）。

## 从本地目录安装（开发或尚未发布时）

DSH 的插件管理会把本地目录以 `link:` 方式装进当前 profile，因此**本仓库目录需要保留在原位**（不要只复制 `dsh-plugin/`）。

**方式一：DSH 插件管理界面**

1. 侧边栏 **Plugins** → **Add plugin**
2. 填入本目录的绝对路径，例如 `D:\path\to\scansci-pdf\dsh-plugin`
3. **Install**，安装完成后确认 bundle 为 **On**

**方式二：让 agent 安装**

```text
安装这个 DSH bundle：<本仓库绝对路径>/dsh-plugin
```

等价的工具调用是 `plugin_manager` 的 `install_bundle`，`target` 传上面的绝对路径。

安装后通常无需重启（profile 启用 HMR 时立即生效）；DSH 会话的技能目录会出现 `scansci-pdf` 与 `scansci-sort`。

## 验证

```text
用 scansci 查一下 "urban heat island" 2023 年以来的 3 篇论文
```

或直接调用 `mcp__scansci__scansci_pdf_diagnostics`（`check="health"`）。健康检查里 `tor` / `browser` 显示 `unavailable` 是正常的：它们是可选组件，不影响 OA、出版商 API 和检索。

## 配置

| 场景 | 做法 |
|---|---|
| `scansci-pdf` 不在 PATH（venv / conda） | 在 Plugins 中打开本 bundle 的 `scansci-pdf-mcp` 行配置，把 `command` 改成解释器绝对路径，`args` 改成 `["-m", "scansci_pdf", "run"]`；或直接把 venv 的 `Scripts\scansci-pdf.exe` 填进 `command` |
| 宿主策略不允许灰源 | `scansci-pdf config-cmd download_strategy legal_only` |
| 批量任务耗时较长 | bundle 已把 MCP 单次调用超时设为 30 分钟（`toolCallTimeoutMs: 1800000`），无需再调 |
| 机构登录 / 出口 IP | 见主 README 的“机构通道”和“浏览器”两节；登录必须发生在 `scansci_pdf_login` 打开的浏览器里 |

## 与其他 agent 的关系

| 宿主 | 注册方式 |
|---|---|
| DeepSeek Harness | 本目录 `dsh-plugin/`（MCP + skills） |
| Claude Code | `.claude/skills/` + 标准 MCP 配置 |
| Codex App | `.codex-plugin/plugin.json` + `.mcp.json` + `skills/` |
| ZCode | `zcode-plugin/` |
| 其他标准 MCP 客户端 | `.mcp.json` 里的 `scansci-pdf run` |

## 卸载

Plugins → 本 bundle → **Remove**。卸载只移除 DSH 注册，不删除 Python 包、下载的 PDF 或 `~/.scansci-pdf` 配置。
