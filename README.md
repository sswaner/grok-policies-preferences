# grok-policies-preferences

A versioned, owner-approved **policies** and **preferences** toolkit for Grok Bot agents.

This repository holds only the generic toolkit. It is specific to Grok Bot. It does not contain
any one owner's policies or preferences. Each owner keeps those in their own source repo
(private is fine) and vendors a copy of this toolkit with `pp init`.

- **POLICY**: constitutional guidance the owner alone sets. Bots obey it and must never create,
  edit, retire, or propose a policy.
- **PREFERENCE**: how the owner wants assigned work done. Bots may propose one they learned
  from the owner. It stays `proposed`, with no effect, until the owner approves it.
- **Precedence:** platform safety rules > POLICY > the owner's explicit instruction in the
  current task > PREFERENCE (bot-specific > role > all). If a live instruction conflicts with
  a policy, the bot should say so and ask; it must not silently break the policy.
- **Human in the loop.** Bots never push to `main` and never merge. Every change goes on a
  `pp/*` branch as a pull request. The owner's merge is the approval.

Bot instructions: [`skills/policies-preferences/SKILL.md`](skills/policies-preferences/SKILL.md).

## Quick start

Clone this toolkit, then scaffold an instance in the owner's source repo:

```bash
export PP_TOOLKIT_REPO=sswaner/grok-policies-preferences
TK="$(mktemp -d)"
gh repo clone "$PP_TOOLKIT_REPO" "$TK" -- --depth 1
"$TK/skills/policies-preferences/install.sh"

# In the owner's new or existing source repo:
cd /path/to/your-source-repo
git switch -c pp/init
"$TK/skills/policies-preferences/bin/pp" --source-dir . init \
  --owner <id> --owner-name "<Name>" \
  --repo <owner/name> --bot <slug> [--bot <slug> ...]
skills/policies-preferences/bin/pp validate
git add -A && git commit -m "policies-preferences: init"
git push -u origin pp/init
gh pr create --base main --title "policies-preferences: init" --body "Scaffolded by pp init."
```

After the owner merges that PR, give each Grok bot the
[`SKILL.md`](skills/policies-preferences/SKILL.md), plus a pointer to the source repo
(`PP_SOURCE_REPO`) and that bot's slug (`PP_BOT`). The owner should require pull requests and
the `validate` check on `main`.

## License

MIT. See [LICENSE](LICENSE).
