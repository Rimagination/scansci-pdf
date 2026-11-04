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
      ['which exposes 18 high-level tools', 'which exposes 20 high-level tools'],
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
      ['[`docs/PLAYBOOK.md`](../docs/PLAYBOOK.md)', '[`references/PLAYBOOK.md`](references/PLAYBOOK.md)'],
      [
        '镜像健康存储曾被重复造过三代）。\n',
        '镜像健康存储曾被重复造过三代）。\n\n' +
          '> 进阶配置（可选增强、浏览器后端、Docker / HTTP 部署、排障速查）见 [`references/REFERENCE.md`](references/REFERENCE.md)。\n',
      ],
    ],
  },
  {
    from: join(repoRoot, 'docs', 'PLAYBOOK.md'),
    to: join(packageRoot, 'skills', 'scansci-pdf', 'references', 'PLAYBOOK.md'),
    prefix:
      '> 适用范围：本文面向 `scansci-pdf` **源码维护者**，涉及仓库内部实现与镜像策略；\n' +
      '> 日常检索与下载无需阅读，遇到具体失败请看 `references/REFERENCE.md`。\n\n',
  },
  {
    from: join(repoRoot, 'docs', 'REFERENCE.md'),
    to: join(packageRoot, 'skills', 'scansci-pdf', 'references', 'REFERENCE.md'),
    edits: [
      ['[`qoder-plugin/`](../qoder-plugin/)', '[`qoder-plugin/`](' + github + '/tree/main/qoder-plugin)'],
      ['[`dsh-plugin/README.md`](../dsh-plugin/README.md)', '[`dsh-plugin/README.md`](' + github + '/blob/main/dsh-plugin/README.md)'],
      ['[`zcode-plugin/`](../zcode-plugin/)', '[`zcode-plugin/`](' + github + '/tree/main/zcode-plugin)'],
      ['[`dsh-plugin/`](../dsh-plugin/)', '[`dsh-plugin/`](' + github + '/tree/main/dsh-plugin)'],
      ['[MIT License](LICENSE)', '[MIT License](' + github + '/blob/main/LICENSE)'],
      ['scansci-pdf sciverse-setup --token YOUR_TOKEN --test', 'scansci-pdf sciverse-setup --token 你的Token --test'],
      ['scansci_pdf_sciverse_setup(token="YOUR_TOKEN", test=true)', 'scansci_pdf_sciverse_setup(token="你的Token", test=true)'],
    ],
  },
  {
    from: join(repoRoot, 'zcode-plugin', 'skills', 'scansci-sort', 'SKILL.md'),
    to: join(packageRoot, 'skills', 'scansci-sort', 'SKILL.md'),
    edits: [
      [
        'description: 大型文献清单分类摸底(嗅探优先)。当用户拿到数百篇以上的文献清单(WOS导出/Excel/DOI列表),要求分类、摸底、区分"哪些是OA开源、哪些Sci-Hub/灰色源有、哪些需机构权限"、为批量下载做路由规划时使用。先30分钟嗅探全分类,再分桶下载,不对全清单跑慢速竞速。',
        'description: 大型文献清单的分类摸底（嗅探优先）。当用户拿到数百篇以上的清单（WOS 导出 / Excel / DOI 列表），要求先分类摸底——区分开放获取、用户指定补充来源、需机构权限三类桶，再为批量下载做路由规划时使用。先全量嗅探分类，再分桶下载，不对整份清单跑慢速竞速。所有取用限于用户已授权的渠道。',
      ],
    ],
  },
]

// Hand-maintained in this bundle (marketplace-facing copy, not generated):
//   .qoder-plugin/plugin.json, .mcp.json, README.md, CONNECTORS.md, LICENSE,
//   icon.png, commands/scansci/*.md
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

if (check) {
  if (stale > 0) process.exit(1)
  console.log('qoder-plugin skills in sync')
}
