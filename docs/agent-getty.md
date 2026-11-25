# agent-getty: remote-control daemons from boot

## Problem

After a login or reboot nothing on a machine listens for new work: an agent session has to be started by hand there
before any can be asked for. Wanted on every machine: something that comes up on its own, keeps Remote Control on,
and starts new agent sessions on that machine on request.

## Name

`agent-getty`, after getty(8), which waits on a line and starts a login session when someone arrives. Here the line is
Remote Control and the session is an agent. "Orchestrator" stays reserved for project coordinators: this is a process
manager.

## Decisions

- **What runs: each harness's own remote-control daemon, supervised.** Claude (`claude remote-control`), Codex
  (`codex remote-control --no-daemon`) and Antigravity (`agy remote-control`) each ship a daemon that starts new
  sessions on this machine when asked from the phone or web app. A daemon spends no tokens while it waits, keeps
  working while the account is out of tokens, and cannot be talked into anything by text it reads. Rejected: an LLM
  agent per pair that starts sessions in tmux when told in plain words (the old claude-skills session orchestrator
  shape): it spends tokens, needs a launcher ported to every harness and macOS, and adds an injectable agent.
- **One getty per harness/profile pair**, each its own supervised unit. Rejected: one launcher that starts the others.
  A pair is only as available as its account; with one per pair a Claude outage leaves the Codex and Antigravity
  gettys working, and each process carries only its own profile's credentials, so work and personal never share one.
- **Detached tmux, one server per pair** (`tmux -L agent-getty-<pair>`): the daemon's screen can be attached
  (`agent-getty attach <pair>`), and stopping or losing one pair never touches another or the owner's own tmux.

## Shape

- `~/.local/bin/agent-getty` (template `roles/claude/templates/agent-getty.j2`, bash 3.2): `run <pair>` starts the
  pair's tmux session unless it runs (adopts it, never a second one), waits while it lives, and exits 1 when it
  ends, so the service manager restarts it; `attach <pair>`; `ls`.
- Pairs and their accounts mirror the shell launchers: `claude-personal` (`cper`), `claude-personal2` (`cper2`),
  `codex-work` (`owork`), `codex-personal` (`oper`). Claude daemons get `--permission-mode` from `claude_default_mode`
  and the name `<host>-<pair>`; Claude's trust question for `agent_getty_dir` (default `~`) is answered, since that folder is
  the owner's choice.
- Linux: systemd user template `agent-getty@<pair>.service`, `Restart=always` with a growing delay (systemd 254+) and
  no start limit, `WantedBy=default.target`: it starts at login, or at boot where the system role enabled linger. macOS: LaunchAgent
  `com.<user>.claude-getty-<pair>` (`RunAtLoad`, `KeepAlive`, `ThrottleInterval` 30, Homebrew on `PATH`); LaunchAgents
  start at GUI login; stderr goes to `~/Library/Logs/agent-getty-<pair>.log`.
- `agy/personal`: agy installs and supervises its own daemon service, started at boot (Linux:
  `antigravity-cli-daemon.service`; macOS: `agy remote-control start` registers it to start at boot, its launchd file
  not yet seen); the role only registers it (`agy remote-control start --name
  <host>-agy-personal`) when it is not active, and stops that one when the pair leaves the list. On a fresh machine agy
  is installed after this step, so the second run registers it.
- Stopping a pair stops its daemon: `run` kills the pane's process group and the pair's tmux server on SIGTERM, since
  tmux built with systemd support moves panes out of the unit's cgroup and launchd's bootout misses the tmux server; on
  macOS the role also runs `tmux -L agent-getty-<pair> kill-server` for a disabled pair.
- Config per machine, `agent_getty_pairs` in `config.yml` (default `[]`, off). Linux work laptop: `[claude/personal,
  codex/work, codex/personal, agy/personal]`. Mac: `[claude/personal, claude/personal2, codex/personal,
  agy/personal]`. A pair from "Not possible yet" below is skipped with a warning; anything else fails provisioning,
  and so does a personal2 pair on a `profile: work` machine.
- A new build of the script or unit does not restart a running pair, since that ends the sessions its daemon started:
  the run warns and names the restart command.
- Replaces `claude_orchestrator_*` and `claude-orchestrator.service` (default off): the role removes the old unit.

## Not possible yet

- `claude/work`: Remote Control needs a claude.ai login ("Remote Control is only available with claude.ai
  subscriptions"); the work profile uses Vertex.
- `agy/work`: there is no work Antigravity launcher or home.
- `codex/personal2`: Codex has no personal2 home. `agy/personal2`: `gper2` is Linux-only, and agy's daemon service is
  one per user.

## Verification

- `scripts/test-agent-getty.py`: pair validation (supported pairs pass; unsupported, malformed and work-machine
  personal2 fail), the rendered unit and plist, and the rendered script against fake harnesses on a private tmux
  socket (account variables stripped, trust answered, a duplicate run adopts, the run exits when the daemon ends).
- This laptop, with the rendered files and without rebooting or logging out: the unit starts the daemon, it shows in
  Remote Control, killing it brings it back, and stopping the unit stops it.

## Out of scope

Queues, schedules, restarting sessions a daemon started, auto-detecting profiles, the missing pairs above.
