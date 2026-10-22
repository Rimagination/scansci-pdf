# 排障

## 发布阶段

### `E403 ... Two-factor authentication or granular access token with bypass 2fa enabled`

npm 账号开了 `auth-and-writes`，但当前 token 不能发布。按可用条件选：

1. **有验证器**：`npm publish --access public --otp=<当前 6 位>`。
2. **只有 Windows Hello / 安全密钥**：CLI 没有 6 位码。到 npm 网站 **Access Tokens → Generate New Token → Granular Access Token**，勾选 **Bypass 2FA**，权限 Read and write / All packages；把 token 写进 `%USERPROFILE%\.npmrc`：
   ```text
   //registry.npmjs.org/:_authToken=npm_...
   ```
   不要发到聊天、日志或仓库里。
3. **CI**：把同样的 token 放进 GitHub secret `NPM_TOKEN`，由 workflow 使用。

`npm token create --bypass-2fa` 也可能要求账号密码/OTP，且不适合在无交互环境里跑；优先用网站生成的 token。

### `EOTP This operation requires a one-time password`

- 在**真实终端**（TTY）里运行 `npm publish`，npm 会打印 `https://www.npmjs.com/auth/cli/...` 并打开浏览器；完成 Windows Hello/验证器后，CLI 自动重试。
- 无 TTY 的 agent shell 里 npm 不会走浏览器流程，只会抛错。不要靠伪造 `isTTY`，会触发 `cursorTo is not a function`；用真实终端窗口，或直接 `--otp`。
- 如果浏览器授权页过期，重新跑一次 `npm publish` 生成新链接；旧的 `doneUrl` 不会返回 token。

### `npm publish` 成功但 `npm view` 404

- registry 传播/缓存延迟，等几秒重试。
- 检查包名是否拼错、是否发到了别的 registry（`npm config get registry`）。
- 国内网络可能先命中 npmmirror 缓存；npmmirror 是只读镜像，npmjs 发布后按需同步，不能直接向它 publish。

### 包名被占用

`npm view <name> version` 返回 404 只表示当前查不到，不保留名称。发布前再查一次；被占用就换无 scope 名或 `@scope/name`（scoped 包记得 `--access public`）。

## 打包阶段

### 从 npm 安装后 skill 找不到 / `index.js` 报 `ENOENT`

- `files` 里没有 `skills/**`，或 `prepack` 没有生成副本。
- 本地跑 `npm pack --dry-run`，确认 tarball 文件清单。
- 运行 `npm pack <name>@<version>` 解压检查，不要只看仓库目录。

### `npm pack` 没有执行 prepack

`npm pack` / `npm publish` 会执行 `prepack`；`npm install <local-dir>` 不会。所以本地目录安装要依赖 `index.js` 的仓库路径回退，或者先手动 `npm run sync-skills`。

### Windows 下 `check-skills` 误报

Git 的 autocrlf 会让副本变成 CRLF。比较前统一：

```js
text.replace(/\r\n/g, '\n')
```

## DSH 安装阶段

### 替换 `link:` 后报旧路径 `ENOENT`

现象：`failed to import: Error: ENOENT ... old/path/index.js`，但旧文件其实存在或刚被删。

原因：Loader 还持有旧的 link 模块路径；pnpm 移除 link 时也可能把 link 目标目录清空。

处理：

1. 先确认仓库已提交；被删的文件用 `git checkout -- dsh-plugin` 恢复。
2. `plugin_manager set_plugin target="include:<row-id>" enabled=false`，再 `enabled=true`。
3. 还不行就重启 DSH（包替换在无 HMR 时需要重启）。

**预防**：移除本地 link 前先提交/备份；或者先用 tarball/npm 包名安装，不要在同一个 profile 里反复 link ↔ registry。

### `loaded skill "..." source must be a string`

`ctx.skills.register` 的对象少了 `source`。加：

```js
source: 'runtime'
```

### `plugin_manager set_plugin` 报 `unknown-plugin`

`target` 不是包名，也不是 row 的 `name`，而是 entry id：

```text
include:<row-id>
```

例如 patch 里 `id: my-plugin-skills` → `include:my-plugin-skills`。

### 改了 `index.js` 不生效

HMR 不会重新 import 已加载的模块。把对应 row 关掉再打开，或重启 DSH。只改 skill 的 `SKILL.md` 通常不需要重载模块，下一次加载会重新读文件。

### 工具有了但调用超时

MCP client 默认 60 秒。批量下载、索引、抓取类工具在 patch 里加：

```yaml
toolCallTimeoutMs: 1800000
```

### 安装被 `minimumReleaseAge` 拦住

新发布的包可能被 pnpm 的供应链策略视为“太新”。DSH 安装结果会提示并写入 `minimumReleaseAgeExclude`；按提示批准，或等过了最小发布年龄再装。

### CLI 不在 PATH

`command: <cli>` 由 DSH 进程启动，继承的是 DSH 的 PATH，不是用户当前 shell 的 PATH。虚拟环境里的 CLI 要用绝对路径，或把 venv 的 `Scripts`/`bin` 配进 DSH 的启动环境。`failOnStartupError: false` 只保证 DSH 不崩，不会让工具自动出现。

## 验证清单

```bash
npm view <name>@<version> version dist.tarball
npm pack <name>@<version>
```

```text
plugin_manager list_bundles
plugin_manager install_bundle target="<name>@<version>"
```

- `source` 应为 `<name>@<version>`，不是 `link:`。
- rows 同时有 MCP 和 skill 两行。
- 调一个只读 MCP 工具成功。
- 技能目录出现目标 skill，描述正确。
