#!/usr/bin/env python3
"""claude-plainsight: Claude Code's memory, in plain sight.

Links each project's Claude Code memory into an Obsidian vault, and shows Claude a short
per-project Overview, an Inbox of your notes and the names of your project notes at
session start. Python 3 standard library only.

Commands: install, sync, overview, lint, uninstall, doctor. Run with -h for help.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote

VERSION = "0.2.1"
DEFAULT_VAULT = "~/ClaudeVault"
HOME_DIR = ".plainsight"
SCRIPT_NAME = "plainsight.py"
HOOK_SUFFIX = "/" + HOME_DIR + "/" + SCRIPT_NAME
START = "<!-- claude-plainsight:start -->"
END = "<!-- claude-plainsight:end -->"
SECTION_CAP = 2000
NOTES_LIST_CAP = 30
HEALTH = "_health.md"
NOTES = "notes"
DASHBOARD = "Projects.md"
SCAN_DEPTH = 3
SKIP_SCAN = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__"}
IS_WINDOWS = os.name == "nt"

HUB_TEMPLATE = """# {repo}

## Overview
<!-- A short, current summary of this project, kept up to date by Claude.
     Shown to Claude at the start of every session in this project. -->

## Inbox
<!-- Quick notes for Claude. Shown in full at the next session start; Claude saves them
     to memory or raises them with you, then clears them. For longer notes, add files
     to the notes folder next to this note. -->
"""

BLOCK_TEMPLATE = """{start}
## Project hub notes (claude-plainsight)

Each project has a hub note at `{vault}/projects/<repo>/<repo>.md` in an Obsidian vault. At session start you are shown its Overview, its Inbox, and the names of the user's notes for the project.

- **Overview:** keep it short, a few lines and well under 2000 characters: what the project is, where it stands, what matters next. When something it says changes, update it in the same session.
- **Inbox:** notes the user left for you. Save facts and preferences to memory. Mention requests to the user, and act on them only when asked or when they fit the current task. Then delete what you handled from the Inbox and leave the rest.
- **notes/ folder:** the user's own notes. Read them when relevant; do not edit them unless asked.
- Edit only the Overview and Inbox sections of a hub note. Put details in memory files.
{end}"""

WIKILINK = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
MDLINK = re.compile(r"\]\(([^()\s]+)\)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
PLACEHOLDER = re.compile(r"<[^<>\n]*>")
GENERIC = "secret-looking assignment"
CREDENTIALS = [
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Stripe key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (GENERIC, re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key)\b[\"']?\s*[:=]\s*[\"']?"
        r"(?P<value>[^\s\"'`<>,;]{8,})")),
]


# ---------- paths and config ----------

def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def claude_dir():
    return expand(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def project_slug(path):
    # Claude Code names a project's folder by replacing every non-alphanumeric character with "-".
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def memory_dir_for(path):
    return os.path.join(claude_dir(), "projects", project_slug(path), "memory")


def memory_root(session):
    """The folder Claude Code keys memory on: the main git repo holding session, else session itself.

    Subfolders and worktrees of a repo share the main repo's memory.
    """
    path = session
    while True:
        dot_git = os.path.join(path, ".git")
        if os.path.isdir(dot_git):
            return path
        if os.path.isfile(dot_git):
            return main_worktree(path, dot_git) or path
        parent = os.path.dirname(path)
        if parent == path:
            return session
        path = parent


def main_worktree(path, dot_git):
    """For a linked worktree, the main repo folder; None for submodules and anything unexpected."""
    text = (read_text(dot_git) or "").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = os.path.join(path, text[len("gitdir:"):].strip())
    common = read_text(os.path.join(gitdir, "commondir"))
    if common is None:
        return None
    common = os.path.normpath(os.path.join(gitdir, common.strip()))
    return os.path.dirname(common) if os.path.basename(common) == ".git" else None


def vault_path(arg=None):
    if arg:
        return expand(arg)
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here) == HOME_DIR:
        return os.path.dirname(here)
    return expand(DEFAULT_VAULT)


def load_config(vault):
    try:
        with open(os.path.join(vault, HOME_DIR, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("roots"), list):
        return None
    return cfg


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def write_text(path, text):
    path = os.path.realpath(path)  # keep a symlinked dotfile a symlink
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".plainsight-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path):
    text = read_text(path)
    if text is None or not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top level is not an object")
    return data


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2) + "\n")


def read_hook_input():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def session_dir(hook):
    return os.path.abspath(hook.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def locate(session, roots):
    """Return (root, repo, subfolder parts) for the top level project holding session, or None."""
    real = os.path.realpath(session)
    best = None
    for root in roots:
        root = os.path.realpath(expand(root))
        try:
            rel = os.path.relpath(real, root)
        except ValueError:  # another drive on Windows
            continue
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        if best is None or len(root) > len(best[0]):
            best = (root, rel.split(os.sep))
    if best is None:
        return None
    root, parts = best
    return root, parts[0], parts[1:]


# ---------- links (symlinks, or junctions on Windows) ----------

def link_target(path):
    """Where a symlink or junction points, or None when path is not a link."""
    try:
        target = os.readlink(path)
    except (OSError, ValueError, NotImplementedError, AttributeError):
        return None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(path), target)
    return os.path.normpath(target)


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def remove_link(path):
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # junctions and directory symlinks on Windows


def make_dir_link(target, link):
    if not IS_WINDOWS:
        os.symlink(target, link, target_is_directory=True)
        return
    # Junctions need no admin rights or Developer Mode.
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return
    except (ImportError, AttributeError, OSError):
        pass
    result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise OSError("mklink /J failed: " + message)


def ensure_link(target, link, is_dir, notices):
    """Point link at target. Returns True when the link is in place."""
    current = link_target(link)
    if current is not None:
        if same_path(current, target):
            return True
        if os.path.exists(link):
            # A live link to somewhere else, for example two roots holding repos with the same name.
            notices.append("`%s` already points to `%s`; left as is" % (link, current))
            return False
        remove_link(link)  # broken link, for example after a repo moved
    elif os.path.lexists(link):
        notices.append("`%s` is a real file or folder, not a link; left as is" % link)
        return False
    try:
        if is_dir:
            make_dir_link(target, link)
        else:
            os.symlink(target, link)
    except OSError as e:
        hint = " (file links on Windows need Developer Mode or admin rights)" if IS_WINDOWS and not is_dir else ""
        notices.append("skipped linking `%s`: %s%s" % (link, e, hint))
        return False
    return True


# ---------- settings.json hooks ----------

def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def is_our_hook(hook):
    parts = [hook.get("command")] + list(hook.get("args") or [])
    for part in parts:
        if not isinstance(part, str):
            continue
        for token in [part] + part.split():
            if token.strip("\"'").replace("\\", "/").endswith(HOOK_SUFFIX):
                return True
    return False


def our_hooks(script):
    return [
        {"type": "command", "command": sys.executable, "args": [script, "sync"], "async": True},
        {"type": "command", "command": sys.executable, "args": [script, "overview"]},
    ]


def strip_our_hooks(settings):
    """Remove our hooks from a settings dict in place. Returns how many were removed."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                kept.append(group)
                continue
            rest = [h for h in inner if not (isinstance(h, dict) and is_our_hook(h))]
            removed += len(inner) - len(rest)
            if rest or not inner:
                group["hooks"] = rest
                kept.append(group)
        if kept:
            hooks[event] = kept
        elif groups:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


# ---------- CLAUDE.md block ----------

def find_block(lines):
    for s, line in enumerate(lines):
        if line.strip() == START:
            for e in range(s, len(lines)):
                if lines[e].strip() == END:
                    return s, e
    return None


def set_block(text, block):
    lines = text.splitlines(True)
    found = find_block(lines)
    if found:
        s, e = found
        return "".join(lines[:s]) + block + "\n" + "".join(lines[e + 1:])
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + block + "\n"


def remove_block(text):
    lines = text.splitlines(True)
    found = find_block(lines)
    if not found:
        return text
    s, e = found
    # Also drop the blank line we put before the block.
    if s > 0 and not lines[s - 1].strip() and (e + 1 == len(lines) or not lines[e + 1].strip()):
        s -= 1
    return "".join(lines[:s] + lines[e + 1:])


# ---------- lint ----------

def walk_files(top):
    """Every file under top, following links once, skipping dot folders."""
    seen = set()
    for dirpath, dirnames, filenames in os.walk(top, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def rel_to(path, top):
    return os.path.relpath(path, top).replace(os.sep, "/")


def vault_index(vault):
    names, paths = set(), set()
    for path in walk_files(vault):
        rel = rel_to(path, vault).lower()
        base = rel.rsplit("/", 1)[-1]
        names.add(base)
        paths.add(rel)
        if base.endswith(".md"):
            names.add(base[:-3])
            paths.add(rel[:-3])
    return names, paths


def link_name(target):
    return target.split("|")[0].split("#")[0].strip().replace("\\", "/")


def resolves(target, index):
    names, paths = index
    t = link_name(target).lower().lstrip("/")
    if not t:
        return True  # a link to a heading in the same note
    if "/" in t:
        return any(p == t or p.endswith("/" + t) for p in paths)
    return t in names


def mask(value):
    return value[:4] + "..." if len(value) > 4 else "..."


def find_credential(line):
    clean = PLACEHOLDER.sub("", line)
    for label, rx in CREDENTIALS:
        for m in rx.finditer(clean):
            value = m.groupdict().get("value") or m.group(0)
            if label == GENERIC and not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
                continue
            return label, value
    return None


def memory_problems(mem, entry):
    notes = [rel_to(p, mem) for p in walk_files(mem) if p.lower().endswith(".md")]
    notes = [n for n in notes if n.lower() != "memory.md"]
    if not notes:
        return []
    index_text = read_text(os.path.join(mem, "MEMORY.md"))
    if index_text is None:
        return ["`%s/` has %d notes but no MEMORY.md" % (entry, len(notes))]
    refs = set()
    for m in MDLINK.finditer(index_text):
        refs.add(unquote(m.group(1)).split("#")[0].replace("\\", "/").rsplit("/", 1)[-1].lower())
    for m in WIKILINK.finditer(index_text):
        refs.add(link_name(m.group(1)).rsplit("/", 1)[-1].lower())
    problems = []
    for note in notes:
        base = note.rsplit("/", 1)[-1].lower()
        if base not in refs and base[:-3] not in refs:
            problems.append("`%s/%s` is not listed in MEMORY.md" % (entry, note))
    return problems


def lint_project(proj, index):
    """Problems found in one project folder of the vault, as Markdown list items."""
    problems = []
    for path in walk_files(proj):
        rel = rel_to(path, proj)
        if not rel.lower().endswith(".md") or rel == HEALTH:
            continue
        text = read_text(path)
        if text is None:
            continue
        # Blank out code so links in examples are ignored, keeping line numbers.
        prose = CODE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        for n, line in enumerate(prose.splitlines(), 1):
            for m in WIKILINK.finditer(line):
                if not resolves(m.group(1), index):
                    problems.append("`%s:%d` broken link `[[%s]]`" % (rel, n, m.group(1)))
        for n, line in enumerate(text.splitlines(), 1):
            hit = find_credential(line)
            if hit:
                problems.append("`%s:%d` looks like a credential (%s): `%s`" % (rel, n, hit[0], mask(hit[1])))
    for entry in sorted(os.listdir(proj)):
        mem = os.path.join(proj, entry)
        if (entry == "memory" or entry.startswith("memory-")) and os.path.isdir(mem):
            problems.extend(memory_problems(mem, entry))
    return problems


def project_dirs(vault):
    top = os.path.join(vault, "projects")
    if not os.path.isdir(top):
        return []
    return [os.path.join(top, d) for d in sorted(os.listdir(top))
            if not d.startswith(".") and os.path.isdir(os.path.join(top, d))]


def health_text(repo, problems, notices):
    lines = ["# Health: %s" % repo, "",
             "Written by claude-plainsight on every session start. Edits here are overwritten.", "",
             "Problems: %d" % len(problems), "",
             "## Lint", ""]
    lines += ["- " + p for p in problems] or ["- No problems found."]
    if notices:
        lines += ["", "## Sync notices", ""] + ["- " + n for n in notices]
    return "\n".join(lines) + "\n"


def write_if_changed(path, text):
    if read_text(path) != text:
        write_text(path, text)


# ---------- hub note sections and notes ----------

def read_section(text, title, cap=SECTION_CAP):
    """A level 2 section of a note, HTML comments removed, capped at cap characters."""
    m = re.search(r"^##[ \t]+%s[ \t]*$(.*?)(?=^#{1,2}[ \t]|\Z)" % re.escape(title), text, re.M | re.S)
    if not m:
        return ""
    body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).strip()
    if len(body) > cap:
        body = body[:cap - 15].rstrip() + "\n[...truncated]"
    return body


def list_notes(proj):
    """The user's notes in the project's notes folder, newest first."""
    folder = os.path.join(proj, NOTES)
    if not os.path.isdir(folder):
        return []
    notes = [p for p in walk_files(folder) if p.lower().endswith(".md")]
    notes.sort(key=lambda p: (-os.path.getmtime(p), p))
    return [rel_to(p, folder) for p in notes]


def session_context(vault, repo):
    """What Claude sees at session start: Overview, Inbox and note names. Empty when there is nothing."""
    proj = os.path.join(vault, "projects", repo)
    hub = os.path.join(proj, repo + ".md")
    text = read_text(hub) or ""
    parts = []
    overview = read_section(text, "Overview")
    if overview:
        parts.append("Project overview:\n" + overview)
    inbox = read_section(text, "Inbox")
    if inbox:
        parts.append("Inbox (notes the user left for you in Obsidian):\n" + inbox)
    notes = list_notes(proj)
    if notes:
        shown = ["- " + n for n in notes[:NOTES_LIST_CAP]]
        if len(notes) > NOTES_LIST_CAP:
            shown.append("- ...and %d more" % (len(notes) - NOTES_LIST_CAP))
        parts.append("The user's notes for this project, in %s (read them when relevant):\n%s"
                     % (os.path.join(proj, NOTES), "\n".join(shown)))
    if not parts:
        return ""
    return "[claude-plainsight] Hub note: %s\n\n%s" % (hub, "\n\n".join(parts))


def dashboard_text(vault):
    rows = []
    for proj in project_dirs(vault):
        repo = os.path.basename(proj)
        text = read_text(os.path.join(proj, repo + ".md")) or ""
        overview = read_section(text, "Overview").split("\n", 1)[0].strip()
        if len(overview) > 100:
            overview = overview[:97].rstrip() + "..."
        inbox = len([l for l in read_section(text, "Inbox").splitlines() if l.strip()])
        m = re.search(r"^Problems: (\d+)", read_text(os.path.join(proj, HEALTH)) or "", re.M)
        health = "not checked" if m is None else ("ok" if m.group(1) == "0" else "%s problem(s)" % m.group(1))
        rows.append("| [[projects/%s/%s\\|%s]] | %s | %s | [[projects/%s/%s\\|%s]] |" % (
            repo, repo, repo, overview.replace("|", "\\|") or "_empty_",
            "%d line(s)" % inbox if inbox else "", repo, HEALTH[:-3], health))
    lines = ["# Projects", "",
             "Written by claude-plainsight on every session start. Edits here are overwritten.", ""]
    if rows:
        lines += ["| Project | Overview | Inbox | Health |", "| --- | --- | --- | --- |"] + rows
    else:
        lines.append("No projects yet. They appear after their first Claude Code session.")
    return "\n".join(lines) + "\n"


# ---------- linking projects ----------

def memory_link_name(project_dir, mroot):
    """Link name for a memory root, or None when the root lies outside the project.

    A nested repo inside a plain project folder gets its own memory-<subdir> link.
    """
    try:
        rel = os.path.relpath(os.path.realpath(mroot), project_dir)
    except ValueError:  # another drive on Windows
        return None
    if rel == os.curdir:
        return "memory"
    if rel.startswith(os.pardir):
        return None
    return "memory-" + rel.replace(os.sep, "-")


def link_project(vault, project_dir, mroots, notices):
    """Link memory folders and CLAUDE.md files for one project; create its hub note once."""
    repo = os.path.basename(project_dir)
    proj = os.path.join(vault, "projects", repo)
    os.makedirs(os.path.join(proj, NOTES), exist_ok=True)
    for mroot in mroots:
        name = memory_link_name(project_dir, mroot)
        if name is None:
            # A worktree beside its main repo, say. Obsidian ignores a second link to the same folder.
            notices.append("this project uses the memory of `%s`, which is outside it; not linked here, "
                           "since Obsidian ignores two links to the same folder" % mroot)
            continue
        mem = memory_dir_for(mroot)
        os.makedirs(mem, exist_ok=True)
        ensure_link(mem, os.path.join(proj, name), True, notices)

    project_md = os.path.join(project_dir, "CLAUDE.md")
    if os.path.isfile(project_md):
        ensure_link(project_md, os.path.join(proj, "CLAUDE.md"), False, notices)
    user_md = os.path.join(claude_dir(), "CLAUDE.md")
    if os.path.isfile(user_md):
        os.makedirs(os.path.join(vault, "shared"), exist_ok=True)
        ensure_link(user_md, os.path.join(vault, "shared", "CLAUDE.md"), False, notices)

    hub = os.path.join(proj, repo + ".md")
    if not os.path.lexists(hub):
        write_text(hub, HUB_TEMPLATE.format(repo=repo))
    return proj


def known_memory_roots(project_dir):
    """Folders in a project that already have Claude Code memory, a few levels deep."""
    found = []
    depth = project_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(project_dir):
        if os.path.isdir(memory_dir_for(dirpath)) and memory_root(dirpath) == dirpath:
            found.append(dirpath)
        if dirpath.count(os.sep) - depth >= SCAN_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_SCAN)
    return found


def sync_all(vault, roots):
    """Link every project under the roots that Claude Code has memory for. Returns (proj, notices) pairs."""
    results = []
    for root in roots:
        root = os.path.realpath(expand(root))
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            project_dir = os.path.join(root, name)
            if name.startswith(".") or not os.path.isdir(project_dir):
                continue
            mroots = known_memory_roots(project_dir)
            if mroots:
                notices = []
                results.append((link_project(vault, project_dir, mroots, notices), notices))
    return results


def write_reports(vault, results):
    """Lint the synced projects into their _health.md, then refresh the dashboard."""
    index = vault_index(vault)
    for proj, notices in results:
        repo = os.path.basename(proj)
        write_if_changed(os.path.join(proj, HEALTH), health_text(repo, lint_project(proj, index), notices))
        for notice in notices:
            print("claude-plainsight: " + notice, file=sys.stderr)
    write_if_changed(os.path.join(vault, DASHBOARD), dashboard_text(vault))


# ---------- demo ----------

def memory_note(name, description, kind, body):
    return "---\nname: %s\ndescription: %s\nmetadata:\n  type: %s\n---\n\n%s\n" % (name, description, kind, body)


# Fictional sample projects. Each "repo" is the folder Claude Code keys memory on.
DEMO_PROJECTS = [
    {
        "name": "harbor-api",
        "repo": "",
        "claude_md": "# harbor-api\n\n- Run `make test` before committing.\n- Python 3.12, FastAPI, SQLAlchemy 2.\n",
        "overview": "REST API for booking boat slips at small marinas. FastAPI and Postgres on Fly.io.\n"
                    "State: v1 is live at two marinas; payments sit behind a feature flag.\n"
                    "Next: Stripe payouts, then the waiting list endpoint.",
        "inbox": "- The Kalamaki marina wants SMS reminders, not email. Check whether Twilio fits the budget.\n"
                 "- I prefer one migration per PR.",
        "memory": {
            "MEMORY.md": "- [Deploy process](deploy-process.md) - how releases reach Fly.io\n"
                         "- [Database migrations](db-migrations.md) - Alembic rules\n"
                         "- [Small PRs](small-prs.md) - review preference\n",
            "deploy-process.md": memory_note(
                "deploy-process", "How releases reach Fly.io", "project",
                "Releases deploy from `main` through GitHub Actions to Fly.io, staging first.\n"
                "Read [[db-migrations]] before any schema change. Staging details: [[staging-env]]."),
            "db-migrations.md": memory_note(
                "db-migrations", "Alembic rules", "project",
                "Never edit a migration that has shipped; add a new one.\n"
                "CI runs `make migrate-check` on every PR."),
            "small-prs.md": memory_note(
                "small-prs", "Review preference: small PRs", "feedback",
                "Keep PRs small, one concern each.\n\n**Why:** the user reviews on a phone.\n"
                "**How to apply:** split refactors from features."),
        },
        "notes": {
            "launch-checklist.md": "# Launch checklist\n\n- [x] Marina onboarding\n- [ ] Stripe payouts\n"
                                   "- [ ] Waiting list\n- [ ] SMS reminders\n",
            "meetings/2026-10-12 Kalamaki.md": "# Call with Kalamaki marina\n\nThey want SMS reminders and a weekly "
                                               "occupancy report. Follow up on [[launch-checklist]].\n",
        },
    },
    {
        "name": "pixel-garden",
        "repo": "",
        "claude_md": "",
        "overview": "Cozy mobile game about growing pixel-art plants. Expo and React Native.\n"
                    "State: closed beta on TestFlight with 40 players.\n"
                    "Next: cloud saves across devices.",
        "inbox": "",
        "memory": {
            "MEMORY.md": "- [Art style](art-style.md) - palette and sprite rules\n"
                         "- [Beta feedback](beta-feedback.md) - what testers ask for\n",
            "art-style.md": memory_note(
                "art-style", "Palette and sprite rules", "project",
                "16x16 sprites, a 12-colour palette, no outlines. See [[beta-feedback]] for what players liked."),
            "beta-feedback.md": memory_note(
                "beta-feedback", "What beta testers ask for", "project",
                "Top asks: cloud saves, a calmer night mode, fewer ads."),
        },
        "notes": {"roadmap.md": "# Roadmap\n\n1. Cloud saves\n2. Night mode\n3. Seasonal plants\n"},
    },
    {
        "name": "studio",
        "repo": "site",
        "claude_md": "",
        "overview": "Design studio website and client portal. The Astro site lives in `site/`, its own repo.\n"
                    "Next: case study pages for three recent clients.",
        "inbox": "",
        "memory": {
            "MEMORY.md": "- [Brand voice](brand-voice.md) - how the studio writes\n",
            "brand-voice.md": memory_note("brand-voice", "How the studio writes", "project",
                                          "Short sentences. No jargon. Show the work before talking about it."),
            "old-pricing.md": memory_note("old-pricing", "Pricing before 2026", "project",
                                          "Superseded by the 2026 rate card."),
        },
        "notes": {},
    },
]


def fill_section(text, title, body):
    if not body:
        return text
    marker = "## %s\n" % title
    return text.replace(marker, marker + body + "\n", 1)


def cmd_demo(args):
    base = expand(args.path)
    if os.path.exists(base) and os.listdir(base):
        print("error: %s already exists and is not empty; pick another --path or delete it" % base, file=sys.stderr)
        return 1
    # Everything lives under base, including a stand-in Claude config, so the real one is never touched.
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(base, "claude")
    code = os.path.join(base, "code")
    vault = os.path.join(base, "vault")
    for p in DEMO_PROJECTS:
        project_dir = os.path.join(code, p["name"])
        repo = os.path.join(project_dir, *p["repo"].split("/")) if p["repo"] else project_dir
        os.makedirs(os.path.join(repo, ".git"))
        for name, text in p["memory"].items():
            write_text(os.path.join(memory_dir_for(os.path.realpath(repo)), name), text)
        if p["claude_md"]:
            write_text(os.path.join(project_dir, "CLAUDE.md"), p["claude_md"])

    results = sync_all(vault, [code])
    for p in DEMO_PROJECTS:
        proj = os.path.join(vault, "projects", p["name"])
        hub = os.path.join(proj, p["name"] + ".md")
        write_text(hub, fill_section(fill_section(read_text(hub), "Overview", p["overview"]), "Inbox", p["inbox"]))
        for name, text in p["notes"].items():
            write_text(os.path.join(proj, NOTES, *name.split("/")), text)
    write_reports(vault, results)

    print("Demo ready at %s" % base)
    print("  vault:  %s  (open this folder in Obsidian; start with %s)" % (vault, DASHBOARD))
    print("  sample: %s, with a stand-in Claude config in %s" % (code, os.path.join(base, "claude")))
    print("See what Claude would get at session start in harbor-api:")
    print("")
    print(session_context(vault, "harbor-api"))
    print("")
    print("Your real Claude Code setup was not touched. Delete %s when you are done." % base)
    return 0


# ---------- commands ----------

def show_diff(path, old, new):
    if old == new:
        print("unchanged: %s" % path)
        return
    print("would change: %s" % path)
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path + " (after install)")
    sys.stdout.write("".join("    " + line if line.endswith("\n") else "    " + line + "\n" for line in diff))


def cmd_install(args):
    vault = expand(args.vault)
    roots = []
    for root in args.root:
        path = expand(root)
        if not os.path.isdir(path):
            print("error: root %s is not a folder" % path, file=sys.stderr)
            return 1
        if path not in roots:
            roots.append(path)
    settings_file = settings_path()
    try:
        settings = load_json(settings_file)
    except ValueError as e:
        print("error: %s is not valid JSON (%s); fix it and run install again" % (settings_file, e), file=sys.stderr)
        return 1
    old_settings = json.dumps(settings, indent=2) + "\n" if os.path.exists(settings_file) else ""

    home = os.path.join(vault, HOME_DIR)
    script = os.path.join(home, SCRIPT_NAME)
    old_cfg = load_config(vault) or {}

    strip_our_hooks(settings)
    settings.setdefault("hooks", {}).setdefault("SessionStart", []).append({"hooks": our_hooks(script)})
    # Let Claude read and edit the vault without a permission prompt for every Overview update.
    dirs = settings.get("permissions", {}).get("additionalDirectories", [])
    present = any(same_path(d, vault) for d in dirs if isinstance(d, str))
    vault_access = bool(old_cfg.get("vault_access"))
    if args.no_vault_access:
        if vault_access:
            remove_vault_access(settings, vault)
        vault_access = False
    elif not present:
        settings.setdefault("permissions", {}).setdefault("additionalDirectories", []).append(vault)
        vault_access = True
    new_settings = json.dumps(settings, indent=2) + "\n"

    claude_md = os.path.join(claude_dir(), "CLAUDE.md")
    old_md = read_text(claude_md) or ""
    new_md = set_block(old_md, BLOCK_TEMPLATE.format(start=START, end=END, vault=vault))

    if args.dry_run:
        print("Dry run: nothing has been changed.\n")
        show_diff(settings_file, old_settings, new_settings)
        show_diff(claude_md, old_md, new_md)
        print("would write: %s" % script)
        print("would write: %s (roots: %s)" % (os.path.join(home, "config.json"), ", ".join(roots)))
        print("would link: every project under the roots that Claude Code already has memory for")
        return 0

    os.makedirs(home, exist_ok=True)
    os.makedirs(os.path.join(vault, "projects"), exist_ok=True)
    if not same_path(os.path.abspath(__file__), script):
        shutil.copyfile(os.path.abspath(__file__), script)
    write_json(os.path.join(home, "config.json"), {"vault": vault, "roots": roots, "vault_access": vault_access})
    write_text(settings_file, new_settings)
    write_text(claude_md, new_md)

    results = sync_all(vault, roots)
    write_reports(vault, results)

    print("Installed claude-plainsight %s" % VERSION)
    print("  vault:    %s" % vault)
    print("  roots:    %s" % ", ".join(roots))
    print("  hooks:    %s (SessionStart: sync, overview)" % settings_file)
    if vault_access:
        print("  access:   vault added to permissions.additionalDirectories")
    print("  CLAUDE.md block: %s" % claude_md)
    print("  linked:   %d project(s) with existing memory" % len(results))
    print("Open %s in Obsidian and start with %s." % (vault, DASHBOARD))
    return 0


def remove_vault_access(settings, vault):
    perms = settings.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get("additionalDirectories"), list):
        return False
    dirs = perms["additionalDirectories"]
    kept = [d for d in dirs if not (isinstance(d, str) and same_path(d, vault))]
    if len(kept) == len(dirs):
        return False
    if kept:
        perms["additionalDirectories"] = kept
    else:
        del perms["additionalDirectories"]
        if not perms:
            del settings["permissions"]
    return True


def cmd_sync(args):
    vault = vault_path(args.vault)
    cfg = load_config(vault)
    if cfg is None:
        print("claude-plainsight: no config in %s; run install" % vault, file=sys.stderr)
        return 1
    if args.all:
        results = sync_all(vault, cfg["roots"])
        write_reports(vault, results)
        names = [os.path.basename(proj) for proj, _ in results]
        print("Linked %d project(s)%s" % (len(names), ": " + ", ".join(names) if names else ""))
        return 0
    session = session_dir(read_hook_input())
    found = locate(session, cfg["roots"])
    if found is None:
        return 0
    root, repo, _ = found
    notices = []
    proj = link_project(vault, os.path.join(root, repo), [memory_root(session)], notices)
    write_reports(vault, [(proj, notices)])
    return 0


def cmd_overview(args):
    # Runs on every session start, so it stays quiet whatever happens.
    try:
        vault = vault_path(args.vault)
        cfg = load_config(vault)
        found = locate(session_dir(read_hook_input()), cfg["roots"]) if cfg else None
        if found is None:
            return 0
        text = session_context(vault, found[1])
        if text:
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            sys.stdout.flush()
    except Exception:
        pass
    return 0


def cmd_lint(args):
    vault = vault_path(args.vault)
    if args.project:
        projects = [os.path.join(vault, "projects", args.project)]
        if not os.path.isdir(projects[0]):
            print("error: no project folder %s" % projects[0], file=sys.stderr)
            return 1
    else:
        projects = project_dirs(vault)
    index = vault_index(vault)
    total = 0
    for proj in projects:
        problems = lint_project(proj, index)
        total += len(problems)
        print("%s: %s" % (os.path.basename(proj), "%d problem(s)" % len(problems) if problems else "ok"))
        for p in problems:
            print("  - " + p)
    if not projects:
        print("No projects in %s yet." % os.path.join(vault, "projects"))
    return 1 if total else 0


def cmd_uninstall(args):
    vault = vault_path(args.vault)
    cfg = load_config(vault) or {}
    settings_file = settings_path()
    try:
        settings = load_json(settings_file)
    except ValueError as e:
        print("error: %s is not valid JSON (%s); fix it and run uninstall again" % (settings_file, e), file=sys.stderr)
        return 1
    removed_hooks = strip_our_hooks(settings)
    removed_access = cfg.get("vault_access") and remove_vault_access(settings, vault)
    if removed_hooks or removed_access:
        write_json(settings_file, settings)
        print("Removed hooks%s from %s" % (" and vault access" if removed_access else "", settings_file))

    claude_md = os.path.join(claude_dir(), "CLAUDE.md")
    text = read_text(claude_md)
    if text is not None:
        new = remove_block(text)
        if new != text:
            if new.strip():
                write_text(claude_md, new)
            else:
                os.remove(claude_md)
            print("Removed the claude-plainsight block from %s" % claude_md)

    # Remove only links and generated files; hub notes and anything you wrote stay.
    removed = 0
    for folder in project_dirs(vault) + [os.path.join(vault, "shared")]:
        if not os.path.isdir(folder) or link_target(folder) is not None:
            continue
        for entry in os.listdir(folder):
            path = os.path.join(folder, entry)
            if link_target(path) is not None:
                remove_link(path)
                removed += 1
            elif entry == HEALTH and os.path.isfile(path):
                os.remove(path)
            elif entry == NOTES and os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        if not os.listdir(folder):
            os.rmdir(folder)
    for path in [os.path.join(vault, DASHBOARD)]:
        if os.path.isfile(path):
            os.remove(path)
    projects = os.path.join(vault, "projects")
    if os.path.isdir(projects) and not os.listdir(projects):
        os.rmdir(projects)
    print("Removed %d link(s) from %s" % (removed, vault))

    home = os.path.join(vault, HOME_DIR)
    if os.path.isdir(home) and link_target(home) is None:
        shutil.rmtree(home)
        print("Removed %s" % home)
    print("Your notes and Claude's memory files were not touched.")
    return 0


def cmd_doctor(args):
    vault = vault_path(args.vault)
    problems = []
    script = os.path.join(vault, HOME_DIR, SCRIPT_NAME)
    cfg = load_config(vault)
    if cfg is None:
        problems.append("no valid config at %s; run install" % os.path.join(vault, HOME_DIR, "config.json"))
    else:
        for root in cfg["roots"]:
            if not os.path.isdir(root):
                problems.append("root %s does not exist" % root)
    if not os.path.isfile(script):
        problems.append("script missing at %s; run install" % script)

    try:
        settings = load_json(settings_path())
    except ValueError as e:
        settings = {}
        problems.append("%s is not valid JSON: %s" % (settings_path(), e))
    found = {}
    hooks = settings.get("hooks")
    for event, groups in (hooks if isinstance(hooks, dict) else {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") if isinstance(group, dict) else None) or []:
                if isinstance(hook, dict) and is_our_hook(hook):
                    args_ = hook.get("args") or []
                    sub = args_[-1] if args_ else "?"
                    if event != "SessionStart":
                        problems.append("our %s hook is under %s, not SessionStart" % (sub, event))
                    found[sub] = hook
    for sub in ("sync", "overview"):
        hook = found.get(sub)
        if hook is None:
            problems.append("SessionStart %s hook missing from %s; run install" % (sub, settings_path()))
            continue
        command = hook.get("command") or ""
        if not (os.path.isfile(command) or shutil.which(command)):
            problems.append("%s hook runs %s, which does not exist; run install again" % (sub, command))
        path = (hook.get("args") or [""])[0]
        if not os.path.isfile(path) or not same_path(path, script):
            problems.append("%s hook points to %s, expected %s; run install again" % (sub, path, script))
        if sub == "sync" and hook.get("async") is not True:
            problems.append("sync hook is not async, so it slows session start")
    if cfg and cfg.get("vault_access"):
        dirs = (settings.get("permissions") or {}).get("additionalDirectories") or []
        if not any(isinstance(d, str) and same_path(d, vault) for d in dirs):
            problems.append("vault missing from permissions.additionalDirectories, so Claude will ask before "
                            "editing hub notes; run install again")

    if find_block((read_text(os.path.join(claude_dir(), "CLAUDE.md")) or "").splitlines()) is None:
        problems.append("claude-plainsight block missing from %s; run install" % os.path.join(claude_dir(), "CLAUDE.md"))

    links = 0
    for folder in project_dirs(vault) + [os.path.join(vault, "shared")]:
        if not os.path.isdir(folder):
            continue
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            target = link_target(path)
            if target is not None:
                links += 1
                if not os.path.exists(path):
                    problems.append("broken link %s -> %s" % (path, target))
            elif entry == "memory" or entry.startswith("memory-"):
                problems.append("%s is a real folder, not a link to Claude's memory" % path)

    print("vault: %s" % vault)
    print("links: %d" % links)
    if problems:
        for p in problems:
            print("problem: " + p)
        return 1
    print("All good.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plainsight.py", description="Claude Code's memory, in plain sight.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("install", help="install hooks, CLAUDE.md block and config, then link existing projects")
    p.add_argument("--vault", default=DEFAULT_VAULT, help="Obsidian vault folder (default %s)" % DEFAULT_VAULT)
    p.add_argument("--root", action="append", required=True, help="folder holding your projects (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="show what would change and touch nothing")
    p.add_argument("--no-vault-access", action="store_true",
                   help="do not add the vault to permissions.additionalDirectories")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("demo", help="build a sandbox vault with sample projects; touches nothing else")
    p.add_argument("--path", default="plainsight-demo", help="folder to create (default ./plainsight-demo)")
    p.set_defaults(func=cmd_demo)

    for name, func, text in [
        ("sync", cmd_sync, "link this session's project into the vault (SessionStart hook)"),
        ("overview", cmd_overview, "print this project's Overview, Inbox and note names (SessionStart hook)"),
        ("lint", cmd_lint, "report broken links, unindexed notes and credentials (read only)"),
        ("uninstall", cmd_uninstall, "remove hooks, CLAUDE.md block and links; keep notes"),
        ("doctor", cmd_doctor, "check hooks, links and config"),
    ]:
        p = sub.add_parser(name, help=text)
        p.add_argument("--vault", help="vault folder (default: where this script is installed, else %s)" % DEFAULT_VAULT)
        if name == "lint":
            p.add_argument("--project", help="only this project folder name")
        if name == "sync":
            p.add_argument("--all", action="store_true",
                           help="link every project under the roots that Claude Code has memory for")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
