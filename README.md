# laptop-setup

Ansible playbook that provisions a developer workstation: packages and pinned binaries, dotfiles, desktop,
Claude Code and Codex, git repositories, containers, and optional system hardening. It targets Fedora (tested
on 44), RHEL and its Corporate Standard Build (CSB), and macOS, with Debian as a secondary target.

It is built to be adopted **one chunk at a time**: low-risk, high-value pieces first (packages, dotfiles,
desktop, agents, repos), system hardening last and deliberately. `make all` applies every enabled chunk,
including the high-risk `sys_*` ones (sshd moves to port 722), so choose chunks before you run it.

## Quick start

```bash
git clone https://github.com/dfarrell07/laptop-setup ~/laptop-setup && cd ~/laptop-setup
sudo dnf install -y make        # Fedora/RHEL; Debian: apt install make python3-venv; macOS: xcode-select --install + Homebrew
make bootstrap                  # installs ansible and tools (sudo on Linux), verifies vendored collections, installs git hooks
cp default.config.yml config.yml   # then edit: replace every CHANGE_ME (desktop_environment, identity), set system_timezone
make setup-yubikeys             # optional: programs YubiKey slot 2 (overwrites it) and sets up the vault
make preflight                  # prerequisite checks; fix each [FAIL] line, then re-run (re-run after editing config.yml too)
make status                     # which chunks are on, their risk, whether they need sudo, and the next suggestion
```

Skip `make setup-yubikeys` for a first run without secrets (the vault stays a stub; preflight warns), and on a
machine that reuses an existing vault: see [docs/migration-checklist.md](docs/migration-checklist.md).

Then roll out one chunk at a time, in `make status` order:

```bash
make plan CHUNK=ssh_client      # preview: a --check --diff run of that chunk only
make chunk-ssh_client           # apply exactly that chunk
make verify CHUNK=ssh_client    # run only the smoke checks for that chunk
```

Undo steps for every chunk are in [docs/rollout-recovery.md](docs/rollout-recovery.md). When you trust it,
`make check` (a full dry run, asks for sudo) and `make all` apply everything enabled.

The first `make status` creates `.venv` (needs network). Do not run make as root; it asks for sudo when a chunk
needs it. Run `make all` and any `sys_*` chunk at the local console or inside tmux, not over SSH: the system
role restarts sshd.

## Choosing chunks

Set `rollout_enable` in `config.yml` to `all` (the default) or a list of chunk names:

```yaml
rollout_enable: [packages, ssh_client, dotfiles, desktop, agents, repos]
```

Disabled chunks are skipped by every make run, and a bare `ansible-playbook` run fails closed rather than apply
them. A chunk's prerequisites must be enabled too (the check names what is missing). The chunk list, in
recommended order, is `group_vars/all/rollout.yml`; the design is in [docs/rollout-plan.md](docs/rollout-plan.md).

## Everyday commands

| Command | What it does |
| --- | --- |
| `make status` | Chunks on/off, risk, sudo needs, next suggestion |
| `make plan CHUNK=x` / `make chunk-x` / `make verify CHUNK=x` | Preview, apply, check one chunk (high-risk ones ask you to type the name) |
| `make check` | Full dry run of the enabled list (sudo) |
| `make all` / `make update` | Apply everything enabled / also update collections and pull repos |
| `make dotfiles`, `make packages`, ... | One role, skipping the high-risk prompt; prefer `make chunk-x` for high-risk chunks |
| `make backup` | Back up dotfiles before re-provisioning |
| `make smoke-test` | Verify a provisioned machine |
| `make lint`, `make syntax-check`, `make test-scripts`, `make test` | Checks before committing |

[CLAUDE.md](CLAUDE.md) has the full command reference and the first-time setup notes (reboot, `gh auth login`,
Tailscale); `make help` lists the main targets.

## Configuration

`default.config.yml` is the tracked set of defaults; `config.yml` (gitignored) holds your overrides, including
`profile` (`work` by default, or `personal`), `desktop_environment` (sway, i3, gnome or aerospace), the identity
variables and feature toggles. Secrets live in the Ansible vault (`group_vars/all/vault.yml`), never in `config.yml`.

## Documentation

| Read | For |
| --- | --- |
| [CLAUDE.md](CLAUDE.md) | Commands, first-time setup, key variables, repository conventions |
| [docs/rollout-recovery.md](docs/rollout-recovery.md) | What each chunk changes and how to undo it |
| [docs/rollout-plan.md](docs/rollout-plan.md), [docs/design-principles.md](docs/design-principles.md) | How chunk selection works and why |
| [references/troubleshooting.md](references/troubleshooting.md) | Common failures by symptom (chunk errors, SSH, DNS, PAM, containers) |
| [docs/migration-checklist.md](docs/migration-checklist.md) | Moving from an old laptop |
| [SECURITY.md](SECURITY.md) | Threat model, vault, hooks, supply chain, hardening scope |
| [AGENTS.md](AGENTS.md) | Rules for AI agents working in this repo |

## Contributing

Use feature branches and signed-off commits (`git commit -s`) with `Scope: noun-phrase summary` subjects of at
most 72 characters. Run `make lint`, `make syntax-check` and `make test-scripts` first; `make test` runs the Linux-capable molecule
and script tests; `make ci` adds the gitleaks config test and the macOS scenario (needs a macOS runner).
The remote is public: keep host-specific details out of tracked files.

Licensed under the Apache License 2.0 ([LICENSE](LICENSE)).
