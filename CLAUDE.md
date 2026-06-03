# laptop-setup

Ansible workstation provisioning playbook for Fedora, RHEL CSB, and macOS.

## Quick Reference

```bash
make all              # Full run (asks for sudo password)
make minimal          # Dotfiles + SSH + repos only (no sudo)
make lint             # ansible-lint + yamllint + shellcheck
make test-fedora      # Molecule Fedora tests (fast, Podman)
make test-vm          # Molecule VM tests (full, Vagrant+libvirt)
make smoke-test       # Post-run verification
make check            # Dry run (--check mode)
```

## Project Structure

- **site.yml** — 3 plays: system (become), user (no become), container (podman connection)
- **13 roles**: common, repos_dnf, system, dotfiles, packages, ssh, git_repos, notes, redhat, containers, desktop, distrobox, claude
- **common/** — Shared task files (CSB detection, failure handler, CSB report, container provisioning)
- **scripts/** — preflight.sh, smoke-test.sh, vault-pass.sh, vault-pass-ci.sh, test-queue-poller.sh
- **molecule/** — Test scenarios (fedora, centos, debian, vm, macos) + shared verify includes

## Key Patterns

- **Profile system**: `profile: work` (default) or `profile: personal` via `-e profile=personal` or `config.yml`
- **become convention**: Play 1 has play-level `become: true`. Play 2 tasks that need root use `become: true` + `tags: [become]`
- **CSB block/rescue**: Tasks that may fail on Corporate Standard Build use `block/rescue` to record failures for the CSB report
- **Config override**: `default.config.yml` (tracked) + `config.yml` (gitignored, user overrides)

## Commit Conventions

- Start with an imperative verb: `Add cgroup mount to molecule configs`
- Max 72 characters, capitalize first word, no trailing period
- Always use `--signoff` (`-s`) when creating git commits

## Vault

- Single vault file: `group_vars/all/vault.yml`
- Password via YubiKey HMAC-SHA1: `scripts/vault-pass.sh`
- Edit: `make vault-edit`

## Testing

- `make lint` — ansible-lint (production profile) + yamllint + shellcheck
- `make syntax-check` — Playbook syntax validation
- `make test-fedora` — Molecule Fedora 43 (common, packages, dotfiles, ssh, git_repos, containers, desktop, claude)
- `make test-vm` — Molecule with Vagrant+libvirt (full system including firewall, sysctl, services)
- `make smoke-test` — Post-provisioning verification (SSH, tools, hardening)
- CI runs linting + Fedora/CentOS/Debian/macOS molecule tests on every PR, VM tests locally
