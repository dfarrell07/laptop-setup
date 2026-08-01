# laptop-setup

Ansible workstation provisioning playbook for Fedora, RHEL CSB, and macOS.

## Quick Reference

**First time on a new machine:**
1. `make bootstrap` — installs Ansible collections, git hooks, creates vault-pass.sh stub
2. Create `config.yml` with at minimum `desktop_environment: sway` (or `i3`/`gnome`) and
   your identity vars (`dotfiles_github_user`, `dotfiles_user_name`, `dotfiles_user_email_work`,
   `dotfiles_user_email_personal`). Without `desktop_environment`, auto-detection fails before
   any WM is installed. Without the identity vars, commits are attributed to `dfarrell07` and
   the notes repo clone fails (private repo). If this IS the `dfarrell07` account and you want
   notes provisioned, add `notes_enabled: true` to `config.yml` (disabled by default).
3. Populate `group_vars/all/vault.yml` with real SSH keys (see Vault section below)
   *then* `ansible-vault encrypt group_vars/all/vault.yml` and replace `scripts/vault-pass.sh`
   with your YubiKey HMAC-SHA1 implementation. For a first provision without real secrets,
   vault.yml ships as plaintext — `make all` works as-is but SSH keys won't be deployed.
4. `make all` — full provisioning (asks for sudo password)
   **Run at the local console or inside tmux, NOT over SSH.** The system role restarts
   sshd mid-play, which sends SIGHUP to SSH sessions and kills the Ansible run. If you
   must use SSH, run inside tmux first: `tmux new-session -s provision 'make all'`
5. After provisioning, see `references/troubleshooting.md` for common surprises:
   SSH now on port 722, CUPS masked, TMOUT=600 in shells, USB storage kernel-blocked,
   AllowTcpForwarding no (VS Code port panel needs `system_ssh_allow_tcp_forwarding: local`)
6. Required manual actions after `make all`:
   - **Reboot** — kernel security params (lockdown, IOMMU, vsyscall, init_on_free) only
     take effect after a reboot. SSH will be on port 722 after reboot.
   - `tailscale up` to authenticate (interactive browser step)
   - Log out and back in for group membership changes (libvirt, kvm groups)
   - For CSB/hybrid machines: `make container` to provision dev container
   - `make smoke-test` to verify the provisioning succeeded

```bash
make all              # Full run (asks for sudo password)
make offline          # Full run without binary downloads (packages_install_binaries=false)
make minimal          # Dotfiles + SSH + repos only (no sudo)
make backup           # Back up dotfiles before re-provisioning
make bootstrap        # Initial setup (install deps, collections, hooks)
make update           # Update collections + full run (sets -e git_repos_pull=true to fetch upstream changes)
make lint             # ansible-lint + yamllint + shellcheck
make shellcheck       # Run shellcheck on scripts and hooks (also run by lint)
make syntax-check     # Playbook syntax validation only
make markdownlint     # markdownlint on all .md files
make preflight        # Run preflight checks (CSB detection, prereqs) — run AFTER make bootstrap
make container        # Distrobox/toolbox dev container
make container-rebuild # Rebuild container from scratch
make test-fedora      # Molecule Fedora tests (fast, Podman)
make test-rocky       # Molecule Rocky Linux 10 tests
make test-debian      # Molecule Debian tests
make test-macos       # Molecule macOS tests
make test-container   # Molecule container provisioning scenario (distrobox/Podman)
make test-container-offline  # Molecule container rescue/degradation path (offline 404 scenario)
make test-vm          # Molecule VM tests (full, Vagrant+libvirt)
make smoke-test       # Post-run verification (host)
make smoke-test-container  # Post-run verification (distrobox)
make check            # Dry run (--check mode)
make diff             # Dotfiles check+diff (dry run)
make csb-audit        # Preflight + common dry-run (CSB detection audit)
make test-scripts     # Bash syntax-check of scripts/ (bash -n on preflight, smoke-test, backup)
make test-poller      # Unit tests for claude-queue-poller.sh helpers
make test             # shellcheck + script syntax + poller unit tests + molecule (Fedora/Rocky/Debian/container/container-offline); Podman only
make ci               # Lint + syntax + test-scripts + test-poller + all non-VM molecule tests (includes macos)
make bootstrap-test   # Install libvirt + Vagrant box (required before make test-vm)
make hooks            # Re-install git hooks without full bootstrap
make commitlint       # Validate commit messages from origin/main..HEAD
make pip-lock         # Regenerate requirements-test.lock from current .venv
make pip-sync         # Create reproducible .venv from requirements-test.lock
```

**Individual role targets** (re-run a single role without a full `make all`):

```bash
make dotfiles         # dotfiles role only (no sudo)
make packages         # packages role only (sudo)
make ssh              # ssh role only (no sudo)
make notes            # notes role only (no sudo)
make repos            # git_repos role — all repo groups (no sudo)
make desktop          # desktop role only (sudo)
make system           # system role only (sudo)
make repos-dnf        # repos_dnf role only (sudo)
make redhat           # redhat role only (sudo)
make containers       # containers role only (sudo)
make claude           # claude role only (no sudo)
make distrobox        # distrobox role only (no sudo)
# Per repo-group subsets of `make repos`:
make repos-ovnk       # OVN-Kubernetes repos only
make repos-konflux    # Konflux repos only
make repos-personal   # personal repos only
make repos-bpfman     # bpfman repos only
make repos-downstream # downstream repos only
make repos-cncf       # CNCF repos only
```

## Project Structure

- **site.yml** — 3 plays: system (become), user (no become), container (podman connection)
- **13 roles**: common, repos_dnf, system, packages, dotfiles, ssh, git_repos, notes, redhat, containers, desktop, distrobox, claude
- **common/** — Shared task files (CSB detection, failure handler, CSB report, container provisioning)
- **scripts/** — preflight.sh (`--profile work|personal`), smoke-test.sh (`--user-only` skips root checks),
  backup.sh, vault-pass.sh, vault-pass-ci.sh, test-queue-poller.sh
- **molecule/** — Test scenarios (fedora, rocky, container, container-offline, debian, vm, macos) + shared verify includes

## Key Patterns

- **Profile system**: `profile: work` (default) or `profile: personal` via `-e profile=personal` or `config.yml`
- **become convention**: Play 1 has play-level `become: true`. Play 2 tasks that need root use `become: true` + `tags: [become]`
- **CSB detection**: `common/tasks/csb_detect.yml` sets `csb_detected` via two paths — RHEL (fapolicyd + internal CA present)
  or Fedora (FQDN ends in `.csb` + internal CA present). Determines `needs_container_tier`: `host-only`
  (standard Fedora/macOS — everything on host), `hybrid` (RHEL or CSB-detected Fedora — basics on host +
  dev tools in container), `container` (CSB restricted with fapolicyd enforcing — minimal host, full dev
  env via `make container`). Note: `csb_detected=true` + fapolicyd inactive = `hybrid`, not `container`.
- **CSB block/rescue**: Tasks that may fail on Corporate Standard Build use `block/rescue` to record failures for the CSB report
- **Config override**: `default.config.yml` (tracked) + `config.yml` (gitignored, user overrides). Key
  top-level variables: `profile` (work/personal), `desktop_environment` (auto/sway/i3/gnome), `ssh_port`
  (default 722); feature toggles `install_docker`, `install_vpn`, `install_virtualization`; repo toggles
  `repo_gh_cli`, `repo_google_chrome`, `repo_google_cloud_sdk`, `repo_rpmfusion`, `repo_slack`,
  `repo_tailscale`, `repo_acli`, `repo_docker_ce`, `repo_mullvad`, `repo_redhat`; container settings
  `container_name`, `container_image`, `container_distro`, `container_release`, `container_replace`. See
  `default.config.yml` for full defaults; the `Additional toggles` comment block there lists 35+
  role-defaults vars overridable in `config.yml`. Key operational ones: `system_aide_enabled` (false —
  AIDE disabled by default; enable only with a log consumer), `system_tmout` (600 s inactivity timeout,
  CIS; set 0 to disable), `system_disable_usb_storage` (true — USB drives kernel-blocked), `system_kernel_lockdown`
  (integrity — kernel lockdown mode; set '' to disable for kdump/kgdb debugging), `system_ipv6_accept_ra`
  (0 — SLAAC disabled), `system_ssh_allow_tcp_forwarding` (no — set 'local' for VS Code port forwarding),
  `system_dns_domains` (~. — catch-all for Tailscale MagicDNS), `system_ssh_max_sessions` (10),
  `system_install_usbguard`, `system_tlp_enabled`, `system_timezone`, `desktop_sway_hidpi_scale`, `system_lid_switch`.
- **environment.d for Make**: `DOCKER_HOST` and `KIND_EXPERIMENTAL_PROVIDER=podman` are in both `.zshrc`
  (interactive shells) AND `~/.config/environment.d/containers.conf` (systemd user session generator).
  The environment.d path is critical for OVN-K/Submariner `make kind` since `make` spawns `sh` not
  `zsh` — see `troubleshooting.md` for the failure mode

## Commit Conventions

- Format: `Scope: noun-phrase summary` — capitalize scope, no trailing period
- Scope is the primary role or file group changed; multi-role changes use `/`
  separator (`Notes/smoke:`) or `Multi:` for unrelated cross-cutting changes
- Max 72 characters total
- Always use `--signoff` (`-s`) when creating git commits
- The commit-msg hook rejects past-tense and gerund first words after the colon
  (Added, Fixed, Adding…) but noun-phrase descriptions are the established norm
- Scope values in commitlint.config.js (warning-level): `common`, `repos-dnf`,
  `packages`, `dotfiles`, `ssh`, `git-repos`, `notes`, `redhat`, `containers`,
  `desktop`, `system`, `distrobox`, `claude`, `ci`, `docs`, `chore`,
  `smoke`, `molecule`, `multi`, `sshd`, `nm`, `auditd`, `backup`,
  `handlers`, `security`

## Vault

- Single vault file: `group_vars/all/vault.yml`
- Password via YubiKey HMAC-SHA1: `scripts/vault-pass.sh`
- Edit: `make vault-edit`
- **Do NOT define `vault_*` variables in `config.yml`** — `include_vars` (precedence 17) outranks
  `group_vars` (4-5), so any `vault_*` key in `config.yml` silently shadows the vault-encrypted
  value. A runtime `assert` in `site.yml` enforces this.

## Testing

- `make lint` — ansible-lint (production profile) + yamllint + shellcheck
- `make syntax-check` — Playbook syntax validation
- `make test-scripts` — Bash syntax-check of scripts/ (bash -n on preflight, smoke-test, backup)
- `make test-poller` — Unit tests for roles/claude/files/claude-queue-poller.sh internal helpers
- `make test` — shellcheck + test-scripts + test-poller + molecule (Fedora/Rocky/Debian/container/container-offline);
  Podman only, no libvirt required. CI coverage minus macOS
- `make ci` — Full CI pipeline locally: lint + syntax-check + test-scripts + test-poller + test-fedora
  + test-rocky + test-debian + test-macos + test-container + test-container-offline (macOS runner required)
- `make test-fedora` — Molecule Fedora 44 (common, packages, dotfiles, ssh, git_repos, notes, containers, desktop, claude)
- `make test-rocky` — Molecule Rocky Linux 10 (work profile, includes redhat role)
- `make test-debian` — Molecule Debian 13 (common, packages, dotfiles, ssh, git_repos, notes, containers, desktop, claude)
- `make test-container` — Molecule container scenario (distrobox/podman container provisioning)
- `make test-container-offline` — Molecule container-offline scenario: rescue block verification,
  fast failure via distrobox_oc_fetch_timeout: 5
- `make test-vm` — Molecule with Vagrant+libvirt (full system including firewall, sysctl, services); requires `make bootstrap-test` first
- `make smoke-test` — Post-provisioning verification (SSH, tools, hardening)
- CI runs linting + Fedora/Rocky/Debian/container/macOS molecule tests on every PR, VM tests locally
- CI skips molecule on doc-only PRs (shell-based git diff, no third-party action)

## CI Security

- All third-party GHA actions SHA-pinned by commit hash
- actionlint, zizmor, and gitleaks run as SHA256-verified binary downloads
  (no third-party node actions — eliminates opaque `dist/index.js` supply chain risk)
- OSSF Scorecard runs weekly with SARIF upload to Security tab
- Branch protection on main: 4 required checks (Ansible Lint, Vault Encryption
  Check, Secret Detection, Ansible Syntax Check), force push blocked, linear history
- GitHub secret scanning + push protection enabled
