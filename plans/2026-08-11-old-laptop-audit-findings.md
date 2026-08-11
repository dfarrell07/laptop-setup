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

#### 47. verify-sway.yml aborts on undefined `desktop_sway_adaptive_sync`

- **Dimension**: molecule-verify-false-pass
- **File**: `molecule/shared/verify-sway.yml:39,46`,
  `molecule/fedora/verify.yml:6-19`
- **Problem**: Line 39 uses `{{ desktop_sway_adaptive_sync }}` and line 46 uses
  `desktop_sway_libva_driver` in a `when` clause, but the Fedora verify
  playbook only loads `group_vars/all/vars.yml` in `pre_tasks` — it does NOT
  load `default.config.yml` or role defaults. Both variables are defined
  exclusively in `roles/desktop/defaults/main.yml`, which is never loaded
  during the verify phase (verify uses `import_tasks`, not `include_role`).
  The undefined variable at line 39 raises `AnsibleUndefinedVariable`, halting
  the verify playbook. All subsequent sway assertions (touchpad, portal
  routing, GTK theme, terminal.conf) plus all post-sway checks (i3, bpfman,
  Claude queue, distrobox) are never executed — molecule fedora sway
  verification coverage is zero.
- **Fix**: Either load `default.config.yml` in the Fedora verify `pre_tasks`
  (like the Debian verify does), or replace the Jinja2 variable interpolation
  with hardcoded expected values matching role defaults.

#### 63. VM verify.yml missing include_vars for group_vars/all/vars.yml

- **Dimension**: jinja2-undefined-variable-risks
- **File**: `molecule/vm/verify.yml:7-22`
- **Problem**: VM verify pre_tasks set ~12 variables via `set_fact` but never
  load `group_vars/all/vars.yml` or `default.config.yml`. The fedora, rocky,
  and debian verify files all load `group_vars/all/vars.yml`; the VM verify
  does not. Molecule does not auto-load project group_vars. Imported shared
  verify files require variables defined exclusively in `group_vars/all/vars.yml`:
  `system_ptrace_scope` (verify-common.yml:511, in a loop list with no
  default), `system_auditd_max_log_file` (verify-common.yml:977),
  `system_disable_avahi` (verify-smoke.yml:210), `repo_tailscale`
  (verify-dnf.yml:144), `repo_google_chrome` (vm/verify.yml:49).
- **Risk**: `make test-vm` verify phase crashes at the first unprotected
  undefined variable — `system_ptrace_scope` at verify-common.yml:511.
- **Fix**: Add `include_vars` for `group_vars/all/vars.yml` (and optionally
  `default.config.yml`) to VM verify `pre_tasks`, matching the pattern used
  by fedora, rocky, and debian verify files.

#### 65. verify-claude-queue.yml unconditional systemd unit assertions fail in container CI

- **Dimension**: molecule-converge-config
- **File**: `molecule/shared/verify-claude-queue.yml:7-29`,
  `roles/claude/tasks/main.yml:208-212`
- **Problem**: `verify-claude-queue.yml` unconditionally asserts that
  `claude-queue.service` and `claude-queue.timer` exist with mode 0644.
  However, the claude role guards systemd unit deployment with
  `not (system_is_container)`, and `system-container-overrides.yml` sets
  `system_is_container: true` in all Podman CI scenarios. The service and
  timer files are never deployed, so these assertions fail in both fedora
  and rocky verify. The poller script and `queue-repos.conf` assertions
  (lines 31-53) would pass since their deploy tasks lack the
  `system_is_container` guard. File exists only on wip1 (commit `44ddd2a6`),
  not yet CI-tested.
- **Fix**: Gate the `.service`/`.timer` assertions with
  `when: not (system_is_container | default(false))`, or split into separate
  includes for container-safe and host-only assertions.

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

#### 48. Smoke test AllowTcpForwarding regex rejects documented `yes` value

- **Dimension**: smoke-test-logic-bugs
- **File**: `scripts/smoke-test.sh:924`,
  `roles/system/templates/sshd-hardening.conf.j2:33`
- **Problem**: The PCRE pattern `P:AllowTcpForwarding:(no|local|remote)` does
  not include `yes` or `all`. The Ansible assertion at `sshd.yml:40` explicitly
  accepts `['yes', 'no', 'all', 'local', 'remote']`, the template passes the
  value verbatim (`AllowTcpForwarding {{ system_ssh_allow_tcp_forwarding }}`),
  and both CLAUDE.md and `default.config.yml` document `yes` as valid for
  enabling both `-L` and `-R` forwarding. A user who follows the docs produces
  a false FAIL: "AllowTcpForwarding directive wrong or absent."
- **Fix**: Change the regex to
  `P:AllowTcpForwarding:(no|local|remote|yes|all)`. Also review
  `AllowAgentForwarding` (line 923, hardcoded to `no`) given CLAUDE.md
  documents `yes` as valid.

#### 49. verify-sway.yml stale touchpad assertions after opt-in defaults change

- **Dimension**: molecule-verify-false-pass
- **File**: `molecule/shared/verify-sway.yml:143-150`,
  `roles/desktop/defaults/main.yml` (commit `8dcded93`)
- **Problem**: Commit `8dcded93` changed `desktop_sway_touchpad_tap` and
  `desktop_sway_touchpad_natural_scroll` from `enabled` to empty string `''`.
  The sway template conditionally emits these directives:
  `{% if desktop_sway_touchpad_tap != '' %}tap {{ ... }}{% endif %}`. With
  empty defaults, neither `tap enabled` nor `natural_scroll enabled` appears
  in the rendered config. The verify assertions at lines 143-146 and 148-150
  unconditionally grep for `'tap enabled'` and `'natural_scroll enabled'` (no
  `when` clause), so they would false-fail. Currently masked by the
  undefined-variable abort from item 47 above. Additionally,
  `default.config.yml` lines 316-317 still document the defaults as `enabled`,
  meaning the documentation was not updated alongside the defaults change.
- **Fix**: Gate these checks with
  `when: desktop_sway_touchpad_tap | default('') | length > 0` or remove them
  since touchpad settings are now opt-in. Update `default.config.yml`
  documentation to reflect the empty-string defaults.

#### 50. verify-common.yml sysctl checks verify key presence without values

- **Dimension**: molecule-verify-false-pass
- **File**: `molecule/shared/verify-common.yml:518-522`
- **Problem**: The sysctl hardening config loop checks five keys for KEY
  PRESENCE only: `net.ipv6.conf.all.use_tempaddr`,
  `net.ipv4.conf.all.rp_filter`, `net.ipv4.conf.default.rp_filter`,
  `net.bridge.bridge-nf-call-iptables`, `vm.max_map_count`. Compare to other
  items in the same loop that DO verify values: `kernel.dmesg_restrict = 1`,
  `kernel.kptr_restrict = 1`, `kernel.yama.ptrace_scope = {{ ... }}`. If
  `use_tempaddr` changes from 2 to 0 (disabling IPv6 privacy extensions) or
  `rp_filter` changes from 2 to 0 (disabling reverse-path filtering), the
  key-only grep still matches and the assertion passes. Additionally,
  `system_sysctl_extra_additional` (applied last in the combine chain at line
  46 of `sysctl.yml`) can silently override any of these values undetected.
- **Fix**: Add value assertions for security-relevant keys: change
  `net.ipv6.conf.all.use_tempaddr` to `net.ipv6.conf.all.use_tempaddr = 2`
  and `net.ipv4.conf.all.rp_filter` to `net.ipv4.conf.all.rp_filter = 2`.

#### 55. Molecule path filter omits `.github/actions/` directory

- **Dimension**: ci-workflows
- **File**: `.github/workflows/molecule.yml:45`
- **Problem**: The path-filter regex `^(\.github/workflows/)` matches
  `.github/workflows/` but NOT `.github/actions/`. The composite action at
  `.github/actions/molecule-setup/action.yml` installs Python, pip deps, and
  Ansible collections for all 9 molecule matrix jobs. A PR modifying only files
  under `.github/actions/` would not trigger molecule tests — a broken
  molecule-setup change (wrong Python version, bad requirements path) could
  merge without molecule coverage and break all 9 scenarios on the next
  unrelated PR.
- **Fix**: Add `\.github/actions/` to the path filter regex.

#### 56. VM verify cramfs assertion stale after template removal

- **Dimension**: modprobe-hardening
- **File**: `molecule/vm/verify.yml:535-538`
- **Problem**: The VM verify test asserts `grep -q cramfs
  /etc/modprobe.d/hardening.conf` but cramfs was intentionally removed from
  `modprobe-hardening.conf.j2` in commit `ab7c166d`. The grep returns exit
  code 1 and Ansible treats it as a task failure. The shared verify
  (`verify-common.yml`) does NOT check for cramfs, so only the VM scenario is
  affected. This will cause `make test-vm` to fail at this task.
- **Fix**: Remove the cramfs grep assertion from `molecule/vm/verify.yml`
  lines 535-538, or replace it with a check for a module that IS in the
  template (e.g., jffs2).

#### 57. No auto-discovery of new cross-role synced variables

- **Dimension**: check-vars-sync
- **File**: `scripts/check-vars-sync.py:30-174,189-240`
- **Problem**: `REQUIRED_KEYS` and `CROSS_ROLE_MIRROR_KEYS` are hardcoded
  Python dictionaries requiring manual updates when new variables are added
  to `vars.yml` and mirrored in role defaults with the standard "fallback;
  authoritative in group_vars/all/vars.yml" comment. A developer adding a new
  mirrored variable must remember to also update `check-vars-sync.py`. Three
  existing cross-role overlaps (`containers_registries`, `registry_tokens`,
  `ssh_git_signing_pubkey`) are not in either dictionary — these have
  intentionally different fallback values, but demonstrate the gap. All 56
  vars.yml-to-system/defaults overlapping keys are covered.
- **Fix**: Add comment-pattern scanning to auto-discover variables with the
  "authoritative in group_vars/all/vars.yml" comment, or add a CI check that
  warns on new overlapping keys absent from either dictionary.

#### 64. `system_kernel_lockdown` undefined in verify-dnf.yml

- **Dimension**: jinja2-undefined-variable-risks
- **File**: `molecule/shared/verify-dnf.yml:131-137`,
  `roles/system/defaults/main.yml:293`
- **Problem**: `system_kernel_lockdown` is used in verify-dnf.yml lines
  131-137 without a `| default()` filter. The variable is defined only in
  `roles/system/defaults/main.yml` (line 293) — it is absent from
  `group_vars/all/vars.yml` (appears only as a comment at line 92). Peer
  variables like `system_init_on_free` (vars.yml line 103) and
  `system_ptrace_scope` (vars.yml line 94) are properly defined there. In
  container CI this is masked by when-list short-circuit:
  `verify_kernel_cmdline.stat.exists` is false (no `/etc/kernel/cmdline` in
  Podman containers), so `system_kernel_lockdown | length > 0` is never
  evaluated. In the VM scenario where `/etc/kernel/cmdline` exists, both
  conditions are evaluated and the undefined variable crashes the play.
- **Fix**: Add `system_kernel_lockdown` to `group_vars/all/vars.yml`
  alongside the other security hardening baselines, or add
  `| default('integrity')` to verify-dnf.yml lines 132 and 137.

#### 66. No molecule scenario exercises GNOME security-relevant dconf writes

- **Dimension**: molecule-converge-config
- **File**: `roles/desktop/tasks/main.yml:53,333-354`
- **Problem**: No molecule scenario tests `desktop_environment: gnome`.
  Fedora converge tests sway and i3; rocky/debian/vm use `unknown`; macOS
  uses `aerospace`. Five security-relevant dconf writes in
  `desktop_gnome_hardening_dconf` (lock-screen notifications, lock-enabled,
  idle-delay, location services, recent-files) are never exercised. The
  system role's `dconf.yml` provides overlapping system-level coverage for
  2 of 5 (idle-delay and lock-enabled), but 3 security/privacy-relevant
  user-level settings (lock-screen notifications, location services,
  recent-files tracking) have no system-level equivalent and are only
  present in the untested desktop GNOME path.
- **Fix**: Add a third `include_role` call in fedora converge `post_tasks`
  with `desktop_environment: gnome` and `desktop_gnome_packages: []` (to
  skip package installs). Note: dconf writes may fail in container CI
  without a dbus session, but `ignore_errors` is already present.

#### 67. Debian apt task omits desktop_sway_packages (Sway uninstalled on Debian)

- **Dimension**: desktop-package-lists
- **File**: `roles/desktop/tasks/main.yml:62-73`,
  `roles/desktop/defaults/main.yml:143-145`
- **Problem**: The dnf task assembles `_wm_pkgs` from `desktop_sway_packages`
  (22 packages including sway itself), but the apt task assembles
  `_apt_wm_pkgs` from only `desktop_sway_apt_packages` (2 packages:
  `lxqt-policykit`, `wl-clipboard`). Both packages in `desktop_sway_apt_packages`
  are duplicates already present in `desktop_sway_packages`, confirming the apt
  list was not designed as a complete replacement. On Debian with
  `desktop_effective=sway`, 20 of 22 Sway packages are not installed — including
  sway itself, swayidle, swaylock, swaybg, wofi, grim, slurp, mako, i3status,
  brightnessctl, wireplumber, xdg-desktop-portal-wlr, and cliphist. The Debian
  molecule test masks this by setting `desktop_environment=unknown` and
  `desktop_sway_packages=[]`.
- **Fix**: Include `desktop_sway_packages` in the apt `_apt_wm_pkgs` assembly
  (most Sway packages have identical names on Debian), or merge
  `desktop_sway_apt_packages` Debian-specific items into a conditional block
  within the main list.

#### 69. P3 bucket at 44% of findings spans code bugs to tooling wishes

- **Dimension**: plan-structure
- **Problem**: Priority distribution is P1=4, P2=8, P3=27, P4=23 (62 total).
  The P3 bucket at 27 items contains 44% of all findings and spans everything
  from real UX bugs (item 48, false FAIL in smoke-test) to test coverage gaps
  (item 33, no GNOME scenario) to tooling alignment wishes (item 39, hadolint).
  This makes it difficult to prioritize the fix backlog within P3.
- **Fix**: Split P3 into actionable sub-tiers: `P3a: code bugs to fix` (~12
  items like 42, 43, 48, 49, 50, 55, 56) and `P3b: coverage/hardening
  improvements` (~15 items like 14, 33, 34, 38, 39, 44, 57). Or add a
  `Quick wins` section to surface the easy-to-fix items currently buried
  in the P3 mass.

#### 70. Items 25 and 26 are confirmations, not deficiency findings

- **Dimension**: plan-structure
- **Problem**: Items 25 (ptrace_scope, P2) and 26 (ICMP redirects, P3) both
  explicitly state "No code change needed" and "Automation handles this
  correctly." These confirm that existing automation closes pre-provisioning
  gaps — they are not deficiency findings. Having a "no code change needed"
  item at P2 severity inflates the apparent critical-work count. The "What's
  solid" section already contains structurally identical confirmatory content
  (e.g., "Firewall-to-sshd ordering: port 722 opened before sshd restart, no
  lockout").
- **Fix**: Demote items 25 and 26 out of findings into the "What's solid"
  section with a note like "sysctl hardening pipeline (ptrace_scope,
  send_redirects) confirmed working — make all closes these pre-provisioning
  gaps." Or demote both to P4-info.

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

#### 51. Distrobox role warns but continues on missing subuid/subgid

- **Dimension**: cross-role-variable-dependencies
- **File**: `roles/distrobox/tasks/main.yml:55-101`,
  `roles/system/tasks/account_hardening.yml:194-214`
- **Problem**: The distrobox role (Play 2) depends on subuid/subgid entries
  provisioned by the system role's `account_hardening.yml` (Play 1) and
  `podman.socket` enabled by the containers role. The distrobox role re-checks
  subuid/subgid at lines 55-69 and podman.socket at lines 84-99, warning and
  continuing when prerequisites are missing. If podman subsequently fails due
  to missing subordinate IDs, the error message is cryptic. The rescue block
  (lines 163-189) handles failure gracefully — recording it for the CSB report
  on RHEL, marking `distrobox_tool: "none"` to skip Play 3.
- **Note**: This is a well-implemented defensive coding pattern, not a latent
  defect. The subuid/subgid provisioning block has no CSB guard and runs on
  all hosts. If the system role fails entirely, Ansible aborts Play 1 and
  Play 2 never runs. Informational only.

#### 52. preflight.sh emits raw ANSI escape codes when piped

- **Dimension**: preflight-smoke-test-consistency
- **File**: `scripts/preflight.sh:20,27-29`
- **Problem**: Color variables (RED/GRN/YLW/NC at line 20) are unconditionally
  set to ANSI escape sequences. When piped or redirected
  (`preflight.sh | tee log.txt`), output contains raw `\033[0;32m` sequences.
  `smoke-test.sh` (lines 18-22) correctly checks `[[ -t 1 ]] && ! $JSON` and
  sets color vars to empty strings when stdout is not a terminal.
- **Fix**: Add the same isatty guard to preflight.sh color initialization:
  `if [[ -t 1 ]] && ! $JSON; then ... else P='' W='' F='' R=''; fi`

#### 53. JSON output schemas differ between preflight and smoke-test

- **Dimension**: preflight-smoke-test-consistency
- **File**: `scripts/preflight.sh:353-361`, `scripts/smoke-test.sh:1893`
- **Problem**: Three mismatches: (1) top-level array key is `checks` vs
  `results`, (2) preflight has `ready` boolean while smoke-test has
  `failures`/`warns` integers, (3) preflight includes context fields
  (`os_family`, `is_csb`, `profile`) absent from smoke-test. The inner
  check-item schema (`{name, status, detail}`) is consistent. Any unified
  JSON consumer must branch on which script produced the output.
- **Fix**: Align schemas: rename one array key to match the other, add
  `ready` to smoke-test, add counts to preflight, or define a shared schema.

#### 54. Status level case mismatch between preflight and smoke-test JSON

- **Dimension**: preflight-smoke-test-consistency
- **File**: `scripts/preflight.sh:22-32,356-359`,
  `scripts/smoke-test.sh:28-37`
- **Problem**: `preflight.sh` uses lowercase status values (`pass`/`fail`/
  `warn`/`skip`) stored pipe-delimited with deferred JSON escaping.
  `smoke-test.sh` uses uppercase (`PASS`/`FAIL`/`WARN`) with no `SKIP`,
  stored as pre-built JSON strings with escaping at record time. A JSON
  consumer parsing both must handle `"status":"pass"` and `"status":"PASS"`.
- **Fix**: Standardize on one case (uppercase to match CIS/audit convention)
  and add `SKIP` to smoke-test or remove it from preflight. Consider
  extracting a shared `record()` implementation.

#### 58. Missing `/var/run/utmp` audit watch (CIS 4.1.3.6)

- **Dimension**: auditd-rules-template
- **File**: `roles/system/templates/auditd-claude.rules.j2:194-198`
- **Problem**: The "Login/logout events (CIS 4.1.3.6)" section watches
  `/var/log/lastlog`, `/var/run/faillock`, `/var/log/wtmp`, and
  `/var/log/btmp` but omits `/var/run/utmp`. CIS 4.1.3.6 specifies
  `-w /var/run/utmp -p wa -k session` alongside the wtmp/btmp watches. On
  this Fedora system, `/var/run/utmp` exists (systemd-257, maintained by
  `systemd-update-utmp.service`). Without the watch, tampering with active
  login session records (hiding from `who`/`w`/`users`) goes undetected.
  Redundant detection exists via wtmp (which IS watched) and
  systemd/journald/loginctl, limiting the practical gap to real-time utmp
  modification during an active session.
- **Fix**: Add `-w /var/run/utmp -p wa -k session` to the login/logout
  events section of the auditd rules template (after line 198).

#### 59. ShellCheck CI step omits git-template hook files

- **Dimension**: ci-workflows
- **File**: `.github/workflows/linting.yml:101-104`
- **Problem**: The CI ShellCheck step covers `scripts/*.sh`,
  `roles/claude/files/*.sh`, `.githooks/pre-commit`, and
  `.githooks/commit-msg`, but omits the 4 git-template hook files at
  `roles/dotfiles/files/git-template-{commit-msg,pre-commit,prepare-commit-msg,pre-push}`.
  All have `#!/bin/sh` shebangs and run on every git operation across all
  repos on the provisioned machine. The local Makefile `shellcheck` target
  (line 182) DOES include all 4 files, so there is a divergence between CI
  and local linting. A shellcheck-detectable bug would be caught locally but
  not in CI.
- **Fix**: Add one line to the CI workflow's shellcheck step to include
  `roles/dotfiles/files/git-template-*`.

#### 60. Molecule prepare systemd wait loops silently pass on exhaustion

- **Dimension**: molecule-prepare-correctness
- **File**: `molecule/fedora/prepare.yml:34,52`,
  `molecule/rocky/prepare.yml:28`, `molecule/debian/prepare.yml:37`
- **Problem**: All systemd wait loops in fedora, rocky, and debian prepare
  files use `failed_when: false` with no fallback assertion. If systemd never
  reaches `running` or `degraded` (e.g., container image broken, cgroup mount
  failure), the retry loop exhausts all attempts, the task "succeeds" with the
  last non-matching stdout, and prepare completes without error. The converge
  then runs against an unstable container and fails with cryptic UNREACHABLE
  or daemon-reload errors that do not point back to the root cause. Total of 4
  systemd wait loops across three files (2 fedora + 1 rocky + 1 debian).
- **Fix**: Add a final assert after each wait loop that checks
  `systemd_state.stdout in ['running', 'degraded']` and fails with a clear
  message like "systemd did not stabilize within N seconds."

#### 61. Fedora prepare `raw` package install failure silently swallowed

- **Dimension**: molecule-prepare-correctness
- **File**: `molecule/fedora/prepare.yml:42-44`
- **Problem**: The `raw: dnf install -y --setopt=tsflags=noscripts
  openssh-server policycoreutils-python-utils` task (line 42) intentionally
  uses `noscripts` to avoid daemon-reload crashes, but also has
  `failed_when: false` (line 44). If the install fails (mirror unreachable,
  disk full), the failure is silently swallowed. The converge's conditional
  install (`when: 'openssh-server' not in ansible_facts.packages`) then
  attempts a normal `dnf install` without `noscripts`, triggering the exact
  daemon-reload crash that prepare was designed to prevent. Rocky and Debian
  prepare files explicitly avoid this pattern — Rocky's comment says
  "do NOT install packages here."
- **Fix**: Add `register` + post-install assertion on the raw task to catch
  install failures, or check the package presence after the raw task.

#### 62. Smoke test only verifies `registry.redhat.io` auth

- **Dimension**: container-registry-authentication
- **File**: `scripts/smoke-test.sh:148-155`
- **Problem**: The ONLY registry auth check in the entire 1904-line script
  verifies only `registry.redhat.io`, gated on work profile AND `oc` binary
  presence. No quay.io or vault-provisioned token checks exist. The containers
  role checks all registries at provisioning time (lines 32-121 for vault
  tokens, non-vault registries), but a vault token expiring post-provisioning
  would go undetected by smoke-test. Impact is limited: quay.io is public
  (unauthenticated pulls work for public images) and `vault_registry_tokens`
  defaults to an empty list.
- **Fix**: Add a quay.io connectivity check and vault-provisioned registry
  token verification to the smoke-test work-profile section.

#### 68. gitconfig template has no `[include]` directive for user-local customizations

- **Dimension**: dotfiles-force-pattern
- **File**: `roles/dotfiles/templates/gitconfig.j2`,
  `roles/dotfiles/tasks/main.yml:95-99`
- **Problem**: The gitconfig template (deployed with `force: true` default) has
  `includeIf` directives for work/personal directory trees but no
  general-purpose `[include] path = config-local` for arbitrary user additions
  (custom aliases, diff/merge tools, credential helpers). Every `make all`
  overwrites any direct edits to `~/.config/git/config`. The SSH config
  template already has `Include ~/.ssh/config.local` (line 62) as an escape
  hatch — the gitconfig lacks an equivalent.
- **Fix**: Add `[include] path = config-local` at the end of `gitconfig.j2`
  (after the filter/lfs block), following the established `ssh_config` pattern.

#### 71. Plan would benefit from a `Quick wins` section

- **Dimension**: plan-structure
- **Problem**: At least 12 findings are fixable in 1-5 lines of code but are
  distributed across P1-P4 with no way to identify them without reading each
  description: item 36 (move comment outside quoted string), item 38 (add one
  deny-list entry), item 48 (add two regex alternatives), item 55 (add one path
  to regex), item 56 (delete 4 stale lines), item 59 (add one shellcheck glob),
  item 22 (add suffix to one string), item 58 (add one auditd watch line),
  item 17 (add one file-state task), item 31 (change one sway binding line),
  item 4 (change WARN to FAIL in 5 preflight lines), item 15 (add existence
  guard). Highlighting these would let someone knock out 12 items in a focused
  session.
- **Fix**: Add a "Quick wins (under 10 lines of code)" section after the
  Approach section listing findings fixable in under 10 lines, with the
  one-line fix description from each finding.

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

---

## Iteration 8

**Date**: 2026-08-11

**Added**:

- **Item 47** (P2): `verify-sway.yml aborts on undefined
  desktop_sway_adaptive_sync` — the Fedora verify playbook only loads
  `group_vars/all/vars.yml`, never loading role defaults where
  `desktop_sway_adaptive_sync` and `desktop_sway_libva_driver` are defined.
  The undefined variable raises `AnsibleUndefinedVariable` at line 39,
  halting the verify playbook and leaving molecule fedora sway verification
  coverage at zero.
- **Item 48** (P3): `Smoke test AllowTcpForwarding regex rejects documented
  yes value` — the PCRE pattern `(no|local|remote)` at smoke-test.sh line
  924 does not include `yes` or `all`, both valid per the Ansible assertion,
  sshd man page, and CLAUDE.md documentation. Produces a false FAIL for
  users following the docs.
- **Item 49** (P3): `verify-sway.yml stale touchpad assertions` — commit
  `8dcded93` changed touchpad defaults from `enabled` to empty string, but
  verify assertions still unconditionally grep for `tap enabled` and
  `natural_scroll enabled`. Would false-fail if they ran; currently masked
  by item 47's undefined-variable abort. `default.config.yml` also not
  updated to reflect the new defaults.
- **Item 50** (P3): `verify-common.yml sysctl checks verify key presence
  without values` — five security-relevant sysctl keys
  (`use_tempaddr`, `rp_filter` x2, `bridge-nf-call-iptables`,
  `max_map_count`) are checked by key name only, not `key = value`. A value
  regression (e.g., `rp_filter` from 2 to 0) passes the assertion.
- **Item 51** (P4): `Distrobox role warns but continues on missing
  subuid/subgid` — cross-role dependency between system role (Play 1
  subuid/subgid provisioning) and distrobox role (Play 2). The distrobox
  role's defensive checks and rescue block handle all failure modes
  gracefully. Informational — documents an existing, working mitigation.
- **Item 52** (P4): `preflight.sh emits raw ANSI escape codes when piped` —
  color variables unconditionally set to escape sequences with no `[[ -t 1 ]]`
  isatty guard, unlike `smoke-test.sh` which handles this correctly.
- **Item 53** (P4): `JSON output schemas differ between preflight and
  smoke-test` — three envelope-level mismatches: `checks` vs `results` array
  key, `ready` bool vs `failures`/`warns` ints, and context fields present
  only in preflight. Inner check-item schema is consistent.
- **Item 54** (P4): `Status level case mismatch between preflight and
  smoke-test JSON` — preflight uses lowercase (`pass`/`fail`/`warn`/`skip`),
  smoke-test uses uppercase (`PASS`/`FAIL`/`WARN`) with no `SKIP`. Different
  storage and escaping strategies for the same conceptual `record()` function.

**Rationale**: Eight findings across four dimensions. The high-severity
verify-sway abort (item 47) is the most impactful — it silences all sway
verification in the Fedora molecule scenario. Items 49-50 are additional
molecule-verify-false-pass gaps: stale assertions masked by the same abort,
and key-only sysctl checks that miss value regressions. The smoke-test
AllowTcpForwarding regex (item 48) is a false-FAIL bug affecting documented
configuration values. Items 52-54 are three preflight-smoke-test consistency
findings — ANSI escaping, JSON schema divergence, and status level case
mismatch — none causing runtime failures but all complicating any future
unified consumption of the scripts' output.

---

## Iteration 9

**Date**: 2026-08-11

**Added**:

- **Item 55** (P3): `Molecule path filter omits .github/actions/ directory` —
  the molecule workflow path-filter regex matches `.github/workflows/` but
  not `.github/actions/`. A broken change to the molecule-setup composite
  action (used by all 9 matrix jobs) could merge without molecule tests and
  break all scenarios on the next unrelated PR.
- **Item 56** (P3): `VM verify cramfs assertion stale after template removal`
  — commit `ab7c166d` removed cramfs from `modprobe-hardening.conf.j2` but
  `molecule/vm/verify.yml:535-538` still asserts its presence. `make test-vm`
  will fail at this task.
- **Item 57** (P3): `No auto-discovery of new cross-role synced variables` —
  `check-vars-sync.py` hardcodes `REQUIRED_KEYS` and `CROSS_ROLE_MIRROR_KEYS`
  with no auto-discovery of new mirrored variables. Three existing cross-role
  overlaps are outside both dictionaries, demonstrating the gap.
- **Item 58** (P4): `Missing /var/run/utmp audit watch (CIS 4.1.3.6)` — the
  auditd rules template watches lastlog, faillock, wtmp, and btmp but omits
  utmp. Practical gap limited to real-time utmp modification detection;
  redundant detection exists via wtmp and systemd/journald.
- **Item 59** (P4): `ShellCheck CI step omits git-template hook files` — CI
  shellcheck covers `scripts/*.sh` and `.githooks/` but omits 4 git-template
  hooks in `roles/dotfiles/files/`. The local Makefile shellcheck target does
  cover them, creating a CI-vs-local divergence.
- **Item 60** (P4): `Molecule prepare systemd wait loops silently pass on
  exhaustion` — 4 systemd wait loops across fedora/rocky/debian prepare files
  use `failed_when: false` with no post-loop assertion. If systemd never
  stabilizes, prepare completes and converge fails with cryptic errors.
- **Item 61** (P4): `Fedora prepare raw package install failure silently
  swallowed` — `failed_when: false` on the noscripts `raw` install means a
  failed package install is swallowed. The converge fallback then attempts a
  normal `dnf install` triggering the exact daemon-reload crash that prepare
  was designed to prevent.
- **Item 62** (P4): `Smoke test only verifies registry.redhat.io auth` —
  the only registry auth check in smoke-test.sh verifies registry.redhat.io,
  gated on work profile and oc presence. No quay.io or vault-provisioned
  token checks. Impact limited since quay.io is public and vault tokens
  default to empty.

**Rationale**: Eight findings across six dimensions. The molecule path-filter
gap (item 55) and stale VM cramfs assertion (item 56) are medium-severity CI
correctness issues — one allows untested composite action changes to merge,
the other causes `make test-vm` to fail on a stale assertion. The
check-vars-sync auto-discovery gap (item 57) is a maintenance/process
concern where new mirrored variables can escape sync checking. The remaining
five low-severity findings cover an auditd template completeness gap (item
58), a CI-vs-local shellcheck divergence (item 59), two molecule prepare
robustness issues where `failed_when: false` masks container initialization
failures (items 60-61), and a smoke-test registry auth coverage gap (item
62).

---

## Iteration 10

**Date**: 2026-08-11

**Added**:

- **Item 63** (P2): `VM verify.yml missing include_vars for
  group_vars/all/vars.yml` — VM verify pre_tasks never load
  `group_vars/all/vars.yml`, unlike fedora/rocky/debian verify files.
  Multiple variables from shared verify includes (`system_ptrace_scope`,
  `system_auditd_max_log_file`, `repo_tailscale`) are undefined.
  `make test-vm` verify phase crashes at `system_ptrace_scope`
  (verify-common.yml:511).
- **Item 64** (P3): `system_kernel_lockdown undefined in verify-dnf.yml` —
  used without `| default()` at lines 131-137 but defined only in role
  defaults, not `group_vars/all/vars.yml`. Masked in container CI by
  when-list short-circuit (no `/etc/kernel/cmdline`); crashes in the VM
  scenario.
- **Item 65** (P2): `verify-claude-queue.yml unconditional systemd unit
  assertions` — unconditionally asserts `.service`/`.timer` exist, but the
  claude role guards deployment with `not system_is_container` and all
  Podman CI sets `system_is_container: true`. Assertions fail in fedora and
  rocky verify. File exists only on wip1, not yet CI-tested.
- **Item 66** (P3): `No molecule scenario exercises GNOME security-relevant
  dconf writes` — 3 of 5 security/privacy-relevant dconf writes
  (lock-screen notifications, location services, recent-files) have no
  system-level equivalent and are only present in the untested GNOME
  desktop path.
- **Item 67** (P3): `Debian apt task omits desktop_sway_packages` — on
  Debian with `desktop_effective=sway`, 20 of 22 Sway packages are not
  installed including sway itself. The apt task uses only the 2-package
  `desktop_sway_apt_packages` supplemental list rather than the full
  22-package `desktop_sway_packages` list.
- **Item 68** (P4): `gitconfig template has no [include] for user-local
  customizations` — deployed with `force: true`, overwrites on every
  `make all`. The SSH config template has `Include config.local`; the
  gitconfig lacks an equivalent escape hatch.
- **Item 69** (P3): `P3 bucket at 44% of findings spans code bugs to
  tooling wishes` — 27 of 62 findings are P3, spanning UX bugs, test
  coverage gaps, and tooling alignment. Consider splitting into actionable
  sub-tiers.
- **Item 70** (P3): `Items 25 and 26 are confirmations, not deficiency
  findings` — both state "No code change needed" and "Automation handles
  this correctly." They belong in the "What's solid" section, not the
  findings list.
- **Item 71** (P4): `Plan would benefit from a Quick wins section` — at
  least 12 findings are fixable in 1-5 lines of code (items 36, 38, 48,
  55, 56, 59, 22, 58, 17, 31, 4, 15) but are distributed across P1-P4
  with no way to identify them without reading each description.

**Rationale**: Nine findings across six dimensions. Two P2 findings (items
63, 65) are molecule verify correctness issues that would crash
`make test-vm` and container CI respectively. Three P3 findings (items 64,
66, 67) are undefined variable, test coverage, and package list gaps. Two
P3 findings (items 69, 70) are plan-structure recommendations for improving
triage prioritization. Two P4 findings (items 68, 71) are a dotfiles
escape-hatch consistency improvement and a plan-organization suggestion.

---

## Letter from the Old Laptop

This section captures what the user ACTUALLY does on this machine — their
habits, tool preferences, and workflow patterns. This is the context you
need to make the P16v feel right, not just correct.

### Who this person is (by the numbers)

- **10,725 lines** of zsh history — heavy CLI user
- **git push** is the #1 git subcommand (1,391 times) — push-intensive PR
  iteration cycle, YubiKey touch per push is real daily friction
- **gvim** (via `gv` alias) used 916 times — graphical vim with tab pages,
  NOT terminal vim. The `gv` alias maps to `gvim -vp`
- **make** used 428 times — build-system-driven workflow. Top target:
  `make apply` (126 times, Konflux release), then `make test-remote` (34),
  `make cve-fix` (30)
- **5 concurrent Claude sessions** running right now via Vertex AI with
  `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=2000`
- **No tmux sessions active** — works in terminal tabs despite the
  automation's cw/ccp aliases expecting tmux. This is important: the tmux
  workflow is aspirational, not current.

### Custom aliases that must survive

| Alias | Expansion | Usage | In automation? |
|-------|-----------|-------|----------------|
| `gv` | `gvim -vp` | 916 uses | Yes (conditional on gvim presence) |
| `gi` | `grep -rniI --color=always --exclude-dir=vendor` | 976 uses | Renamed to `gri` (forgit claims `gi`) |
| `ping` | `ping -i .2` | frequent | Yes |
| `pingg` | `ping google.com` | frequent | Yes |
| `ping8` | `ping 8.8.8.8` | frequent | Yes |
| `keyb` | bluetooth keyboard connect | periodic | No — device-specific |
| `mx` | bluetooth mouse connect | periodic | No — device-specific |
| `fixd` | fix_displays.sh | periodic | No — X11/i3-specific, not needed on Sway |

The `gi` → `gri` rename is a real muscle-memory break at 976 uses. Consider
keeping `gi` as a secondary alias alongside `gri`.

### Hardware context

- **Dual monitors**: laptop eDP-1 + external HDMI-1 2560x1440 at desk
- **Docking station**: 2x DisplayPort 1920x1200 (one rotated right) via
  `fix_displays.sh` script
- **Peripherals**: Keychron K2 (wireless mechanical), Logitech MX Master
  (multiple generations), Bang & Olufsen Beoplay Portal headphones,
  Sennheiser MOMENTUM 3
- **YubiKey**: 5C Nano USB-C, firmware 5.2.3 (pre-5.7, EUCLEAK vulnerable)
- USBGuard must whitelist: Keychron K2, MX Master (all variants), B&O
  headphones, Sennheiser — in addition to the internal keyboard/touchpad

### Editor preferences (will be lost on P16v without action)

The user's vimrc is 77 lines of carefully tuned preferences. The automation
deploys vimrc with `force: false` (seed-only), so it won't overwrite on
THIS machine. But on the NEW machine, the automation's seed vimrc will be
deployed instead. Key differences:

| Setting | User's preference | Automation seed |
|---------|-------------------|-----------------|
| Tabs | `noexpandtab` (hard tabs) | `expandtab` (spaces) |
| Leader key | `,` (comma) | Not set |
| textwidth | 140 | Not set |
| autochdir | yes | No |
| backup dir | `~/.vimbackup//` | No (noswapfile) |
| Folding | disabled | Not configured |
| Buffer list | F5 shortcut | Not configured |
| Spell toggle | `<leader>ss` | Not configured |

**Action**: The user should either copy their vimrc to the P16v before
`make all`, or add it to `scripts/backup.sh` and restore from backup.

### Window manager transition (i3 → Sway)

The user's current i3 config uses **Mod1 (Alt)** as the modifier. The
automation switches to **Mod4 (Super/Windows)**. This affects EVERY
keybinding and is the single biggest muscle-memory break.

Additionally, i3's default focus keys are j/k/l/semicolon. The automation
uses h/j/k/l (vim-style). Better ergonomically but different from what
10,725 lines of history were built on.

The user had almost no i3 customization — essentially the wizard default
plus `nm-applet`, a few volume/brightness keys, and display fix scripts.
The automation's Sway config is a major upgrade with clipboard history,
screenshot tools, notifications, idle lock, etc.

### Git config (security upgrade, some friction)

The old gitconfig has two dangerous settings the automation correctly
removes:

- `sslVerify = false` — disables ALL HTTPS certificate verification
- `safe.directory = *` — trusts every git repo on the filesystem

Both are security risks. The user should be aware these are gone and that
any workflow depending on them (e.g., internal repos with self-signed
certs) needs proper CA cert configuration instead.

### What's stale (safe to drop)

These items exist on the old laptop but are unused/outdated:

- Travis CI integration (`source ~/.travis/travis.sh`)
- RVM (Ruby Version Manager) PATH
- Packer PATH
- VAGRANT_HOME with root/non-root split
- `unset GREP_OPTIONS` (deprecated years ago)
- Linuxbrew eval
- gitreview.username (OpenStack-era)
- `starwars` telnet alias

### Repo layout migration

The user has **106 repos (6.6 GB)** in `~/go/src/` using the pre-modules
GOPATH layout:

```
~/go/src/github.com/openshift/ovn-kubernetes/
~/go/src/submariner-io/submariner-operator/
~/go/src/github.com/stolostron/submariner-addon/
```

The automation clones to `~/src/` with a flat layout:

```
~/src/openshift/ovn-kubernetes/
~/src/submariner-io/submariner-operator/
~/src/stolostron/submariner-addon/
```

Both will coexist on the P16v if the user copies ~/go/src/ from the old
machine. The automation provides no migration path, cleanup task, or
warning. Go modules (go.mod) don't care about filesystem location, so
both layouts work — it's just confusing to have two copies.

### What the user ACTUALLY needs on day 1

Based on the shell history, these are the workflows the user will try
first on the P16v:

1. **Clone and push to Submariner repos** — needs SSH keys, gh auth,
   fork remotes configured
2. **Run `make apply` for Konflux releases** — needs oc login to
   Konflux prod cluster, registry auth
3. **Run 5+ concurrent Claude sessions** — needs Vertex AI env vars,
   Claude Code installed, CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION set
4. **Use gvim to edit code** — needs gvim installed (in vim-enhanced
   package), user's vimrc deployed
5. **Build OVN-K with kind** — needs kind, podman socket, environment.d,
   kernel modules, sysctl tuning
6. **Connect bluetooth peripherals** — needs bluetooth enabled,
   USBGuard whitelist including Keychron/MX Master
7. **Run CVE fixes** — needs grype (not automated, item 41), subctl
   (automated), golangci-lint (automated)

### VPN context

The user has multiple VPN profiles:
- Red Hat Global VPN (corporate, tun0) — currently active
- Mullvad/NordVPN city endpoints (Amsterdam, Beijing, Brisbane,
  Singapore, Tel Aviv, Tokyo) — personal privacy VPN

On CSB hybrid, Mullvad repo is blocked (`not csb_detected`). The user may
need to install Mullvad manually or override the guard.

### Claude Code reality vs automation

The automation deploys a security-hardened multi-instance Claude Code setup.
The user's ACTUAL setup on this old laptop is very different:

**Instance isolation does NOT exist yet:**
- `~/.claude-work/` and `~/.claude-personal/` do not exist
- `~/.config/claude/work-env` does not exist
- Single `~/.claude/` serves both work and personal
- The P16v would be the FIRST machine with instance isolation
- Expect friction: the user has never used `cw`/`ccp` aliases

**Dangerous mode is the standard:**
- User launches with `--dangerously-skip-permissions --plugin-dir .`
- `skipDangerousModePermissionPrompt: true` in settings.json
- The automation deploys `sandbox.enabled: true` and
  `failIfUnavailable: true` — this is a significant behavior change
- The deny list in settings.json blocks `rm -rf /*`, `sudo`, `su`, `dd`,
  `curl`, `wget` — but these are bypassed by dangerous mode anyway

**Scale of usage:**
- 5 concurrent Claude sessions is normal (not exceptional)
- `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=2000` (40x default)
- 98 project contexts in `~/.claude/projects/`
- 6.8 GB in `~/.claude/jobs/` (788 jobs)
- 29 MB in plugins (5 plugin caches)
- 4 plugin marketplaces configured

**Vertex AI routing (work):**
- `CLAUDE_CODE_USE_VERTEX=1` currently in `.zshrc` directly
- The automation moves Vertex vars to `work-env` sourced by `cw()` only
- GCP project: `itpc-gcp-hcm-pe-eng-claude`, region: `us-east5`
- Model pinned: `claude-opus-4-6[1m]` via `ANTHROPIC_DEFAULT_OPUS_MODEL`

**Plugin ecosystem:**
- 4 marketplaces: ai-helpers (openshift-eng), claude-skills (personal),
  submariner-release (stolostron), submariner (shipyard)
- Atlassian MCP server via SSE (`https://mcp.atlassian.com/v1/sse`)
- Personal skills: career-advocate, cve-agent, cve-jira-triage, jira,
  notes, work-summary
- Co-authored-by DISABLED (`includeCoAuthoredBy: false`)

**The delta the P16v agent must navigate:**
The automation's security model (sandbox, instance isolation, no dangerous
mode) is aspirational — the user's actual workflow is `--dangerously-skip-
permissions` with 5 concurrent sessions. The P16v agent should deploy the
security infrastructure but warn the user about the behavior change. The
`cw`/`ccp` aliases will be new muscle memory to build.

### Development environment baseline

**Tool versions on this old laptop** (what the user is used to):

| Tool | Version | Path | Automation version |
|------|---------|------|--------------------|
| Go | 1.25.10 | /usr/bin/go | system dnf |
| kind | 0.31.0 | /usr/local/bin | 0.32.0 |
| helm | 3.17.2 | /usr/local/bin | 4.2.3 |
| kustomize | 5.6.0 | /usr/local/bin | 5.8.1 |
| oc | 4.19.5 | /usr/local/bin | 4.22.7 |
| gofumpt | 0.3.1 | ~/go/bin | 0.11.0 |
| Python | 3.13.5 | linuxbrew | system dnf |
| Node | 24.4.1 | linuxbrew | system dnf |
| Claude | 2.1.227 | ~/.local/bin | latest |

**NOT installed on this laptop** (automation adds these):
k9s, stern, cosign, tkn, operator-sdk, ec, gitleaks, actionlint, sops,
zizmor, transcrypt (in ~/.local/bin but not /usr/local/bin)

**Linuxbrew** provides Python3 and Node.js on this laptop. The automation
uses system dnf packages instead. This is a deliberate change.

**GONOSUMDB=\*** is set (disables all module checksum verification).
The automation sets this conditionally via `dotfiles_gonosumdb` only
when non-empty, which is more secure.

**~/go/bin has 100+ binaries** including many OpenShift CI tools that
the automation doesn't install. These accumulated organically via
`go install` over time. The P16v will start clean with only the
automation's curated set.
