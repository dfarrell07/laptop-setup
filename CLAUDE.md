# laptop-setup

Ansible workstation provisioning playbook for Fedora, RHEL CSB, and macOS (Debian secondary). Human front door: [README.md](README.md).

**Agents do not run provisioning** (`make all`, `check`, `update`, `system`, `chunk-*`, any sudo target); the owner does, at the console.
Read [docs/design-principles.md](docs/design-principles.md) before changing roles, Make targets or hardening. Rollout design:
[docs/rollout-plan.md](docs/rollout-plan.md); undo steps: [docs/rollout-recovery.md](docs/rollout-recovery.md). The remote is public: host
facts go in the gitignored `CLAUDE.local.md`.

## Quick Reference

### SECURITY: config.yml template injection (SSTI)

Ansible loads `config.yml` with `trusted_as_template=True`, so `{{ ... }}` / `{% ... %}` in any value is evaluated with the privilege of the
play (root in Play 1). `common/tasks/config_ssti_guard.yml` scans every value and rejects template markers before any task runs. Never put
Jinja in `config.yml`; mark values containing braces literal with `!unsafe` (the scan still rejects `{{` and `{%` inside tagged values):

```yaml
dotfiles_goprivate: !unsafe 'git.example.internal,pkgs.example.internal'
```

### SECURITY: never use `--start-at-task`

It skips play-level `pre_tasks` (SSTI guard, URL and version validation, identity assertions), so `-e` values such as `claude_install_url`
could reach installers unchecked. Each role repeats its critical assertions with `tags: [always]` as defence in depth, but do not rely on
that: restart with `make all`, or run one role (`make packages`) with full pre-flight. Never call `ansible-playbook` directly; make targets
go through `scripts/verify-ansible-args.sh`. See SECURITY.md § "Ansible Playbook Safety".

### SECURITY: vault password and shell tracing

`scripts/vault-pass.sh` prints the vault password. Never run make or the playbook under `set -x` / `bash -x`: stderr would leak it into CI
logs and transcripts. The wrapper and the vault scripts disable xtrace, but do not depend on that.

### SECURITY: USBGuard first provision

`system_install_usbguard: true` (default) enforces from first start: devices present then are kept, anything plugged in later is blocked
unless a rule allows it. Before the first `sys_usbguard` run, connect every device you use (drives, docks, keyboards), run `lsusb`, then
`make usb-add` (or add `"allow id VVVV:PPPP"` entries to `system_usbguard_extra_rules`). ThinkPad P16v Gen 1 AMD: ACPI reports internal USB
devices as `not used`, not `hardwired`, so the base rule fails and internal devices need explicit VID:PID rules. Blocked
afterwards: plug in, `make usb-add`, `make system`. Details: `references/troubleshooting.md`.

### Internal SSH git hosts

For internal hosts set `ssh_work_keyscan_hosts` in `config.yml` (`{host: ..., fingerprint: "SHA256:..."}`); get fingerprints out of band.
Without `fingerprint` it is trust-on-first-use. See SECURITY.md § "Internal SSH Git Host Setup".

### First time on a new machine

0. Enable Secure Boot in firmware: `lockdown=integrity` is bypassable by a physical attacker booting a custom kernel without it
   (`make smoke-test` FAILs `secure-boot` until enabled).
1. `make bootstrap` (fresh Fedora/RHEL: `sudo dnf install -y make` first; macOS: `xcode-select --install` and Homebrew first).
2. `cp default.config.yml config.yml`, then replace every `CHANGE_ME`: `desktop_environment` (Linux: sway/i3/gnome; macOS: aerospace),
   `dotfiles_github_user`, `dotfiles_user_name`, `dotfiles_user_email_work`, `dotfiles_user_email_personal`. Also set `system_timezone`
   (default `UTC`; not a `CHANGE_ME` sentinel; preflight fails if the key is absent from `config.yml`).
3. `make setup-yubikeys` programs the keys and writes `vault-pass.sh`; then fill `group_vars/all/vault.yml` (SSH keys) and run
   `ansible-vault encrypt` on it. A first provision without secrets works at the console from the plaintext stub: container provisioning is
   deferred (`make container`) and internal git hosts with placeholder fingerprints are skipped (`make ssh repos`). Provisioning over SSH
   with no ed25519/FIDO2 key is blocked. **WARNING**: with an empty vault no `authorized_keys` is written; after reboot sshd is on port 722
   and hardening rejects RSA/ECDSA keys (only ed25519 and `sk-ssh-ed25519`, plain or certificate variants), so an RSA-only
   `authorized_keys` locks you out. Put an ed25519 key in the vault or in `authorized_keys` before rebooting.
3b. `make preflight` (re-run after any `config.yml` change).
3c. `make check`: sudo `--check --diff` of every play. `make check` and `make all` log to `~/.cache/laptop-setup/logs/`
    (`check-latest.log`, `all-latest.log`; newest 20 kept).
3d. Recommended over `make all`: roll out in chunks. Optionally set `rollout_enable: [list]` in `config.yml`, then per chunk
    `make status`, `make plan CHUNK=x`, `make chunk-x`, `make verify CHUNK=x`. The first run creates `.venv` (needs network); until then the
    chunk-config check is skipped.
4. `make all` applies every enabled chunk (asks for sudo). **Run at the local console or in tmux, NOT over SSH**: the system role restarts
   sshd and kills an SSH-hosted run (`tmux new-session -s provision 'make all'`).
5. Surprises afterwards (SSH on port 722, printing, TCP forwarding, IPv6 SLAAC, lockdown, ...): `references/troubleshooting.md` and the
   toggle comments in `default.config.yml`.
6. Manual actions after `make all`:
   - **Reboot**: lockdown, IOMMU, vsyscall and init_on_free take effect only then; until lockdown=integrity is active, kexec is guarded
     only by the `kexec_load_disabled` sysctl. SSH is then on port 722.
   - `make smoke-test`, then `tailscale up` (browser step).
   - `gh auth login`, then `make packages repos notes` (the first run skips private repos and the notes clone while gh is logged out).
   - Work profile with `oc`: `podman login registry.redhat.io` unless vault `registry_tokens` automates it.
   - Log out and back in for libvirt/kvm group changes; on CSB/hybrid machines run `make container`; re-run `make smoke-test`.

```bash
make status                 # chunks on/off (rollout_enable); list: group_vars/all/rollout.yml
make plan CHUNK=x           # preview one chunk (--check --diff); whole enabled list: make check
make chunk-x                # apply one enabled chunk (high-risk ones need a typed name and a terminal)
make verify CHUNK=x         # smoke checks for that chunk (all enabled if no CHUNK); fails on WARN/SKIP; sys_* need sudo
make all | offline | minimal  # full / no binary downloads / dotfiles+ssh+repos only, no sudo
make update                 # collections update + full run with git_repos_pull=true
make check                  # full sudo dry run; runs preflight first
make preflight              # prerequisites and CSB detection; run after bootstrap
make backup | backup-dry-run  # back up dotfiles before re-provisioning
make bootstrap | hooks | bootstrap-hooks  # deps and collections | git hooks | checksum-pinned gitleaks
make vendor-collections     # re-vendor into collections-dist/ (SECURITY.md Collections Supply Chain)
make container | container-rebuild  # distrobox/toolbox dev container
make usb-add                # allow blocked USB devices, save rules to config.yml (then make system)
make aide-rebaseline        # accept current files as the AIDE baseline
make smoke-test | smoke-test-user | smoke-test-container   # host | no sudo | distrobox
make vault-edit | vault-view | vault-diff
make migration-inventory | workstation-audit  # private reports for a laptop move
make lint                   # ansible-lint, yamllint, shellcheck, markdownlint, check-vars-sync, script integrity
make syntax-check | shellcheck | markdownlint | commitlint | check-vars-sync
make test-scripts           # bash -n of scripts + hook tests + scripts/test-*.py suites
make test-poller            # claude-queue-poller.sh helper tests
make test                   # shellcheck + test-scripts + test-poller + Podman molecule scenarios
make ci                     # lint + syntax-check + all non-VM tests (adds macOS); make test-vm needs bootstrap-test
make test-fedora | test-rocky | test-debian | test-macos | test-container | test-packages-binaries  # one molecule scenario each
make pip-lock | pip-sync    # regenerate / install requirements-test.lock
```

Role targets re-run one role (`make dotfiles packages ssh notes repos desktop system repos_dnf redhat containers claude codex`) and apply
all its enabled chunks without the high-risk prompt; prefer `make chunk-x` for `sys_*` and `ssh_authorized_keys`. `make repos-<group>`
runs `git_repos` for one `repo_category`. `make help` lists everything; scenario names are in `molecule/`.

## Project Structure

- **site.yml**: 3 plays (system with become, user without, container over podman). **14 roles** in `roles/`; `common/` holds shared task
  files (CSB detection, failure handler, CSB report, pre-flight checks, config SSTI guard, become probe, git-lfs setup);
  `scripts/` holds preflight, smoke-test, backup, vault and test scripts; `molecule/shared/` holds task includes reused by scenarios, and
  `system-container-overrides.yml` there holds the CI-critical variable overrides for fedora/rocky/debian converge.
- **System role**: `roles/system/tasks/main.yml` mostly orchestrates (inline assertions for sysctl, SELinux, kernel
  lockdown and SSH AllowUsers, the systemd drop-in and /etc/dbus-1/system.d scans, and login-banner deployment also live there and are
  edited in main.yml); subsystem logic goes in the per-subsystem sub-file (`sshd.yml`, `sysctl.yml`, `pam.yml`,
  `usbguard.yml`, ...). `hardening_core.yml` is a sub-orchestrator for mounts, module blacklist, sysctl, firewall, kernel lockdown and
  coredumps. Each import except `hardening_core.yml` carries its chunk tag (`sys_*`, see `group_vars/all/rollout.yml`);
  the six `hardening_core.yml` includes carry theirs.
- **Packages role**: `main.yml` delegates to `install_*.yml` extractors by kind (dnf, binary downloads, tarballs, go tools, oc/opm,
  standalone binaries, brew, krew, pipx, virtualization). Add a new binary to the matching extractor; downloads are SHA256-pinned.

## Key Patterns

- **Debian** has no firewalld, USBGuard, grubby cmdline hardening, rpm-ownership pre-plant checks or unattended upgrades.
- **Profile**: `profile: work` (default) or `personal`, via `config.yml` or `-e profile=personal`.
- **become**: Play 1 has play-level `become: true`; Play 2 tasks needing root use `become: true` and `tags: [become]`.
- **is_dnf / is_apt**: booleans in `group_vars/all/vars.yml`, overridden at runtime by `common/tasks/csb_detect.yml`. Use them, not
  `ansible_pkg_mgr`, for package-manager branching.
- **CSB detection**: `csb_detect.yml` sets `csb_detected` (RHEL: fapolicyd and internal CA; Fedora: `.csb` FQDN or fapolicyd, and internal
  CA) and `needs_container_tier`: `host-only` (standard Fedora/macOS), `hybrid` (RHEL or CSB Fedora: basics on host, dev tools in
  container), `container` (CSB with fapolicyd enforcing). `csb_detected` with fapolicyd inactive is `hybrid`.
- **csb_rhel** (`vars.yml`): `csb_detected` and RHEL. `not csb_rhel` guards harden Fedora hybrid but skip RHEL CSB (IT owns it);
  `not csb_detected` guards skip every CSB tier (chrony, dnf-automatic and the NM `99-dns.conf` override use this). Tasks that may fail on
  CSB use block/rescue to feed the CSB report.
- **Variable visibility**: role defaults are invisible to other plays; variables read outside their role live in `vars.yml` with mirrors
  checked by `make check-vars-sync`.
- **Local repo list**: repos on internal hosts go in gitignored `group_vars/all/local_repos.yml` (`git_repos_local`; copy
  `local_repos.yml.example`); `repos.yml` holds public repos only and both concatenate into `git_repos_list`.
- **Config**: tracked `default.config.yml` plus gitignored `config.yml` overrides. Top-level keys: `profile`, `desktop_environment`,
  `rollout_enable`, `ssh_port` (722), feature toggles (`install_docker`, `install_vpn`, `install_virtualization`), `repo_*` toggles and
  `container_*` settings. Its `Additional toggles` comment block documents 100+ role-default variables; read it, not this file, for
  defaults and rationale. Do not set `system_core_pattern` via `system_sysctl_extra` (toggle-managed). `containers_environment_d_enabled`
  (default false) is opt-in because it injects `DOCKER_HOST` into every graphical session; see troubleshooting.

## Commit Conventions

- `Scope: noun-phrase summary`: capitalized scope, no trailing period, at most 72 characters, always `--signoff` (`-s`).
- Scope is the primary role or file group (`Packages`, `System`, `Docs`, `Ci`, `Molecule`, `Smoke`, `Chore`, `Security`, ...); several
  roles join with `/` (`Notes/Smoke:`); `Multi:` for unrelated cross-cutting changes. The scope list is not enumerated by a hook.
- The commit-msg hook rejects past-tense and gerund first words after the colon (Added, Fixed, Adding...); use a noun phrase.
- GPG enforcement is temporarily off (`.gpg-enforcement`); hooks and CI still require signoff, hashes and secret scans (SECURITY.md).

## Vault

- One vault file, `group_vars/all/vault.yml`; password from a YubiKey via `make setup-yubikeys`; `make vault-edit|vault-view|vault-diff`.
  `vault-pass.sh` templates: SECURITY.md § "Setting Up vault-pass.sh".
- **Never define `vault_*` variables in `config.yml`**: `include_vars` (precedence 18) outranks `group_vars` and silently shadows the vault
  value. `common/tasks/pre_flight_checks.yml` asserts this.
- **The GRUB password hash must be vault-sourced** (`system_grub_password_enabled: true`): generate with `grub2-mkpasswd-pbkdf2`, store as
  `vault_grub_password_hash`, which the playbook maps to `system_grub_password_hash`. A PBKDF2 hash in plaintext config or CI artifacts
  allows offline brute force.

## Testing and CI

- Before committing run the focused `scripts/test-*.py` for the change (`.venv/bin/python3 scripts/test-X.py`), then `make lint`,
  `make syntax-check`, `make test-scripts` and `make check-vars-sync`. `LAPTOP_SETUP_TEST_TIMEOUT=180 make test-scripts` raises the
  Ansible test deadlines on a busy host.
- Molecule scenarios (Podman, no libvirt): fedora, rocky, debian, container, container-offline, container-offline-distrobox,
  distrobox-role, packages-binaries, ssti-guard; macos needs a macOS runner; vm needs `make bootstrap-test`. CI runs lint plus these on
  every PR and skips molecule for doc-only PRs.
- Third-party GitHub Actions are SHA-pinned; actionlint, zizmor and gitleaks are SHA256-verified binary downloads. Branch protection on
  main requires every lint/supply-chain job, the per-platform Molecule jobs and the `All Checks Pass` / `All Molecule Checks Pass`
  aggregates: renaming or deleting a job blocks merges until protection is updated. Force push is blocked; history is linear.
- Collections are vendored in `collections-dist/` with SHA256 and Python-manifest checks (`scripts/verify-collections.sh`); changing them
  follows SECURITY.md § "Ansible Collections Supply Chain".

## Moving from the old laptop

See the [transfer checklist](docs/migration-checklist.md). Direct launchers `cwork`, `cper`, `cper2`, `owork`, `oper` and `gper` keep the
old workflow (`gper` needs the separate Antigravity CLI); `~/.config/zsh/local.zsh` is preserved.

Codex installs in user space from a committed npm integrity lock with lifecycle scripts disabled; `packages_install_binaries: false` skips
the download and `codex_enabled: false` skips the role. Config files are seed-only. To bump the pin, edit `roles/codex/files/package.json`,
run `npm install --package-lock-only --ignore-scripts --prefix roles/codex/files`, review every archive URL and integrity value, and run
the migration tests (optional download test: `CODEX_INSTALL_TEST=1 .venv/bin/python3 scripts/test-laptop-migration.py`).
Power-stack (TLP) logic lives in `roles/system/tasks/power_management.yml`; native package and service behavior needs validation on the
destination.
