import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

// The Qoder plugin bundle must stay self-contained: a submitted package cannot
// rely on repository-relative paths. Keep the canonical prose in `skill/` and
// `docs/`, and generate the bundle copies here. `--check` is for CI.
const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = dirname(here)
const repoRoot = dirname(packageRoot)

const github = 'https://github.com/Rimagination/scansci-pdf'

// [source, target, transforms[]] — each transform is an exact [find, replace] pair.
const generated = [
  {
    from: join(repoRoot, 'skill', 'SKILL.md'),
    to: join(packageRoot, 'skills', 'scansci-pdf', 'SKILL.md'),
    edits: [
      // The Qoder edition is legal-only. The generated copy must not advertise
      // grey sources, grey strategies, or the embedded Tor helper; the
      // canonical skill/ copy stays unchanged for the full package.
      [
        'which exposes 18 high-level tools covering download (OA/grey/institutional channels),',
        'which exposes 19 high-level tools covering download (OA/institutional channels),',
      ],
      [
        'TRIGGER when: user mentions downloading papers, DOI, arXiv ID, Sci-Hub, paper search,',
        'TRIGGER when: user mentions downloading papers, DOI, arXiv ID, paper search,',
      ],
      [
        'scansci-pdf 是一个 MCP 服务器，提供 **20 个高层工具**，覆盖学术论文的搜索、下载（开放获取 / 灰色源 / 机构渠道）、',
        'scansci-pdf 是一个 MCP 服务器，提供 **19 个高层工具**，覆盖学术论文的搜索、下载（开放获取 / 机构渠道）、',
      ],
      ['## MCP 工具参考（20 个）', '## MCP 工具参考（19 个）'],
      [
        '| `scansci_pdf_download` | 单篇下载（OA/灰色源/机构级联） |',
        '| `scansci_pdf_download` | 单篇下载（OA/机构级联） |',
      ],
      [
        '| `scansci_pdf_batch_download` | 批量下载 | `identifiers`（必需）、`scihub_enabled`、`batch_id`（断点续传）、`resume`（默认 true） |',
        '| `scansci_pdf_batch_download` | 批量下载 | `identifiers`（必需）、`batch_id`（断点续传）、`resume`（默认 true） |',
      ],
      [
        '| `fastest`（默认） | 全部源并行竞速（含灰色源，若 `scihub_enabled=true`） |\n' +
        '| `oa_first` | OA 源优先，失败后回退灰色源 |\n' +
        '| `scihub_first` | 灰色源优先，失败后回退合法源 |\n' +
        '| `scihub_only` | 仅 Sci-Hub |\n' +
        '| `legal_only` | 仅合法源（Unpaywall/出版商/OpenAIRE 等），不碰 Sci-Hub/LibGen |',
        '| `fastest`（默认） | 全部合法源并行竞速（本版本固定 legal-only） |\n' +
        '| `oa_first` | OA 源优先 |\n' +
        '| `legal_only` | 仅合法源（Unpaywall/出版商/OpenAIRE 等） |',
      ],
      [
        '**来源授权不可被调度扩张**：用户显式设置 `scihub_enabled=false` 或 `legal_only` 时，任何批量/车道调度都不会重新启用灰色源；只有显式 `--scihub`/`scihub_enabled=true` 或全局策略允许时才启用。',
        '**来源授权不可被调度扩张**：本版本固定 `legal_only`，不启用灰色源；批量/车道调度也不会重新启用。',
      ],
      [
        '| `scansci_pdf_tor` | 内嵌 Tor：`action=install\\|start\\|stop` | `action`、`use_bridges` |\n',
        '',
      ],
      [
        '### 流程 7：内嵌 Tor（灰色源被墙时）\n\n```\n1. scansci_pdf_tor(action="install")   # 首次下载 Tor Expert Bundle\n2. scansci_pdf_tor(action="start")     # 受限网络可 use_bridges=true\n3. scansci_pdf_download(identifier="...", strategy="scihub_only")\n```\n\n',
        '',
      ],
      [
        '还可能留 `10.xxxx_Sci-Hub.pdf`、`_CrossrefPage.pdf`',
        '还可能留 `_CrossrefPage.pdf`',
      ],
      [
        'or user asks about non-academic PDFs (invoices, reports, etc.).\n---',
        'or user asks about non-academic PDFs (invoices, reports, etc.).\n' +
          '  All retrieval stays inside the channels the user is authorized to use;\n' +
          '  without entitlement the tool returns open-access or preprint versions only.\n---',
      ],
      [
        '## ⚠️ 修改反爬/嗅探/镜像相关代码前必读\n\n先读',
        '## ⚠️ 维护者须知（修改源码前）\n\n以下内容只面向 `scansci-pdf` 源码维护者，普通使用无需阅读。修改反爬/嗅探/镜像相关代码前必读：先读',
      ],
      [
        '[`docs/PLAYBOOK.md`](../docs/PLAYBOOK.md)',
        '[`docs/PLAYBOOK.md`](' + github + '/blob/main/docs/PLAYBOOK.md)',
      ],
      [
        '镜像健康存储曾被重复造过三代）。\n',
        '镜像健康存储曾被重复造过三代）。\n\n' +
          '> 进阶配置（可选增强、浏览器后端、Docker / HTTP 部署、排障速查）见 [`docs/REFERENCE.md`](' + github + '/blob/main/docs/REFERENCE.md)。\n',
      ],
    ],
  },
]

// Hand-maintained in this bundle (marketplace-facing copy, not generated):
//   .qoder-plugin/plugin.json, .mcp.json, README.md, CONNECTORS.md, RELEASE.md,
//   LICENSE, icon.png, commands/scansci/*.md
// Only the manifest version is machine-checked, against pyproject.toml below.
const check = process.argv.includes('--check')
const normalize = (text) => text.replace(/\r\n/g, '\n')
let stale = 0

for (const entry of generated) {
  let text = normalize(readFileSync(entry.from, 'utf8'))
  for (const [find, replace] of entry.edits ?? []) {
    if (!text.includes(find)) {
      console.error(`anchor missing in ${entry.from}: ${find.slice(0, 48)}`)
      process.exit(1)
    }
    text = text.replaceAll(find, replace)
  }
  if (entry.prefix) text = entry.prefix + text

  if (check) {
    let current = null
    try {
      current = normalize(readFileSync(entry.to, 'utf8'))
    } catch {
      // A missing generated file is stale.
    }
    if (current !== text) {
      console.error(`stale: ${entry.to}`)
      stale += 1
    }
    continue
  }

  mkdirSync(dirname(entry.to), { recursive: true })
  writeFileSync(entry.to, text, 'utf8')
  console.log(`generated: ${entry.to}`)
}

// The manifest version is hand-maintained, so assert it against the package
// version: a marketplace listing whose version drifts from the pip release
// promises tools the installed engine may not have.
const manifestPath = join(packageRoot, '.qoder-plugin', 'plugin.json')
const declaredVersion = JSON.parse(readFileSync(manifestPath, 'utf8')).version
const pyproject = normalize(readFileSync(join(repoRoot, 'pyproject.toml'), 'utf8'))
const [, packagedVersion] = pyproject.match(/^version\s*=\s*"([^"]+)"/m) ?? []
if (!packagedVersion) {
  console.error('could not read the package version from pyproject.toml')
  process.exit(1)
}
if (declaredVersion !== packagedVersion) {
  console.error(
    `version drift: ${manifestPath} declares ${declaredVersion}, ` +
      `pyproject.toml has ${packagedVersion}`
  )
  process.exit(1)
}
console.log(`version ${declaredVersion} matches pyproject.toml`)

if (check) {
  if (stale > 0) process.exit(1)
  console.log('qoder-plugin skills in sync')
}
