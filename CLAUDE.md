# laptop-setup

Ansible workstation provisioning playbook for Fedora, RHEL CSB, and macOS.

## Quick Reference

```bash
make all              # Full run (asks for sudo password)
make minimal          # Dotfiles + SSH + repos only (no sudo)
make backup           # Back up dotfiles before re-provisioning
make bootstrap        # Initial setup (install deps, collections, hooks)
make update           # Update collections + full run
make lint             # ansible-lint + yamllint + shellcheck
make syntax-check     # Playbook syntax validation only
make markdownlint     # markdownlint on all .md files
make preflight        # Run preflight checks (CSB detection, prereqs)
make container        # Distrobox/toolbox dev container
make container-rebuild # Rebuild container from scratch
make test-fedora      # Molecule Fedora tests (fast, Podman)
make test-centos      # Molecule Rocky Linux 10 tests
make test-debian      # Molecule Debian tests
make test-macos       # Molecule macOS tests
make test-vm          # Molecule VM tests (full, Vagrant+libvirt)
make smoke-test       # Post-run verification (host)
make smoke-test-container  # Post-run verification (distrobox)
make check            # Dry run (--check mode)
make diff             # Dotfiles check+diff (dry run)
make csb-audit        # Preflight + common dry-run (CSB detection audit)
make ci               # Lint + syntax + all non-VM molecule tests (mirrors CI pipeline)
make bootstrap-test   # Install libvirt + Vagrant box (required before make test-vm)
make hooks            # Re-install git hooks without full bootstrap
make commitlint       # Validate commit messages from origin/main..HEAD
```

## Project Structure

- **site.yml** — 3 plays: system (become), user (no become), container (podman connection)
- **13 roles**: common, repos_dnf, system, dotfiles, packages, ssh, git_repos, notes, redhat, containers, desktop, distrobox, claude
- **common/** — Shared task files (CSB detection, failure handler, CSB report, container provisioning)
- **scripts/** — preflight.sh (`--profile work|personal`), smoke-test.sh (`--user-only` skips root checks), backup.sh, vault-pass.sh, vault-pass-ci.sh, test-queue-poller.sh
- **molecule/** — Test scenarios (fedora, centos, debian, vm, macos) + shared verify includes

## Key Patterns

- **Profile system**: `profile: work` (default) or `profile: personal` via `-e profile=personal` or `config.yml`
- **become convention**: Play 1 has play-level `become: true`. Play 2 tasks that need root use `become: true` + `tags: [become]`
- **CSB detection**: `common/tasks/csb_detect.yml` sets `csb_detected` via two paths — RHEL (fapolicyd + internal CA present) or Fedora (FQDN ends in `.csb` + internal CA present). Determines `needs_container_tier`: `host-only` (standard Fedora/macOS — everything on host), `hybrid` (RHEL or CSB-detected Fedora — basics on host + dev tools in container), `container` (CSB restricted with fapolicyd enforcing — minimal host, full dev env via `make container`). Note: `csb_detected=true` + fapolicyd inactive = `hybrid`, not `container`.
- **CSB block/rescue**: Tasks that may fail on Corporate Standard Build use `block/rescue` to record failures for the CSB report
- **Config override**: `default.config.yml` (tracked) + `config.yml` (gitignored, user overrides)
- **environment.d for Make**: `DOCKER_HOST` and `KIND_EXPERIMENTAL_PROVIDER=podman` are in both `.zshrc` (interactive shells) AND `~/.config/environment.d/containers.conf` (systemd user session generator). The environment.d path is critical for OVN-K/Submariner `make kind` since `make` spawns `sh` not `zsh` — see `troubleshooting.md` for the failure mode

## Commit Conventions

- Start with an imperative verb: `Add cgroup mount to molecule configs`
- Max 72 characters, capitalize first word, no trailing period
- Always use `--signoff` (`-s`) when creating git commits
- Optional scope from commitlint enum: `common`, `repos-dnf`, `packages`,
  `dotfiles`, `ssh`, `git-repos`, `notes`, `redhat`, `containers`,
  `desktop`, `system`, `distrobox`, `claude`, `ci`, `docs`, `chore`

## Vault

- Single vault file: `group_vars/all/vault.yml`
- Password via YubiKey HMAC-SHA1: `scripts/vault-pass.sh`
- Edit: `make vault-edit`

## Testing

- `make lint` — ansible-lint (production profile) + yamllint + shellcheck
- `make syntax-check` — Playbook syntax validation
- `make test-fedora` — Molecule Fedora 44 (common, packages, dotfiles, ssh, git_repos, notes, containers, desktop, claude)
- `make test-centos` — Molecule Rocky Linux 10 (work profile, includes redhat role)
- `make test-debian` — Molecule Debian 12 (common, packages, dotfiles, ssh, git_repos, notes, containers, desktop, claude)
- `make test-vm` — Molecule with Vagrant+libvirt (full system including firewall, sysctl, services)
- `make smoke-test` — Post-provisioning verification (SSH, tools, hardening)
- CI runs linting + Fedora/Rocky/Debian/macOS molecule tests on every PR, VM tests locally
- CI skips molecule on doc-only PRs (shell-based git diff, no third-party action)

## CI Security

- All third-party GHA actions SHA-pinned by commit hash
- actionlint, zizmor, and gitleaks run as SHA256-verified binary downloads
  (no third-party node actions — eliminates opaque `dist/index.js` supply chain risk)
- OSSF Scorecard runs weekly with SARIF upload to Security tab
- Branch protection on main: 4 required checks (Ansible Lint, Vault Encryption
  Check, Secret Detection, Ansible Syntax Check), force push blocked, linear history
- GitHub secret scanning + push protection enabled
