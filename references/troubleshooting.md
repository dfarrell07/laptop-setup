# Troubleshooting

Symptom first, grouped by area; chunk errors come first. Per-chunk undo steps and the rescue-boot recipe are in
[docs/rollout-recovery.md](../docs/rollout-recovery.md). First-run steps are in `CLAUDE.md`. Set overrides in `config.yml`, then re-run the
named make target.

## Chunk selection

| Symptom | Fix |
| --- | --- |
| a role target (`make dotfiles`...) does nothing | its chunk is not in `rollout_enable`; `make status` shows ON/off, add it to `config.yml` |
| `chunk 'x' is not enabled; add it to rollout_enable` (`make chunk-x`, `plan`, `verify`) | same: add the chunk to `rollout_enable` |
| `unknown chunk 'x' (did you mean ...)` or `rollout_enable ... unknown chunk` | typo; names are in `make status` / `group_vars/all/rollout.yml` |
| `rollout_enable is missing prerequisites` | add the listed chunks too; prerequisites are never enabled implicitly |
| `rollout_enable lists a chunk twice`, `use 'all' or a list of chunk names`, `config.yml is not valid YAML` | fix `rollout_enable` in `config.yml` |
| `-e rollout_enable=...` rejected | set it in `config.yml` only |
| Play 0: `every chunk that is not enabled must be passed as --skip-tags` or `rollout_enable must be 'all' or a list of known chunks` | you ran `ansible-playbook` directly; use `make` (or `scripts/verify-ansible-args.sh`), which adds the skip tags |
| `usage: make plan CHUNK=<name>` | `plan` needs a chunk; `make check` dry-runs the whole enabled list |
| `'x' is a high-risk chunk ... run it from a terminal` or `not confirmed` | run `make chunk-x` from a terminal and type the chunk name when asked |
| `make verify CHUNK=x` says state is unknown | no smoke check is mapped to that chunk (prints `state is unknown`). A mapped check that did not run prints `SKIP  <check> — check did not run on this box (not applicable, or needs root)` and the run exits 1 (see the next row) |
| `make verify CHUNK=x` exits 1 though nothing FAILed | it also fails on WARN and SKIP; root-only checks are WARN or SKIP without sudo: run the `sudo scripts/smoke-test.sh --system-only --only ...` command it prints |

## Hardening toggles that surprise people

| Symptom | Setting in `config.yml` (default) | Re-run |
| --- | --- | --- |
| No Bluetooth adapter, `rfkill` blocked | `system_disable_bluetooth: false` (true) | `make system` |
| USB drive does not mount | `make usb-add` (USBGuard blocks unlisted drives); `system_disable_usb_storage` (false; true also blocks `usb_storage`/`uas` in the kernel) | `make usb-add`, `make system` |
| Phone tethering or USB modem missing | `system_disable_modemmanager: false` (false) | `make system` |
| `.local` names do not resolve | `system_disable_avahi: false` (true) | `make system` |
| ABRT units `masked` | `system_mask_abrt: false` (true) | `make system` |
| Shell exits when idle | `system_tmout: 0` (0 = off; 1-900 enforces) | `make system` |
| `ssh -L` / VS Code port panel fails | `system_ssh_allow_tcp_forwarding: local` (local; `no` blocks all, `yes` adds `-R`) | `make system` |
| Account locked for 15 minutes | `system_faillock_unlock_time` (900); immediate fix in [recovery](../docs/rollout-recovery.md#rescue-boot) | `make system` |

Overrides persist across runs; delete one to restore the default.

## Boot fails or lockdown blocks a tool

`make system` adds `lockdown=integrity`, the IOMMU flag, `vsyscall=none` and `init_on_free=1` with `grubby` (Fedora and hybrid CSB only;
RHEL CSB and Debian skip it). If the machine does not boot, follow [Rescue boot](../docs/rollout-recovery.md#rescue-boot).

`make system` re-adds removed arguments, so also set `system_kernel_lockdown: ''` and/or `system_init_on_free: false` in `config.yml`
(each weakens hardening). Lockdown also causes:

- `systemctl hibernate` returns EPERM (suspend is unaffected).
- `bpftrace` `kprobe:`/`kretprobe:` fail with EPERM even as root. Use `tracepoint:`/`uprobe:`, or disable lockdown as above.
  State: `cat /sys/kernel/security/lockdown`.

## SSH

| Symptom | Cause / fix |
| --- | --- |
| Connection refused after provisioning | sshd moved to port 722 (`ssh_port`): `ssh -p 722 user@host` or `Port 722` in `~/.ssh/config`. To change it, set `ssh_port` (not 22) in `config.yml` and re-run `make system` from the console or tmux |
| sshd fails or rejects you | from the console inspect `/etc/ssh/sshd_config.d/00-hardening.conf` (`AllowUsers`, `Port`) and `sudo sshd -T \| grep -i port`. Only ed25519 and FIDO2 (`sk-ssh-ed25519`) keys are accepted; an empty vault writes no `authorized_keys` |
| FIDO2 signing: "agent refused operation" | `SSH_AUTH_SOCK` points at a GNOME agent. The dotfiles role masks `gcr-ssh-agent` and deploys `ssh-agent.service` plus `environment.d`; log out and in, then `echo $SSH_AUTH_SOCK` must be `/run/user/<uid>/ssh-agent.socket` |
| `no matching key exchange method` to an old server | crypto policy is `system_crypto_policy_target` (`DEFAULT:NO-SHA1`). Scope an exception to one host: `ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa user@host`, or the same options in a `Host` stanza |
| Smoke test cannot see the YubiKey | `systemctl status pcscd.socket` (`sudo systemctl enable --now pcscd.socket`), `lsusb \| grep -i yubico`, and USBGuard must allow `1050:*`. FIDO2 SSH keys do not need pcscd |

## Vault password file missing

`ERROR! The vault password file .../scripts/vault-pass.sh was not found`: the file is gitignored. Run `make bootstrap` (creates a stub),
then `make setup-yubikeys` or follow `SECURITY.md` "Setting Up vault-pass.sh". `make preflight` reports stub, valid, too short or empty.

## system: USBGuard Blocks YubiKey or Keyboard

Plug the device in and run `make usb-add`, then `make system`. While USBGuard runs, `usb-add` allows every blocked device now and saves
rules to `system_usbguard_extra_rules`; while it is stopped it only saves rules for connected devices no rule covers. By hand:
`sudo usbguard list-devices --blocked`, `sudo usbguard allow-device <id>`, then add a rule to `system_usbguard_extra_rules`. `usb-add`
writes this form (hub interface class 09:00:00):

```yaml
system_usbguard_extra_rules:
  - 'allow id 2109:0817 name "USB3.0 Hub" with-interface 09:00:00'
```

On CSB, USBGuard may already be IT-managed.

## Account lockout, PAM and authselect

Lockout fixes (faillock, inactive account, no root) are in [Rescue boot](../docs/rollout-recovery.md#rescue-boot).

If `make smoke-test` reports `authselect-profile`/`authselect-check` FAIL or `pam_faillock.so` is missing from `/etc/pam.d/system-auth`:
the role runs `authselect select sssd --force` only on non-CSB hosts. By hand there:

```bash
sudo authselect select sssd --force
sudo authselect enable-feature with-faillock
sudo authselect enable-feature with-pwhistory
sudo authselect check
```

On CSB hosts do not use `--force` (it wipes IT-managed PAM customizations); ask IT.

## DNS

`system_dot_mode: opportunistic` writes `/etc/systemd/resolved.conf.d/99-dot.conf` (Cloudflare, catch-all `system_dns_domains: "~."`);
skipped on CSB hosts. Diagnose with `resolvectl status` and `resolvectl query example.com`.

- **Slow or failing on a restricted network:** set `system_dot_mode: "no"` (unencrypted DNS), optionally `system_dns_primary` to the local
  resolver; revert afterwards.
- **Internal names fail on a VPN:** NetworkManager VPNs that push routing domains win over `~.`. If not, set `system_dns_domains: ""`
  and re-run `make system`.
- **standalone wg-quick DNS:** `wg*` interfaces are NM-unmanaged on non-CSB hosts, so a `DNS =` line gets no routing domain and the
  catch-all wins. Register one in the tunnel config, or import the profile into NetworkManager
  (`nmcli connection import type wireguard file /etc/wireguard/wg0.conf`):

  ```ini
  PostUp = resolvectl dns %i <vpn-nameserver-ip>; resolvectl domain %i ~<internal-domain>
  PreDown = resolvectl revert %i
  ```

- **`resolv.conf` symlink task fails:** NetworkManager owns `/etc/resolv.conf`. Re-run `make system` (deploys
  `/etc/NetworkManager/conf.d/99-dns.conf` with `dns=systemd-resolved`), restart NetworkManager from the console or tmux, and check
  `readlink /etc/resolv.conf` is `/run/systemd/resolve/stub-resolv.conf`. On any CSB host (`csb_detected`) `99-dns.conf` is not deployed
  (removed if present); only the stub symlink is enforced on hybrid Fedora CSB, and RHEL CSB skips it. Chrony NTS and dnf-automatic are
  likewise not applied on CSB.

## Containers and OVN-K / Submariner

- **Rootless podman `newuidmap` / "not enough IDs":** `/etc/subuid` and `/etc/subgid` lack your user. The system role writes
  `<user>:165536:165536` except on RHEL CSB; there (or if `sys_signin` is off) run
  `echo "$USER:165536:165536" | sudo tee -a /etc/subuid /etc/subgid` or ask IT, then check `podman unshare cat /proc/self/uid_map`.
- **`kind` run through `make` in the OVN-K/Submariner repos (their own target, not one here) uses Docker or cannot reach Podman:**
  `make` runs `sh`, not zsh, so `.zshrc` exports are not seen. Set `containers_environment_d_enabled: true` (default false), run
  `make containers`, log out and in. It writes `~/.config/environment.d/containers.conf` (`DOCKER_HOST`,
  `KIND_EXPERIMENTAL_PROVIDER=podman`). Check `ls "$XDG_RUNTIME_DIR/podman/podman.sock"`.
- **`ovs-vsctl: cannot connect to ovsdb-server`:** the system role masks openvswitch and OVN units on the work profile. Temporary:

  ```bash
  sudo systemctl unmask ovsdb-server.service ovs-vswitchd.service
  sudo systemctl start ovsdb-server.service ovs-vswitchd.service
  ```

  The next `make system` re-masks them.
- **`nf_conntrack: table full`:** the playbook sets `net.netfilter.nf_conntrack_max` to 524288. Check
  `sysctl net.netfilter.nf_conntrack_max`; if lower, `sudo modprobe nf_conntrack && sudo sysctl --system`
  (modprobe fails until reboot while `system_modules_disabled: 1`).

## kernel.unprivileged_bpf_disabled=2 Is Write-Once

`sysctl -w kernel.unprivileged_bpf_disabled=0` fails with EPERM even as root. The default (`system_unprivileged_bpf_disabled: 2`) locks
the sysctl until reboot; there is no supported override at 2. Root-level bpfman and ovnkube-node are unaffected. Do not lower it
casually: the distrobox container holds CAP_BPF and CAP_PERFMON, which become active for every process in it (including package-manager
dependencies) once unprivileged `bpf()` is allowed. Value 1 needs a custom kernel.

## CSB (Corporate Standard Build)

- **fapolicyd blocks `pip`, `go install` or downloaded binaries ("Operation not permitted"):** only RPM-installed binaries may run. Do
  those installs in the distrobox container (`make container`), or ask IT for a trust rule (`fapolicyd-cli --file add <path>`).
- **fapolicyd blocks `distrobox enter`:** it watches tmpfs and overlay mounts. Ask IT to drop `tmpfs` from `watch_fs` in
  `/etc/fapolicyd/fapolicyd.conf`; use the RPM-installed distrobox (or Toolbx, the role's fallback).
- **Ansible `AnsiballZ_*.py: Operation not permitted`:** `ansible.cfg` sets `pipelining = true`; run from the repo root so it loads.
- **Third-party repos (Chrome, Docker CE, Tailscale...) on CSB:** Chrome is skipped on any CSB; Docker CE and Tailscale are skipped on
  RHEL CSB only. A repo failure prints only a `REPO SETUP FAILED` warning, and no IT-ticket text is recorded for these repos (only acli
  and the Red Hat internal repo record it, on RHEL CSB). Use the container for those tools.
- **Login shell stays `/bin/bash` on an SSSD account:** the role writes `override_shell` to `/etc/sssd/conf.d/90-laptop-setup-shell.conf`
  only if `sssd.conf` or `conf.d` has a `[domain/...]` section. Otherwise add `override_shell = /bin/zsh` to the `[domain/...]`
  stanza in a root-owned `/etc/sssd/conf.d/*.conf` (same group and mode as `sssd.conf`), then `sudo systemctl restart sssd`, or ask IT to
  set `override_shell` or the LDAP `loginShell`. Do not use `chsh`: it cannot change an account with no `/etc/passwd` entry.
- **Firewall tasks on RHEL CSB** skip the drop-zone and libvirt edits (IT owns zone policy), but the CSB fallback still opens the SSH port
  in the default zone. The `Assert trusted zone incompatible with CSB hardening` task fails the run when the default zone is `trusted`;
  move the host to `public` or `FedoraWorkstation` (or set `system_firewall_manage_zone: true`) before re-running.

## Notes repo

- **Clone fails (`Permission denied`, `Repository not found`):** the repo is private. `gh auth login` (HTTPS fallback) or set
  `notes_remote_url`/`notes_remote_url_https` in `config.yml`; `notes_enabled: false` (default) skips the role. Then `make notes`.
- **Files in `~/notes` are ciphertext (`U2FsdGVkX1...`):** set `vault_notes_transcrypt_password` in `group_vars/all/vault.yml`,
  `make packages` if transcrypt is missing, then `make notes`.

## Makefile refuses to run as root

`ERROR: Do not run as root. Use -K for privilege escalation (make all).` (`guard-not-root`): run `make` as your own user; Ansible
escalates itself. As root it would install dotfiles into `/root` and write `AllowUsers root`.
