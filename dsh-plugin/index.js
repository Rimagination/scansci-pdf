import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// A repository checkout reads the canonical skill files first; an npm tarball
// falls back to the copies that scripts/sync-skills.mjs writes during prepack.
const packageRoot = dirname(fileURLToPath(import.meta.url))

const DEFAULT_SKILLS = [
  {
    name: 'scansci-pdf',
    files: ['../skill/SKILL.md', './skills/scansci-pdf/SKILL.md'],
    note:
      '> DeepSeek Harness exposes these MCP tools as ' +
      '`mcp__scansci__<tool>` — for example `mcp__scansci__scansci_pdf_download`.\n\n',
  },
  {
    name: 'scansci-sort',
    files: [
      '../zcode-plugin/skills/scansci-sort/SKILL.md',
      './skills/scansci-sort/SKILL.md',
    ],
  },
]

export const inject = ['skills']

export function apply(ctx, config = {}) {
  const entries =
    Array.isArray(config.skills) && config.skills.length > 0
      ? config.skills
      : DEFAULT_SKILLS

  for (const entry of entries) {
    if (!entry) continue

    const candidates =
      Array.isArray(entry.files)
        ? entry.files
        : typeof entry.file === 'string'
          ? [entry.file]
          : []
    if (candidates.length === 0) continue

    let file
    let raw
    for (const candidate of candidates) {
      const resolved = resolve(packageRoot, candidate)
      try {
        raw = readFileSync(resolved, 'utf8')
        file = resolved
        break
      } catch (error) {
        // A repo checkout falls back from source files to the copies that
        // prepack writes into the npm tarball.
        if (candidate === candidates[candidates.length - 1]) {
          console.warn(
            `[scansci-pdf] cannot read any skill file for ${entry.name ?? 'unknown'}: ${error.message}`,
          )
        }
      }
    }
    if (!file) continue

    const { meta, body } = parseFrontmatter(raw)
    const name = entry.name ?? meta.name
    const description = entry.description ?? meta.description
    if (!name || !description) {
      console.warn(`[scansci-pdf] skill ${file} has no name or description`)
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
