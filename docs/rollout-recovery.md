# Rollout recovery sheet

<!-- markdownlint-disable MD013 -->

Companion to [rollout-plan.md](rollout-plan.md). Print it or keep it on another device: after the agent chunk the Claude sandbox
blocks `~/.ssh`, shell rc files and sudoers paths and cannot supply the sudo password, so recovery from system chunks is human-only.
One `##` section per chunk, named exactly like `group_vars/all/rollout.yml` (a test checks this), in registry order. Each lists why the
chunk exists, how it breaks, how to verify it and how to undo it. Replace `<...>` placeholders.

## Rules for every recovery

1. **Remove the chunk from `rollout_enable` in `config.yml` first.** That stops `make all`, `make update` and direct runs from
   re-applying it; it undoes nothing by itself. `all` cannot exclude a chunk: set `rollout_enable` to a list of the others
   (`make status` names them), keeping their prerequisites.
2. **Expect an idle root shell to exit when `system_tmout` is set.** `sudo -s` and `sudo -i` both read the readonly `TMOUT` from
   `/etc/profile.d/tmout.sh` (Fedora `/etc/bashrc` sources profile.d for non-login interactive shells; `/etc/profile` does so for login
   shells), so choosing one does not avoid it. The role deploys this file only off RHEL CSB, so on RHEL CSB check `/etc/profile.d` and
   `system_tmout` first. Keep a second terminal or console open until the check passes.
3. **Files created under `sudo`** inherit `umask 077` and the sudoers `umask=0027`. Use `install -m 0644 -o root -g root SRC DEST` or
   `cp -a`, then `restorecon -Rv <path>`. Never `sudo tee` into a managed directory.
4. **Restore single files, not whole directories.** IT automation rewrites some files on its own schedule.
5. After `sys_signin`, sudo sessions of non-root users may be I/O-logged (`log_input,log_output`): type no secrets into a root shell.
6. After any rollback or reboot, run IT's compliance job once and confirm it reports success, then `make verify CHUNK=<name>` (a chunk you removed from `rollout_enable` is refused as not enabled: run `scripts/smoke-test.sh --user-only --only <its checks from its Verify line>` instead, where WARN on the settings the undo reverted is expected). It exits
   non-zero on any mapped check that is FAIL, WARN or SKIP. Root-only checks are WARN or SKIP for a normal user: run
   `sudo scripts/smoke-test.sh --system-only --only <checks>` (`make verify` prints the exact command after a failure).

## Before any system chunk: snapshot and rehearse

```bash
sudo install -d -m 0700 /var/lib/laptop-setup-rollback
sudo sh -c 'umask 077; tar --xattrs --acls --selinux --numeric-owner -czf /var/lib/laptop-setup-rollback/etc-$(date +%Y%m%d-%H%M).tar.gz /etc'
sudo authselect current --raw | sudo tee /var/lib/laptop-setup-rollback/authselect-current.txt >/dev/null
sudo cp -a /etc/default/grub /etc/kernel/cmdline /var/lib/laptop-setup-rollback/
{ firewall-cmd --get-default-zone; firewall-cmd --get-active-zones; update-crypto-policies --show; } > ~/net-before.txt
dconf dump / > ~/dconf-before.ini
cp -a ~/.ssh/authorized_keys ~/authorized_keys.before   # point-in-time copy for undo
```

The tarball holds host keys and secrets: keep it root-only, on this disk, outside the repo, and copy it off-box only encrypted.
Before starting, confirm you know the disk unlock passphrase (or its escrow), have a local break-glass way to become root (a known root
password, which `sys_signin` invalidates by locking root on non-CSB hosts, or a rehearsed rescue boot), and have a live USB.

## Rescue boot

Rehearse once at the console. At the GRUB menu press `e`, go to the line starting `linux`, append `rd.break`, press Ctrl-x. At the
initramfs prompt: `mount -o remount,rw /sysroot`, `chroot /sysroot`, make the fix, `touch /.autorelabel`, exit twice. It needs no root
password. A GRUB password (`system_grub_password_enabled`) blocks the edit.

- **Kernel will not boot:** on the same `linux` line delete `lockdown=...` (or the other new argument) instead of adding `rd.break`
  (this boot only). Or pick an older kernel. Persistent fix once booted:
  `sudo grubby --remove-args="lockdown" --update-kernel=ALL`, then `sudo grubby --info=ALL | grep -E '^(index|title|args)'`.
  Also set `system_kernel_lockdown: ""` in `config.yml` (see `## sys_lockdown` Undo); otherwise `/etc/kernel/cmdline` re-adds it on the
  next kernel install and the next chunk run.
- **Account locked by faillock** ("Account locked", 5 failures): wait `system_faillock_unlock_time` (900 s) or, from another root shell,
  `faillock --user <name> --reset`.
- **Account inactive** (`chage -I 30`; not applied on RHEL CSB): in the chroot `chage -I -1 <name>` and `passwd <name>`.
- **No root and no working account:** in the chroot `passwd -u root; passwd root`, reboot, fix the account, then `passwd -l root`.
  `rescue.target` works only if root has a password.
- **Locked out of SSH or PAM:** from the console check `authselect current`, `systemctl status sssd` and
  `journalctl -b -u sssd -u sshd`. Restore PAM from the saved authselect profile (`sys_signin`) and the sshd drop-in or key file
  (`sys_remote_access`, `ssh_authorized_keys`), then reboot and re-test.

## packages

- **Why:** CLI tools, pinned binaries, Go tools, pipx, krew, Tailscale package, virtualization (libvirt, qemu).
- **Breaks if wrong:** a pinned-binary download can fail (the run warns and goes on). System-level effects: starts and enables `tailscaled`;
  adds you to the `libvirt` and `kvm` groups (`libvirt` membership is root-equivalent; active after the next login); enables
  `virtqemud.socket` and `virtnetworkd.socket`; overwrites same-named files in `/usr/local/bin` with no backup.
- **Verify:** no check mapped. `make plan CHUNK=packages` shows changed=0 for the dnf, pipx and pinned-download parts only: check mode
  skips the standalone binaries, Go tools, krew plugins and the group add. Check those with
  `scripts/smoke-test.sh --user-only --only sops,transcrypt,gitleaks,krew`.
- **Undo:** do not `dnf remove` the `roles/packages/defaults` lists: they include packages the machine already had (podman, gh, zsh).
  Right after the run use `sudo dnf history list`, then `sudo dnf history info <id>` for the run's transaction(s): `sudo dnf history undo <id>`
  also downgrades anything listed as Upgraded there, so if there are Upgraded lines, `sudo dnf remove` only the Installed ones
  instead. Undo this chunk after `login_shell`, `distrobox`, `containers` and `agents`, and keep zsh while it is the login shell.
  Then `sudo gpasswd -d "$USER" libvirt; sudo gpasswd -d "$USER" kvm`,
  `sudo systemctl disable --now tailscaled virtqemud.socket virtnetworkd.socket`, and delete what the role placed outside dnf:
  the pinned files in `/usr/local/bin` (oc, kubectl, opm, kind, helm, kustomize, k9s, transcrypt, sops, gitleaks, actionlint, zizmor,
  operator-sdk, cosign, tkn, ec), `/var/lib/ansible-packages`, the Go tools it installed in `~/go/bin`, `~/.local/bin/subctl*`,
  `pipx uninstall yamllint ansible-lint`, and `~/.krew` (this also removes krew plugins you added yourself).

## ssh_client

- **Why:** pinned GitHub `known_hosts` and vault-backed SSH and signing keys (`~/.ssh/config` and the git signing settings come from `dotfiles`).
- **Breaks if wrong:** the role removes ALL `github.com` `known_hosts` entries and re-adds only the pinned ED25519 key
  (`roles/ssh/defaults/main.yml`). Never repair with `ssh-keyscan` (it trusts whatever answers).
- **Verify:** `make verify CHUNK=ssh_client` (`known-hosts-github`: a github.com key is in `known_hosts`).
- **Undo:** run `make backup` BEFORE the chunk (the role overwrites key files without a copy), then restore `~/.ssh` files from
  that directory (`~/laptop-setup-backup-<timestamp>`), or re-run `make chunk-ssh_client` to
  re-seed `known_hosts` (not `make ssh`: it also rewrites `~/.ssh/authorized_keys`). Stop commit signing (on when the vault holds a signing key) with `git config --global commit.gpgsign false`;
  `make dotfiles` re-enables it until `vault_ssh_signing_key_pub` is cleared in the vault.

## dotfiles

- **Why:** shell, git, tmux and tool config, plus `~/.ssh/config` (it needs the pinned GitHub `known_hosts` from `ssh_client`, hence the requirement).
- **Breaks if wrong:** a bad `~/.zshrc`/`~/.bashrc`/`~/.gitconfig` affects every new shell and commit; a bad `~/.ssh/config` affects every SSH connection.
- **Verify:** `make verify CHUNK=dotfiles` (`dotfile-gitconfig`, `git-hooks-path`, `ssh-config-hash-known-hosts`, `ssh-config-github-strict`).
- **Undo:** run `make backup` BEFORE the chunk (an older backup predates earlier deploys), then restore the file from that
  `~/laptop-setup-backup-<timestamp>` directory. Replaced `.zshrc`, `.bashrc`, `.ssh/config`, `.config/git/config` and
  `.config/tmux/tmux.conf` also keep a copy beside them named `<file>.<pid>.<date>@<time>~`. Unmanaged git template hooks and
  `environment.d` files the role moved aside are in `~/.local/state/laptop-setup/quarantine/` (move them back, dropping the
  timestamp suffix). One-way steps: `~/.gitconfig` and `~/.tmux.conf` are moved to `<file>.pre-xdg-migration` (move back if
  wanted) and a stock `~/.oh-my-zsh` is deleted (re-clone it). The chunk also adds the `cwork`/`cper`/`cper2` launchers to `~/.zshrc`
  and switches the SSH agent: `systemctl --user unmask gcr-ssh-agent.socket gcr-ssh-agent.service`,
  `systemctl --user disable --now ssh-agent`, delete `~/.config/autostart/gnome-keyring-ssh.desktop`,
  `~/.config/environment.d/ssh-agent.conf` and `~/.config/systemd/user/ssh-agent.service`, then log in again so `SSH_AUTH_SOCK` changes back.

## desktop

- **Why:** GNOME, sway, i3 or AeroSpace configuration.
- **Breaks if wrong:** a bad compositor config can leave the session unusable; keys that IT or `sys_baseline` lock are ignored.
- **Verify:** no check is mapped, so `make verify CHUNK=desktop` exits 1 (state unknown) by design. Pass criterion: `make plan
  CHUNK=desktop` shows changed=0 and no dconf warning, then log in again from a TTY or second session.
- **Before:** `dconf dump / > ~/dconf-before.ini` (the system snapshot block also takes it; do it before the first desktop run).
- **Undo:** `dconf reset <key>` for each key the role wrote (`/org/gnome/desktop/interface/color-scheme` under sway; the
  `desktop_gnome_hardening_dconf` and `desktop_gnome_ui_dconf` keys in `roles/desktop/defaults/main.yml` under gnome), then
  `dconf load -f / < ~/dconf-before.ini` (load merges and never unsets keys, so reset first; `-f` skips IT-locked keys, without it the first one cancels the load). Never
  `dconf reset /org/gnome/`: it wipes every GNOME setting. Restore or delete `~/.config/sway/config`, `~/.config/swaylock/config`,
  `~/.config/i3/config`, `~/.config/i3status/config`, `~/.config/xdg-desktop-portal/sway-portals.conf`,
  `~/.config/gtk-3.0/settings.ini`, `~/.config/gtk-4.0/settings.ini`, `~/.config/alacritty/alacritty.toml` (in the `make backup`
  directory) and `~/.config/environment.d/terminal.conf`; `sudo rm /etc/X11/xorg.conf.d/90-trackpoint.conf` where present.
  Packages: `sudo dnf history list`, `sudo dnf history info <id>` for the one transaction the desktop run installed, then
  `sudo dnf history undo <id>`. Never `dnf remove` dconf or gnome-keyring by name: GDM and gnome-session depend on them.

## agents

- **Why:** Claude Code and Codex installs and settings.
- **Breaks if wrong:** it installs `bubblewrap` and `socat` with dnf (Claude Code refuses to start without them, `failIfUnavailable: true`), and it overwrites each instance `CLAUDE.md` and deletes a legacy-shaped `settings.local.json` with no backup of its own; settings merges can drop local edits. Run `make backup` right before `make chunk-agents` and check that `~/laptop-setup-backup-<timestamp>/` holds `.claude*/CLAUDE.md` and `.claude*/settings.local.json`.
- **Verify:** `make verify CHUNK=agents` (`claude-privacy-tmpfiles`: the tmpfiles file exists; `codex`: `codex --version` runs, so a failed Codex npm install, which the role only warns about, shows up here). Claude's sandbox settings are not mapped: check `jq .sandbox ~/.claude/settings.json`.
- **Undo:** per instance, restore the oldest `~/.claude*/settings.json.bak-<timestamp>` (written by the merge, which keeps every backup); if the instance had no `settings.json` before the chunk, delete `settings.json` instead (the backup then holds only the seeded file), and delete instance directories the chunk created (for example `~/.claude-per2`). The replaced `CLAUDE.md` and legacy
  `settings.local.json` are only in the `make backup` directory if you ran it before the chunk. Delete `~/.local/share/codex-cli`, `~/.local/bin/codex`, the seeded `config.toml` and `AGENTS.md` in `~/.codex` and `~/.codex-personal` (keep the directories: they also hold the Codex login and history), `~/.config/claude/` and `~/.config/user-tmpfiles.d/claude-privacy.conf`. If `claude_queue_enabled` was on:
  `systemctl --user disable --now claude-queue.timer`, delete `~/.config/systemd/user/claude-queue.{service,timer}` and
  `~/.local/bin/claude-queue-poller.sh`, `systemctl --user daemon-reload`, and `sudo rm /etc/logrotate.d/claude-queue-poller`.
  The `bubblewrap` and `socat` packages can stay (restoring `settings.json` removes the sandbox requirement); `sudo dnf remove socat` if
  wanted.

## repos

- **Why:** git clones under `~/src`, plus the opt-in notes clone.
- **Breaks if wrong:** nothing system-level, but only missing directories are cloned, so `~/src` (and `~/notes`, when `notes_enabled`) can hold
  clones that predate the chunk, and any clone can hold unpushed commits, stashes or untracked files.
- **Verify:** no check mapped, so `make verify CHUNK=repos` exits 1 ("state is unknown") by design; run `make plan CHUNK=repos` and
  confirm changed=0. Repos on an unreachable VPN host or an unseeded SSH host are filtered out before cloning, so changed=0 does not
  cover them.
- **Undo:** delete only the clones this run created (the "Clone repositories" items that `make plan CHUNK=repos` showed as changed before
  the run), and only after `git -C <dir> status --porcelain`, `git -C <dir> stash list` and
  `git -C <dir> log --branches --not --remotes` are all empty. Never delete `~/src` or `~/notes` (`notes_repo_path` in
  `roles/notes/defaults/main.yml`) wholesale. The lfs filter in `~/.gitconfig` belongs to `dotfiles`; undo it there.

## containers

- **Why:** rootless podman pull policy and fully-qualified image names.
- **Breaks if wrong:** pulls from registries outside `containers_policy_unsigned_registries` (and, on DNF hosts, the Red Hat registries, which need a valid signature) are rejected; short image names fail. The chunk also writes `containers.conf` (`k8s-file` log driver, so `journalctl --user` no longer shows container logs; pids and nofile limits) and enables `podman.socket`.
  Rootless podman needs `/etc/subuid`, `/etc/subgid` and `loginctl enable-linger`, which `sys_signin` writes except on RHEL CSB
  (`csb_rhel`), where IT owns them; on an SSSD account without them (not RHEL CSB), enable `sys_signin` first or run
  `echo "$USER:165536:165536" | sudo tee -a /etc/subuid /etc/subgid` and `loginctl enable-linger $USER` (`usermod` cannot see an SSSD-only
  account); on RHEL CSB, ask IT.
- **Verify:** `make verify CHUNK=containers` (`fqin-enforcement`, `containers-policy`).
- **Undo:** if `distrobox` is enabled, remove it from `rollout_enable` as well (it requires `containers`; `make containers` refuses the config otherwise). Then set `containers_policy_enabled: false` and/or `containers_enforce_fqin: false` (and `containers_environment_d_enabled: false` to remove
  `~/.config/environment.d/containers.conf`, `containers_prune_enabled: false` to stop `podman-prune.timer`) in `config.yml`, run `make containers`
  (removes `~/.config/containers/policy.json` and `registries.conf`, keeping no backup). The flags do not touch the rest of the chunk: run `systemctl --user disable --now podman.socket`, and restore the pre-chunk `~/.config/containers/containers.conf` from its oldest `containers.conf.<timestamp>~` (or delete the file if there was none). Restore the oldest `policy.json.<timestamp>~` or `registries.conf.<timestamp>~` only if you had your own file before the chunk; newer backups are older generated files.

## distrobox

- **Why:** dev container for tools the host cannot run (CSB).
- **Breaks if wrong:** needs the rootless-podman prerequisites from `## containers`, and `distrobox` from the `packages` chunk applied
  first. Without the `distrobox` binary the role falls back to Toolbx when `toolbox` exists: no `distrobox.ini` network or capability
  settings. Apply `packages`, then rebuild with `make container-rebuild`.
- **Verify:** `make verify CHUNK=distrobox` (`distrobox-container`). WARN while the vault is still the empty stub: the role defers
  container creation until `vault_ssh_auth_key(_pub)` are set (`make setup-yubikeys`, then `make container`).
- **Undo:** `podman rm -f fedora-dev` (or the configured `container_name`; works for Distrobox and Toolbx containers). Then remove the
  exported wrappers that would shadow host tools:
  `for f in oc kubectl golangci-lint; do grep -q '^# distrobox_binary' ~/.local/bin/$f 2>/dev/null && rm ~/.local/bin/$f; done`.
  Tools Go-installed into the shared `~/go/bin` stay; delete them by hand if unwanted.

## login_shell

- **Why:** zsh as the login shell. Before applying, record the current shell: `[ -e ~/login-shell-before.txt ] || getent passwd $USER | cut -d: -f7 | tee ~/login-shell-before.txt` (the guard keeps a second run from overwriting it with `/bin/zsh`).
- **Breaks if wrong:** sudo and login resolve the account through sssd, so a broken sssd change can lock out the only account. If sssd will not start, sudo stops working: use `## Rescue boot` and in the chroot delete `/etc/sssd/conf.d/90-laptop-setup-shell.conf`. Have a local root break-glass first, as for system chunks.
- **Verify:** `make verify CHUNK=login_shell` (`login-shell-zsh`) and a fresh TTY login.
- **Undo:** on a local account `sudo usermod -s "$(cat ~/login-shell-before.txt 2>/dev/null || echo /bin/bash)" $USER`. On an SSSD account, in order:
  1. Remove `login_shell` from `rollout_enable`.
  2. `sudo rm /etc/sssd/conf.d/90-laptop-setup-shell.conf`.
  3. `sudo sssctl config-check && sudo systemctl restart sssd`; `getent passwd $USER` must show the old shell.
  4. `sudo sss_cache -E`. Only now remove zsh if wanted.

## redhat

- **Why:** Red Hat IT tooling (VPN client, certificates).
- **Breaks if wrong:** on RHEL CSB the role sets the IT-owned `splunk` account shell to nologin (`roles/redhat/tasks/main.yml`).
- **Verify:** no check mapped.
- **Undo:** `sudo dnf remove` the VPN client and certificate packages the role installed; IT's own VPN client is untouched. For
  `splunk`: `sudo usermod -s <old shell> splunk` (old shell from the snapshot `/etc/passwd`); tell IT first, it is their account.

## repos_dnf

- **Why:** third-party dnf repositories (RPM Fusion, Tailscale and similar).
- **Breaks if wrong:** a bad repo or key breaks `dnf` until removed. IT's repo files are never edited.
- **Verify:** no check mapped; `dnf repolist`.
- **Undo:** delete the `.repo` files the role wrote under `/etc/yum.repos.d` (RPM Fusion ones belong to the `rpmfusion-free-release` /
  `rpmfusion-nonfree-release` packages: `sudo dnf remove` those instead), `sudo dnf versionlock delete google-cloud-cli acli` if the role
  pinned them, then `sudo dnf clean all`. Imported repo GPG keys stay in the rpmdb (harmless).

## sys_baseline

- **Why:** baseline settings: SELinux enforcing, service masking, logind, journald, cron access, dconf/GDM locks, banners, firmware,
  timezone and locale.
- **Breaks if wrong:** masked units stop working (Bluetooth, ModemManager, avahi, ABRT, openvswitch/OVN, cups-browsed, depending on
  toggles); `cron.allow` blocks non-root crontabs.
- **Verify:** `make verify CHUNK=sys_baseline` (`selinux`, `login-banner`, `cron-allow-root`, `journald-persistent`, `ctrl-alt-del-masked`, `logind-idle-sec`, `fwupd-refresh-timer`).
- **Undo:** delete what the role created (restore from the snapshot where IT or the package owns the file), then
  `sudo systemctl daemon-reload`:
  - `/etc/systemd/logind.conf.d/99-hardening.conf`, `/etc/systemd/journald.conf.d/99-hardening.conf`;
  - `/etc/dconf/db/local.d/{50-hardening,51-screensaver}` and their `locks/` files, `/etc/dconf/db/gdm.d/03-hardening` and
    `locks/03-hardening`; then `sudo dconf update`; `/etc/gdm/custom.conf` (XDMCP line);
  - `/etc/cron.allow`, `/etc/at.allow`; restore `/etc/issue` and `/etc/issue.net`;
  - masks: list with `systemctl list-unit-files | grep masked`, then `sudo systemctl unmask <unit>` (including `passim`);
    `sudo systemctl disable --now fwupd-refresh.timer`; restore `/etc/fwupd/fwupd.conf` and `/etc/fwupd/remotes.d/lvfs.conf`;
  - `/etc/locale.conf`, `/etc/vconsole.conf`, timezone (`sudo timedatectl set-timezone <old>`), `/etc/bluetooth/main.conf`;
  - SELinux: the persistent mode is `/etc/selinux/config` (`sudo setenforce 0` is a temporary test only).
  - Power stack (only with `system_tlp_enabled: true`, default false): set it `false` and re-run the chunk (a guarded transition back to
    tuned), or by hand `sudo systemctl disable --now tlp`, unmask and enable the native service, `sudo dnf remove tlp`; delete
    `/etc/tlp.d/50-thinkpad.conf`.

## sys_sysctl

- **Why:** kernel hardening (ptrace scope, kptr restrictions, rp filter), core dumps, module blacklist, kernel modules for kind.
- **Breaks if wrong:** `ptrace_scope=1` blocks debugger attach to non-children; IPv6 router advertisements are off by default. The
  Bluetooth modules are blacklisted and unloaded live (`rfcomm bnep btusb bluetooth`), so a Bluetooth keyboard or mouse drops at this
  step: use a wired or dongle keyboard, or set `system_disable_bluetooth: false`.
- **Verify:** `make verify CHUNK=sys_sysctl` (`coredump-configured`, `modprobe-hardening`); `sysctl-ptrace-scope` and `sysctl-no-accept-ra` read the expected value from the deployed file; other sysctl values are not mapped.
- **Undo:** delete `/etc/sysctl.d/90-hardening.conf`, `/etc/modprobe.d/hardening.conf`,
  `/etc/systemd/coredump.conf.d/coredump.conf`, `/etc/security/limits.d/90-coredump.conf` and the kind modules in `/etc/modules-load.d`
  (iptables, bridge, overlay, `tun`, KVM, vhost; check `ls /etc/modules-load.d`). On RHEL CSB the role writes
  `/etc/sysctl.d/91-ansible-ipforward.conf` instead (delete it). Then `sudo sysctl --system` and reboot: re-reading files does not
  reset values already set in the running kernel.

## sys_time_dns

- **Why:** chrony NTS, DNS-over-TLS, dnf-automatic.
- **Breaks if wrong:** DoT fails on restricted networks (see `references/troubleshooting.md` § DNS). On a CSB host (`csb_detected`)
  chrony NTS, dnf-automatic and the DoT drop-in are not applied (an existing `99-dot.conf` is removed), so the chunk does not add chrony
  or dnf-automatic there. On hybrid CSB it still enables systemd-resolved and writes `98-llmnr.conf`. `make verify` passes
  `dnf-automatic` by design and FAILs `chronyd-service` unless IT already runs chronyd. IT-owned time, update and DNS config is never replaced.
- **Verify:** `make verify CHUNK=sys_time_dns` (`chronyd-service`, `dnf-automatic`).
- **Undo:** on a CSB host (`csb_detected`) delete only `/etc/systemd/resolved.conf.d/98-llmnr.conf` and run
  `sudo systemctl restart systemd-resolved`; the rest applies elsewhere: restore `/etc/chrony.conf` and `/etc/dnf/automatic.conf` from the
  snapshot (the role keeps no backup); delete
  `/usr/local/bin/laptop-setup-validate-chrony` and `/etc/systemd/resolved.conf.d/{98-llmnr,99-dot}.conf`, then
  `sudo systemctl restart systemd-resolved`; `sudo systemctl disable --now dnf5-automatic.timer` (`dnf-automatic.timer` on dnf4).

## sys_optional

- **Why:** session timeout, AIDE, weekly setuid scan, GRUB password, kernel module-load lock; each is off or opt-in except the scan.
- **Breaks if wrong:** `system_tmout` ends idle shells; AIDE adds log volume; a GRUB password blocks the `e` edit in
  `## Rescue boot`; the module lock is one-way (below).
- **After later system chunks:** with AIDE on, each later system chunk edits monitored paths; run `make aide-rebaseline` after each, or the daily check alerts.
- **Verify:** `make verify CHUNK=sys_optional` (`setuid-scan-timer` only; the timeout, AIDE and GRUB password are opt-in and have no mapped check).
- **Undo:**
  - Timeout: `system_tmout: 0` and re-run; the role removes it from `/etc/profile.d/tmout.sh`, `/etc/zshrc`, `/etc/zsh/zshrc` and
    `/etc/bash.bashrc`, so deleting the profile.d file alone is not enough.
  - AIDE: `system_aide_enabled: false` and re-run removes the units and package (or `sudo systemctl disable --now aide-check.timer`).
  - Setuid scan (`system_setuid_scan_enabled`, default on, independent of AIDE): set it `false` and re-run, or
    `sudo systemctl disable --now setuid-scan.timer` and delete `/usr/local/lib/setuid-scan.sh`, `setuid-scan.{service,timer}`
    in `/etc/systemd/system` and `/var/lib/setuid-scan/`; `sudo systemctl daemon-reload`.
  - GRUB password: `system_grub_password_enabled: false` and re-run removes the `set superusers=`, `password_pbkdf2` and CIS 1.4.2
    comment lines from `/etc/grub.d/40_custom` (the file and any user entries stay), deletes `/etc/grub.d/41_protect_entries`, and
    regenerates the config.
  - Module lock (`system_modules_disabled`, default 0): once `kernel.modules_disabled=1` is active only a reboot clears it. Setting 0
    stops, disables and removes `kernel-modules-lockdown.service`; the role does not remove
    `/usr/local/libexec/laptop-setup-wait-firewalld.py`, so delete it by hand (`sudo rm /usr/local/libexec/laptop-setup-wait-firewalld.py`).

## sys_mounts

- **Why:** `nosuid`, `nodev`, `noexec` on world-writable mounts.
- **Breaks if wrong:** an invalid `fstab` stops boot; `/home` `nosuid` can break `sudo` inside the dev container (not verified).
- **Verify:** `make verify CHUNK=sys_mounts` (`tmp-hardening`, `shm-hardening`, `home-nosuid`, `boot-hardening`, `boot-efi-hardening`); `sudo findmnt --verify`.
- **Undo:** restore the backup with the oldest date in its name (`ls /etc/fstab.*~ | sort -t. -k3 | head -1`; names start with the
  PID, so do not sort by name alone; each edit made one), then `sudo findmnt --verify` before rebooting. Delete
  `/etc/systemd/system/tmp.mount.d/hardening.conf` and the remount units the role wrote under `/etc/systemd/system`
  (`dev-shm-harden.service`, and `boot-harden.service` or `boot-efi-harden.service` where present), then
  `sudo systemctl daemon-reload`.

## sys_lockdown

- **Why:** kernel lockdown (`lockdown=integrity`), `vsyscall=none`, `init_on_free`, IOMMU arguments.
- **Breaks if wrong:** boot failure; endpoint agents that load kernel code can stop working (behaviour under lockdown unverified).
  Needs a reboot to take effect.
- **Verify:** `make verify CHUNK=sys_lockdown` (`kernel-lockdown`, `kernel-cmdline`); `/proc/cmdline` after reboot; agents' health.
- **Undo:** if it boots, set `system_kernel_lockdown: ''` and re-run: this removes only `lockdown=`; `vsyscall=none`, `init_on_free=1`
  and the IOMMU arguments stay until `grubby --remove-args`. If it does not boot, see `## Rescue boot`, then restore `/etc/default/grub`
  and `/etc/kernel/cmdline` from the snapshot (the role keeps no backup). `system_remove_luks_key: true` (default false) removes
  `rd.luks.key` from the kernel arguments and has no undo: restore the arguments from the snapshot.

## sys_auditd

- **Why:** audit trail for sensitive file access.
- **Breaks if wrong:** if the audit log is ingested by a SIEM, a noisy rule set becomes telemetry volume; a halting `disk_full_action`
  could stop the machine (unverified).
- **Verify:** `make verify CHUNK=sys_auditd` (`auditd-service`, `auditd-rules`).
- **Undo:** delete `/etc/audit/rules.d/claude-code.rules` and `/etc/audit/claude-code-policy.json`, then `sudo augenrules --load`.
  On non-CSB hosts (`-e 2`, immutable) that reload fails with EPERM and the removed rules stay active until the next reboot; boot then
  loads the rules without them. On CSB-detected hosts (`-e 1`) the reload applies immediately.
  Restore `/etc/audit/auditd.conf` from the snapshot (overwritten with no backup; skipped on CSB-detected hosts). Empty watch-parent
  directories the helper created (for example `/etc/usbguard`, `/var/lib/aide`, `~/.gnupg`, `~/.aws`, `~/.kube`) are harmless. The role
  also deletes `-a task,never` lines from the `/etc/audit/rules.d/*.rules` files (for example a copy of the packaged
  `10-no-audit.rules`, where present) with no backup: restore that file from the snapshot. With AIDE on, run `make aide-rebaseline` afterwards.

## sys_usbguard

- **Why:** block unknown USB devices.
- **Breaks if wrong:** a keyboard, dock or security key not allowed before USBGuard starts is blocked. Run `make usb-add` with every
  device you use attached first.
- **Verify:** `make verify CHUNK=sys_usbguard` (`usbguard`, `usbguard-implicit-policy`, `usbguard-yubikey-rule`; the last two read 0600 files, so run as root).
- **Undo:** `sudo systemctl disable --now usbguard`, or `system_install_usbguard: false` and re-run (the role stops and disables it).
  Restore `/etc/usbguard/rules.conf` and `usbguard-daemon.conf` from the snapshot (overwritten/edited, no backup).

## sys_signin

- **Why:** sudo logging and `use_pty`, password quality, faillock, `pam_wheel`, account aging, rootless-podman prerequisites.
- **Breaks if wrong:** PAM changes can lock the account out. The sudoers drop-in scopes `log_input,log_output`, `umask=0027` and
  `use_pty` to non-root invokers (`Defaults:!root`), so IT's root-run automation is unaffected. On non-CSB hosts root is locked
  (`password_lock`), so a "known root password" break-glass no longer works: use `## Rescue boot`.
- **Verify:** `make verify CHUNK=sys_signin` (`authselect-check`, `authselect-faillock`, `authselect-pwhistory`, `sudoers-hardening`, `pam-wheel`).
- **Undo:**
  - PAM: `sudo authselect select <saved profile> <saved features> --force` from `authselect-current.txt` (the role's own PAM backup
    is deleted after the run).
  - Sudoers: delete `/etc/sudoers.d/99-hardening` (and a `go-env` drop-in if present); check with `sudo visudo -c`.
  - `pam_wheel` line in `/etc/pam.d/su`: remove it, or `system_pam_wheel_su: false` and re-run.
  - Restore `/etc/security/{faillock,pwquality,pwhistory}.conf` from the snapshot; delete `/etc/tmpfiles.d/sudo-io.conf` and
    `/etc/logrotate.d/sudo`; `sudo systemctl unmask fprintd.service` if `system_disable_fingerprint` is true.
  - Aging (every host except RHEL CSB): `sudo useradd -D -f -1`, plus `sudo chage -M 99999 -I -1 $USER` only for a local
    `/etc/shadow` account (SSSD/IPA users are skipped by the role), and the snapshot copy of `/etc/login.defs`. Root:
    `sudo passwd -u root` helps only if a root password was ever set.
  - Rootless podman: `loginctl disable-linger $USER`; remove the `<user>:165536:165536` lines from `/etc/subuid` and `/etc/subgid` only
    if nothing else uses the range (containers and distrobox depend on them).

## ssh_authorized_keys

- **Why:** key-only login needs exactly the vault key in `authorized_keys`.
- **Breaks if wrong:** the exclusive write removes every other key, including any backup key; with sshd hardened an RSA key is rejected.
- **Verify:** `make verify CHUNK=ssh_authorized_keys`; log in from a second device with the new key before closing any session.
- **Undo:** restore `~/.ssh/authorized_keys` from `~/authorized_keys.before` (the `cp -a ~/.ssh/authorized_keys` line of the snapshot block: run it
  just before this chunk if no system chunk has been run yet, the role takes no copy; `make backup` also copies this file, but the copy
  taken just before the write is the reliable restore point), keeping a console or second session open until then.

## sys_remote_access

- **Why:** inbound SSH over Tailscale (the corporate VPN client does not carry inbound SSH); sshd on port 722, ed25519 or FIDO2 keys
  only. IT's sshd drop-in (`99-csb-remote-access.conf`) and the `redhat` zone file are not rewritten; the local `ssh` service they
  reference now points at the sshd port. The default zone (now `drop`), the `drop` zone and
  the `trusted` zone (tailscale0) are changed (see Undo); the IT-managed `FedoraWorkstation` zone is left alone on CSB hosts.
- **Breaks if wrong:** an unaccepted key or a firewall/sshd mismatch locks out remote access. On hybrid CSB Fedora with `ssh_port`
  other than 22, the firewalld `ssh` service is repointed to the sshd port; `tailscale0` is in the `trusted` zone, so no port rule is
  needed for the tailnet.
- **Verify:** `make verify CHUNK=sys_remote_access` (`sshd-hardening`, `sshd-port`, `sshd-port-bound`, `firewall-ssh-port`, `firewall-tailscale-permanent`); login from a second tailnet
  device, `sudo sshd -T`, and a local console login.
- **Undo:** keep a working session open until a fresh login succeeds. Most of these steps are skipped on RHEL CSB.
  1. sshd: delete `/etc/ssh/sshd_config.d/00-hardening.conf`, `sudo systemctl reload sshd`. The role also adds an `Include` line to
     `/etc/ssh/sshd_config` (backup `sshd_config.<pid>.<YYYY-MM-DD@HH:MM:SS>~`) and deletes every other
     `sshd_config.d/*.conf` except its own, the package-owned crypto-policy and `50-redhat.conf` files and, on `csb_detected` hosts, IT's
     `99-csb-remote-access.conf` (leave that one to IT): restore the others from the snapshot. Last, once sshd is back on port 22,
     `sudo systemctl unmask sshd.socket` (the role masks it; skipped on RHEL CSB).
  2. Firewall: delete `/etc/firewalld/services/ssh.xml`. The default zone becomes `drop`, physical interfaces move into it with a
     permanent `722/tcp` rule. Using `~/net-before.txt` from the snapshot:
     `sudo firewall-cmd --set-default-zone=<previous>`, `sudo firewall-cmd --permanent --zone=drop --remove-port=722/tcp`, for each interface that `--get-active-zones` in `~/net-before.txt`
     lists in another zone `sudo firewall-cmd --permanent --zone=<its zone> --change-interface=<if>`,
     on non-CSB hosts only, `sudo firewall-cmd --permanent --zone=FedoraWorkstation --add-service={ssh,dhcpv6-client,samba-client}` (the role also removed the
     1025-65535 tcp/udp ports there: re-add only if wanted), then `sudo firewall-cmd --reload`. Zone files are also in the snapshot `/etc/firewalld/`.
  3. SELinux: `sudo semanage port -d -t ssh_port_t -p tcp 722`, only after sshd is back on port 22.
  4. Crypto policy: only on hosts that are not `csb_detected` (a CSB host keeps IT's policy, nothing to undo), `sshd.yml` runs
     `update-crypto-policies --set DEFAULT:NO-SHA1`, which also affects TLS, dnf and Kerberos, and restarts
     sshd and chronyd. Undo there: `sudo update-crypto-policies --set <previous>` (`DEFAULT` on stock Fedora; value in `~/net-before.txt`).
  5. NetworkManager: delete `tailscale.conf`, `99-wifi-powersave.conf`, `99-dhcp-privacy.conf` and `99-dns.conf` under
     `/etc/NetworkManager/conf.d`, `sudo systemctl restart NetworkManager`, and
     `sudo firewall-cmd --permanent --zone=trusted --remove-interface=tailscale0 && sudo firewall-cmd --reload`.
  6. Broad alternative: extract `/etc/firewalld/` and `/etc/NetworkManager/` from the snapshot to a scratch directory and copy single
     files back.
