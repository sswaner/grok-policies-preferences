# policies-preferences

Generic, owner-agnostic Grok Bot skill + `pp` CLI. An owner keeps their Grok bots' **policies**
(constitutional, owner-set) and **preferences** (how bots do assigned work) in one versioned git
repo. Bots load and cite them, propose preferences, and record feedback only through `pp/*` PRs
that the owner merges. Bot instructions: [`SKILL.md`](SKILL.md).

```bash
./install.sh                                   # venv with pyyaml + jsonschema (idempotent; --uninstall)
bin/pp --source-dir <repo> init --owner <id> --owner-name "<Name>" --repo <owner/name> --bot <slug>
bin/pp config                                  # resolved settings (flags > PP_* env > pp.config.yaml > defaults)
bin/pp load --bot <slug> --role coding         # what applies, in precedence order, with citation
bin/pp propose --by <slug> --applies-to coding --kind learned --title "..." --rule "..." --evidence "..."
bin/pp feedback --id PREF-001 --by <slug> --note "owner: not what I meant ..."
bin/pp render-review --out review.md --decisions-template decisions.yaml
bin/pp apply-decisions <home>/decisions/<date>-review.yaml [--dry-run]
bin/pp validate
bin/pp check-diff --base origin/main           # bot-safe change check
bin/pp-submit --by <slug> --title "preferences: propose PREF-003"   # pp/* branch + PR; never main
test/run-tests.sh                              # generic tests, incl. a no-owner-names check
```

Env vars: `PP_SOURCE_REPO`, `PP_SOURCE_DIR` (legacy alias `PP_TOOLS_DIR`), `PP_HOME`, `PP_OWNER`,
`PP_OWNER_NAME`, `PP_BOT`, `PP_CONFIG` (config file path), `PP_PYTHON`, `PP_TOOLKIT_REPO` (used by
the SKILL's first-time setup; default `sswaner/grok-policies-preferences`). A `pp` that lives inside an instance repo always uses that repo
unless `--source-dir` is given. Schemas: `schema/`. CI template: `templates/`.

Keep this directory free of any owner's names, repos, or bot slugs; instance data lives in the
owner's preferences directory and `pp.config.yaml`.
