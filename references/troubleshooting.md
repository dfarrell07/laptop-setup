# Troubleshooting

Known failure modes, by area. First-run steps and post-provision actions are in `CLAUDE.md`. Set overrides in `config.yml`, then
re-run the named make target.

## Hardening toggles that surprise people

| Symptom | `config.yml` override (default) | Re-run |
| --- | --- | --- |
| No Bluetooth adapter / `rfkill` blocked | `system_disable_bluetooth: false` (true) | `make system` |
| USB drive does not mount | `make usb-add` (USBGuard blocks unlisted drives); `system_disable_usb_storage: false` (false; blocks `usb_storage`/`uas` when true) | `make usb-add`, `make system` |
| Phone tethering / USB modem missing | `system_disable_modemmanager: false` (false) | `make system` |
| `.local` names do not resolve | `system_disable_avahi: false` (true) | `make system` |
| ABRT units show `masked` | `system_mask_abrt: false` (true) | `make system` |
| CUPS not running | `system_disable_printing: false` (false) | `make system` |
| Shell exits when idle | `system_tmout: 0` (0 = off; 1-900 enforces) | `make system` |
| `ssh -L`/VS Code port panel fails | `system_ssh_allow_tcp_forwarding: local` (local; `no` blocks all) | `make system` |
| `bpftool prog dump jited` shows blinded constants | `system_bpf_jit_harden: 1` (1) | `make system` |
| Account locked for 15 minutes | `system_faillock_unlock_time` (900) | `make system` |

Override-driven weakening persists across runs; delete the override to restore the default.

## Boot fails or lockdown blocks a tool

`make system` adds `lockdown=integrity`, the IOMMU flag, `vsyscall=none` and `init_on_free=1` with `grubby` (Fedora and hybrid CSB
only; RHEL CSB and Debian skip it).

Recovery: at GRUB press `e`, delete the offending argument on the `linux` line, Ctrl+X (this boot only), or boot an older kernel.
If it never reaches userspace, add `rd.break`, then `mount -o remount,rw /sysroot && chroot /sysroot`. Once booted:

```bash
sudo grubby --remove-args="lockdown" --update-kernel=ALL
sudo grubby --info=ALL | grep args
```

`make system` re-adds the arguments, so also set `system_kernel_lockdown: ''` and/or `system_init_on_free: false` in `config.yml`
(these weaken physical-access and memory hardening). Lockdown also causes:

- `systemctl hibernate` returns EPERM (lockdown refuses hibernation images; suspend is unaffected).
- `bpftrace` `kprobe:`/`kretprobe:` fail with EPERM even as root. Use `tracepoint:`/`uprobe:`, or disable lockdown as above.
  Check state with `cat /sys/kernel/security/lockdown`.

## SSH

- **Connection refused after provisioning:** sshd moved to port 722 (`ssh_port`). Use `ssh -p 722 user@host` or add `Port 722` to
  `~/.ssh/config`. To revert, set `ssh_port: 22` and re-run `make system` from the console or tmux.
- **sshd fails or rejects you:** inspect `/etc/ssh/sshd_config.d/00-hardening.conf` (`AllowUsers`, `Port`) and `sshd -T | grep -i port`;
  fix from the console. Only ed25519 and FIDO2 (`sk-ssh-ed25519`) keys are accepted; an empty vault writes no `authorized_keys`.
- **FIDO2 signing says "agent refused operation":** `SSH_AUTH_SOCK` points at a GNOME agent. The dotfiles role masks
  `gcr-ssh-agent` and deploys `ssh-agent.service` plus `environment.d`; log out and back in, then confirm
  `echo $SSH_AUTH_SOCK` is `/run/user/<uid>/ssh-agent.socket`.
- **`no matching key exchange method` to an old server:** the crypto policy is `system_crypto_policy_target` (`DEFAULT:NO-SHA1`).
  Scope the exception to one host: `ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa user@host`
  (or the same two options in a `Host` stanza for that exact hostname).
- **Smoke test cannot see the YubiKey:** `systemctl status pcscd.socket` (enable with `sudo systemctl enable --now pcscd.socket`),
  `lsusb | grep -i yubico`, and confirm USBGuard allows `1050:*`. FIDO2 SSH keys do not need pcscd.

## Vault password file missing

`ERROR! The vault password file .../scripts/vault-pass.sh was not found`: the file is gitignored. Run `make bootstrap` (creates a
stub), then `make setup-yubikeys` or follow `SECURITY.md` "Setting Up vault-pass.sh". `make preflight` reports whether the script is
a stub, valid, too short or empty.

## system: USBGuard Blocks YubiKey or Keyboard

Plug the device in and run `make usb-add` (while USBGuard is running it allows every blocked device now and saves rules to
`system_usbguard_extra_rules`; while USBGuard is not running it only saves rules for connected devices no rule covers), then
`make system`. By hand: `sudo usbguard list-devices --blocked`, `sudo usbguard allow-device <id>`, and add a rule to
`system_usbguard_extra_rules`. `make usb-add` saves rules in this form (hub interface class 09:00:00):

```yaml
system_usbguard_extra_rules:
  - 'allow id 2109:0817 name "USB3.0 Hub" with-interface 09:00:00'
```

On CSB, USBGuard may already be IT-managed.

## Account lockout recovery

Root is password-locked and `system_grub_password_enabled: true` blocks GRUB edits.

- **faillock** ("Account locked", 5 failures): wait `system_faillock_unlock_time` seconds or run `faillock --user <name> --reset` from
  another root shell.
- **Inactive account** (`chage -I 30`, not applied on RHEL CSB): boot with `rd.break` at GRUB, then
  `mount -o remount,rw /sysroot && chroot /sysroot`, `chage -I -1 <name>`, `passwd <name>`, and reboot.
- **No root and no working account:** same `rd.break` shell, `passwd -u root; passwd root`, reboot, fix the account, then
  `passwd -l root`. `rescue.target` only works if root has a password.

## PAM lockout inactive (authselect)

`make smoke-test` reports `authselect-profile`/`authselect-check` FAIL, or `pam_faillock.so` is missing from
`/etc/pam.d/system-auth`. The role runs `authselect select sssd --force` (not on CSB-detected hosts). Run it by hand on non-CSB
hosts:

```bash
sudo authselect select sssd --force
sudo authselect enable-feature with-faillock
sudo authselect enable-feature with-pwhistory
sudo authselect check
```

On CSB hosts do not use `--force`; ask IT (it wipes IT-managed PAM customizations).

## DNS

The system role deploys `/etc/systemd/resolved.conf.d/99-dot.conf` (Cloudflare, `system_dot_mode: opportunistic`, catch-all
`system_dns_domains: "~."`). Skipped on CSB hosts. Diagnose with `resolvectl status` and `resolvectl query example.com`.

- **Slow or failing on a restricted network:** set `system_dot_mode: "no"` (unencrypted DNS), and optionally `system_dns_primary` to
  the local resolver. Revert when done.
- **Internal names fail on a VPN:** NetworkManager VPNs that push routing domains win over `~.` automatically. If not, set
  `system_dns_domains: ""` and re-run `make system`.
- **standalone wg-quick DNS:** `wg*` interfaces are NM-unmanaged on non-CSB hosts, so a `DNS =` line gets no routing domain and
  the catch-all wins.
  Register one in the tunnel config, or import the profile into NetworkManager
  (`nmcli connection import type wireguard file /etc/wireguard/wg0.conf`):

  ```ini
  PostUp = resolvectl dns %i <vpn-nameserver-ip>; resolvectl domain %i ~<internal-domain>
  PreDown = resolvectl revert %i
  ```

- **`resolv.conf` symlink task fails:** NetworkManager owns `/etc/resolv.conf`. Re-run `make system` (deploys
  `/etc/NetworkManager/conf.d/99-dns.conf` with `dns=systemd-resolved`), restart NetworkManager from the console or tmux, and verify
  `readlink /etc/resolv.conf` is `/run/systemd/resolve/stub-resolv.conf`. On RHEL CSB IT owns DNS.

## Containers and OVN-K / Submariner

- **Rootless podman: `newuidmap` / "not enough IDs":** `/etc/subuid` and `/etc/subgid` lack your user. The system role writes
  `<user>:165536:165536` except on RHEL CSB; there (or if skipped) run
  `echo "$USER:165536:165536" | sudo tee -a /etc/subuid /etc/subgid` or ask IT, then check `podman unshare cat /proc/self/uid_map`.
- **`make kind` (OVN-K/Submariner repo target) uses Docker or cannot reach Podman:** `make` runs `sh`, not zsh, so `.zshrc` exports
  are not seen. Set
  `containers_environment_d_enabled: true` (default false), run `make containers`, then log out and back in. It writes
  `~/.config/environment.d/containers.conf` (`DOCKER_HOST`, `KIND_EXPERIMENTAL_PROVIDER=podman`). Verify the socket exists:
  `ls "$XDG_RUNTIME_DIR/podman/podman.sock"`.
- **`ovs-vsctl: cannot connect to ovsdb-server`:** the system role masks openvswitch and OVN units on the work profile. Temporary:

  ```bash
  sudo systemctl unmask ovsdb-server.service ovs-vswitchd.service
  sudo systemctl start ovsdb-server.service ovs-vswitchd.service
  ```

  The next `make system` re-masks them. More detail: `~/.local/share/ansible-packages/ovn-services-README.txt`.
- **`nf_conntrack: table full`:** the playbook sets `net.netfilter.nf_conntrack_max` to 524288. Check `sysctl
  net.netfilter.nf_conntrack_max`; if lower, `sudo modprobe nf_conntrack && sudo sysctl --system` (modprobe fails while
  `system_modules_disabled: 1` is active until reboot).

## kernel.unprivileged_bpf_disabled=2 Is Write-Once

`sysctl -w kernel.unprivileged_bpf_disabled=0` fails with EPERM even as root. The playbook default
(`system_unprivileged_bpf_disabled: 2`) locks the sysctl until reboot, and there is no supported override at 2. Root-level bpfman and
ovnkube-node are unaffected. Do not lower it casually: the distrobox container holds CAP_BPF and CAP_PERFMON, which become active for
every process in it (including package-manager dependencies) once unprivileged `bpf()` is allowed. Value 1 needs a custom kernel.

## CSB (Corporate Standard Build)

- **fapolicyd blocks `pip`/`go install`/downloaded binaries ("Operation not permitted"):** only RPM-installed binaries may run. Do
  those installs inside the distrobox container (`make container`), or ask IT for a trust rule (`fapolicyd-cli --file add <path>`).
- **fapolicyd blocks `distrobox enter`:** it watches tmpfs and overlay mounts. Ask IT to drop `tmpfs` from `watch_fs` in
  `/etc/fapolicyd/fapolicyd.conf`, and use the RPM-installed distrobox (or Toolbx, which the role falls back to).
- **Ansible `AnsiballZ_*.py: Operation not permitted`:** `ansible.cfg` sets `pipelining = true`; run from the repo root so it is
  loaded.
- **Third-party repos (Docker CE, Chrome, Tailscale, ...) fail:** policy blocks them; the run records failures in the CSB report with
  IT-ticket text. Use the container for those tools.
- **Login shell stays `/bin/bash` on an SSSD account:** the desktop role writes `override_shell` to
  `/etc/sssd/conf.d/90-laptop-setup-shell.conf` only if `sssd.conf` or `conf.d` has a `[domain/...]` section. Otherwise run
  `chsh -s /bin/zsh`, or ask IT to set `override_shell = /bin/zsh` or the LDAP `loginShell`.
- **Firewall tasks on RHEL CSB** are skipped, not failed; IT owns the firewall.

## Notes repo

- **Clone fails (`Permission denied`/`Repository not found`):** the repo is private. Run `gh auth login` (HTTPS fallback) or set
  `notes_remote_url`/`notes_remote_url_https` in `config.yml`; `notes_enabled: false` (default) skips the role. Then `make notes`.
- **Files in `~/notes` are ciphertext (`U2FsdGVkX1...`):** set `vault_notes_transcrypt_password` in `group_vars/all/vault.yml`, run
  `make packages` if transcrypt is missing, then `make notes`.

## Makefile refuses to run as root

`ERROR: Do not run as root.` (`guard-not-root`): run `make all` as your own user; Ansible escalates with `--ask-become-pass`. Running
as root would install dotfiles into `/root` and write `AllowUsers root`.
