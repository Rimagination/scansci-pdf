#!/usr/bin/env node
// Pre-submission gate for the Qoder plugin bundle.
//
// The bundle's connector runs `scansci-pdf run --profile qoder`, so the exact
// version declared in `.qoder-plugin/plugin.json` must already be on PyPI and
// must actually accept `--profile`. Checking only "the version string exists"
// is not enough: 1.19.0 exists but predates the profile flag.
//
// Usage:
//   node qoder-plugin/scripts/check-release.mjs          # PyPI existence only
//   node qoder-plugin/scripts/check-release.mjs --smoke  # + clean-venv CLI check
//
// Run this BEFORE rebuilding the ZIP and uploading it to the Qoder marketplace.

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = dirname(here)
const manifest = JSON.parse(
  readFileSync(join(packageRoot, '.qoder-plugin', 'plugin.json'), 'utf8')
)
const version = manifest.version
const pkg = 'scansci-pdf'
const smoke = process.argv.includes('--smoke')

class CheckFailed extends Error {}

const fail = (message) => {
  throw new CheckFailed(message)
}

// Rich/Typer styles each option name, so a PTY can emit `-\x1b[...m-profile`
// instead of the literal `--profile`. Strip ANSI before matching; NO_COLOR is
// set on the child as well, but stripping keeps a forced-color environment
// from turning this gate into a false negative.
const stripAnsi = (text) => text.replace(/\x1b\[[0-9;]*m/g, '')

async function main() {
  console.log(`Qoder manifest version: ${version}`)

  let payload
  try {
    const res = await fetch(`https://pypi.org/pypi/${pkg}/${version}/json`)
    if (res.status === 404) {
      fail(
        `${pkg} ${version} is not on PyPI. Publish it first, then rebuild the ` +
          `Qoder ZIP. The connector cannot start on an older package that lacks --profile.`
      )
    }
    if (!res.ok) fail(`PyPI returned HTTP ${res.status} for ${pkg} ${version}`)
    payload = await res.json()
  } catch (error) {
    if (error instanceof CheckFailed) throw error
    fail(`could not query PyPI: ${error.message}`)
  }
  console.log(`✓ ${pkg} ${version} exists on PyPI (${payload.info.project_url || 'pypi.org'})`)

  if (!smoke) {
    console.log('\nNext: run with --smoke in a clean venv, then rebuild the ZIP.')
    return
  }

  const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
  const venv = mkdtempSync(join(tmpdir(), 'scansci-qoder-'))
  const venvPython =
    process.platform === 'win32' ? join(venv, 'Scripts', 'python.exe') : join(venv, 'bin', 'python')
  const cli =
    process.platform === 'win32' ? join(venv, 'Scripts', 'scansci-pdf.exe') : join(venv, 'bin', 'scansci-pdf')

  try {
    console.log(`\nCreating a clean venv: ${venv}`)
    // The venv's bundled pip can lag PyPI's index (observed with pip 24.0
    // immediately after a release). Upgrade it and query PyPI directly so the
    // gate tests the artifact, not a stale mirror/cache.
    execFileSync(python, ['-m', 'venv', '--upgrade-deps', venv], { stdio: 'inherit' })
    execFileSync(
      venvPython,
      [
        '-m', 'pip', 'install', '--quiet', '--no-cache-dir',
        '--index-url', 'https://pypi.org/simple',
        `${pkg}==${version}`,
      ],
      { stdio: 'inherit' }
    )

    const installed = execFileSync(
      venvPython,
      ['-c', 'import scansci_pdf; print(scansci_pdf.__version__)'],
      { encoding: 'utf8' }
    ).trim()
    console.log(`Installed package version: ${installed}`)

    const help = stripAnsi(
      execFileSync(cli, ['run', '--help'], {
        encoding: 'utf8',
        env: { ...process.env, NO_COLOR: '1', TERM: 'dumb' },
      })
    )
    if (!help.includes('--profile')) {
      console.error('--- scansci-pdf run --help (first 2000 chars) ---')
      console.error(help.slice(0, 2000))
      console.error('--- end help ---')
      fail(
        `${pkg} ${version} is on PyPI but its CLI does not expose --profile. ` +
          `Do not upload the Qoder ZIP; release a version that includes the profile first.`
      )
    }
    console.log(`✓ ${pkg} ${version} exposes --profile`)
    console.log('\nRelease gate passed. Rebuild the Qoder ZIP from this commit, then submit.')
  } finally {
    rmSync(venv, { recursive: true, force: true })
  }
}

main().catch((error) => {
  if (error instanceof CheckFailed) {
    console.error(`\n✗ ${error.message}`)
  } else {
    console.error(error)
  }
  process.exitCode = 1
})
