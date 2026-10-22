# 模板

把 `<name>`、`<version>`、`<command>`、`<serverName>` 换成实际值。示例假设仓库结构：

```text
repo/
  skill/SKILL.md                 # 规范 skill
  dsh-plugin/                    # npm 包根目录
    package.json
    cordis.patch.yml
    index.js
    scripts/sync-skills.mjs
```

## package.json

```json
{
  "name": "<name>",
  "version": "<version>",
  "description": "DeepSeek Harness bundle for ...",
  "type": "module",
  "license": "Apache-2.0",
  "author": { "name": "<author>" },
  "homepage": "https://github.com/<owner>/<repo>",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/<owner>/<repo>.git",
    "directory": "dsh-plugin"
  },
  "bugs": { "url": "https://github.com/<owner>/<repo>/issues" },
  "keywords": ["deepseek-harness", "dsh", "dsh-plugin", "mcp"],
  "engines": { "node": ">=20" },
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

## cordis.patch.yml（MCP + skill）

```yaml
- insert:
    - id: <name>-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: <serverName>
        transport: stdio
        command: <command>
        args:
          - run
        toolCallTimeoutMs: 1800000
        failOnStartupError: false

    - id: <name>-skills
      name: <name>
      config: {}
```

只注册 skill 时删掉 MCP 行。

## index.js

```js
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = dirname(fileURLToPath(import.meta.url))

const SKILLS = [
  {
    name: '<skill-name>',
    files: ['../skill/SKILL.md', './skills/<skill-name>/SKILL.md'],
  },
]

export const inject = ['skills']

export function apply(ctx, config = {}) {
  const entries = Array.isArray(config.skills) && config.skills.length > 0
    ? config.skills
    : SKILLS

  for (const entry of entries) {
    const candidates = Array.isArray(entry.files) ? entry.files : [entry.file]
    let file
    let raw
    for (const candidate of candidates) {
      if (typeof candidate !== 'string') continue
      try {
        file = resolve(packageRoot, candidate)
        raw = readFileSync(file, 'utf8')
        break
      } catch {
        // 继续尝试包内副本
      }
    }
    if (!raw) continue

    const { meta, body } = parseFrontmatter(raw)
    const name = entry.name ?? meta.name
    const description = entry.description ?? meta.description
    if (!name || !description) continue

    const skill = {
      name,
      description,
      source: 'runtime',
      content: body.replace(/^\s+/, ''),
      resourceBase: { kind: 'directory', path: dirname(file) },
    }
    if (meta.whenToUse) skill.whenToUse = meta.whenToUse

    ctx.effect(() => ctx.skills.register(skill))
  }
}

function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(raw)
  if (!match) return { meta: {}, body: raw }

  const meta = {}
  for (const line of match[1].split(/\r?\n/)) {
    const m = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(line)
    if (!m) continue
    let value = m[2].trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    meta[m[1]] = value
  }
  return { meta, body: raw.slice(match[0].length) }
}
```

## scripts/sync-skills.mjs

```js
import { copyFileSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = dirname(here)
const repoRoot = dirname(packageRoot)

const mappings = [
  {
    from: join(repoRoot, 'skill', 'SKILL.md'),
    to: join(packageRoot, 'skills', '<skill-name>', 'SKILL.md'),
  },
]

const check = process.argv.includes('--check')
const normalize = (text) => text.replace(/\r\n/g, '\n')
let stale = 0

for (const { from, to } of mappings) {
  const source = readFileSync(from, 'utf8')

  if (check) {
    let target = null
    try {
      target = readFileSync(to, 'utf8')
    } catch {
      // missing counts as stale
    }
    if (target === null || normalize(target) !== normalize(source)) {
      console.error(`stale: ${to}`)
      stale += 1
    }
    continue
  }

  mkdirSync(dirname(to), { recursive: true })
  copyFileSync(from, to)
  console.log(`synced: ${to}`)
}

if (check) {
  if (stale > 0) process.exit(1)
  console.log('skills in sync')
}
```

## locale/en.json 与 locale/zh.json

```json
{
  "meta": {
    "title": "My Plugin",
    "description": "One sentence shown on the Plugins card."
  }
}
```

`zh.json` 用中文标题/描述即可；缺少语言时 DSH 回退到英文或包名。

## .github/workflows/npm-publish.yml

```yaml
name: Publish DSH bundle to npm

on:
  workflow_dispatch:
    inputs:
      version:
        description: "Version to publish; must equal dsh-plugin/package.json"
        required: true

permissions:
  contents: read

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          registry-url: "https://registry.npmjs.org"

      - name: Verify version and generated skills
        env:
          VERSION: ${{ inputs.version }}
        run: |
          node -e "const p = require('./dsh-plugin/package.json'); if (p.version !== process.env.VERSION) { console.error('package.json is ' + p.version); process.exit(1) }"
          node dsh-plugin/scripts/sync-skills.mjs --check

      - name: Publish
        working-directory: dsh-plugin
        run: npm publish --access public
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
```

仓库 secret `NPM_TOKEN` 需要 npm granular token（Bypass 2FA）或 CI 专用 token。不要把 token 写进 workflow。

## 安装后的验证命令

```bash
npm view <name> version dist.tarball
npm pack <name>@<version> --dry-run
```

DSH 内：

```text
plugin_manager install_bundle target="<name>@<version>"
plugin_manager list_bundles
```

检查 `source`、`enabled`、rows；再调用一个只读 MCP 工具，并看技能目录是否出现新 skill。
