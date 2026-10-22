---
name: dsh-npm
description: Package an MCP server or agent plugin as a DeepSeek Harness (DSH) Cordis bundle and publish it to registry.npmjs.org so DSH's Plugins → Add plugin installs it by package name. Use when adapting a plugin to DSH, making a DSH bundle npm-installable, publishing a DSH plugin, or debugging a bundle that fails after npm install.
whenToUse: The user wants others to install a plugin through DSH's Plugins UI, or wants to turn an existing MCP server, CLI, or set of skills into an npm-installable DSH bundle.
---

# DSH 插件适配与 npm 发布

## 目标

产出一个 `name@version`，在 `registry.npmjs.org` 上可安装；用户在 DSH 的 **Plugins → Add plugin** 里只填包名（或 `包名@版本`）就能得到：

- 一个 stdio MCP server，工具以 `mcp__<serverName>__<tool>` 暴露；
- 一个或多个 skill，出现在会话技能目录；
- 可选的 bundle 标题、描述、图标。

DSH 的 Add plugin 支持四种来源：npm 包名、Git 地址、tarball URL、本地绝对路径。npm 包名只是其中一种，但它是第三方安装体验最好的一种。

## 先做三个决定

| 问题 | 建议 |
|---|---|
| 只注册 skill，还是要连 MCP server？ | 只要 skill：一个 Host 插件行即可；要 MCP：再加 `@deepseek-ai/dsh-mcp-client` 行 |
| 包名用无 scope 还是 `@scope/name`？ | 无 scope 最容易让用户输入；先 `npm view <name> version`，404 只表示当前可用，不保留 |
| 包和 Python/CLI 同版本吗？ | 是。DSH bundle 只装注册层，CLI 仍要用户自己安装；版本对齐便于排障 |

## 目录骨架

```text
dsh-plugin/
  package.json
  cordis.patch.yml
  index.js
  locale/en.json
  locale/zh.json
  icon.png                 # 可选，128x128 PNG
  skills/
    my-skill/SKILL.md      # npm 包内必须自包含
  scripts/sync-skills.mjs  # 从仓库其他位置复制 skill
  README.md
  LICENSE
```

`README.md` 和 `LICENSE` 会被 npm 默认/显式打包；`skills/` 必须写进 `files`。

## 1. package.json

最小可用字段：

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "DeepSeek Harness bundle for ...",
  "type": "module",
  "license": "Apache-2.0",
  "exports": {
    ".": "./index.js",
    "./locale/*.json": "./locale/*.json",
    "./icon": "./icon.png",
    "./package.json": "./package.json"
  },
  "files": [
    "index.js",
    "cordis.patch.yml",
    "locale/*.json",
    "icon.png",
    "README.md",
    "LICENSE",
    "skills/**"
  ],
  "scripts": {
    "sync-skills": "node scripts/sync-skills.mjs",
    "check-skills": "node scripts/sync-skills.mjs --check",
    "prepack": "node scripts/sync-skills.mjs"
  },
  "publishConfig": { "access": "public" },
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

关键点：

- **不要** `private: true`；它会让 `npm publish` 直接失败。
- `dsh.bundle.patch` 必须指向包内文件，且该文件必须在 `files` 里，否则装完 DSH 找不到 bundle。
- `exports` 至少暴露 `"."`；`locale` 和 `icon` 用于 UI 显示。`./package.json` 便于工具读取清单。
- `prepack` 在 `npm pack` / `npm publish` 前运行，生成的文件会进入 tarball；从 registry 安装时不会再次运行。
- `publishConfig.access: public` 避免 scoped 包默认 private。

## 2. cordis.patch.yml

```yaml
- insert:
    - id: my-plugin-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: myplugin          # 工具前缀 mcp__myplugin__...
        transport: stdio
        command: my-cli               # 必须在 DSH 进程的 PATH 上
        args:
          - run
        toolCallTimeoutMs: 1800000    # 长任务（批量下载/索引）按需放大
        failOnStartupError: false     # CLI 没装时不要让整个 harness 启动失败

    - id: my-plugin-skills
      name: my-plugin                 # 等于 package.json 的 name
      config: {}
```

规则：

- 每一行 `id` 唯一；`name` 是模块名。Host 插件行写包名；MCP 行写 `@deepseek-ai/dsh-mcp-client`。
- `serverName` 决定工具命名空间；不要用会变的版本号。
- 没有 MCP server 时删掉第一行，只保留 skill 行。
- 需要多个 server 时复制 MCP 行，换 `id` / `serverName` / `command`。
- `failOnStartupError: false` 时，CLI 缺失不会阻止 DSH 启动，但工具不会出现；skill 必须告诉用户怎么装 CLI。

## 3. index.js（注册 skill）

```js
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = dirname(fileURLToPath(import.meta.url))
const SKILL_FILES = [
  '../skill/SKILL.md',                    // 仓库开发时的规范源文件
  './skills/my-skill/SKILL.md',           // npm tarball 内的副本
]

export const inject = ['skills']

export function apply(ctx, config = {}) {
  let file
  let raw
  for (const candidate of SKILL_FILES) {
    try {
      file = resolve(packageRoot, candidate)
      raw = readFileSync(file, 'utf8')
      break
    } catch {
      // 继续尝试下一个位置
    }
  }
  if (!raw) return

  const { meta, body } = parseFrontmatter(raw)
  ctx.effect(() => ctx.skills.register({
    name: meta.name,
    description: meta.description,
    source: 'runtime',                    // 必填，且必须是字符串
    content: body,
    resourceBase: { kind: 'directory', path: dirname(file) },
  }))
}

function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(raw)
  if (!match) return { meta: {}, body: raw }
  const meta = {}
  for (const line of match[1].split(/\r?\n/)) {
    const m = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(line)
    if (m) meta[m[1]] = m[2].replace(/^['"]|['"]$/g, '')
  }
  return { meta, body: raw.slice(match[0].length) }
}
```

要点：

- `source` 必填；漏掉会报 `loaded skill "..." source must be a string`。
- `resourceBase` 指向 skill 所在目录，skill 里引用的相对文件才能解析；npm 包里引用的仓库外部文档不会存在，skill 要自包含。
- 用 `ctx.effect(() => ctx.skills.register(...))`，卸载时自动注销。
- 如果需要多个 skill，循环注册；每个都要有独立 `name` 和 `description`。

## 4. 自包含 skills（最容易翻车）

npm 只上传 `files` 里的内容。`index.js` 里写 `../skill/SKILL.md` 在仓库里能跑，从 registry 装完就找不到。

做法：

1. 规范 skill 放在仓库某处（如 `skill/SKILL.md`）。
2. `scripts/sync-skills.mjs` 在 `prepack` 时复制到 `dsh-plugin/skills/<name>/SKILL.md`。
3. `index.js` 先试仓库路径，再试包内副本；这样本地开发和 npm 安装都能用。
4. `check-skills` 在 CI 里比较源文件和副本（换行统一为 `\n`），防止漂移。
5. `npm pack --dry-run` 确认 `skills/**` 真的在文件清单里。
6. 如果要支持 **Git 地址安装**，把生成的 `skills/` 提交进仓库，或同时加 `prepare` 脚本；`prepack` 只在 `npm pack` / `npm publish` 时运行，Git 安装不会触发。

如果 skill 引用了 `docs/`、`examples/` 等外部文件，要么一并放进 `files`，要么把关键内容内联；否则用户装完只有半套说明。

## 5. 发布到 npm

```bash
npm view my-plugin version          # 404 = 名称当前可用
npm login --auth-type=web
npm publish --access public
npm view my-plugin@1.0.0 dist.tarball
```

发布前：

```bash
npm pack --dry-run
```

检查输出里是否有 `cordis.patch.yml`、`index.js`、`locale/`、`icon.png`、`skills/`、`README.md`、`LICENSE`。

### 2FA

账号开了 `auth-and-writes` 时，`npm publish` 会要求二次验证：

- 有验证器：直接 `--otp=<6 位>`，或让 npm 在真实 TTY 里走浏览器授权。
- 只有 Windows Hello / 安全密钥：CLI 没有 6 位码。到 npm 网站 **Access Tokens → Granular Access Token**，勾选 **Bypass 2FA**，权限 Read and write / All packages；把 token 写进用户 `.npmrc`，不要发到聊天里：
  ```text
  //registry.npmjs.org/:_authToken=npm_...
  ```
- 报 `EOTP` 且给出 `auth/cli` 链接：在真实终端运行 `npm publish`，npm 会打开浏览器完成授权；无 TTY 的自动化环境拿不到完整流程。

### CI 发布

仓库加 `.github/workflows/npm-publish.yml`，用 `NPM_TOKEN` secret，`actions/setup-node` 配 `registry-url: https://registry.npmjs.org`，然后 `npm publish --access public`。workflow 里先跑 `check-skills` 和版本校验。

## 6. 在 DSH 里验证

用户侧：

1. **Plugins → Add plugin**
2. 填 `my-plugin` 或 `my-plugin@1.0.0`
3. Install，确认 bundle 为 **On**
4. 新会话里检查工具和 skill

Agent 侧：

```text
plugin_manager install_bundle target="my-plugin@1.0.0"
plugin_manager list_bundles
```

看 `source` 是否为 `my-plugin@1.0.0`（而不是 `link:`），`enabled: true`，rows 里有 MCP 和 skill 两行。再调用一个只读 MCP 工具确认 server 起来了。

### 替换本地 link 时特别注意

如果之前用本地目录装过（`link:`），先 **提交/备份** 那个目录，再移除旧 bundle。pnpm 11 在移除 link 时可能把 link 目标目录里的文件一起删掉；本项目的 `dsh-plugin/` 就被删过一次，靠 git 恢复。

正确顺序：

```text
plugin_manager remove_bundle target="my-plugin"     # 旧 link
plugin_manager install_bundle target="my-plugin@1.0.0"
```

如果装完报 `ENOENT ... 旧路径/index.js`，是 Loader 还持有旧的模块路径：

- 先 `plugin_manager set_plugin target="include:my-plugin-skills" enabled=false`，再 `enabled=true`；
- 或在 Plugins 页面把该行关掉再打开；
- 仍不行就重启 DSH。

## 7. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `private: true` / `npm publish` 拒绝 | 包声明为私有 | 删除 `private`，确认 `publishConfig.access=public` |
| 装完 DSH 说找不到 bundle | `dsh.bundle.patch` 不在 `files` 或路径写错 | 检查 `npm pack --dry-run` 清单 |
| `source must be a string` | `skills.register` 少了 `source` | 加 `source: 'runtime'` |
| `unknown-plugin` | `set_plugin` 传了包名/行名 | 用 `include:<rowId>` |
| 改了 `index.js` 不生效 | HMR 不重载模块 | 把对应行关掉再打开 |
| 从 npm 装完 skill 缺失 | `prepack` 没跑或 `files` 少了 `skills/**` | 本地 `npm pack` 检查 tarball |
| 工具调用超时 | MCP 默认 60s | `toolCallTimeoutMs` 调大 |
| 新包被 pnpm 拦截 | `minimumReleaseAge` 供应链策略 | 在 DSH 提示里批准，或等待/加 exclude |
| Windows 下 check-skills 误报 | CRLF/LF 差异 | 比较前统一 `replace(/\r\n/g, '\n')` |

## 8. 交付前检查清单

- [ ] `npm view <name> version` 确认名称可用
- [ ] `package.json` 没有 `private`，有 `dsh.bundle.patch`、`files`、`exports`
- [ ] `cordis.patch.yml` 的 row id 唯一，MCP 的 `serverName` 稳定
- [ ] MCP `command` 在 DSH 的 PATH 上；缺失时 `failOnStartupError: false`
- [ ] 长任务设置 `toolCallTimeoutMs`
- [ ] skill 在包内自包含；`source` 是字符串；`resourceBase` 正确
- [ ] `npm pack --dry-run` 包含 patch、index、locale、icon、skills、README、LICENSE
- [ ] 本地从 tarball 或 registry 装过，工具和 skill 都验证
- [ ] README 写明 CLI/Python 前置依赖，以及 npm / tarball / 本地目录三种安装方式
- [ ] 没有 token、cookie、私钥进入仓库或 tarball

## 参考

- [`references/templates.md`](references/templates.md) — 可直接改的 package/patch/index/sync/workflow 模板
- [`references/troubleshooting.md`](references/troubleshooting.md) — 发布、安装、HMR、2FA 的详细排障
- 现成例子：`scansci-pdf` 的 `dsh-plugin/`，npm 包 `scansci-pdf@1.18.2`，MCP `scansci` + skills `scansci-pdf` / `scansci-sort`
