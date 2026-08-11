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

#### 36. Jinja2 parse error in sysctl.yml `#` comment inside double-quoted string

- **Dimension**: ansible-lint
- **File**: `roles/system/tasks/sysctl.yml:31-46`
- **Problem**: The inline `# pre-reboot; lockdown=integrity makes it redundant
  post-reboot` comment on line 42 is inside a double-quoted YAML string (lines
  31-46). YAML preserves `#` as a literal character inside double quotes. When
  YAML folds the multiline string, the `#` text becomes part of the Jinja2
  `{{ ... }}` expression, causing `TemplateSyntaxError: unexpected char '#' at
  661`. Confirmed with `jinja2.Environment().parse()` and `ansible-lint`. All
  Molecule tests skip this code path because `system-container-overrides.yml`
  sets `system_is_container: true`. A real host `make all` (or `make test-vm`)
  would crash the system role at this task, preventing all sysctl hardening and
  subsequent system tasks.
- **Evidence**: Introduced in commit `211c5363`, reworded in 5 subsequent
  commits, each time preserving the `#` inside the double-quoted string.
- **Fix**: Move the inline comment to a standalone YAML comment line above the
  `set_fact` task (outside the double-quoted string), or delete it entirely.

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

#### 25. ptrace_scope=0 allows unrestricted process attach

- **Dimension**: kernel-boot-hardening
- **File**: `group_vars/all/vars.yml:94`,
  `roles/system/defaults/main.yml:451`, `roles/system/tasks/sysctl.yml:40`
- **Current state**: `kernel.yama.ptrace_scope = 0` — any process can attach to
  any other same-UID process (read/modify memory of SSH agents, browsers,
  password managers).
- **Target**: `system_ptrace_scope: 1` deployed via
  `/etc/sysctl.d/90-hardening.conf` (CIS 1.6.3 — parent-only attach).
- **Risk**: With ptrace_scope=0, a compromised process can read secrets from
  any co-user process. The automation correctly sets value 1 (parent-child only);
  normal `gdb` child-process debugging is unaffected.
- **Status**: Automation handles this correctly. Gap exists only on
  pre-provisioned machines. No code change needed — confirms `make all` closes
  this gap.

#### 37. No proxy forwarding into distrobox container

- **Dimension**: container-provisioning
- **File**: `roles/distrobox/templates/distrobox.ini.j2`,
  `common/tasks/container-provision-tasks.yml`
- **Problem**: `csb_detect.yml` detects `network_proxied` and `proxy_url` from
  host env vars (HTTP_PROXY, HTTPS_PROXY), but `distrobox.ini.j2` does not pass
  these as environment variables. On a CSB hybrid machine behind a corporate
  proxy, dnf installs, `go install`, and `oc` downloads inside the container
  fail. Distrobox normally inherits the host env, but Ansible Play 3's
  `containers.podman.podman` connection runs `podman exec` directly, bypassing
  distrobox's env inheritance. Host-side equivalents (`install_go_tools.yml`,
  `install_subctl_versions.yml`) explicitly set HTTPS_PROXY/HTTP_PROXY, but the
  container-side `go install` and `uri`/`get_url` tasks have no proxy
  environment block. Additionally, the `add_host` registration does not forward
  `network_proxied` or `proxy_url` to the container host.
- **Fix**: Add `http_proxy`, `https_proxy`, `no_proxy` environment variables to
  `distrobox.ini.j2` `init_hooks` or `additional_flags` when
  `network_proxied` is true. Add `environment:` blocks to `go install` and
  `uri` tasks in `container-provision-tasks.yml`. Forward `network_proxied` and
  `proxy_url` via the `add_host` registration.

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

#### 26. ICMP redirect sending enabled (workstation is not a router)

- **Dimension**: kernel-boot-hardening
- **File**: `roles/system/defaults/main.yml:21-22`,
  `roles/system/tasks/sysctl.yml`
- **Current state**: `net.ipv4.conf.all.send_redirects = 1` (kernel default) —
  host sends ICMP redirect packets, which can be abused for MITM attacks.
- **Target**: `net.ipv4.conf.all.send_redirects: 0` and
  `net.ipv4.conf.default.send_redirects: 0` in `system_sysctl_hardening` dict,
  deployed via `/etc/sysctl.d/90-hardening.conf` (CIS).
- **Risk**: ICMP redirect sending on a workstation that is not a router serves
  no purpose and expands the attack surface. Zero UX impact from disabling.
- **Status**: Automation handles this correctly. Smoke-test verifies both `all`
  and `default` send_redirects are 0. Gap exists only on pre-provisioned
  machines. No code change needed.

#### 27. `make bootstrap` requires make and git not installed on fresh Fedora

- **Dimension**: day-1-onboarding-friction
- **File**: `Makefile:60-77`, `CLAUDE.md` (step 1)
- **Problem**: The bootstrap target runs `sudo dnf install -y ansible-core git
  ykpers make ShellCheck`, but invoking `make bootstrap` requires `make` (and
  `git` for the initial clone) to already be present. Fedora 44 Workstation does
  not ship either. The Makefile has a comment on line 58 noting the workaround
  (`sudo dnf install -y make ShellCheck`) but CLAUDE.md step 1 jumps straight to
  `make bootstrap` with no mention of the prerequisite.
- **Fix**: Add a pre-bootstrap step to CLAUDE.md telling users to run
  `sudo dnf install -y make git` before anything else. Consider a `bootstrap.sh`
  wrapper script that handles the chicken-and-egg problem.

#### 29. SSHD rescue block cannot undo firewall port mismatch

- **Dimension**: partial-failure-resilience
- **File**: `roles/system/tasks/sshd.yml:90-127,152-179`,
  `roles/system/tasks/hardening_core.yml:47`
- **Problem**: firewall.yml runs before sshd.yml, setting the drop zone with
  only port 722 open (immediate+permanent). If the SELinux seport task at
  sshd.yml line 157 fails, the rescue block removes 00-hardening.conf so sshd
  reverts to port 22, which the firewall blocks. No force_handlers is set, so
  the Restart sshd handler does not fire and the state persists across reboot.
  The rescue is the correct choice (without it, sshd fails to bind port 722
  under SELinux enforcing without the port label — total lockout), but it leaves
  a split-brain state with no firewall revert or warning.
- **Fix**: When the sshd rescue block removes 00-hardening.conf, also revert the
  firewall to allow port 22 in the drop zone, or emit a warning that the machine
  is in a split-brain state (firewall expects 722, sshd serves 22).

#### 30. Parallel source trees from GOPATH migration

- **Dimension**: migration-friction
- **File**: `roles/git_repos/tasks/main.yml`
- **Problem**: 106 repos exist in ~/go/src/ (6.6 GB) using the pre-modules
  GOPATH convention. `make repos` clones to ~/src/ with a category-prefixed
  layout, producing two parallel source trees with ~16 overlapping project
  repos at different paths. No migration, cleanup, or warning exists in the
  automation.
- **Fix options**:
  - A: Add a one-time migration task that detects ~/go/src/ and prints a
    cleanup checklist (audit for unmerged branches, push unpushed work, archive)
  - B: Document the parallel-tree situation in default.config.yml or CLAUDE.md
  - C: Both — automated detection with a migration guide

#### 31. Sway exit binding has no confirmation dialog

- **Dimension**: ux-transition
- **File**: `roles/desktop/templates/sway.config.j2:118`,
  `roles/desktop/templates/i3.config.j2:88`
- **Problem**: i3 binds Mod+Shift+e to `i3-nagbar` with a confirmation dialog.
  Sway binds the same key to bare `swaymsg exit` with no confirmation.
  Accidentally hitting Mod+Shift+e immediately kills the session — all unsaved
  work in GUI apps is lost. The upstream Sway default config uses `swaynag` for
  this binding; this project actively deviates by omitting confirmation.
- **Fix**: Add a confirmation wrapper:
  `bindsym $mod+Shift+e exec swaynag -t warning -m 'Exit Sway?' -B 'Yes' 'swaymsg exit'`

#### 33. No Molecule scenario tests desktop_environment=gnome

- **Dimension**: testing
- **File**: `molecule/` (all scenarios), `roles/desktop/tasks/main.yml:333-354`
- **Problem**: No Molecule scenario sets `desktop_environment` to `gnome`. The
  desktop role has a GNOME hardening block (dconf settings for auto-mount,
  screensaver lock, etc.) that is never exercised. Fedora tests sway and i3;
  Rocky/Debian/VM all use `unknown`; macOS uses `aerospace`. The GNOME path
  could silently break. The block uses `ignore_errors: true`, which prevents
  playbook breakage but also means failures are completely silent — a
  refactoring error in variable names, dconf key paths, or uint32 value syntax
  would go undetected.
- **Fix**: Add a third desktop role `include_role` pass in the Fedora scenario
  with `desktop_environment: gnome`, or create a dedicated GNOME scenario.

#### 34. `git_repos_pull=true` path (make update) never tested

- **Dimension**: testing
- **File**: `roles/git_repos/defaults/main.yml:9`,
  `roles/git_repos/tasks/main.yml:173-188`
- **Problem**: `git_repos_pull` defaults to `false`. No Molecule scenario,
  shared CI variable file, or GitHub Actions workflow ever sets it to `true`.
  The only place it is set to `true` is the manual `make update` Makefile
  target, which is not part of automated testing. The three tasks gated by
  `git_repos_pull | bool` (ff-only pull, failure reporter, failure assertion)
  have zero CI coverage.
- **Fix**: Add a second `git_repos` role pass in an existing scenario with
  `git_repos_pull: true`, or create a dedicated scenario that clones then
  pulls.

#### 38. Git hooks directory not write-denied for Claude

- **Dimension**: deny-list-completeness
- **File**: `roles/claude/defaults/main.yml` (`claude_deny_write_only`),
  `roles/dotfiles/templates/gitconfig.j2:79`
- **Problem**: `~/.config/git/template/hooks/**` is absent from both
  `claude_deny_sensitive` and `claude_deny_write_only`. `core.hooksPath` in
  `gitconfig.j2` (line 79) points to this directory. Ansible deploys four hooks
  there (pre-commit with gitleaks, commit-msg, prepare-commit-msg, pre-push).
  Claude could overwrite them via Edit/Write tools — disabling gitleaks
  (allowing secrets to be committed) or injecting arbitrary code that executes
  with full user privileges on every git commit/push across all repos. Commit
  `45195885` added `~/.gitconfig` and `~/.config/git/config` to the deny lists
  to prevent `core.hooksPath` redirection, but this does not prevent overwriting
  the hook files at the existing path.
- **Fix**: Add `~/.config/git/template/hooks/**` to `claude_deny_write_only` in
  `roles/claude/defaults/main.yml`.

#### 39. hadolint (Dockerfile linter) not installed

- **Dimension**: notes-mining
- **File**: `roles/packages/tasks/install_standalone_binaries.yml`
- **Problem**: hadolint is labeled "Priority: Must-have" in the MCN content-type
  tooling doc (`notes-ai/mcn/2026-05-19-content-type-tooling.md`) and marked
  "Yes" for Phase 1 adoption in the tooling proposal. Not installed on the
  workstation. The packages role already installs analogous linting tools
  (actionlint, zizmor, gitleaks, shellcheck, yamllint) via
  `install_standalone_binaries.yml` and `install_pipx.yml`, establishing a clear
  pattern that hadolint fits.
- **Fix**: Add hadolint as a SHA256-verified binary download in
  `install_standalone_binaries.yml` (work profile, Linux x86_64+arm64),
  following the existing pattern.

#### 42. AIDE dnf-automatic update hook ignores common exit codes

- **Dimension**: aide-file-integrity-monitoring
- **File**: `roles/system/tasks/dnf_automatic.yml` (aide-update ExecStartPost)
- **Problem**: The `aide --update` post-hook promotes the new database only on
  exit codes 0 (no changes) and 7 (all three change types). AIDE uses a
  bitmask: bit 0=new(1), bit 1=removed(2), bit 2=changed(4). A typical dnf
  security update that only modifies existing binaries produces exit code 4
  (changed) or 5 (new+changed), neither of which triggers the `mv` command.
  The database is not promoted, so the next daily `aide --check` flags the
  legitimate package changes as violations — exactly the false positives the
  hook was designed to prevent.
- **Fix**: Expand the case statement to accept all non-error AIDE exit codes
  (0-7): `case $? in 0|[1-7]) mv ... ;; esac`. Codes >= 14 indicate AIDE
  internal errors and should still skip promotion.

#### 43. Smoke test chrony-nts config check hardcodes wrong path on Debian

- **Dimension**: chrony-nts-dns-interaction
- **File**: `scripts/smoke-test.sh:1163`,
  `roles/system/tasks/chrony.yml:25`
- **Problem**: The NTS config grep hardcodes `/etc/chrony.conf` but
  `chrony.yml` deploys to `/etc/chrony/chrony.conf` on Debian (`is_apt`). On
  Debian the grep silently fails (file does not exist at that path), emitting a
  false WARN "NTS not configured in chrony.conf" even when NTS is correctly
  deployed. The chronyd service name check at line 1170 handles Debian
  correctly, demonstrating the config path check was simply missed. The same
  inconsistency exists in `molecule/shared/verify-common.yml:1069` but is
  masked in CI because `system_chrony_service_enabled` defaults to false in
  containers.
- **Fix**: Add a path variable before the NTS config grep:
  `_chrony_conf=/etc/chrony.conf; grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && _chrony_conf=/etc/chrony/chrony.conf`

#### 44. Logind settings mostly unverified by smoke test and Molecule

- **Dimension**: logind-lid-power-behavior
- **File**: `scripts/smoke-test.sh:1657-1663`,
  `molecule/shared/verify-common.yml:794`,
  `molecule/vm/verify.yml:799`,
  `roles/system/tasks/logind.yml`
- **Problem**: `logind.yml` deploys 8 keys via a single `ini_file` loop, but
  only 2 are verified anywhere: smoke-test checks `IdleAction=lock` and
  `IdleActionSec=`; Molecule verify-common checks `IdleAction=lock`; VM verify
  checks `IdleActionSec=`. The remaining 6 keys (`HandleLidSwitch`,
  `HandleLidSwitchExternalPower`, `HandleLidSwitchDocked`,
  `HandleHibernateKey`, `HandlePowerKey`, `KillUserProcesses`) are never
  verified. A value regression (e.g., `HandlePowerKey` changed from `lock` to
  `poweroff`, or `KillUserProcesses` from `no` to `yes`) would pass all tests
  silently.
- **Fix**: Add smoke-test checks and/or Molecule verify assertions for at
  minimum `HandleLidSwitch`, `HandlePowerKey`, and `KillUserProcesses` — the
  three most impactful settings (lid-close suspend, power-button lock, process
  survival on logout).

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

#### 28. Mullvad VPN install attempted without repo on CSB hybrid

- **Dimension**: hybrid-csb-day1-experience
- **File**: `roles/repos_dnf/tasks/main.yml:173-175`,
  `roles/system/tasks/services.yml:67-76`
- **Problem**: The repos_dnf role skips the Mullvad repo with `not csb_detected`,
  but the system role's services.yml attempts `dnf install mullvad-vpn` guarded
  only by `is_fedora` — no `csb_detected` check. On CSB hybrid Fedora the
  install soft-fails and emits "VPN protection is not active on this host," which
  is misleading when GlobalProtect is the intended corporate VPN (installed by
  the redhat role).
- **Fix**: Add `not csb_detected` guard to the Mullvad VPN install block in
  `roles/system/tasks/services.yml` to match the repos_dnf guard, or make the
  warning message context-aware for CSB machines.

#### 32. Firewall-to-sshd ordering gap on first provision

- **Dimension**: partial-failure-resilience
- **File**: `roles/system/tasks/hardening_core.yml:47`,
  `roles/system/tasks/main.yml:54`
- **Problem**: firewall.yml runs as step 5 in hardening_core.yml, setting the
  default zone to drop and opening only port 722/tcp (immediate+permanent).
  sshd.yml runs as step 15 in main.yml. If any task between steps 5-14 causes
  a hard failure, the play aborts with firewall=drop+port-722-open but sshd
  still on port 22. On reboot the machine is SSH-inaccessible from the network.
- **Mitigating factors**: CLAUDE.md requires running from local console (not
  SSH), so the operator has physical access. On re-provisions sshd is already
  on 722. Many intermediate tasks use `failed_when: false`. Console recovery is
  trivial (`firewall-cmd --add-port=22/tcp`).
- **Fix**: Consider reordering sshd.yml to run immediately after firewall.yml,
  or keep port 22 open in the drop zone until sshd.yml confirms the port
  switch.

#### 35. SELinux enforcement not verified in any CI scenario

- **Dimension**: testing
- **File**: `molecule/shared/system-container-overrides.yml:38-39`,
  `molecule/vm/verify.yml:568-579`
- **Problem**: `system-container-overrides.yml` sets
  `system_selinux_enforcing_required=false` with an explicit "CI gap" comment.
  Only the VM scenario (`make test-vm`, local-only, not in CI) checks
  `getenforce==Enforcing`. A regression in SELinux tasks in
  `hardening_core.yml` would pass all CI checks. This is a well-documented,
  inherent limitation of container-based testing (Podman containers cannot
  enforce their own SELinux policy), not an oversight.
- **Fix**: Acknowledge as a known CI limitation. Ensure `make test-vm` is run
  before releases.

#### 40. Claude Code not installed inside distrobox container

- **Dimension**: container-provisioning
- **File**: `common/tasks/container-provision-tasks.yml`, `site.yml`
- **Problem**: The claude role runs in Play 2 (host only).
  `container-provision-tasks.yml` installs oc, kubectl, golangci-lint, subctl,
  and dev packages but not Claude Code. On CSB restricted machines (fapolicyd
  enforcing) where `troubleshooting.md` (line 329) recommends installing Claude
  Code inside the distrobox container, there is no automation. The claude role's
  rescue block handles this gracefully via `csb-failure-handler.yml`, recording
  the failure with manual install instructions — consistent with how all
  CSB-restricted tool installs are handled across the project.
- **Fix**: Consider adding a Claude Code install step to
  `container-provision-tasks.yml` gated on `claude_install_method != 'skip'` and
  `packages_install_binaries`, or document the manual step in the
  post-provisioning checklist.

#### 41. grype not provisioned by automation

- **Dimension**: notes-mining
- **File**: `roles/packages/tasks/install_standalone_binaries.yml`
- **Problem**: grype (Anchore container vulnerability scanner) v0.112.0 is
  installed manually at `/usr/local/bin/grype` but is not provisioned by any
  role or task. The CVE agent uses grype for scanning container images across
  Submariner release branches. A clean reprovisioned machine would fall back to
  the container path (`anchore/grype:latest` via Podman), which works but is
  slower. The `run_grype()` function in `lib.sh` implements this container
  fallback, so the CVE pipeline would not break — only lose the performance
  benefit of the local binary.
- **Fix**: Add grype as a SHA256-verified binary download in
  `install_standalone_binaries.yml` (work profile, Linux x86_64+arm64),
  following the gitleaks/actionlint/zizmor pattern.

#### 45. No restore script or documented restore procedure

- **Dimension**: backup-readiness
- **File**: `scripts/backup.sh`, `Makefile`
- **Problem**: The backup creates a timestamped directory
  (`~/laptop-setup-backup-YYYYMMDD-HHMMSS/`) mirroring the source tree, but
  there is no `make restore` target, no `restore.sh` script, and no
  documentation explaining how to transfer the backup to a new machine and
  place files back. For cross-machine migration (X1C7 to P16v), the user must
  manually rsync/scp the backup and copy subtrees back. In practice the backup
  directory mirrors `$HOME` paths exactly, making restore a one-liner
  (`rsync -a $BACKUP_DIR/ ~/`), and `make all` regenerates all
  template-managed files — the backup only matters for gitignored/unmanaged
  content (SSH keys, vault.yml, config.yml, credentials).
- **Fix**: Add a brief restore procedure to `references/troubleshooting.md`
  covering: rsync backup to new machine, restore config.yml and vault-pass.sh
  first, then `make all`. A `restore.sh` script is optional given the trivial
  restore path.

#### 46. uBlock Origin MV2 missing from Chrome ExtensionInstallAllowlist

- **Dimension**: chrome-security-policy
- **File**: `roles/desktop/files/chrome-security-policy.json:4-7`
- **Problem**: uBlock Origin MV2 (`cjpalhdlnbpafiamejdnhcphjbkeiagm`) is
  absent from the `ExtensionInstallAllowlist`. The research doc at
  `notes-ai/laptop-setup/2026-05-29-chrome-security-hardening.md` recommends
  both MV2 and MV3 in the allowlist (line 254). The wildcard blocklist (line 2)
  blocks any extension not explicitly allowlisted. uBlock Origin Lite MV3 IS
  force-installed via `ExtensionInstallForcelist`, so ad/tracker blocking is
  present out of the box. The deployed policy was evolved well beyond the
  research doc (adding 10+ additional policies), suggesting MV2 omission may
  be an intentional forward-looking choice. The research doc itself labels
  MV3 Lite as the MV2 replacement and notes MV2 "will eventually stop
  working."
- **Fix**: Add `cjpalhdlnbpafiamejdnhcphjbkeiagm` to
  `ExtensionInstallAllowlist` if MV2's more capable dynamic filtering is
  desired. Otherwise, document the intentional MV3-only decision.

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

---

## Iteration 1

**Date**: 2026-08-11

**Added**:

- **Item 25** (P2): `ptrace_scope=0 allows unrestricted process attach` —
  pre-provisioning security gap where `kernel.yama.ptrace_scope=0` allows any
  same-UID process to read memory of SSH agents, browsers, and password
  managers. Automation correctly sets value 1 (CIS 1.6.3). No code change
  needed; confirms `make all` closes this gap.
- **Item 26** (P3): `ICMP redirect sending enabled` — pre-provisioning gap
  where `net.ipv4.conf.all.send_redirects=1` allows ICMP redirect sending on a
  workstation that is not a router. Automation correctly disables this via
  `system_sysctl_hardening` dict. No code change needed.

**Rationale**: Both findings are confirmed pre-provisioning sysctl hardening
gaps on the old laptop. The automation already handles them correctly — they
document real security exposure that `make all` closes, validating the sysctl
hardening pipeline (`vars.yml` -> `sysctl.yml` -> `90-hardening.conf`
template).

---

## Iteration 2

**Date**: 2026-08-11

**Added**:

- **Item 27** (P3): `make bootstrap requires make and git not installed on fresh
  Fedora` — chicken-and-egg problem where the bootstrap target needs `make` to
  run but installs it as part of the target. CLAUDE.md step 1 does not mention
  the prerequisite. Makefile has a code comment workaround but nothing
  user-facing.
- **Item 28** (P4): `Mullvad VPN install attempted without repo on CSB hybrid` —
  inconsistent guards between repos_dnf (`not csb_detected`) and
  system/services.yml (no CSB check) cause a misleading "VPN not active" warning
  on hybrid machines where GlobalProtect is the intended VPN. Cosmetic only — the
  soft-fail path works correctly with no security or functionality impact.

---

## Iteration 3

**Date**: 2026-08-11

**Added**:

- **Item 29** (P3): `SSHD rescue block cannot undo firewall port mismatch` —
  if the SELinux seport task fails in sshd.yml, the rescue block correctly
  removes 00-hardening.conf (preventing total lockout from sshd failing to bind
  under SELinux), but leaves a split-brain state where the firewall allows only
  port 722 while sshd reverts to port 22. No firewall revert or warning emitted.
- **Item 30** (P3): `Parallel source trees from GOPATH migration` — 106 repos
  (6.6 GB) in ~/go/src/ using pre-modules GOPATH layout will coexist with the
  new ~/src/ category-prefixed layout after `make repos`. ~16 overlapping repos
  cloned to different paths with no migration task, cleanup, or warning.
- **Item 31** (P3): `Sway exit binding has no confirmation dialog` — i3 uses
  i3-nagbar confirmation on Mod+Shift+e but Sway binds the same key to bare
  `swaymsg exit`. Upstream Sway default config uses swaynag; this project
  deviates by omitting the confirmation step.
- **Item 32** (P4): `Firewall-to-sshd ordering gap on first provision` —
  temporal gap between firewall hardening (step 5, drop zone with port 722
  only) and sshd reconfiguration (step 15, port 22 to 722) means a hard
  failure in between leaves the machine SSH-inaccessible from the network.
  Mitigated by console-only provisioning requirement and trivial recovery.

**Rationale**: Two partial-failure-resilience findings document real ordering
dependencies in the system role's firewall-to-sshd pipeline — one in the
happy path (item 32, low severity due to console requirement) and one in the
sshd rescue path (item 29, medium severity as it produces a silent
split-brain). The migration-friction finding (item 30) documents the GOPATH
to ~/src/ parallel-tree gap. The ux-transition finding (item 31) captures a
missing safety net in the i3-to-Sway migration.

---

## Iteration 5

**Date**: 2026-08-11

**Added**:

- **Item 33** (P3): `No Molecule scenario tests desktop_environment=gnome` —
  the desktop role's GNOME hardening block (dconf settings for auto-mount,
  screensaver lock, idle timeout, location services, recent files) is never
  exercised in any Molecule scenario. Fedora tests sway and i3; Rocky/Debian/VM
  use `unknown`; macOS uses `aerospace`. The block uses `ignore_errors: true`,
  so a refactoring error in dconf key paths or value syntax would be completely
  silent.
- **Item 34** (P3): `git_repos_pull=true path never tested` — the `make update`
  pull path (`git pull --ff-only` + failure reporter + failure assertion) has
  zero CI coverage. `git_repos_pull` defaults to `false` and no Molecule
  scenario or workflow ever sets it to `true`.
- **Item 35** (P4): `SELinux enforcement not verified in any CI scenario` —
  `system-container-overrides.yml` disables `system_selinux_enforcing_required`
  with an explicit "CI gap" comment. Only the local-only VM scenario checks
  `getenforce==Enforcing`. Known inherent limitation of Podman-based testing,
  not an oversight.

**Rationale**: Three testing-dimension findings document CI coverage gaps. The
GNOME and git_repos_pull gaps (items 33-34) are actionable with new Molecule
scenario passes. The SELinux gap (item 35) is an inherent container-based
testing limitation already documented in the codebase; mitigation is ensuring
`make test-vm` runs before releases.

---

## Iteration 6

**Date**: 2026-08-11

**Added**:

- **Item 36** (P1): `Jinja2 parse error in sysctl.yml` — inline `#` comment
  inside a double-quoted YAML string (lines 31-46) is preserved as a literal
  character by YAML and injected into the Jinja2 expression, causing
  `TemplateSyntaxError`. All Molecule tests skip this path
  (`system_is_container: true`). A real host `make all` crashes the system role,
  preventing all sysctl hardening. Introduced in commit `211c5363`, persisted
  through 5 rewrites.
- **Item 37** (P2): `No proxy forwarding into distrobox container` — on CSB
  hybrid machines behind a corporate proxy, the `containers.podman.podman`
  connection bypasses distrobox's env inheritance. `distrobox.ini.j2` has no
  proxy env vars, and container-side `go install`/`uri`/`get_url` tasks lack
  proxy environment blocks (unlike their host-side equivalents). All container
  network operations fail behind a proxy.
- **Item 38** (P3): `Git hooks directory not write-denied for Claude` —
  `~/.config/git/template/hooks/**` is absent from both `claude_deny_sensitive`
  and `claude_deny_write_only`. Commit `45195885` blocked `core.hooksPath`
  redirection but not overwriting the deployed hooks (gitleaks pre-commit,
  commit-msg, pre-push) at the existing path. Defense-in-depth gap requiring
  active Edit/Write tool misuse.
- **Item 39** (P3): `hadolint not installed` — labeled "Must-have" in MCN
  content-type tooling doc and "Yes" for Phase 1 adoption. Not provisioned
  despite the packages role installing analogous linters (actionlint, zizmor,
  gitleaks, shellcheck, yamllint) via the same pattern.
- **Item 40** (P4): `Claude Code not installed inside distrobox container` —
  the claude role runs host-only (Play 2). On CSB restricted machines
  (fapolicyd enforcing), `troubleshooting.md` recommends container install but
  no automation exists. The rescue block handles this gracefully with manual
  install instructions, consistent with all CSB-restricted tool handling.
- **Item 41** (P4): `grype not provisioned by automation` — manually installed
  at `/usr/local/bin/grype` but absent from any role. The CVE agent's
  `run_grype()` has a Podman container fallback (`anchore/grype:latest`), so
  the pipeline would not break on a clean machine — only lose the performance
  benefit of the local binary.

**Rationale**: Six findings across four dimensions. The critical sysctl.yml
Jinja2 parse error (item 36) is a real host-only crash bug invisible to
container-based CI. The proxy forwarding gap (item 37) blocks all container
network operations on proxied CSB hybrid machines. The Claude deny-list gap
(item 38) is a defense-in-depth hardening miss. The remaining three (items
39-41) are missing tool provisioning — two convenience improvements and one
MCN tooling alignment.

---

## Iteration 7

**Date**: 2026-08-11

**Added**:

- **Item 42** (P3): `AIDE dnf-automatic update hook ignores common exit codes`
  — the `aide --update` post-hook case statement only promotes the new database
  on exit codes 0 and 7. AIDE uses a bitmask (1=new, 2=removed, 4=changed), so
  a typical dnf update producing exit code 4 (changed files only) or 5
  (new+changed) skips promotion. The next daily `aide --check` then flags
  legitimate package changes as violations, producing false positives that
  train operators to ignore real alerts.
- **Item 43** (P3): `Smoke test chrony-nts config check hardcodes wrong path
  on Debian` — smoke-test.sh line 1163 hardcodes `/etc/chrony.conf` but
  `chrony.yml` deploys NTS config to `/etc/chrony/chrony.conf` on Debian
  (`is_apt`). Produces a false WARN on Debian even when NTS is correctly
  deployed. The service-name check at line 1170 handles Debian correctly,
  demonstrating the config-path check was simply missed.
- **Item 44** (P3): `Logind settings mostly unverified by smoke test and
  Molecule` — `logind.yml` deploys 8 keys but only `IdleAction` and
  `IdleActionSec` are verified anywhere. The remaining 6 keys
  (`HandleLidSwitch`, `HandleLidSwitchExternalPower`, `HandleLidSwitchDocked`,
  `HandleHibernateKey`, `HandlePowerKey`, `KillUserProcesses`) have zero test
  coverage. A regression in `HandlePowerKey` or `KillUserProcesses` could
  cause data loss and pass all tests silently.
- **Item 45** (P4): `No restore script or documented restore procedure` — the
  backup creates a timestamped directory mirroring `$HOME` paths but no
  `make restore`, `restore.sh`, or documentation explains the restore path.
  In practice the restore is a trivial `rsync -a` and `make all` regenerates
  template-managed files, so this is a documentation convenience gap rather
  than a functional risk.
- **Item 46** (P4): `uBlock Origin MV2 missing from Chrome
  ExtensionInstallAllowlist` — MV2 is absent from the allowlist despite the
  research doc recommending both MV2 and MV3. MV3 Lite IS force-installed, so
  ad blocking is present. The omission appears intentional given the policy was
  evolved well beyond the research doc and MV2 is being deprecated.

**Rationale**: Five findings across five dimensions. The AIDE exit code bug
(item 42) degrades file integrity monitoring by producing false positives
after routine updates. The chrony path mismatch (item 43) and logind test
gap (item 44) are test coverage blind spots — one Debian-specific, one
cross-platform. The backup restore gap (item 45) is a minor documentation
miss given the trivial restore path. The Chrome extension finding (item 46)
documents a likely-intentional policy decision that should be explicitly
acknowledged.
