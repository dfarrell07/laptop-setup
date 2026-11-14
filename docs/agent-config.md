# Agent configuration and context

Audit of the `claude` and `codex` roles, the dotfiles launchers and the repository's own agent instructions,
checked against a source laptop that still uses hand-written alias launchers (November 2026). Host specifics
stay private; what the automation does is in the roles and [migration-checklist.md](migration-checklist.md) § 4.

## Findings

1. **Alias-era homes mix accounts.** Aliases that point `cwork` and `cper` at the same `~/.claude` leave work
   plugins enabled and work-repo memory in the personal home, so every personal session lists the work skills.
   The roles fix this with `~/.claude-work`, but restored state is not split for you.
2. **Project memory will not be found after migration.** Memory is keyed by absolute repository path, and repos
   now clone under `git_repos_base_dir` (`~/src/<owner>/<repo>`), into a different home for work.
3. **`cper` stops reading `~/.claude.json`.** The managed launchers always export `CLAUDE_CONFIG_DIR`, and
   Claude then keeps account state in `$CLAUDE_CONFIG_DIR/.claude.json`. User-scope MCP servers and project trust
   restored to `~/.claude.json` do not apply to `cper`.
4. **The managed `CLAUDE.md`, `AGENTS.md` and `GEMINI.md` replace hand-written global instructions.** A rule that
   lives only there is lost on the first agents run. On the source laptop that is the scratch half of the `rm`
   rule (cleanup under `/tmp/claude-<uid>/`); the literal-path half is now managed.
5. **`--plugin-dir .` loaded every start directory as a plugin while adding almost no context.** No manifest is
   needed: `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `monitors/` and `bin/` (put on
   the Bash PATH) all load, and a same-named installed plugin is silently replaced. Measured below. Done: the
   launchers dropped it and pass `--plugin-dir` only inside listed plugin repositories.
6. **This repository's `CLAUDE.md` is mostly loaded for nothing.** About 6.3 KB of its 17.5 KB is
   first-provision material that agents never act on: "First time on a new machine" (4.3 KB), "USBGuard first
   provision", "Internal SSH git hosts" and the Codex pin-bump procedure. Make, `preflight.sh`, `vault-init.sh`
   and `setup-yubikeys.sh` send humans to that section by name or step number.
7. **Codex seeding is skipped by a full restore.** `config.toml` is seeded with `force: false`, so restoring the
   backed-up file first keeps old project trust and marketplace paths.
8. **Launcher loose ends.** The `claude()` guard named `cw`/`ccp`. Done: the guard names `cwork`/`cper`/`cper2`,
   and `ccp` with its `~/.claude-personal` home is retired; `cls`, `ca` and `cstatus` now cover `cw`/`cwt` sessions
   on the work profile only.

Sound as is: one shared baseline for every agent tool, policy re-merged every run, work plugins only in the
work home, MCP servers and hooks left to the user, memory indexes small, transcripts pruned after 30 days and
excluded from migration.

## `--plugin-dir .`: what it added

Scan of 366 Git checkouts and the session directories in use (November 2026), repeated independently by a
second reviewer. Tokens are estimated as characters / 4 of each listed name and description, the part a session
carries until a skill is used; the real per-entry overhead is somewhat higher.

| Where the session starts | Loaded by `--plugin-dir .` | Added context |
| --- | --- | --- |
| Ordinary repositories, subdirectories, the home directory: most sessions | Nothing, or `bin/` on PATH | 0 tokens |
| `claude-skills` (8 skills) | Working copy replaces the installed plugin | 0-160 net in `cper`; about 420 in `cper2` |
| Release-management checkouts (18 skills, MCP) | Working copy replaces the installed plugin | 0 net; one deferred MCP server |
| `shipyard` (1 skill) | That skill | Under 100 |
| Large skill repositories, not opened in the last 30 days | Up to 136 skills, commands and agents | Up to about 10,600 |

No directory in use has `hooks/hooks.json`, `monitors/` or `.lsp.json`. `bin/` is real: one checkout puts eight
build outputs on PATH, shadowing installed names. Of about 270 sessions in 30 days, about 23 started at a plugin
root; most of the rest are ordinary repositories or headless runs. Dropping the flag saves close to nothing in
context; it was dropped because any cloned repository could add hooks, MCP servers or PATH entries to a session,
and work checkouts replaced installed plugins without notice.

The general skills come from the installed `claude-skills` plugin, not from the flag. `delegate` and `notes`
(and `sessions`, not yet released) are the ones most sessions want; `jira` and `work-summary` are periodic;
`career-advocate`, `cve-agent`, `cve-jira-triage` and `personal-jira-triage` are task-specific. At audit time
`~/.claude` had the released plugin, and inside the repository the flag swapped in a feature-branch working copy;
`~/.claude-per2` had no plugins, so `cper2` had these skills only at the repository root, not in the
subdirectory where most of its sessions start.

What keeps them in every session without the flag:

1. Install the plugin in each home once by hand: `CLAUDE_CONFIG_DIR=~/.claude-per2 claude plugin install
   claude-skills@claude-skills`, and `claude plugin update claude-skills@claude-skills` in each home after a
   release (migration checklist § 4).
2. Every `make claude` adds missing plugin declarations to an existing `settings.json` (`--defaults` in
   `merge-claude-settings.py`) and never changes a value already there, so a restored or older home gets them
   and a disabled plugin stays disabled. Work declarations go only to the work home.
3. For plugin development, `cper`, `cper2` and `cwork` pass `--plugin-dir <Git top level>` when the repository's
   common Git directory is the `.git` of a `dotfiles_claude_plugin_dev_dirs` entry, so subdirectories and linked
   worktrees load the working copy; nested clones and submodules do not. The default lists `claude-skills` only;
   work plugin repositories are left out because every session there would load the checked-out branch.

Rejected alternatives:

- Symlinking single skills into each home's `skills/`: the agent cannot write there, `backup.sh` skips
  symlinked skills, and uncommitted edits would reach every session.
- Re-enforcing the plugin keys on every run: that would reset a deliberate `/plugin disable`.

## Startup context, measured

First-request input tokens of a headless `claude -p` run (November 2026). Identical runs vary by about 3,000, so
each saving below is an A/B of otherwise identical sessions, repeated.

- Empty directory: about 21,700 in `~/.claude` and 19,300-22,200 in `~/.claude-per2`; Codex 13,000-14,400.
- `claude-skills` costs 525. Release-management costs about 1,030 wherever it is enabled. The account-synced
  `productivity` and `engineering` plugins together cost 1,522, and neither was used in 30 days. Release-management
  and the two synced plugins are now disabled at user scope in the personal home. The release repository still
  enables its plugins at project scope for sessions started at its root, and
  `--settings '{"enabledPlugins":{"<plugin>": true}}'` turns one back on for a single session.
- A repository's `CLAUDE.md` is the largest variable cost. `@path` imports inline whole files: 19 imported workflow
  files add 24,600 to every session started in the release repository. They stay, by choice: sessions there get
  the full release context up front, and only those sessions pay for it. This repository's `CLAUDE.md` costs about
  7,900, of which the first-provision sections (proposal 3) are 2,186.
- MCP tools and claude.ai connectors load on demand; only their names and server instructions count.

## Proposals

1. **Migration checklist § 4**, three lines (findings 1-3, 7):
   - Move work-repo directories from `~/.claude/projects/` to `~/.claude-work/projects/`, renaming each to the
     new path's key (`/` becomes `-`), and do not enable work plugins in `~/.claude`.
   - Under `cper`, sign in and re-add user MCP servers with `claude mcp add --scope user`; do not copy
     `~/.claude.json` into `~/.claude/`.
   - Codex: restore `auth.json` only before `make codex`; re-trust projects one at a time.
2. **`CLAUDE.md.j2`**: the `rm` rule (finding 4). Done: the template now carries it.
3. **Move first-provision material out of `CLAUDE.md`** into `docs/first-provision.md`, linked from the README
   quick start; repoint the Make and script messages and `.claude/skills/setup/SKILL.md`. Keep the SSTI,
   `--start-at-task` and xtrace sections, which apply to edits (finding 6). Regenerate `Makefile.sha256`.
