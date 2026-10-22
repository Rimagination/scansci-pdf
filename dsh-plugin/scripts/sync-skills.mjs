import { copyFileSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

// Keep the published tarball self-contained without maintaining a second copy
// of the skills by hand. `npm pack` / `npm publish` run this through prepack;
// `--check` is for CI.
const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = dirname(here)
const repoRoot = dirname(packageRoot)

const mappings = [
  {
    from: join(repoRoot, 'skill', 'SKILL.md'),
    to: join(packageRoot, 'skills', 'scansci-pdf', 'SKILL.md'),
  },
  {
    from: join(repoRoot, 'zcode-plugin', 'skills', 'scansci-sort', 'SKILL.md'),
    to: join(packageRoot, 'skills', 'scansci-sort', 'SKILL.md'),
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
      // A missing generated file is stale.
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
