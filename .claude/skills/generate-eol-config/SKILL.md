---
name: generate-eol-config
description: Generate an eol_config.<project>.json for the EOL tracker from inventory inputs. Use when the user wants to create a tracker config from dependency manifests (pom.xml, build.gradle, package.json), a wiki/Confluence EOL table, a spreadsheet, or a prose software list. Routes clean manifests to generate_config.py and messy or mixed inputs to the eol-config-extractor subagent.
---

# Generate an EOL config

Route by input type — do not extract by hand:

1. **Only clean dependency manifests** (`pom.xml` / `*.gradle*` / `package.json`
   in a folder): run the deterministic scanner, then verify:

   ```
   python generate_config.py <folder> --name <project>
   ```

   Review the `_skipped_npm_packages` list in the output, then live-verify the
   generated entries per the universal norms in `AGENTS.md`.

2. **Anything messier** (wiki/Confluence tables, spreadsheets, prose, or mixed
   manifest + document inputs): dispatch the `eol-config-extractor` subagent
   with the input file path(s) and the project name. It reads
   `eol_config_generation_prompt.md` (the canonical extraction spec), verifies
   every slug/package live, writes the config, and smoke-runs it.

3. **Updating an existing config?** Stop — use the `update-eol-config` skill
   instead. Regenerating from scratch destroys human curation.

Done when: the config parses (`python -c "import json;
json.load(open('eol_config.<project>.json'))"`), a smoke run
(`python lambda_function.py eol_config.<project>.json`) shows no unexpected
error rows, and the user has the verification report (entry counts, verified
checklist, Needs-Manual-Review list).
