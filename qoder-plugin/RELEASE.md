# Qoder 插件发布检查

顺序不要跳：

1. **版本一致**：`pyproject.toml`、`src/scansci_pdf/__init__.py`、各 host manifest、`qoder-plugin/.qoder-plugin/plugin.json` 必须是同一个版本。`node qoder-plugin/scripts/sync-skills.mjs --check` 会断言 manifest 与 pyproject 一致。
2. **发 PyPI**：用 `Publish to PyPI` workflow 发布该版本，确认 `scansci-pdf==<version>` 能安装。
3. **跑发布闸门**：

   ```bash
   node qoder-plugin/scripts/check-release.mjs --smoke
   ```

   或 Actions → `Qoder release check` → Run workflow。它会从 PyPI 把 manifest 声明的版本装进干净 venv，并确认 `scansci-pdf run --help` 里真的有 `--profile`。

4. **重打 ZIP**：从当前 commit 用脚本打包，不要手搓 `Compress-Archive`（会把 `scripts/`、`RELEASE.md` 一起塞进去）。

   ```bash
   python qoder-plugin/scripts/build-bundle.py
   ```

   产物：`dist/scansci-pdf-qoder-plugin-<version>.zip`，默认带一层 `scansci-pdf/`，与上次安装成功的包结构一致。首次安装请当作结构测试；如果安装器要求平铺根目录，再用 `--flat` 重打一次确认。

5. **再提交 marketplace**：ZIP 应包含 `scansci-pdf/.qoder-plugin/plugin.json`、`.mcp.json`、`skills/scansci-pdf/SKILL.md`、`commands/scansci/*.md`、`README.md`、`CONNECTORS.md`、`LICENSE`、`icon.png`；不应包含 `scripts/`、`RELEASE.md`、`references/`（PLAYBOOK.md / REFERENCE.md）、`skills/scansci-sort/`。脚本会在打包后断言这些排除项并打印文件清单。

> 1.19.0 没有 `--profile`，不能用来发 Qoder 版。版本不一致时，connector 会在启动阶段报 unknown option，而不是退化到灰色源。
