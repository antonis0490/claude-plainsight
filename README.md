# claude-plainsight

[![test](https://github.com/antonis0490/claude-plainsight/actions/workflows/test.yml/badge.svg)](https://github.com/antonis0490/claude-plainsight/actions/workflows/test.yml)

Claude Code's memory, in plain sight.

Claude Code keeps per-project memory in `~/.claude/projects/<project>/memory/`, a folder you never look at. claude-plainsight links those folders into an Obsidian vault, one folder per project, so you can read, search and edit what Claude remembers. It also gives each project a hub note with a short Overview that Claude reads at the start of every session and keeps up to date.

It costs close to nothing in tokens: the only thing added to Claude's context is the Overview (capped at 2000 characters, and nothing at all while it is empty) plus a short block in your user `CLAUDE.md`.

One file, Python 3.8 or newer, standard library only. Tested on macOS, Linux and Windows.

## Install

```sh
git clone https://github.com/antonis0490/claude-plainsight
cd claude-plainsight
python3 plainsight.py install --root ~/code --root ~/work
```

Options:

- `--root DIR` (required, repeatable): a folder that holds your projects. Every folder directly inside a root counts as a project.
- `--vault PATH` (default `~/ClaudeVault`): the Obsidian vault to use. It can be a new folder or an existing vault.

Install copies the script and a `config.json` into `<vault>/.plainsight/`, so you can delete the clone afterwards. It also:

- adds two `SessionStart` hooks to your Claude Code `settings.json`: `sync` (runs in the background) and `overview`;
- adds a marked block to your user `CLAUDE.md` asking Claude to keep each project's Overview short and current.

Then open the vault folder in Obsidian. A project shows up after its next Claude Code session. Running install again is safe; it updates the hooks, block and roots in place.

## What you get in the vault

```
ClaudeVault/
  projects/
    my-app/
      my-app.md        hub note with the Overview, created once and never overwritten
      memory/          link to Claude's memory for ~/code/my-app
      memory-web/      link to Claude's memory for a nested repo at ~/code/my-app/web
      CLAUDE.md        link to ~/code/my-app/CLAUDE.md
      _health.md       lint report, rewritten on every session start
  shared/
    CLAUDE.md          link to your user CLAUDE.md
```

The links are real symlinks (junctions for folders on Windows), so edits in Obsidian change Claude's actual memory files.

## Commands

| Command | What it does |
| --- | --- |
| `install --root DIR [--vault PATH]` | Set everything up. |
| `sync` | Link the current session's project into the vault, create its hub note if missing, write `_health.md`. Run by the hook. |
| `overview` | Print the project's Overview, with HTML comments removed, capped at 2000 characters. Prints nothing when empty. Run by the hook. |
| `lint [--project NAME]` | Read only. Reports broken `[[wikilinks]]`, memory notes missing from `MEMORY.md`, and strings that look like credentials (text in `<angle brackets>` is treated as a placeholder and ignored). Found secrets are masked in the output. |
| `doctor` | Checks the config, hooks, `CLAUDE.md` block and links, and says what is wrong. |
| `uninstall` | Removes the hooks, the `CLAUDE.md` block, the links, `_health.md` files and `<vault>/.plainsight/`. Hub notes, your own notes and Claude's memory files stay. |

After install, run commands with the installed copy:

```sh
python3 ~/ClaudeVault/.plainsight/plainsight.py doctor
```

## How the session folder is found

`sync` and `overview` use the `cwd` from the hook's JSON input, then `CLAUDE_PROJECT_DIR`, then the current folder. The project is the top level folder under the closest matching root. Sessions outside every root are ignored.

Claude Code keeps one memory folder per git repository: sessions in a subfolder or a worktree of a repo share the main repo's memory. claude-plainsight follows the same rule, so that memory is linked as `memory/`. A separate `memory-<subdir>/` link only appears when a project folder holds nested repos or plain subfolders that Claude Code treats as their own projects.

`CLAUDE_CONFIG_DIR` is honoured wherever Claude Code's own folder is used.

## Windows notes

Folder links are junctions, which need no admin rights. Linking a file such as `CLAUDE.md` needs Developer Mode or admin rights; without them that link is skipped and a notice goes into `_health.md`. Files are never copied, so the vault cannot drift from the real file.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests use a temporary `CLAUDE_CONFIG_DIR` and temporary folders, and clean up after themselves. Your real Claude Code settings are never touched.

## License

MIT. See [LICENSE](LICENSE).

claude-plainsight is an independent community project. It is not made by or affiliated with Anthropic.
