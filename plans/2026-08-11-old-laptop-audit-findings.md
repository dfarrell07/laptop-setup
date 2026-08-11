# Old Laptop Audit Findings

Audit performed 2026-08-11 from the old ThinkPad X1 Carbon 7th Gen (Fedora 42,
i3, Intel) against the wip1 branch targeting the new ThinkPad P16v Gen 1 (AMD,
Fedora 44, Sway, HiDPI 2560x1600, CSB hybrid).

18 agents explored the full codebase, ~/notes-ai research corpus, and current
machine state. The wip1 branch is remarkably well-honed — AMD/Intel dispatch,
Fedora 44 compatibility, dnf5 handling, vault bootstrap, and binary download
idempotency are all clean. The findings below are real UX gaps for the P16v,
not theoretical concerns.

## Old laptop state (the "before")

- ThinkPad X1 Carbon 7th Gen, i7-8665U, 16 GB RAM
- Fedora 42 (EOL since 2026-05-27)
- i3 v4.25.1 (X11, not Sway)
- No config.yml — running on default.config.yml defaults only
- No kernel hardening (no lockdown, no iommu, no vsyscall=none in /proc/cmdline)
- Firewall default zone: FedoraWorkstation (not drop)
- Repos in old ~/go/src/ layout (not ~/src/)
- Docker AND Podman coexist
- Tailscale NOT installed
- YubiKey 5C Nano firmware 5.2.3 (pre-5.7, EUCLEAK CVE-2024-45678)
- Linger disabled

---

## Findings

### P1 — Critical (could brick or block the user)

#### 1. USBGuard `hardwired` rule is no-op on AMD

- **File**: `roles/system/defaults/main.yml:134-137`
- **Problem**: The base whitelist rule `allow with-connect-type "hardwired"` does
  not work on AMD machines — AMD ACPI reports internal ports as "not used"
  instead of "hardwired." On the P16v, USBGuard will block the internal
  keyboard, touchpad, webcam, and Bluetooth at boot unless per-device VID:PID
  rules are added via `system_usbguard_extra_rules`.
- **Risk**: Bricked input on first reboot after enabling USBGuard.
- **Evidence**: Comment at defaults line 134-136 documents the problem but
  provides no guardrail.
- **Fix options**:
  - A: Pre-task that runs `usbguard generate-policy` on AMD hosts and warns if
    `system_usbguard_extra_rules` is empty
  - B: Assert that `system_usbguard_extra_rules` is non-empty on AMD before
    enabling USBGuard
  - C: Document exact P16v VID:PID rules in default.config.yml as a commented
    example

#### 2. SSH client KexAlgorithms too narrow for corporate environments

- **File**: `roles/dotfiles/templates/ssh_config.j2:12`
- **Problem**: Client config offers only curve25519-sha256 (plus optional PQ
  variants). Connections to RHEL 7 jump boxes, older corporate servers, or
  embedded devices that only support `ecdh-sha2-nistp256` or
  `diffie-hellman-group-exchange-sha256` fail with "no matching key exchange
  method found."
- **Risk**: Day-1 SSH connectivity blocker on a work laptop. User spends 30 min
  debugging before finding the cause.
- **Evidence**: The sshd server config (`vars.yml:119-120`) already allows 7
  algorithms including ECDH and DH-GEX, but the client does not mirror this.
- **Fix options**:
  - A: Add `ecdh-sha2-nistp256` and `diffie-hellman-group-exchange-sha256` to
    the client KEX list (mirrors sshd)
  - B: Document `~/.ssh/config.local` as the escape hatch for older servers
  - C: Both — broader defaults with config.local documented for further tuning

#### 3. `output * scale` breaks external monitors at different DPI

- **File**: `roles/desktop/templates/sway.config.j2:10`,
  `roles/desktop/defaults/main.yml:34`
- **Problem**: `desktop_sway_output` defaults to `"*"` (all outputs). Setting
  `desktop_sway_hidpi_scale: 1.5` applies 1.5x to ALL monitors including
  external 1080p displays, rendering them at effective 720p. No per-output scale
  mechanism exists.
- **Risk**: Plugging in any external monitor produces comically oversized or
  unusably tiny content.
- **Fix options**:
  - A: Change default `desktop_sway_output` to built-in display name (e.g.,
    `eDP-1`) — requires runtime detection or config.yml override
  - B: Support a list/dict of per-output scale directives (similar to
    `desktop_sway_workspace_outputs`)
  - C: Add prominent documentation warning in default.config.yml

### P2 — High (misleading or surprising behavior)

#### 4. Preflight WARN vs `make all` FAIL mismatch

- **File**: `scripts/preflight.sh:219-227`,
  `common/tasks/pre_flight_checks.yml:63-109`
- **Problem**: Preflight reports WARN (exit 0) for CHANGE_ME identity variables,
  telling the user "Ready to run make all." Then `make all` immediately
  hard-fails on those same sentinels in pre_flight_checks.yml.
- **Fix**: Change the five identity var checks in preflight.sh from WARN to
  FAIL, matching the Ansible assertion behavior.

#### 5. `make update` hard-fails on repos with local work

- **File**: `roles/git_repos/tasks/main.yml:~188`
- **Problem**: `git pull --ff-only` fails on any repo not on its default branch
  or with local commits. A subsequent `fail` task aborts the entire playbook.
  Common for developers with topic branches checked out.
- **Fix options**:
  - A: Skip repos not on default branch with a warning instead of failing
  - B: Collect failures and report at the end (like CSB failure handler)
  - C: Check branch before pulling, skip with per-repo warning

#### 6. Task queue: `set -e` disabled in `(...) || {}` subshell

- **File**: `roles/claude/files/claude-queue-poller.sh:151-206`
- **Problem**: Bash disables `set -e` inside the LHS of `||`. Failed
  `git fetch`, `git checkout`, `git pull --ff-only` silently continue. Claude
  runs on stale code and creates PRs from a broken state.
- **Fix**: Replace `(...) || { }` with explicit `if ! (...); then` or use a
  function with explicit error checking.

#### 7. Task queue: `Bash(git *)` permits `git push --force`

- **File**: `roles/claude/files/claude-queue-poller.sh:111`
- **Problem**: Default allowedTools fallback includes `Bash(git *)` which
  permits any git subcommand. Claude can independently `git push --force`,
  `git remote set-url`, or `git config`.
- **Fix**: Restrict to specific subcommands:
  `Bash(git diff *),Bash(git log *),Bash(git status),Bash(git show *)`

#### 8. Task queue: systemd security directives silently ignored

- **File**: `roles/claude/templates/claude-queue.service.j2:23-37`
- **Problem**: 10+ directives (`ProtectSystem`, `PrivateTmp`,
  `ProtectKernelTunables`, etc.) are silently no-ops in user-scoped systemd
  units. Creates a false sense of security hardening.
- **Fix options**:
  - A: Remove the no-op directives and add a comment explaining user unit
    limitations
  - B: Add `PrivateUsers=true` to enable namespace-based sandboxing (requires
    testing)
  - C: Move to system-level unit (requires become)

### P3 — Medium (day 1-3 friction)

#### 9. Window borders invisible at HiDPI

- **File**: `roles/desktop/defaults/main.yml:61`
- **Problem**: `desktop_wm_border_width: 1` at 1.5x scale on 188 PPI is
  ~0.13mm — nearly impossible to see which window is focused.
- **Fix**: Default to 2 when `desktop_sway_hidpi_scale > 1.0`, or document the
  override.

#### 10. RPM Fusion hard-blocked on CSB hybrid

- **File**: `roles/repos_dnf/tasks/main.yml:318-358`
- **Problem**: `not csb_detected` guard with no config.yml override. A user on
  hybrid Fedora CSB with full admin control cannot enable multimedia codecs even
  by setting `repo_rpmfusion_free: true`.
- **Fix**: Change guard to a defaulting variable like
  `system_rpmfusion_allow: "{{ not csb_detected }}"` overridable in config.yml.

#### 11. No battery indicator in status bar by default

- **File**: `roles/desktop/defaults/main.yml:25`
- **Problem**: `desktop_i3status_battery_num` defaults empty. On a laptop, no
  battery percentage visible in the status bar.
- **Fix options**:
  - A: Auto-detect battery presence and default to `'0'` on laptops
  - B: Document clearly in default.config.yml as a must-set for laptops

#### 12. `--user-only` smoke-test skips world-readable checks

- **File**: `scripts/smoke-test.sh:684`
- **Problem**: Entire 1,100-line system block skipped, including checks that
  don't require sudo: Podman socket, battery thresholds, kernel cmdline,
  amdgpu runtime PM, sysctl values via /proc.
- **Fix**: Move world-readable checks out of the sudo-gated block into the
  always-run user section.

#### 13. USB storage module loaded-vs-blacklisted false positive

- **File**: `scripts/smoke-test.sh` (usb-storage check)
- **Problem**: Smoke test checks config file but not `lsmod`. Reports PASS for
  `usb-storage-blocked` even when the module is still loaded (only removed at
  next boot).
- **Fix**: Add `lsmod | grep -q usb_storage` check alongside config file check.
  WARN if config correct but module still loaded.

#### 14. No Molecule test for CSB hybrid path

- **File**: `molecule/` (all scenarios)
- **Problem**: No scenario tests `csb_detected=true, csb_rhel=false,
  is_fedora=true`. Hybrid-specific code paths (redhat firewall zone, WiFi MAC
  randomization, RPM Fusion skip, DoT removal) are untested in CI.
- **Fix**: Add a molecule scenario or modify the fedora scenario to include a
  hybrid variant.

#### 15. `cw()` breaks without claude role's work-env file

- **File**: `roles/dotfiles/templates/zshrc.j2:117`
- **Problem**: `source ~/.config/claude/work-env` has no existence guard. If
  user runs `make dotfiles` alone, `cw` produces a cryptic file-not-found error.
- **Fix**: Add `[[ -f ~/.config/claude/work-env ]] || { echo "Run make claude
  first" >&2; return 1; }` before the tmux launch.

#### 16. No config.yml template generator

- **File**: `Makefile`
- **Problem**: User must create config.yml from scratch knowing 6 required
  variables (desktop_environment, dotfiles_github_user, dotfiles_user_name,
  dotfiles_user_email_work, dotfiles_user_email_personal, system_timezone).
- **Fix**: Add `make init-config` target that generates a config.yml stub with
  the required variables and TODO comments.

#### 17. Missing `~/.config/systemd/user/` directory creation

- **File**: `roles/claude/tasks/main.yml:217`
- **Problem**: Timer and service templates deployed to a directory that may not
  exist before first graphical login.
- **Fix**: Add a `file: state=directory` task for `~/.config/systemd/user/`
  before the template deployments.

### P4 — Low (cosmetic or minor)

#### 18. No `gtk-xft-dpi` for XWayland GTK apps

- **File**: `roles/desktop/templates/gtk-settings.ini.j2`
- **Problem**: XWayland GTK2 apps fall back to 96 DPI and get blurry upscaling.
- **Fix**: Add `gtk-xft-dpi={{ (96 * scale * 1024) | int }}` to the template.

#### 19. No managed mako/wofi configs

- **File**: `roles/desktop/templates/sway.config.j2:26,56`
- **Problem**: Notification and launcher font/width not tunable via Ansible.

#### 20. Stale Molecule image digest

- **File**: 6 molecule.yml files
- **Problem**: Pinned SHA differs from current `:latest` for
  `geerlingguy/docker-fedora44-ansible`.

#### 21. `packages_security_yubikey` undefined in defaults

- **File**: `roles/packages/tasks/install_dnf_packages.yml:16`
- **Problem**: Referenced but never defined. Branch never taken due to falsy
  undefined evaluation. `yubikey-manager`/`ykpers` may only install as
  dependencies.

#### 22. sntrup761 suffix inconsistency

- **File**: `ssh_config.j2:12` vs `vars.yml:119`
- **Problem**: Client omits `@openssh.com` suffix on `sntrup761x25519-sha512`.
  Both aliases work but inconsistent for auditing.

#### 23. `ccp()` work-dir guard absent with default empty `dotfiles_work_src_dirs`

- **File**: `roles/dotfiles/templates/zshrc.j2:124-128`
- **Problem**: Default `[]` means the entire guard block is omitted. No
  protection against personal Claude in work repos.

#### 24. Task queue Podman isolation not implemented

- **File**: migration plan vs `roles/claude/`
- **Problem**: Plan requires bare Podman container with 16+ security flags.
  Current implementation runs Claude directly on host.

---

## What's solid (no gaps found)

- AMD/Intel dispatch: IOMMU, thermald, TLP, GPU, pstate all runtime-detected
- Fedora 44: zero breaking changes across packages, systemd, NM, authselect,
  firewalld
- dnf5: properly detected and branched throughout
- Vault bootstrap: plaintext stub works without YubiKey, secret-dependent tasks
  skip gracefully
- SSH agent: GNOME Keyring disabled, custom ssh-agent single source of truth
- Binary idempotency: version checks prevent re-downloads on re-runs
- Firewall-to-sshd ordering: port 722 opened before sshd restart, no lockout
- CSB hybrid tier: correctly does NOT inherit RHEL CSB restrictions via
  `csb_rhel` discriminator

---

## Approach

Address findings in priority order. For each fix, respect that wip1 has been
honed and audited on the P16v — prefer minimal targeted changes over
refactoring. Test each change against the existing Molecule scenarios before
expanding test coverage.

Batch into logical commits per the project's `Scope: noun-phrase summary`
convention with `--signoff`.
