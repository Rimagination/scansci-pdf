import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The bundle is installed from this repository, so its skill files stay next to
// the code instead of being duplicated into the package.
const packageRoot = dirname(fileURLToPath(import.meta.url))

const DEFAULT_SKILLS = [
  {
    name: 'scansci-pdf',
    file: '../skill/SKILL.md',
    note:
      '> DeepSeek Harness exposes these MCP tools as ' +
      '`mcp__scansci__<tool>` — for example `mcp__scansci__scansci_pdf_download`.\n\n',
  },
  { name: 'scansci-sort', file: '../skills/scansci-sort/SKILL.md' },
]

export const inject = ['skills']

export function apply(ctx, config = {}) {
  const entries =
    Array.isArray(config.skills) && config.skills.length > 0
      ? config.skills
      : DEFAULT_SKILLS

  for (const entry of entries) {
    if (!entry || typeof entry.file !== 'string') continue

    const file = resolve(packageRoot, entry.file)
    let raw
    try {
      raw = readFileSync(file, 'utf8')
    } catch (error) {
      // A missing reference must not take the MCP connection down with it.
      console.warn(`[scansci-pdf-dsh] cannot read skill ${file}: ${error.message}`)
      continue
    }

    const { meta, body } = parseFrontmatter(raw)
    const name = entry.name ?? meta.name
    const description = entry.description ?? meta.description
    if (!name || !description) {
      console.warn(`[scansci-pdf-dsh] skill ${file} has no name or description`)
      continue
    }

    const skill = {
      name,
      description,
      source: 'runtime',
      content: `${entry.note ?? ''}${body.replace(/^\s+/, '')}`,
      resourceBase: { kind: 'directory', path: dirname(file) },
    }
    if (meta.whenToUse) skill.whenToUse = meta.whenToUse

    ctx.effect(() => ctx.skills.register(skill))
  }
}

/** Read the small YAML subset the repository's SKILL.md files use. */
function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(raw)
  if (!match) return { meta: {}, body: raw }

  const lines = match[1].split(/\r?\n/)
  const meta = {}

  for (let index = 0; index < lines.length; index += 1) {
    const field = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/.exec(lines[index])
    if (!field) continue

    const key = field[1]
    let value = field[2].trim()

    if (/^[>|][+-]?$/.test(value)) {
      const parts = []
      while (
        index + 1 < lines.length &&
        /^[ \t]+\S/.test(lines[index + 1])
      ) {
        parts.push(lines[index + 1].trim())
        index += 1
      }
      value = (value.startsWith('|') ? parts.join('\n') : parts.join(' ')).trim()
    } else {
      value = value.replace(/^(['"])(.*)\1$/, '$2')
    }

    meta[key] = value
  }

  return { meta, body: raw.slice(match[0].length) }
}
