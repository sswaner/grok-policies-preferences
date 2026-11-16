#!/usr/bin/env python3
"""pp: load, propose, validate, review, and apply an owner's Grok Bot policies and preferences.

Single source of truth: one git repo (the "source") holding a preferences directory
(policies.yaml, preferences.yaml, CHANGELOG.md, decisions/). Which repo, directory, and owner
come from config: flags > PP_* env vars > pp.config.yaml at the source root > defaults
(see `pp config`). Scaffold a new instance with `pp init`.

Rules enforced here:
  * POLICY is set by the owner alone. No bot-facing command creates, edits, or proposes one.
  * PREFERENCE may be *proposed* by a bot (status: proposed); only the owner's decisions
    (`apply-decisions`, reviewer: <owner>) activate, edit, or retire it.
  * Policy always overrides preference. Among preferences, the more specific scope wins
    (bot > role > all).
  * Every write bumps the file's semver `version` and prepends a CHANGELOG.md entry.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "pp: PyYAML is required. Run skills/policies-preferences/install.sh "
        "or `pip install pyyaml jsonschema`.\n"
    )
    sys.exit(3)

try:
    import jsonschema  # optional; built-in checks always run
except ImportError:  # pragma: no cover
    jsonschema = None

TOOLKIT = Path(__file__).resolve().parents[1]
CONFIG_FILE = "pp.config.yaml"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
POL_ID = re.compile(r"^(EXAMPLE-)?POL-\d{3,}$")
PREF_ID = re.compile(r"^(EXAMPLE-)?PREF-\d{3,}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
BUMPS = ["none", "patch", "minor", "major"]
POLICY_STATUSES = {"active", "retired"}
PREF_STATUSES = {"proposed", "active", "retired", "rejected"}
REV_STATUSES = {"pending", "applied", "rejected"}
SCOPE_RANK = {"all": 0, "role": 1, "bot": 2}
EDITABLE = {"title", "rule", "applies_to", "polarity", "rationale", "supersedes"}

POL_HEADER = """\
# GROK BOT POLICIES: constitutional guidance set by {name} alone.
# Bots: READ-ONLY. Never create, edit, or propose a policy. Policy always overrides preference.
# Every change lands through a PR that {name} merges; bots never push to main.
# Changes only through `pp apply-decisions` with {name}'s decisions (bumps `version`, writes CHANGELOG.md).
# Managed by the policies-preferences toolkit (`pp`). Schema: skills/policies-preferences/schema/
"""
PREF_HEADER = """\
# GROK BOT PREFERENCES: how {name} wants their Grok bots to do their assigned work.
# Bots may only ADD `proposed` entries or pending feedback, on a pp/* branch + PR (never push to main).
# Only {name} activates, edits, or retires an entry (`pp apply-decisions`, reviewer: {owner}).
# Managed by the policies-preferences toolkit (`pp`). Schema: skills/policies-preferences/schema/
"""


class PPError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except Exception:
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _is_instance(src: Path) -> bool:
    return (src / CONFIG_FILE).exists() or (src / "preferences" / "policies.yaml").exists()


def _repo_from_remote(src: Path) -> str | None:
    try:
        url = subprocess.run(["git", "-C", str(src), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
    except Exception:
        return None
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m and not url.startswith("/") else None


class Config:
    """Resolved settings. Precedence: flags > env > pp.config.yaml > defaults.

    Source dir exception (safety): a toolkit that lives inside an instance repo (the normal,
    vendored layout, including throwaway worktrees) always uses that repo unless --source-dir
    is given, so an exported PP_SOURCE_DIR can't redirect a worktree's writes to the shared clone.
    """
    KEYS = ("source_dir", "source_repo", "home", "owner", "owner_name", "bot", "config_file")

    def __init__(self, **kw):
        for k in self.KEYS:
            setattr(self, k, kw.get(k))

    def as_dict(self) -> dict:
        return {k: (str(getattr(self, k)) if getattr(self, k) is not None else None) for k in self.KEYS}


def resolve_config(home=None, source_dir=None, owner=None, owner_name=None, repo=None, bot=None,
                   env=None) -> Config:
    env = os.environ if env is None else env
    if source_dir:
        src = Path(source_dir)
    elif home:
        src = _git_toplevel(Path(home)) or Path(home).resolve().parent
    else:
        top = _git_toplevel(TOOLKIT)
        if top and _is_instance(top):
            src = top
        elif env.get("PP_SOURCE_DIR") or env.get("PP_TOOLS_DIR"):  # PP_TOOLS_DIR: legacy alias
            src = Path(env.get("PP_SOURCE_DIR") or env["PP_TOOLS_DIR"])
        else:
            src = _git_toplevel(Path.cwd()) or Path.cwd()
    cfg_path = Path(env["PP_CONFIG"]) if env.get("PP_CONFIG") else src / CONFIG_FILE
    filecfg = {}
    if cfg_path.exists():
        filecfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(filecfg, dict):
            raise PPError(f"{cfg_path}: must be a mapping")

    def pick(flag, envkey, filekey, default=None):
        return flag or env.get(envkey) or filecfg.get(filekey) or default

    h = home or env.get("PP_HOME")
    h = Path(h) if h else src / (filecfg.get("home") or "preferences")
    return Config(source_dir=src, home=h, config_file=cfg_path if cfg_path.exists() else None,
                  source_repo=pick(repo, "PP_SOURCE_REPO", "source_repo") or _repo_from_remote(src) or "local",
                  owner=pick(owner, "PP_OWNER", "owner"),
                  owner_name=pick(owner_name, "PP_OWNER_NAME", "owner_name"),
                  bot=pick(bot, "PP_BOT", "bot"))


def default_home() -> Path:
    return resolve_config().home


def now() -> str:
    return os.environ.get("PP_NOW") or dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return now()[:10]


def bump(version: str, level: str) -> str:
    m = SEMVER.match(version or "")
    if not m:
        raise PPError(f"not a semver version: {version!r}")
    ma, mi, pa = map(int, m.groups())
    if level == "major":
        return f"{ma + 1}.0.0"
    if level == "minor":
        return f"{ma}.{mi + 1}.0"
    if level == "patch":
        return f"{ma}.{mi}.{pa + 1}"
    return version


def max_bump(a: str, b: str) -> str:
    return a if BUMPS.index(a) >= BUMPS.index(b) else b


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise PPError(f"missing file: {path}")
    if not isinstance(data, dict):
        raise PPError(f"{path}: top level must be a mapping")
    return data


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):  # indent list items
        return super().increase_indent(flow, False)


def dump_yaml(data: dict, header: str) -> str:
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100,
                     default_flow_style=False)
    return header + "\n" + body


def git_rev(root: Path) -> str:
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "nogit"


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, root: Path | str | None = None, cfg: Config | None = None):
        self.cfg = cfg or resolve_config(home=root)
        self.root = Path(root) if root else Path(self.cfg.home)
        self.pol_path = self.root / "policies.yaml"
        self.pref_path = self.root / "preferences.yaml"
        self.cl_path = self.root / "CHANGELOG.md"
        local = self.root / "schema"
        self.schema_dir = local if local.is_dir() else TOOLKIT / "schema"
        self.pol = load_yaml(self.pol_path)
        self.pref = load_yaml(self.pref_path)
        # The owner: configured value, else the instance's own `owner:` field.
        self.owner = self.cfg.owner or self.pol.get("owner")
        if not self.owner:
            raise PPError("no owner configured: set `owner:` in policies.yaml, pp.config.yaml, or PP_OWNER")
        self.owner_name = self.cfg.owner_name or self.owner

    # -- accessors
    @property
    def policies(self) -> list:
        return self.pol.setdefault("policies", []) or []

    @property
    def preferences(self) -> list:
        return self.pref.setdefault("preferences", []) or []

    @property
    def scopes(self) -> dict:
        return self.pol.get("scopes") or {}

    def find(self, entry_id: str):
        for e in self.policies:
            if e.get("id") == entry_id:
                return "policies", e
        for e in self.preferences:
            if e.get("id") == entry_id:
                return "preferences", e
        raise PPError(f"no policy or preference with id {entry_id}")

    def citation(self) -> str:
        return (f"policies v{self.pol.get('version')}, preferences v{self.pref.get('version')} "
                f"({self.cfg.source_repo}@{git_rev(self.root)})")

    def next_id(self, kind: str) -> str:
        prefix, items = ("POL", self.policies) if kind == "policies" else ("PREF", self.preferences)
        nums = [int(e["id"].split("-")[-1]) for e in items
                if re.match(rf"^{prefix}-\d+$", str(e.get("id", "")))]
        return f"{prefix}-{(max(nums) + 1) if nums else 1:03d}"

    def save(self):
        fmt = {"name": self.owner_name, "owner": self.owner}
        self.pol_path.write_text(dump_yaml(self.pol, POL_HEADER.format(**fmt)), encoding="utf-8")
        self.pref_path.write_text(dump_yaml(self.pref, PREF_HEADER.format(**fmt)), encoding="utf-8")

    def write_changelog(self, lines: list[str], by: str, via: str):
        heading = (f"## {today()}: policies {self.pol['version']} · "
                   f"preferences {self.pref['version']}")
        block = heading + "\n\n" + f"_by {by} via {via}_\n\n" + "\n".join(f"- {l}" for l in lines) + "\n\n"
        text = self.cl_path.read_text(encoding="utf-8") if self.cl_path.exists() else "# Changelog\n\n"
        idx = text.find("\n## ")
        text = (text[: idx + 1] + block + text[idx + 1:]) if idx >= 0 else (text.rstrip("\n") + "\n\n" + block)
        self.cl_path.write_text(text, encoding="utf-8")

    def commit_versions(self, bumps: dict, pending_history: list):
        """Apply semver bumps per file, stamp history entries with the new version."""
        for kind, level in bumps.items():
            if level == "none":
                continue
            doc = self.pol if kind == "policies" else self.pref
            doc["version"] = bump(doc["version"], level)
            doc["updated"] = today()
        for kind, hist in pending_history:
            hist["version"] = (self.pol if kind == "policies" else self.pref)["version"]


# ---------------------------------------------------------------- validate
def _schema_registry(schema_dir: Path):
    """Offline registry of our schemas so cross-file $refs never hit the network."""
    if jsonschema is None:
        return None
    try:
        from referencing import Registry, Resource
    except ImportError:  # very old jsonschema: built-in checks only
        return None
    res = []
    for sp in sorted(schema_dir.glob("*.schema.json")):
        doc = json.loads(sp.read_text(encoding="utf-8"))
        res.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(res)


def validate(store: Store) -> list[str]:
    errs: list[str] = []
    OWNER, NAME = store.owner, store.owner_name
    registry = _schema_registry(store.schema_dir)
    if registry is not None:
        for doc, name in ((store.pol, "policies"), (store.pref, "preferences")):
            sp = store.schema_dir / f"{name}.schema.json"
            if sp.exists():
                schema = json.loads(sp.read_text(encoding="utf-8"))
                v = jsonschema.Draft202012Validator(schema, registry=registry)
                for e in sorted(v.iter_errors(doc), key=lambda e: list(e.path)):
                    loc = "/".join(str(p) for p in e.path) or "<root>"
                    errs.append(f"{name}.yaml schema: {loc}: {e.message}")

    for doc, kind in ((store.pol, "policies"), (store.pref, "preferences")):
        if doc.get("kind") != kind:
            errs.append(f"{kind}.yaml: kind must be {kind!r}")
        if not SEMVER.match(str(doc.get("version", ""))):
            errs.append(f"{kind}.yaml: version must be semver MAJOR.MINOR.PATCH")
        if doc.get("owner") != OWNER:
            errs.append(f"{kind}.yaml: owner must be {OWNER!r}")

    scopes = store.scopes
    if "all" not in scopes:
        errs.append("policies.yaml: scopes must define 'all'")
    for name, meta in scopes.items():
        if not NAME_RE.match(str(name)):
            errs.append(f"scope {name!r}: names are lowercase [a-z0-9_-]")
        if not isinstance(meta, dict) or meta.get("kind") not in SCOPE_RANK:
            errs.append(f"scope {name!r}: kind must be one of all|role|bot")

    seen: set[str] = set()

    def common(e, kind, statuses, id_re):
        eid = str(e.get("id", ""))
        where = f"{kind}.yaml {eid or '<no id>'}"
        if not id_re.match(eid):
            errs.append(f"{where}: bad id (want {'POL' if kind == 'policies' else 'PREF'}-NNN)")
        if eid in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(eid)
        is_ex = eid.startswith("EXAMPLE-")
        if bool(e.get("example")) != is_ex:
            errs.append(f"{where}: EXAMPLE- ids and example: true must go together")
        if is_ex and not str(e.get("title", "")).startswith("EXAMPLE"):
            errs.append(f"{where}: example title must start with 'EXAMPLE'")
        for f in ("title", "rule", "applies_to", "status"):
            if not e.get(f):
                errs.append(f"{where}: missing {f}")
        if e.get("status") not in statuses:
            errs.append(f"{where}: status must be one of {sorted(statuses)}")
        for s in e.get("applies_to") or []:
            if s not in scopes:
                errs.append(f"{where}: unknown scope {s!r} ({NAME} adds scopes in policies.yaml)")
        for r in e.get("pending_revisions") or []:
            if r.get("status") not in REV_STATUSES:
                errs.append(f"{where}: revision {r.get('rev')}: bad status")
            if not r.get("note"):
                errs.append(f"{where}: revision {r.get('rev')}: note required")
        return where

    for e in store.policies:
        where = common(e, "policies", POLICY_STATUSES, POL_ID)
        if e.get("set_by") != OWNER:
            errs.append(f"{where}: set_by must be {OWNER!r} (only {NAME} sets policy)")
        if e.get("polarity") not in ("prohibitive", "directive"):
            errs.append(f"{where}: polarity must be prohibitive|directive")
        for r in e.get("pending_revisions") or []:
            if r.get("proposed_rule") and r.get("by") != OWNER:
                errs.append(f"{where}: revision {r.get('rev')}: only {NAME} may draft policy text")

    for e in store.preferences:
        where = common(e, "preferences", PREF_STATUSES, PREF_ID)
        src = e.get("source") or {}
        if not src.get("by") or not src.get("kind"):
            errs.append(f"{where}: source.by and source.kind required")
        if e.get("status") in ("active", "retired") and not e.get("example"):
            rv = e.get("reviewed") or {}
            if rv.get("by") != OWNER:
                errs.append(f"{where}: status {e.get('status')} requires reviewed.by: {OWNER} "
                            "(bots never activate preferences)")
        for sid in e.get("supersedes") or []:
            if not any(p.get("id") == sid for p in store.preferences):
                errs.append(f"{where}: supersedes unknown {sid}")

    if store.cl_path.exists():
        cl = store.cl_path.read_text(encoding="utf-8")
        heads = [l for l in cl.splitlines() if l.startswith("## ")]
        pv, fv = store.pol.get("version"), store.pref.get("version")
        if not any(f"policies {pv}" in h for h in heads):
            errs.append(f"CHANGELOG.md: no entry for policies {pv}")
        if not any(f"preferences {fv}" in h for h in heads):
            errs.append(f"CHANGELOG.md: no entry for preferences {fv}")
    else:
        errs.append("CHANGELOG.md missing")
    return errs


def require_valid(store: Store):
    errs = validate(store)
    if errs:
        raise PPError("validation failed:\n  " + "\n  ".join(errs))


# ---------------------------------------------------------------- load
def resolve_names(store: Store, bot: str | None, roles: list[str]) -> tuple[set, list]:
    names, warnings = set(), []
    for n in ([bot] if bot else []) + list(roles or []):
        if n in store.scopes:
            names.add(n)
        else:
            warnings.append(f"unknown scope name {n!r}; only 'all' entries and known names apply. "
                            f"Ask {store.owner_name} to register it in policies.yaml scopes.")
    return names, warnings


def specificity(store: Store, entry: dict, names: set) -> int:
    ranks = [SCOPE_RANK[store.scopes[s]["kind"]] for s in entry.get("applies_to", [])
             if s == "all" or s in names]
    return max(ranks) if ranks else -1


def applicable(store: Store, bot: str | None, roles: list[str], include_examples=False):
    names, warnings = resolve_names(store, bot, roles)

    def ok(e):
        if e.get("status") != "active":
            return False
        if e.get("example") and not include_examples:
            return False
        a = e.get("applies_to") or []
        return "all" in a or bool(set(a) & names)

    pols = sorted([e for e in store.policies if ok(e)], key=lambda e: e["id"])
    prefs = [e for e in store.preferences if ok(e)]
    superseded = {s for e in prefs for s in (e.get("supersedes") or [])}
    prefs = [e for e in prefs if e["id"] not in superseded]
    prefs.sort(key=lambda e: (-specificity(store, e, names), e["id"]))
    return pols, prefs, warnings


def render_load(store, bot, roles, fmt, include_examples=False) -> str:
    pols, prefs, warnings = applicable(store, bot, roles, include_examples)
    if fmt == "json":
        return json.dumps({"citation": store.citation(), "bot": bot, "roles": roles,
                           "precedence": ["policy", "preference(bot)", "preference(role)", "preference(all)"],
                           "policies": pols, "preferences": prefs, "warnings": warnings},
                          indent=2, default=str)
    out = [f"Governing set: {store.citation()}",
           f"Applies to: bot={bot or '-'} roles={','.join(roles) or '-'}",
           "Precedence: POLICY overrides PREFERENCE. Among preferences, bot-specific > role > all.",
           "Cite the governing set above when you report what guidance you followed.", ""]
    for w in warnings:
        out.append(f"WARNING: {w}")
    out.append("POLICIES (constitutional; must obey; never edit or propose):")
    out += [f"  [{p['id']}] {p['title']}: {p['rule']}  (applies_to: {', '.join(p['applies_to'])})"
            for p in pols] or ["  (none)"]
    out.append("")
    out.append(f"PREFERENCES (how {store.owner_name} wants the work done; yield to any policy):")
    out += [f"  [{p['id']}] {p['title']}: {p['rule']}  (applies_to: {', '.join(p['applies_to'])})"
            for p in prefs] or ["  (none)"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- bot writes
def propose(store: Store, by: str, title: str, rule: str, applies_to: list[str],
            evidence: str, supersedes: list[str] | None = None, kind: str = "learned") -> str:
    if not by or not NAME_RE.match(by):
        raise PPError("--by must be the proposing bot's (or human's) strict lowercase name")
    if not evidence:
        raise PPError(f"--evidence is required: quote or cite what {store.owner_name} said or did")
    for s in applies_to:
        if s not in store.scopes:
            raise PPError(f"unknown scope {s!r}; known: {', '.join(sorted(store.scopes))}")
    for e in store.preferences:
        if e.get("status") in ("proposed", "active") and norm(e.get("rule")) == norm(rule):
            raise PPError(f"duplicate of {e['id']} ({e['status']}); add feedback to it instead")
    for sid in supersedes or []:
        store.find(sid)
    pid = store.next_id("preferences")
    hist = {"at": now(), "version": None, "action": "proposed", "by": by, "note": evidence}
    entry = {"id": pid, "title": title, "rule": rule, "applies_to": applies_to, "status": "proposed",
             "source": {"kind": kind, "by": by, "at": now(), "evidence": evidence},
             "reviewed": None, "pending_revisions": [], "history": [hist]}
    if supersedes:
        entry["supersedes"] = supersedes
    store.preferences.append(entry)
    store.commit_versions({"preferences": "patch"}, [("preferences", hist)])
    entry["added_in"] = store.pref["version"]
    require_valid_after_change(store, [f"preferences: {pid} proposed by {by}: \"{title}\""], by, "pp propose")
    return pid


def feedback(store: Store, entry_id: str, note: str, by: str, proposed_rule: str | None = None,
             feedback_from: str | None = None) -> str:
    OWNER = store.owner
    feedback_from = feedback_from or OWNER
    kind, e = store.find(entry_id)
    if not note:
        raise PPError("--note is required")
    if kind == "policies" and proposed_rule and by != OWNER:
        raise PPError(f"bots may not draft policy text; record {store.owner_name}'s note only")
    revs = e.setdefault("pending_revisions", []) or []
    e["pending_revisions"] = revs
    rid = f"R{len(revs) + 1}"
    rev = {"rev": rid, "status": "pending", "at": now(), "by": by, "from": feedback_from, "note": note}
    if proposed_rule:
        rev["proposed_rule"] = proposed_rule
    revs.append(rev)
    hist = {"at": now(), "version": None, "action": f"feedback {rid}", "by": by, "note": note}
    e.setdefault("history", []).append(hist)
    store.commit_versions({kind: "patch"}, [(kind, hist)])
    require_valid_after_change(store, [f"{kind}: {entry_id} feedback {rid} awaiting review ({note[:80]})"],
                               by, "pp feedback")
    return rid


def revise(store: Store, entry_id: str, rev_id: str, proposed_rule: str, by: str):
    kind, e = store.find(entry_id)
    if kind == "policies" and by != store.owner:
        raise PPError("bots may not draft policy text")
    rev = _rev(e, rev_id)
    if rev["status"] != "pending":
        raise PPError(f"{entry_id} {rev_id} is {rev['status']}, not pending")
    rev["proposed_rule"] = proposed_rule
    rev["drafted_by"] = by
    hist = {"at": now(), "version": None, "action": f"drafted {rev_id}", "by": by, "note": proposed_rule}
    e.setdefault("history", []).append(hist)
    store.commit_versions({kind: "patch"}, [(kind, hist)])
    require_valid_after_change(store, [f"{kind}: {entry_id} {rev_id} draft text added by {by}"], by, "pp revise")


def require_valid_after_change(store: Store, lines, by, via):
    store.write_changelog(lines, by, via)
    errs = validate(store)
    if errs:
        raise PPError("change would leave the set invalid:\n  " + "\n  ".join(errs))
    store.save()


def _rev(e, rev_id):
    for r in e.get("pending_revisions") or []:
        if r.get("rev") == rev_id:
            return r
    raise PPError(f"{e.get('id')}: no revision {rev_id}")


# ---------------------------------------------------------------- the owner's decisions
def apply_decisions(store: Store, decisions: dict, source: str = "decisions", dry_run=False) -> list[str]:
    OWNER = store.owner
    if (decisions or {}).get("reviewer") != OWNER:
        raise PPError(f"decisions file must say `reviewer: {OWNER}`; only {store.owner_name}'s decisions are applied")
    bumps = {"policies": "none", "preferences": "none"}
    hist_refs, lines = [], []
    at = now()

    def record(kind, e, action, level, note=None):
        bumps[kind] = max_bump(bumps[kind], level)
        h = {"at": at, "version": None, "action": action, "by": OWNER}
        if note:
            h["note"] = note
        e.setdefault("history", []).append(h)
        hist_refs.append((kind, h))

    def mark_reviewed(kind, e):
        e["reviewed"] = {"by": OWNER, "at": at, "version": None}
        hist_refs.append((kind, e["reviewed"]))

    def apply_fields(e, d, kind):
        allowed = EDITABLE if kind == "policies" else EDITABLE - {"polarity", "rationale"}
        changed = []
        for f in sorted(allowed):
            if f in d and d[f] is not None:
                e[f] = d[f]
                changed.append(f)
        return changed

    for d in decisions.get("decisions") or []:
        action = (d.get("action") or "").strip()
        if not action:
            continue  # undecided rows in a template are skipped
        kind, e = store.find(d["id"])
        eid, st, note = e["id"], e.get("status"), d.get("note")
        breaking = "major" if d.get("breaking") else None
        if action == "approve":
            if kind != "preferences" or st != "proposed":
                raise PPError(f"{eid}: approve applies to proposed preferences (status {st})")
            changed = apply_fields(e, d, kind)
            e["status"] = "active"
            mark_reviewed(kind, e)
            record(kind, e, "approved" + (f" with edits ({', '.join(changed)})" if changed else ""), "minor", note)
            lines.append(f"{kind}: {eid} approved: \"{e['title']}\"")
        elif action == "reject":
            if kind != "preferences" or st != "proposed":
                raise PPError(f"{eid}: reject applies to proposed preferences")
            e["status"] = "rejected"
            mark_reviewed(kind, e)
            record(kind, e, "rejected", "patch", note)
            lines.append(f"{kind}: {eid} rejected")
        elif action == "edit":
            changed = apply_fields(e, d, kind)
            if not changed:
                raise PPError(f"{eid}: edit needs at least one of {sorted(EDITABLE)}")
            if st == "active":
                mark_reviewed(kind, e)
            level = breaking or ("minor" if st == "active" else "patch")
            record(kind, e, f"edited ({', '.join(changed)})", level, note)
            lines.append(f"{kind}: {eid} edited ({', '.join(changed)})")
        elif action == "retire":
            if st != "active":
                raise PPError(f"{eid}: only active entries can be retired")
            e["status"] = "retired"
            mark_reviewed(kind, e)
            record(kind, e, "retired", "major", note)
            lines.append(f"{kind}: {eid} retired: \"{e['title']}\"")
        elif action == "reactivate":
            if st != "retired":
                raise PPError(f"{eid}: only retired entries can be reactivated")
            e["status"] = "active"
            mark_reviewed(kind, e)
            record(kind, e, "reactivated", "minor", note)
            lines.append(f"{kind}: {eid} reactivated")
        elif action == "feedback":
            if not note:
                raise PPError(f"{eid}: feedback needs a note")
            revs = e.setdefault("pending_revisions", []) or []
            e["pending_revisions"] = revs
            rid = f"R{len(revs) + 1}"
            rev = {"rev": rid, "status": "pending", "at": at, "by": OWNER, "from": OWNER, "note": note}
            if d.get("rule"):
                rev["proposed_rule"] = d["rule"]
            revs.append(rev)
            record(kind, e, f"feedback {rid}", "patch", note)
            lines.append(f"{kind}: {eid} feedback {rid} awaiting review")
        elif action in ("approve-revision", "reject-revision"):
            rev = _rev(e, d.get("rev") or "")
            if rev["status"] != "pending":
                raise PPError(f"{eid} {rev['rev']}: not pending")
            if action == "reject-revision":
                rev["status"] = "rejected"
                record(kind, e, f"rejected {rev['rev']}", "patch", note)
                lines.append(f"{kind}: {eid} {rev['rev']} rejected")
            else:
                new_rule = d.get("rule") or rev.get("proposed_rule")
                if not new_rule:
                    raise PPError(f"{eid} {rev['rev']}: no text to apply; give `rule:` in the decision")
                e["rule"] = new_rule
                for f in ("title", "applies_to"):
                    if d.get(f):
                        e[f] = d[f]
                rev["status"] = "applied"
                if e.get("status") == "active":
                    mark_reviewed(kind, e)
                record(kind, e, f"applied {rev['rev']}", breaking or "minor", note)
                lines.append(f"{kind}: {eid} {rev['rev']} applied: \"{new_rule[:80]}\"")
        else:
            raise PPError(f"{eid}: unknown action {action!r}")

    for d in decisions.get("new_policies") or []:
        pid = store.next_id("policies")
        e = {"id": pid, "title": d["title"], "rule": d["rule"], "polarity": d.get("polarity", "directive"),
             "applies_to": d.get("applies_to", ["all"]), "status": "active", "set_by": OWNER}
        for f in ("rationale",):
            if d.get(f):
                e[f] = d[f]
        e["pending_revisions"], e["history"] = [], []
        store.policies.append(e)
        record("policies", e, "created", "minor", d.get("note"))
        lines.append(f"policies: {pid} created: \"{e['title']}\"")
    for d in decisions.get("new_preferences") or []:
        pid = store.next_id("preferences")
        e = {"id": pid, "title": d["title"], "rule": d["rule"], "applies_to": d.get("applies_to", ["all"]),
             "status": "active", "source": {"kind": "stated", "by": OWNER, "at": at,
                                            "evidence": d.get("note") or "set directly in review"},
             "reviewed": None, "pending_revisions": [], "history": []}
        store.preferences.append(e)
        mark_reviewed("preferences", e)
        record("preferences", e, "created", "minor", d.get("note"))
        lines.append(f"preferences: {pid} created: \"{e['title']}\"")

    if decisions.get("bump"):
        for k in bumps:
            if bumps[k] != "none":
                bumps[k] = max_bump(bumps[k], decisions["bump"])
    if not lines:
        return []
    store.commit_versions(bumps, hist_refs)
    for kind, e in [(k, x) for k in ("policies", "preferences")
                    for x in (store.policies if k == "policies" else store.preferences)]:
        e.setdefault("added_in", (store.pol if kind == "policies" else store.pref)["version"])
    store.pol["reviewed"] = store.pref["reviewed"] = {"by": OWNER, "at": at}
    if dry_run:
        errs = validate_without_changelog(store)
        if errs:
            raise PPError("decisions would leave the set invalid:\n  " + "\n  ".join(errs))
        return lines
    require_valid_after_change(store, lines, OWNER, f"apply-decisions ({source})")
    return lines


def validate_without_changelog(store):
    return [e for e in validate(store) if not e.startswith("CHANGELOG.md")]


# ---------------------------------------------------------------- review
def render_review(store: Store, fmt="markdown") -> tuple[str, dict]:
    pending, rows = [], []
    for kind, items in (("policies", store.policies), ("preferences", store.preferences)):
        for e in items:
            if e.get("example"):
                continue
            if kind == "preferences" and e.get("status") == "proposed":
                pending.append({"id": e["id"], "action": "",
                                "_hint": f"proposed by {e['source']['by']}: {e['title']}. approve | reject | approve with rule:/title:/applies_to: edits"})
            for r in e.get("pending_revisions") or []:
                if r.get("status") == "pending":
                    pending.append({"id": e["id"], "rev": r["rev"], "action": "",
                                    "_hint": f"feedback: {r['note'][:70]}. approve-revision (+rule:) | reject-revision"})
    template = {"reviewer": store.owner, "reviewed_at": today(),
                "reviewing": {"policies": store.pol["version"], "preferences": store.pref["version"]},
                "decisions": pending, "new_policies": [], "new_preferences": []}
    if fmt == "json":
        return json.dumps({"citation": store.citation(), "policies": store.policies,
                           "preferences": store.preferences, "decisions_template": template},
                          indent=2, default=str), template

    def entry_md(e, kind):
        out = [f"#### {e['id']} · {e['title']}" + ("  _(EXAMPLE, never loaded)_" if e.get("example") else ""),
               f"- **Status:** {e['status']} · **Applies to:** {', '.join(e.get('applies_to', []))}"
               + (f" · **Polarity:** {e['polarity']}" if kind == "policies" else ""),
               f"- **Rule:** {e['rule']}"]
        if e.get("rationale"):
            out.append(f"- **Rationale:** {e['rationale']}")
        src = e.get("source")
        if src:
            out.append(f"- **Source:** {src.get('kind')} · by {src.get('by')} · {str(src.get('at', ''))[:10]}"
                       f": {src.get('evidence', '')}")
        rv = e.get("reviewed")
        out.append(f"- **Last reviewed:** {str(rv.get('at'))[:10]} by {rv.get('by')} (v{rv.get('version')})"
                   if rv else "- **Last reviewed:** never")
        for r in e.get("pending_revisions") or []:
            if r.get("status") == "pending":
                out.append(f"- **Pending {r['rev']}** ({r.get('from', r.get('by'))}, {str(r.get('at'))[:10]}): "
                           f"{r['note']}" + (f" → proposed text: \"{r['proposed_rule']}\"" if r.get("proposed_rule") else ""))
        return "\n".join(out)

    need = [e for e in store.preferences if e.get("status") == "proposed" and not e.get("example")]
    revs = [(k, e) for k, items in (("policies", store.policies), ("preferences", store.preferences))
            for e in items if not e.get("example")
            and any(r.get("status") == "pending" for r in e.get("pending_revisions") or [])]
    md = [f"# Policies & preferences review: {today()}", "",
          f"Reviewing **{store.citation()}**. Policy overrides preference; only you set policy "
          "and only you activate a preference.", "",
          f"**Needs your decision:** {len(need)} proposed preference(s), "
          f"{len(revs)} with pending feedback.", ""]
    md += ["## 1. Proposed preferences (approve, edit, or reject)", ""]
    md += [entry_md(e, "preferences") + "\n" for e in need] or ["_None._\n"]
    md += ["## 2. Feedback awaiting a revision decision", ""]
    md += [entry_md(e, k) + "\n" for k, e in revs] or ["_None._\n"]
    md += ["## 3. Active policies (constitutional, set by you)", ""]
    md += [entry_md(e, "policies") + "\n" for e in store.policies if e["status"] == "active"] or ["_None._\n"]
    md += ["## 4. Active preferences", ""]
    md += [entry_md(e, "preferences") + "\n" for e in store.preferences if e["status"] == "active"] or ["_None._\n"]
    retired = [e for e in store.policies + store.preferences if e["status"] in ("retired", "rejected")]
    md += ["## 5. Retired or rejected", ""]
    md += [f"- {e['id']} · {e['title']} ({e['status']})" for e in retired] or ["_None._"]
    md += ["", "## 6. Your decisions",
           "Fill in `action` for each row (leave blank to skip). For any other entry, add a row with "
           "`edit` (+ `rule:`), `retire`, `reactivate`, or `feedback` (+ `note:`). Add new ones under "
           "`new_policies` / `new_preferences`. Then run `pp apply-decisions <file>`.", "",
           "```yaml", yaml.dump(template, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100).rstrip(),
           "```", ""]
    return "\n".join(md), template


# ---------------------------------------------------------------- bot-safe diff check
def check_diff(root: Path, base: str, cfg: Config | None = None) -> list[str]:
    """Verify a bot's working-tree change vs `base` only proposes preferences / adds feedback."""
    def at_base(name):
        rel = os.path.relpath(root / name, _git_top(root))
        out = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise PPError(f"cannot read {rel} at {base}: {out.stderr.strip()}")
        return yaml.safe_load(out.stdout)

    errs = []
    base_pol, base_pref = at_base("policies.yaml"), at_base("preferences.yaml")
    cur = Store(root, cfg)
    pol_a, pol_b = copy.deepcopy(base_pol), copy.deepcopy(cur.pol)
    for doc in (pol_a, pol_b):
        doc.pop("version", None); doc.pop("updated", None)
        for p in doc.get("policies") or []:
            p.pop("pending_revisions", None); p.pop("history", None)
    if pol_a != pol_b:
        errs.append("policies.yaml content changed; bots may never edit policies (feedback notes only)")
    for p_old in base_pol.get("policies") or []:
        p_new = next((p for p in cur.policies if p["id"] == p_old["id"]), None)
        if p_new:
            errs += _check_revs_append_only(p_old, p_new, policy=True)
    old = {p["id"]: p for p in base_pref.get("preferences") or []}
    for p in cur.preferences:
        if p["id"] not in old:
            if p.get("status") != "proposed":
                errs.append(f"{p['id']}: new preferences must be status proposed")
            if (p.get("reviewed") or {}).get("by"):
                errs.append(f"{p['id']}: bots may not set reviewed")
            continue
        o = old.pop(p["id"])
        a, b = copy.deepcopy(o), copy.deepcopy(p)
        for x in (a, b):
            x.pop("pending_revisions", None); x.pop("history", None)
        if a != b:
            errs.append(f"{p['id']}: existing preference changed; only the owner's decisions may edit it")
        errs += _check_revs_append_only(o, p)
    for gone in old:
        errs.append(f"{gone}: preference removed")
    for kind, b_doc, c_doc in (("policies", base_pol, cur.pol), ("preferences", base_pref, cur.pref)):
        bm, cm = SEMVER.match(str(b_doc.get("version"))), SEMVER.match(str(c_doc.get("version")))
        if bm and cm and bm.groups()[:2] != cm.groups()[:2]:
            errs.append(f"{kind}.yaml: bot changes may only bump PATCH ({b_doc['version']} → {c_doc['version']})")
    errs += validate(cur)
    return errs


def _check_revs_append_only(old, new, policy=False):
    errs = []
    o, n = old.get("pending_revisions") or [], new.get("pending_revisions") or []
    for i, r in enumerate(o):
        if i >= len(n):
            errs.append(f"{old['id']}: revision {r.get('rev')} removed")
            continue
        r2 = dict(n[i])
        if r2.get("status") != r.get("status"):
            errs.append(f"{old['id']}: revision {r.get('rev')} status changed by a bot")
        if not policy:
            r2.pop("proposed_rule", None); r2.pop("drafted_by", None)
            r1 = {k: v for k, v in r.items() if k not in ("proposed_rule", "drafted_by")}
        else:
            r1 = r
        if r1 != r2:
            errs.append(f"{old['id']}: revision {r.get('rev')} altered")
    for r in n[len(o):]:
        if r.get("status") != "pending":
            errs.append(f"{old['id']}: new revision {r.get('rev')} must be pending")
        if policy and r.get("proposed_rule"):
            errs.append(f"{old['id']}: bots may not draft policy text")
    return errs


def _git_top(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()


# ---------------------------------------------------------------- init (new owner)
def init_instance(target: Path, owner: str, owner_name: str | None = None, repo: str | None = None,
                  home_dir: str = "preferences", bots: list[str] | None = None, ci: bool = True,
                  force: bool = False) -> list[str]:
    """Scaffold an empty, valid instance in `target` (a git repo clone). Vendors the toolkit into
    target/skills/policies-preferences when it isn't already there. Writes nothing on error."""
    import shutil
    target = Path(target).resolve()
    if not NAME_RE.match(owner or ""):
        raise PPError("--owner must be lowercase [a-z0-9_-], e.g. 'jdoe'")
    for b in bots or []:
        if not NAME_RE.match(b) or b in ("all", "coding", "review", "comms", "research", owner):
            raise PPError(f"bad or reserved bot slug {b!r}")
    if Path(home_dir).is_absolute() or ".." in Path(home_dir).parts:
        raise PPError("--home-dir must be a relative path inside the repo")
    name = owner_name or owner
    repo = repo or _repo_from_remote(target) or "local"
    home = target / home_dir
    if (home / "policies.yaml").exists() and not force:
        raise PPError(f"{home} already holds an instance (use --force to overwrite)")
    out = []
    tk = target / "skills" / "policies-preferences"
    if tk.resolve() != TOOLKIT:
        if tk.exists() and not force:
            raise PPError(f"{tk} exists; refusing to overwrite (use --force)")
        if tk.exists():
            shutil.rmtree(tk)
        shutil.copytree(TOOLKIT, tk, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        out.append(f"vendored toolkit -> {tk.relative_to(target)}")
    at, day = now(), today()
    scopes = {"all": {"kind": "all", "description": f"Every one of {name}'s Grok bots."},
              "coding": {"kind": "role", "description": "Grok bots doing repository work."},
              "review": {"kind": "role", "description": "Grok bots reviewing pull requests."},
              "comms": {"kind": "role", "description": f"Grok bots that message people or bots on {name}'s behalf."},
              "research": {"kind": "role", "description": "Grok bots doing research, analysis, or data work."}}
    for b in bots or []:
        scopes[b] = {"kind": "bot", "description": f"Grok bot {b}."}
    hist = [{"at": at, "version": "1.0.0", "action": "created (init example)", "by": owner}]
    pol = {"schema_version": 1, "kind": "policies", "version": "1.0.0", "updated": day, "owner": owner,
           "scopes": scopes, "policies": [{
               "id": "EXAMPLE-POL-001", "example": True,
               "title": "EXAMPLE (not a real policy) - no external sends without approval",
               "rule": "EXAMPLE ONLY, never loaded. Do not send messages to anyone outside the owner's "
                       "own accounts and bots unless the owner approved that specific message.",
               "polarity": "prohibitive", "applies_to": ["comms"],
               "rationale": "Illustrates the policy format.", "status": "active", "set_by": owner,
               "added_in": "1.0.0", "pending_revisions": [], "history": copy.deepcopy(hist)}]}
    pref = {"schema_version": 1, "kind": "preferences", "version": "1.0.0", "updated": day, "owner": owner,
            "preferences": [{
                "id": "EXAMPLE-PREF-001", "example": True,
                "title": "EXAMPLE (not a real preference) - lead with the answer",
                "rule": "EXAMPLE ONLY, never loaded. Put the outcome in the first sentence of a status "
                        "report, then the details.",
                "applies_to": ["all"], "status": "active",
                "source": {"kind": "learned", "by": owner, "at": at,
                           "evidence": "Illustrates the format of a learned preference."},
                "reviewed": None, "added_in": "1.0.0", "pending_revisions": [],
                "history": copy.deepcopy(hist)}]}
    home.mkdir(parents=True, exist_ok=True)
    (home / "decisions").mkdir(exist_ok=True)
    (home / "decisions" / ".gitkeep").write_text("", encoding="utf-8")
    fmt = {"name": name, "owner": owner}
    (home / "policies.yaml").write_text(dump_yaml(pol, POL_HEADER.format(**fmt)), encoding="utf-8")
    (home / "preferences.yaml").write_text(dump_yaml(pref, PREF_HEADER.format(**fmt)), encoding="utf-8")
    (home / "CHANGELOG.md").write_text(
        "# Changelog: Grok Bot policies & preferences\n\nNewest first. Written by `pp`; do not "
        f"hand-edit past entries. Cite as `policies vX.Y.Z, preferences vX.Y.Z ({repo}@<sha>)`.\n\n"
        f"## {day}: policies 1.0.0 · preferences 1.0.0\n\n_by {owner} via pp init_\n\n"
        "- policies: scope registry created (all; roles coding, review, comms, research"
        + (f"; bots {', '.join(bots)}" if bots else "") + "); EXAMPLE-POL-001 format example (never loaded).\n"
        "- preferences: EXAMPLE-PREF-001 format example (never loaded).\n\n", encoding="utf-8")
    (home / "README.md").write_text(
        f"# {home_dir}/\n\nThe single, versioned source of {name}'s Grok Bot **policies** (constitutional, "
        f"set by {name} alone) and **preferences** (how bots do their assigned work). Managed with "
        "`skills/policies-preferences/bin/pp`; don't hand-edit. Bots change it only through `pp/*` "
        f"pull requests; {name}'s merge is the approval.\n", encoding="utf-8")
    out.append(f"wrote {home_dir}/ (policies.yaml, preferences.yaml, CHANGELOG.md, README.md, decisions/)")
    cfgd = {"source_repo": repo, "home": home_dir, "owner": owner, "owner_name": name}
    (target / CONFIG_FILE).write_text(
        "# policies-preferences instance config (read by `pp`; env vars PP_* override it).\n"
        + yaml.safe_dump(cfgd, sort_keys=False), encoding="utf-8")
    out.append(f"wrote {CONFIG_FILE}")
    if ci:
        wf = target / ".github" / "workflows" / "preferences-validate.yml"
        if wf.exists() and not force:
            out.append(f"kept existing {wf.relative_to(target)}")
        else:
            wf.parent.mkdir(parents=True, exist_ok=True)
            tpl = (TOOLKIT / "templates" / "preferences-validate.yml").read_text(encoding="utf-8")
            wf.write_text(tpl.replace("__HOME__", home_dir).replace("__TOOLKIT__", "skills/policies-preferences"),
                          encoding="utf-8")
            out.append(f"wrote {wf.relative_to(target)}")
    errs = validate(Store(home, resolve_config(source_dir=target)))
    if errs:
        raise PPError("init produced an invalid set (bug):\n  " + "\n  ".join(errs))
    out.append(f"OK: valid instance for owner {owner!r}. Next: commit on a branch, open a PR, merge it yourself.")
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pp", description=__doc__.split("\n")[0])
    ap.add_argument("--home", help="preferences directory (default: $PP_HOME, config `home`, or <source>/preferences)")
    ap.add_argument("--source-dir", help="instance repo clone (default: the repo this toolkit lives in, else $PP_SOURCE_DIR / $PP_TOOLS_DIR, else cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load", help="print the policies + preferences that apply to a bot")
    s.add_argument("--bot", help="this Grok bot's slug (default: $PP_BOT / config `bot`)")
    s.add_argument("--role", action="append", default=[], help="role scope (repeatable): coding, comms, ...")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--include-examples", action="store_true")

    sub.add_parser("cite", help="print the version citation string")
    sub.add_parser("validate", help="schema + semantic validation")
    sub.add_parser("scopes", help="list known scope names")

    s = sub.add_parser("propose", help="propose a new preference (status: proposed)")
    s.add_argument("--by"); s.add_argument("--title", required=True)
    s.add_argument("--rule", required=True); s.add_argument("--evidence", required=True)
    s.add_argument("--applies-to", action="append", default=[])
    s.add_argument("--supersedes", action="append", default=[])
    s.add_argument("--kind", choices=["learned", "stated"], default="learned",
                   help="stated = the owner said it outright; learned = inferred from their behavior/corrections")

    s = sub.add_parser("feedback", help="record the owner's feedback on any entry (creates a pending revision)")
    s.add_argument("--id", required=True); s.add_argument("--note", required=True)
    s.add_argument("--by"); s.add_argument("--from", dest="frm", default=None)
    s.add_argument("--proposed-rule", help="draft replacement text (preferences only, unless --by is the owner)")

    s = sub.add_parser("revise", help="attach draft text to a pending preference revision")
    s.add_argument("--id", required=True); s.add_argument("--rev", required=True)
    s.add_argument("--rule", required=True); s.add_argument("--by")

    s = sub.add_parser("render-review", help="render the review document for the owner")
    s.add_argument("--format", choices=["markdown", "json"], default="markdown")
    s.add_argument("--out"); s.add_argument("--decisions-template")

    s = sub.add_parser("apply-decisions", help="apply the owner's reviewed decisions file")
    s.add_argument("file"); s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("check-diff", help="verify a bot change only proposes / adds feedback")
    s.add_argument("--base", default="origin/main")

    s = sub.add_parser("config", help="print the resolved configuration")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--get", help="print one key only (source_dir, source_repo, home, owner, owner_name, bot)")

    s = sub.add_parser("init", help="scaffold a new, empty instance in a repo (for a new owner)")
    s.add_argument("--owner", required=True, help="owner's identifier, lowercase (who alone sets policy)")
    s.add_argument("--owner-name", help="owner's display name (default: --owner)")
    s.add_argument("--repo", help="source repo owner/name (default: from git remote origin)")
    s.add_argument("--home-dir", default="preferences", help="preferences directory inside the repo")
    s.add_argument("--bot", action="append", default=[], help="register a bot slug (repeatable)")
    s.add_argument("--no-ci", action="store_true", help="don't write the GitHub Actions workflow")
    s.add_argument("--force", action="store_true", help="overwrite an existing instance")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "init":
            target = Path(a.source_dir) if a.source_dir else (_git_toplevel(Path.cwd()) or Path.cwd())
            for line in init_instance(target, a.owner, a.owner_name, a.repo, a.home_dir, a.bot,
                                      ci=not a.no_ci, force=a.force):
                print(line)
            return 0
        cfg = resolve_config(home=a.home, source_dir=a.source_dir)
        if a.cmd == "config":
            d = cfg.as_dict()
            try:
                st = Store(cfg=cfg)
                d["owner"], d["owner_name"] = st.owner, st.owner_name
            except PPError:
                pass
            if a.get:
                print(d.get(a.get) or "")
            elif a.format == "json":
                print(json.dumps(d, indent=2))
            else:
                for k, v in d.items():
                    print(f"{k}\t{v}")
            return 0
        store = Store(cfg=cfg)
        for attr in ("by", "bot"):
            if hasattr(a, attr) and not getattr(a, attr):
                setattr(a, attr, cfg.bot)
        if a.cmd in ("propose", "feedback", "revise") and not a.by:
            raise PPError("--by <slug> required (or set PP_BOT)")
        if a.cmd == "load":
            sys.stdout.write(render_load(store, a.bot, a.role, a.format, a.include_examples))
        elif a.cmd == "cite":
            print(store.citation())
        elif a.cmd == "scopes":
            for n, m in sorted(store.scopes.items(), key=lambda kv: (SCOPE_RANK[kv[1]["kind"]], kv[0])):
                print(f"{n}\t{m['kind']}\t{m.get('description', '')}")
        elif a.cmd == "validate":
            errs = validate(store)
            if errs:
                print("INVALID\n  " + "\n  ".join(errs), file=sys.stderr)
                return 1
            print(f"OK {store.citation()}" + ("" if jsonschema else " (jsonschema not installed; built-in checks only)"))
        elif a.cmd == "propose":
            pid = propose(store, a.by, a.title, a.rule, a.applies_to or ["all"], a.evidence, a.supersedes, a.kind)
            print(f"{pid} proposed (status: proposed; inert until {store.owner_name} approves). Now {store.citation()}")
        elif a.cmd == "feedback":
            rid = feedback(store, a.id, a.note, a.by, a.proposed_rule, a.frm)
            print(f"{a.id} {rid} pending review. Now {store.citation()}")
        elif a.cmd == "revise":
            revise(store, a.id, a.rev, a.rule, a.by)
            print(f"{a.id} {a.rev} draft recorded. Now {store.citation()}")
        elif a.cmd == "render-review":
            text, tmpl = render_review(store, a.format)
            if a.out:
                Path(a.out).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            if a.decisions_template:
                Path(a.decisions_template).write_text(
                    yaml.dump(tmpl, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
        elif a.cmd == "apply-decisions":
            with open(a.file, encoding="utf-8") as fh:
                d = yaml.safe_load(fh)
            lines = apply_decisions(store, d, source=os.path.basename(a.file), dry_run=a.dry_run)
            print(("DRY RUN, nothing written.\n" if a.dry_run else "")
                  + ("\n".join(f"- {l}" for l in lines) or "no decisions to apply")
                  + f"\n{'Would be' if a.dry_run else 'Now'}: policies v{store.pol['version']}, "
                  f"preferences v{store.pref['version']}")
        elif a.cmd == "check-diff":
            errs = check_diff(store.root, a.base, cfg)
            if errs:
                print("NOT BOT-SAFE\n  " + "\n  ".join(errs), file=sys.stderr)
                return 1
            print("OK: change only proposes preferences and/or records feedback")
    except PPError as exc:
        print(f"pp: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
