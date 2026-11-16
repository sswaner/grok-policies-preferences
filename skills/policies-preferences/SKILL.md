---
name: policies-preferences
description: >-
  Load and obey your owner's Grok Bot POLICIES (constitutional, set by the owner alone) and
  PREFERENCES (how the owner wants assigned work done) from one versioned git source, and propose,
  give feedback on, or review them only through owner-approved pull requests. Use at the start of
  every task, whenever the owner states or shows how they want something done (to propose a
  preference), when they give feedback on a policy or preference, when they ask to review them,
  and when a new owner wants to set the system up (`pp init`).
---

# Policies & preferences (Grok Bot)

One versioned git repo (the **source**) governs every one of the owner's Grok bots. It holds a
preferences directory (`policies.yaml`, `preferences.yaml`, `CHANGELOG.md`, `decisions/`), a
`pp.config.yaml`, and the `pp` toolkit in `skills/policies-preferences/`.

- **POLICY**: constitutional guidance the owner alone sets. Obey it. **Never** create, edit,
  retire, or propose a policy, or draft policy text.
- **PREFERENCE**: how the owner wants you to do your assigned work. You may **propose** one you
  learned from them. It stays `proposed`, with no effect, until the owner approves it.
- **Precedence:** platform safety rules > POLICY > the owner's explicit instruction in the current
  task > PREFERENCE (bot-specific > role > all). If a live instruction conflicts with a policy,
  say so and ask; don't silently break the policy. If two policies conflict, follow the more
  restrictive one and tell the owner.
- **No copies.** Never save policies or preferences into memory, notes, or other files. Your
  memory may hold only the pointer: source repo, clone dir, and your slug. Always read the repo.
- **Human in the loop.** Never push to `main`, and never merge. Every change goes on a `pp/*`
  branch with a pull request. The owner's merge is the approval.
- When you ask the owner more than one question, number them.

## Configuration

`pp` resolves each setting as flag > env var > `pp.config.yaml` (source root) > default. Run
`"$PP" config` to see the result.

| Setting | Env var | Default |
|---|---|---|
| Source repo (`owner/name`) | `PP_SOURCE_REPO` | config `source_repo`, else git `origin` |
| Shared clone on the box | `PP_SOURCE_DIR` (legacy alias `PP_TOOLS_DIR`) | the repo `pp` lives in |
| Preferences dir | `PP_HOME` | config `home`, else `preferences` |
| Owner id (sole policy setter; `reviewer:`) | `PP_OWNER` | config `owner`, else `owner:` in `policies.yaml` |
| Owner display name | `PP_OWNER_NAME` | config `owner_name`, else the id |
| Your bot slug (`--bot`, `--by`) | `PP_BOT` | none; pass `--bot`/`--by` |
| Toolkit repo (for `pp init` only) | `PP_TOOLKIT_REPO` | `sswaner/grok-policies-preferences` |

A `pp` inside an instance repo, including a throwaway worktree, always uses that repo, whatever
the env vars say. Only `--source-dir` overrides it.

## Setup (shared box: Shell, `gh` authenticated)

The box and its clone are shared by all of the owner's bots. Each bot keeps its own memory, so
each bot needs its own pointer.

```bash
export PP_SOURCE_REPO="<owner/name>"   # from env, or the pointer in your memory; ask the owner if neither
export PP_BOT="<your-slug>"
T="${PP_SOURCE_DIR:-${PP_TOOLS_DIR:-$HOME/code/${PP_SOURCE_REPO#*/}}}"
[ -d "$T/.git" ] || gh repo clone "$PP_SOURCE_REPO" "$T"
gh auth setup-git
"$T/skills/policies-preferences/install.sh"    # idempotent: venv with PyYAML + jsonschema
PP="$T/skills/policies-preferences/bin/pp"
```

Keep `$T` on `main` and only read from it. Write only in your own worktree (step 2).
`"$PP" scopes` lists the registered bot slugs and roles. If yours is missing, ask the owner to
add it: scopes change only through the owner's decisions.

## 1. At task start: load and cite

```bash
git -C "$T" pull --ff-only -q || echo "WARN: using local copy, may be stale"
"$PP" load --bot "$PP_BOT" [--role <role> ...]
```

- Follow every listed policy, then every listed preference unless a policy or the owner's live
  instruction overrides it. `proposed`, `retired`, `rejected`, and `EXAMPLE-` entries are never
  loaded. Don't act on them.
- An unknown slug gets only `all` entries, with a warning. Tell the owner.
- When you report what guidance you followed, cite the first line of `load` (or `"$PP" cite`),
  e.g. `policies v1.0.0, preferences v1.1.0 (<owner/name>@abc1234)`. If the pull failed, say the
  version may be stale.

## 2. Making a change: throwaway worktree, then a PR

```bash
W="$(mktemp -d /tmp/pp-$PP_BOT-XXXX)"
git -C "$T" fetch -q origin main && git -C "$T" worktree add -q --detach "$W" origin/main
WPP="$W/skills/policies-preferences/bin/pp"; WH="$("$WPP" config --get home)"
# ... run the "$WPP" command from step 3, 4, or 5 ...
"$W/skills/policies-preferences/bin/pp-submit" --title "<PR title>" [--decisions]
git -C "$T" worktree remove --force "$W"
```

`pp-submit` validates the change. Without `--decisions`, it also runs `check-diff`, which fails
if you touched a policy, activated anything, or edited an existing entry. It then commits to
`pp/<slug>-<timestamp>`, pushes, opens a PR against `main`, and prints the URL. Give the owner
the link. If it can't push or open a PR, give the owner the diff instead.

## 3. When you learn a preference: propose it

When the owner states or clearly shows how they want work done ("in the future, do X", or a
repeated correction), propose it. Don't just remember it.

```bash
"$WPP" propose --applies-to <scope> [--applies-to ...] --kind stated|learned \
  --title "<short title>" --rule "<one clear directive>" \
  --evidence "<the owner's words, quoted, or what they did and when>"
```

Use the PR title `preferences: propose <PREF-id> (<your-slug>)`. You may follow the instruction in
the current task. It becomes standing guidance only after the owner merges.
If the owner wants a new **policy**, don't propose it. Record their exact words as a
`new_policies` row in a decisions file (step 5) and confirm it with them first.

## 4. When the owner gives feedback ("that's not what I meant")

```bash
"$WPP" feedback --id <PREF-or-POL-id> --note "<the owner's feedback, quoted>" \
  [--proposed-rule "<draft replacement>"]       # draft text only for PREFERENCES
```

This creates a pending revision. The entry keeps its current text until the owner approves the
revision. To add draft text later: `"$WPP" revise --id <PREF-id> --rev R1 --rule "..."`.
Use the PR title `preferences: feedback on <id>`.

## 5. When the owner asks to review

```bash
"$PP" render-review --out /tmp/pp-review.md --decisions-template /tmp/pp-decisions.yaml
```

Present the review as a Page or as markdown in chat. Ask the owner to decide each item, with
numbered questions. In a fresh worktree (step 2), write **only their** decisions to
`$WH/decisions/<YYYY-MM-DD>-review.yaml` with `reviewer: <owner id>` (`"$PP" config --get owner`).
The actions are:

- `approve`, `reject`
- `edit` (+`rule:`/`title:`/`applies_to:`)
- `retire`, `reactivate`
- `feedback` (+`note:`)
- `approve-revision` / `reject-revision` (+`rev:`)

New entries go under `new_policies` / `new_preferences`. Then:

```bash
"$WPP" apply-decisions "$WH/decisions/<file>.yaml" --dry-run   # show the owner the result
"$WPP" apply-decisions "$WH/decisions/<file>.yaml"
"$W/skills/policies-preferences/bin/pp-submit" --decisions --title "preferences: apply owner review <date>"
```

Give the owner the PR link and the resulting citation. Never fill in, guess, or pre-approve a
decision the owner didn't make.

## First-time setup for a new owner

Do this only when the owner asks. They choose the repo (private is fine), their owner id, and
their bots' slugs.

```bash
TK="$(mktemp -d)"; gh repo clone "${PP_TOOLKIT_REPO:-sswaner/grok-policies-preferences}" "$TK" -- --depth 1
"$TK/skills/policies-preferences/install.sh"
gh repo clone <owner/name> "$HOME/code/<name>"            # the owner's new or existing repo
cd "$HOME/code/<name>" && git switch -c pp/init
"$TK/skills/policies-preferences/bin/pp" --source-dir . init --owner <id> --owner-name "<Name>" \
  --repo <owner/name> --bot <slug> [--bot <slug> ...]     # [--home-dir <dir>] [--no-ci]
skills/policies-preferences/bin/pp validate
git add -A && git commit -m "policies-preferences: init" && git push -u origin pp/init
gh pr create --base main --title "policies-preferences: init" --body "Scaffolded by pp init."
```

`init` copies the toolkit into the repo. It writes `pp.config.yaml`, an empty instance (a scope
registry with `all`, the roles `coding`, `review`, `comms`, and `research`, and the bots you
list), a CHANGELOG, `decisions/`, a CI workflow, and one `EXAMPLE-` policy and preference that
are never loaded. The owner reviews and merges the PR. Then give each bot its pointer
(`PP_SOURCE_REPO`, its `PP_BOT` slug) and this skill. The owner should require PRs and the
`validate` check on `main`.
