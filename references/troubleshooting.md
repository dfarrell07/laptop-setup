# Troubleshooting

Known failure patterns organized by Ansible role. Each entry includes the symptom, root cause, fix, and whether an IT ticket is required on RHEL CSB.

---

## Post-Provisioning Checklist

Steps required after every `make all`. Complete these in order before the machine is considered provisioned.

> **Before running `make all`:** Run from a **local console or inside `tmux`**, not a bare SSH session.
> The system role restarts sshd mid-play (port 22 → 722), which kills the SSH connection and leaves
> provisioning incomplete. If you must use SSH: `tmux new-session -s prov 'make all'` — reconnect
> with `ssh -p 722 user@host tmux attach -t prov` after the port changes.

> **After `make all`, reboot before testing anything.** Kernel security parameters
> (lockdown=integrity, IOMMU, vsyscall=none, init_on_free) only take effect after reboot.
> SSH will be on **port 722** after reboot — update `~/.ssh/config` on other machines:
> ```
> Host mybox
>     HostName mybox.example.com
>     Port 722
>     IdentityFile ~/.ssh/id_ed25519_sk
> ```

1. **Reboot** — activates kernel security params and confirms sshd starts cleanly on port 722 with SELinux label applied.

2. **Verify provisioning succeeded:** `make smoke-test` — review any WARN/FAIL entries before proceeding.

3. **Authenticate Tailscale:** `tailscale up` opens a browser window to join the tailnet. Required on every new node. For headless machines use `tailscale up --auth-key=tskey-auth-...`. See [Tailscale Not Authenticated After Provisioning](#system-tailscale-not-authenticated-after-provisioning).

4. **Log out and log back in** — group membership changes (libvirt, kvm, podman groups) take effect only on new login sessions. Use `newgrp libvirt` for an in-session reload if a full logout is inconvenient. See [Group Membership Changes Require Logout](#make-test-vm-group-membership-changes-require-logout).

5. **CSB/hybrid machines only: `make container`** — provisions the Distrobox dev container with dev tools that cannot run on the hardened host. Required before OVN-K, bpfman, or Konflux workflows. See [distrobox: fapolicyd Blocks Container Startup](#distrobox-fapolicyd-blocks-container-startup).

6. **If notes are enabled: `gh auth login` then `make notes`** — the private notes repo requires GitHub authentication for the initial clone. Decryption requires `vault_notes_transcrypt_password` populated in vault. See [notes: Transcrypt Clone/Decrypt Failures](#notes-transcrypt-clonedecrypt-failures).

7. **If using YubiKey for vault or SSH signing: verify pcscd** — on a fresh machine pcscd may not be running. Check with `systemctl status pcscd`; enable with `sudo systemctl enable --now pcscd`. See [YubiKey Not Detected by pcscd / FIDO2](#packages-yubikey-not-detected-by-pcscd--fido2).

8. **Re-run `make smoke-test`** — confirms Tailscale WARN clears and no new failures appeared after login/container steps.

---

## repos_dnf: Third-Party Repos Blocked on CSB

**Symptom:**
```
No match for argument: tailscale-repo
No match for argument: mullvad-signing
```
`dnf install` or `dnf config-manager addrepo` fails for any non-Red Hat repository (Tailscale, Mullvad, Docker CE, Google Chrome, gh-cli, RPM Fusion).

**Cause:**
RHEL CSB STIG policy prohibits third-party RPM repositories. The fapolicyd trust database only covers RPM-installed binaries, and the repo configuration may be locked by policy.

**Fix:**
- Use `block/rescue` in the role to catch failures and append to `csb_failures` list.
- At end of run, render a CSB compatibility report with pre-drafted IT ticket text requesting repo exceptions.
- For tools available as static binaries (Tailscale, gh), install inside the Distrobox container instead.
- Tailscale specifically: use static binary with userspace networking (`tailscaled --tun=userspace-networking`) -- zero system modification, no repo or root needed.

**CSB IT ticket:** Yes. Request exception for each required third-party repo, or accept container-only workaround.

---

## packages: fapolicyd Blocks pip/go/binary Installs

**Symptom:**
```
Operation not permitted
```
`pip install --user`, `go install`, `npm install -g`, or any binary downloaded to `~/.local/bin` or `~/go/bin` is blocked on execution. The binary is written to disk but cannot run.

**Cause:**
fapolicyd enforces a deny-all, permit-by-exception policy. Only binaries installed via RPM (tracked in the RPM trust database) are allowed to execute. User-writable paths (`~/.local/bin`, `~/go/bin`, `/tmp`) are untrusted.

**Fix:**
- Move all pip/go/npm/binary-download installs into the Distrobox Fedora container, where there is no fapolicyd.
- On the host, use only `dnf install` for packages.
- If host-side execution is required, request an fapolicyd trust rule from IT (`fapolicyd-cli --file add /path/to/binary`).
- Exported Distrobox binaries (`distrobox-export --bin`) are also wrapper scripts in `~/.local/bin` and will be blocked -- work inside the container instead.

**CSB IT ticket:** Yes, if host-side execution of non-RPM binaries is needed. Otherwise, container workaround avoids the ticket.

---

## system: Failed to Restart firewalld

**Symptom:**
```
FAILED! => {"changed": false, "msg": "Unable to restart service firewalld: ..."}
```
or
```
Authorization required, but no authorization protocol specified
```
Ansible's `firewalld` module or `firewall-cmd --permanent` commands fail.

**Cause:**
CSB manages the firewall centrally. STIG requires the `drop` zone and admin-managed rules. The local user may not have sudo permission for firewall modifications, or the firewall configuration may be locked by policy.

**Fix:**
- On CSB, the playbook detects `csb_detected` and skips the drop-zone, ICMP-inversion, and SSH-port tasks entirely — they are omitted, not rescued. Port 722 is never added to the drop zone on CSB (the drop zone has no interface there), so the task would create a dead rule. Provisioning completes without a firewall failure on CSB.
- On **hybrid Fedora CSB** (`csb_detected=true`, Fedora distribution, no fapolicyd): the playbook does open port 722 in the default firewall zone (typically `FedoraWorkstation`) so the primary NIC remains reachable after provisioning. No IT ticket needed for this tier.
- Do not attempt to set the default zone or add custom rules without confirmed sudo access.
- For Tailscale: userspace networking mode avoids all firewall changes.
- On non-CSB machines (Fedora, macOS), firewall tasks should work normally with `--ask-become-pass`.

**CSB IT ticket:** Only if SSH on port 722 must be reachable from non-Tailscale sources on **full RHEL CSB** (fapolicyd enforcing). On hybrid Fedora CSB, the playbook handles port 722 automatically. Port 722 over Tailscale requires no IT ticket on any tier.

---

## system: SELinux Blocks Non-Default SSH Port

**Symptom:**
```
sshd: error: Bind to port XXXX on 0.0.0.0 failed: Permission denied
```
sshd fails to start after changing `Port` in `sshd_config` to a non-standard value.

**Cause:**
SELinux targeted policy only allows sshd to bind to ports labeled `ssh_port_t`. The default is port 22. Non-standard ports are unlabeled and blocked.

**Fix:**
Run `semanage port -a -t ssh_port_t -p tcp <port>` before restarting sshd. Requires `policycoreutils-python-utils` package.

In the Ansible role:
```yaml
- name: Label non-default SSH port for SELinux
  community.general.seport:
    ports: "{{ ssh_port }}"
    proto: tcp
    setype: ssh_port_t
    state: present
  when: ssh_port != 22
```

**CSB IT ticket:** Possibly. If sudo is scoped, the user may not have permission to run `semanage`. Request IT to label the port or grant scoped sudo for `semanage`.

---

## desktop: i3 Not in RHEL Repos

**Symptom:**
```
No match for argument: i3
No match for argument: i3status
No match for argument: i3lock
```
`dnf install i3` fails on RHEL because i3 is not in the base or AppStream repos.

**Cause:**
RHEL ships GNOME as the only desktop environment. i3 and related packages (i3status, i3lock, dmenu) are community packages available in EPEL but not in base RHEL.

**Fix:**
- Install EPEL repository first (may itself require IT approval on CSB).
- If EPEL is available: `dnf install i3 i3status i3lock dmenu` from EPEL.
- If EPEL is blocked: use GNOME with tiling extensions, or install i3 inside a Distrobox container and run it from there (requires X11/Wayland forwarding, which Distrobox provides).
- Gate the i3 tasks on `ansible_distribution`:
```yaml
- name: Install i3
  ansible.builtin.dnf:
    name: [i3, i3status, i3lock, dmenu]
    state: present
  when: ansible_os_family == 'RedHat' and (ansible_distribution == 'Fedora' or epel_enabled | default(false))
```

**CSB IT ticket:** Yes, to enable EPEL. Alternatively, accept GNOME on the CSB host and run i3 only on Fedora machines.

---

## distrobox: fapolicyd Blocks Container Startup

**Symptom:**
Container creation succeeds but `distrobox enter` hangs or fails. `podman start` may show permission errors. Alternatively, Distrobox itself cannot run if installed via curl to `~/.local/bin/`.

**Cause:**
fapolicyd uses fanotify, which operates below container namespace boundaries. It is not namespace-aware -- it monitors the host kernel's filesystem events regardless of which namespace generated them. Container processes executing binaries from tmpfs or overlay mounts are blocked because those filesystems are not in the trust database. The `watch_fs` directive in `/etc/fapolicyd/fapolicyd.conf` defaults to monitoring tmpfs, which containers use heavily.

**Fix:**
- Remove `tmpfs` from the `watch_fs` list in `/etc/fapolicyd/fapolicyd.conf` and restart fapolicyd. This requires root.
- If Distrobox was installed via curl (to `~/.local/bin/`), the Distrobox binary itself is blocked. Use Toolbx (`dnf install toolbox`) as the fallback -- it is RPM-installed and trusted by fapolicyd.
- Distrobox is available in EPEL 10 (`dnf install distrobox`) -- the RPM-installed version is fapolicyd-trusted.

**CSB IT ticket:** Yes. Request modification of `watch_fs` in `/etc/fapolicyd/fapolicyd.conf` to exclude `tmpfs`. This is the single biggest risk to the Distrobox workflow on CSB.

---

## containers: Podman Rootless subuid/subgid Not Configured

**Symptom:**
```
ERRO[0000] cannot setup namespace using "/usr/bin/newuidmap": exit status 1
Error: cannot re-exec process
```
or
```
Error: could not get runtime: there might not be enough IDs available in the namespace
```
Rootless `podman` commands fail immediately.

**Cause:**
Podman rootless requires entries in `/etc/subuid` and `/etc/subgid` mapping subordinate UIDs/GIDs to the user. RHEL ships Podman but does not automatically configure subuid/subgid for all users. On CSB, these files are root-owned.

**Fix:**
- An admin must add entries: `echo "username:100000:65536" | sudo tee -a /etc/subuid /etc/subgid`
- Verify with `podman unshare cat /proc/self/uid_map`.
- In the Ansible role, detect and report the missing configuration:
```yaml
- name: Check subuid for current user
  ansible.builtin.command: grep -q "^{{ ansible_user_id }}:" /etc/subuid
  register: subuid_check
  changed_when: false
  failed_when: false

- name: Configure subuid (requires become)
  ansible.builtin.lineinfile:
    path: /etc/subuid
    line: "{{ ansible_user_id }}:100000:65536"
    create: true
  become: true
  when: subuid_check.rc != 0
```

**CSB IT ticket:** Yes, if sudo is unavailable. Request IT to add your user to `/etc/subuid` and `/etc/subgid`. This is a one-time setup.

---

## claude: npm Install Deprecated

**Symptom:**
```
npm install -g @anthropic-ai/claude-code
```
Installs successfully but pulls ~300 npm dependencies, any of which could be compromised. Or on CSB, `npm install -g` writes to a path blocked by fapolicyd.

**Cause:**
The npm installation method was deprecated in Claude Code v2.1.15 (January 2026). In March 2026, the npm registry saw concurrent supply chain attacks (axios trojan alongside a Claude Code source leak in v2.1.88). The npm install path carries unnecessary supply chain risk.

**Fix:**
Install via the native binary installer:
```bash
curl -fsSL https://claude.ai/install.sh | bash
```
The native binary is SHA256-verified and has zero npm dependencies. It installs to `~/.claude/bin/` (add to PATH).

On CSB where curl-installed binaries are blocked by fapolicyd, install Claude Code inside the Distrobox container where fapolicyd does not apply.

**CSB IT ticket:** No. The Distrobox container workaround avoids the need for host-side installation.

---

## ssh: GNOME Keyring / gcr-ssh-agent Conflicts with FIDO2

**Symptom:**
```
sign_and_send_pubkey: signing failed for ED25519-SK ... from agent: agent refused operation
```
SSH operations fail despite the YubiKey being plugged in and the correct key being available. `ssh-add -l` shows the key but signing fails. Git push and SSH login both affected.

**Cause:**
GNOME Keyring (on GNOME < 46 / RHEL 9) or its replacement `gcr-ssh-agent` (on GNOME 46+ / Fedora 42 / RHEL 10) advertise themselves as SSH agents and load `~/.ssh/*.pub` keys. Neither supports FIDO2 key operations (ed25519-sk, ecdsa-sk). When `SSH_AUTH_SOCK` points to the GNOME agent instead of OpenSSH's ssh-agent, FIDO2 signing silently fails.

**Fix:**
All steps are handled automatically by the playbook — no manual intervention is required after `make dotfiles` or `make all`:

- `roles/dotfiles`: deploys `~/.config/autostart/gnome-keyring-ssh.desktop` with `Hidden=true` (suppresses GNOME Keyring SSH component on GNOME < 46 / RHEL 9)
- `roles/dotfiles`: masks `gcr-ssh-agent.socket` and `gcr-ssh-agent.service` via systemd user scope (prevents socket-activation on GNOME 50+ / Fedora 42 / RHEL 10)
- `roles/dotfiles`: deploys `~/.config/systemd/user/ssh-agent.service`, enables and starts it, and writes `~/.config/environment.d/ssh-agent.conf`

If the issue persists after re-provisioning, log out and back in — `environment.d` changes require a fresh login session. Verify the correct socket is set:
```
echo $SSH_AUTH_SOCK
# Expected: /run/user/<uid>/ssh-agent.socket
# Wrong:    /run/user/<uid>/gcr/ssh  (or any gnome/gcr path)
```

If `SSH_AUTH_SOCK` still points to a gcr path, check that the gcr socket is masked:
```
systemctl --user status gcr-ssh-agent.socket
```

**CSB IT ticket:** No. All changes are user-level (systemd user units, autostart overrides, environment.d).

---

## Ansible: Temp File Execution Blocked by fapolicyd

**Symptom:**
```
MODULE FAILURE
...
/bin/sh: /home/user/.ansible/tmp/AnsiballZ_xxx.py: Operation not permitted
```
Every Ansible module execution fails on a host with fapolicyd enforcing. Even basic tasks like `ansible.builtin.copy` or `ansible.builtin.dnf` fail because Ansible cannot execute its generated Python scripts.

**Cause:**
Ansible's default execution model transfers a Python script to a temp directory on the target (`~/.ansible/tmp/`), then executes it via `/bin/sh`. fapolicyd blocks execution of files in user-writable temp directories because they are not in the RPM trust database. This is a known incompatibility -- Red Hat's own Ansible Automation Platform documentation states AAP is "not supported when fapolicyd is enforcing."

**Fix:**
Enable pipelining in `ansible.cfg`:
```ini
[defaults]
pipelining = true
```
With pipelining enabled, Ansible pipes the module code directly into the Python interpreter over the SSH connection instead of writing a temp file. No temp file is created, so fapolicyd has nothing to block.

Requirements for pipelining:
- `requiretty` must NOT be set in `/etc/sudoers` (Fedora/RHEL default is no requiretty).
- The target must have Python available (it does -- RHEL ships Python).

**CSB IT ticket:** No. `pipelining = true` is a client-side Ansible configuration change.

**Note:** This project's `ansible.cfg` already includes `pipelining = true`. If you see this error, verify the ansible.cfg is present and being picked up (run from the repo root) — do not copy it to a different directory.

---

## vault: Password File Not Found on First Run

**Symptom:**
```
ERROR! The vault password file /home/user/laptop-setup/scripts/vault-pass.sh was not found
```
Running `make all` fails immediately before any task executes.

**Cause:**
`ansible.cfg` references `vault_password_file = scripts/vault-pass.sh`. This file is gitignored — it is never committed to the repo. On a fresh machine (or after a fresh clone without restoring from backup), the script will not exist.

**Fix:**
`scripts/vault-pass.sh` is a gitignored file — it is never in the repo and must be restored from backup or created manually per the template in `SECURITY.md`. Installing `ykpers` is a prerequisite (it provides `ykchalresp`) but does not auto-create the script.

Bootstrap procedure for first run:
0. Run `make bootstrap` (installs ansible-core, git, collections, git hooks; creates vault-pass.sh stub).
1. Restore `scripts/vault-pass.sh` from backup, or follow the template in `SECURITY.md` to create it. Then `chmod 700 scripts/vault-pass.sh`.
2. If the YubiKey is not yet configured for HMAC-SHA1 challenge-response, or as a temporary workaround, create a plaintext password file instead:
   ```bash
   echo "your-vault-password" > ~/.vault_pass && chmod 0600 ~/.vault_pass
   ```
3. Run `make all`.
4. After setup completes, remove the temporary file (`rm ~/.vault_pass`) and ensure `scripts/vault-pass.sh` is in place for future runs.

`scripts/preflight.sh` detects both the script and the `~/.vault_pass` fallback, and reports which vault source is active.

**CSB IT ticket:** No. This is a bootstrap ordering issue, not a CSB restriction.

---

## packages: Bitwarden CLI Supply Chain Risk

**Symptom:**
```
npm install -g @bitwarden/cli
```
installs a potentially compromised package. In April 2026, a trojan was published to `@bitwarden/cli@2026.4.0` on npm for approximately 90 minutes.

**Cause:**
The npm ecosystem has repeated supply chain compromises. The Bitwarden CLI published via npm inherits this risk. The April 2026 incident was detected and removed, but the window of exposure was real.

**Fix:**
- Verify you are on `@bitwarden/cli@2026.4.1` or later if installed via npm.
- Prefer the official standalone binary from Bitwarden's GitHub releases page, verified by checksum:
  ```bash
  curl -sLo bw.zip "https://github.com/bitwarden/clients/releases/download/cli-v2026.4.1/bw-linux-2026.4.1.zip"
  # Verify SHA256 against the published checksum
  echo "<expected-sha256>  bw.zip" | sha256sum -c
  unzip bw.zip -d ~/.local/bin/
  chmod +x ~/.local/bin/bw
  ```
- In the Ansible packages role, use the binary download path with SHA256 verification (same pattern as other binary installs), not `npm install`.
- On CSB, install inside the Distrobox container.

**CSB IT ticket:** No. Binary goes inside the container or uses a verified download path.

---

## system: USBGuard Blocks YubiKey or Keyboard

**Symptom:**
YubiKey stops responding after USBGuard is enabled. External keyboard (Moonlander) is not recognized. Devices work after `usbguard allow-device`.

**Cause:**
USBGuard blocks all USB devices not in the whitelist (`/etc/usbguard/rules.conf`). The default deployed rules whitelist YubiKey (`1050:*`), Moonlander (`3297:1969`), and hardwired devices, but a new device or firmware update may change the device ID.

**Fix:**
- List blocked devices: `usbguard list-devices --blocked`
- Temporarily allow: `usbguard allow-device <id>`
- Permanently add to whitelist: update `system_usbguard_whitelist` in `roles/system/defaults/main.yml` and re-run `make system`
- Generate a fresh policy from current devices: `usbguard generate-policy -P`

**CSB IT ticket:** USBGuard may already be managed by IT. Check before modifying rules.

**Thunderbolt dock USB devices blocked:** `boltd` authorizes the Thunderbolt controller, but USBGuard is a separate gate for the USB devices behind it (hub, keyboard, ethernet, etc.). Dock devices appear with `connect_type "hotplug"` and are blocked by default. Fix: add rules to `config.yml` and re-run `make system`:
```yaml
system_usbguard_extra_rules:
  - 'allow id 2109:0817 name "USB3.0 Hub" with-connect-type "hotplug"'
```
Find blocked dock devices with `usbguard list-devices --blocked` and their VID:PID with `lsusb`.

---

## system: sshd Fails After Hardening Drop-in

**Symptom:**
```
sshd: error: Bind to port XXXX failed
```
or SSH connections rejected after deploying `00-hardening.conf`.

**Cause:**
The sshd hardening drop-in restricts `AllowUsers` to the current Ansible user. If the username differs from what's expected, or if the port conflicts with an existing config, sshd may fail.

**Fix:**
- Check the deployed config: `cat /etc/ssh/sshd_config.d/00-hardening.conf`
- Verify the `AllowUsers` line matches your actual username
- Ensure the `Port` value matches the `ssh_port` variable in `default.config.yml`
- Check for conflicts with the base `sshd_config`: `sshd -T | grep -i port`
- Restart: `systemctl restart sshd`
- If locked out, use console access or another user to fix the config

---

## notes: Clone Fails with Permission Denied (Not the dfarrell07 Account)

**Symptom:** Notes role fails with `Permission denied (publickey)` or `Repository not found` cloning `git@github.com:dfarrell07/notes`.

**Cause:** The notes repo (`dfarrell07/notes`) is private. If you are not `dfarrell07`, your SSH keys have no access to it.

**Fix:** Add to `config.yml`:

```yaml
notes_enabled: false  # or remove `notes_enabled: true` from config.yml (false is now the default)
```

Then re-run `make notes` (or `make all`). The notes role will skip entirely.

If you want your own private notes repo, set `dotfiles_notes_repo` to your own repository in `config.yml` and ensure transcrypt is initialized.

---

## notes: Transcrypt Clone/Decrypt Failures

**Symptom:**
```
Could not clone notes repo via SSH or HTTPS.
```
or files in `~/notes` contain ciphertext (`U2FsdGVkX1...`) instead of plaintext.

**Cause:**
The notes repo is private and encrypted with transcrypt. Clone requires GitHub auth (SSH keys or `gh auth login`). Decryption requires the transcrypt password from vault.

**Fix:**
- Clone failure: run `gh auth login` then `make notes`
- Ciphertext visible: set `vault_notes_transcrypt_password` in vault.yml, then `make notes`
- Transcrypt not installed: run `make packages` first
- To manually initialize: `cd ~/notes && transcrypt -c aes-256-cbc -p '<password>' -y`
- To rekey (e.g., switch to YubiKey): `cd ~/notes && transcrypt --rekey`

**CSB IT ticket:** No. The notes repo is personal and does not require system changes.

---

## system: Bluetooth Not Working After Provisioning

**Symptom:**
Bluetooth is unavailable after running the playbook. `bluetoothctl` shows no adapter, or `rfkill list` shows the Bluetooth device hard-blocked. GNOME Bluetooth panel may be missing entirely.

**Cause:**
The `system` role conditionally masks `bluetooth.service` and blacklists the `btusb`/`bluetooth` kernel modules when `system_disable_bluetooth: true` is set in `config.yml`. If this was set intentionally for a machine without Bluetooth, re-enabling it requires an explicit config change.

**Fix:**
To re-enable Bluetooth, set in `config.yml` (gitignored, per-machine):
```yaml
system_disable_bluetooth: false
```
Then re-run `make system` or `make all`. The mask on `bluetooth.service` will be removed and the kernel modules will no longer be blacklisted.

Note: `system_disable_bluetooth` defaults to `false` (Bluetooth enabled). It only gets masked if explicitly set to `true` in `config.yml` or passed via `-e system_disable_bluetooth=true`.

**CSB IT ticket:** No. This is a per-machine playbook configuration setting.

---

## packages: YubiKey Not Detected by pcscd / FIDO2

**Symptom:**
```
ykman info
Error: No YubiKey detected!
```
or SSH signing with an ed25519-sk key fails immediately (not the GNOME agent issue — the YubiKey itself is not seen). `lsusb` may show the device but `ykman` cannot communicate with it.

**Cause:**
FIDO2 operations (ed25519-sk) go through the kernel HID driver directly and do not require pcscd. However, YubiKey Manager (`ykman`) and challenge-response (`ykchalresp`) use the PCSC interface, which requires `pcscd` to be running. On a fresh machine, pcscd may not be started or the user may lack access to the PCSC socket.

**Fix:**
1. Verify the YubiKey is visible to the kernel: `lsusb | grep -i yubico`
2. Check pcscd is running: `systemctl status pcscd`
3. If pcscd is stopped: `sudo systemctl enable --now pcscd`
4. Verify the user is in the `plugdev` group (required on some distros): `groups | grep plugdev`
5. For FIDO2/SSH (ed25519-sk), pcscd is not required — check that the kernel `u2f_hid` module is loaded: `lsmod | grep u2f_hid`
6. USBGuard: ensure the YubiKey's VID:PID (`1050:*`) is in the whitelist — re-run `make system` or check `/etc/usbguard/rules.conf`

The playbook installs `yubikey-manager`, `ykpers`, and `libfido2` via the `packages` role on supported distros. If these are missing, run `make packages`.

**CSB IT ticket:** Possibly, if pcscd is blocked by policy or the user cannot be added to `plugdev`.

---

## system: USB Drives Not Mounting After Provisioning

**Symptom:**

USB drives are not recognized. `lsblk` shows nothing when a USB drive is plugged in. `dmesg` may show `usb_storage: module is disabled`.

**Root Cause:**

The `system` role blacklists `usb_storage` and `uas` kernel modules at the `/bin/false` level via `/etc/modprobe.d/hardening.conf` when `system_disable_usb_storage: true` (the default). This is a kernel-level block — USBGuard policies cannot override it. Any USB mass-storage device, even one in the USBGuard whitelist, will not mount.

**Fix:**

Set in `config.yml` (gitignored, per-machine):

```yaml
system_disable_usb_storage: false
```

Then re-run `make system` to regenerate `/etc/modprobe.d/hardening.conf`. The modprobe change takes effect for new device connections immediately; existing plugged devices may need to be re-plugged. If the initramfs includes usb_storage (check with `lsinitrd | grep usb_storage`), run `sudo dracut -f` to regenerate.

**Without re-provisioning (temporary):**

```bash
sudo modprobe usb_storage && sudo modprobe uas
```

This re-enables USB storage for the current boot session only. It reverts at the next reboot unless you re-provision with `system_disable_usb_storage: false`.

---

## system: Tailscale Not Authenticated After Provisioning

**Symptom:**
```
tailscale: WARN — not connected
```
The smoke test (`make smoke-test`) records this warning. Running `tailscale status` shows "not logged in" or the daemon exits immediately. The VPN tunnel is not established even though `tailscaled` is running.

**Cause:**
The playbook installs `tailscale`, configures NetworkManager to ignore Tailscale interfaces, assigns `tailscale0` to the firewall trusted zone, and enables and starts `tailscaled` — but it does not authenticate the node. Authentication requires an interactive browser step or a pre-issued auth key. This cannot be automated without storing credentials in vault, so it is intentionally left as a manual post-provisioning step.

**Fix:**
After `make all` completes, authenticate the node. For interactive (desktop) machines:
```bash
tailscale up
```
This opens a browser window. Complete the login and the node joins the tailnet immediately.

For headless or SSH-only machines, generate a reusable or ephemeral auth key from the Tailscale admin console (`https://login.tailscale.com/admin/settings/keys`) and pass it directly:
```bash
tailscale up --auth-key=tskey-auth-...
```

For tagged/server nodes that require pre-authorization:
```bash
tailscale up --auth-key=tskey-auth-... --advertise-tags=tag:server
```

Verify the node is connected:
```bash
tailscale status
tailscale ip -4
```

Re-run the smoke test to confirm the WARN clears:
```bash
make smoke-test
```

**CSB IT ticket:** No. Authentication is user-level and requires no system changes beyond what the playbook already configures. On CSB where the Tailscale repo is blocked, use the static binary with userspace networking instead (see `repos_dnf: Third-Party Repos Blocked on CSB` above).

## kind: Cannot Connect to Docker/Podman Socket

**Symptom:** `make kind` or `kind create cluster` in OVN-K/Submariner fails with:
```
ERROR: failed to create cluster: failed to create node with docker: command "docker create ..." failed: ...
```
or silently tries Docker instead of Podman.

**Cause:** `KIND_EXPERIMENTAL_PROVIDER=podman` and `DOCKER_HOST` must be in the shell environment when `make` runs. `make` spawns `sh` (not `zsh`), so `.zshrc` is not sourced. The environment.d config at `~/.config/environment.d/containers.conf` injects these via the systemd user session manager — but only after a **fresh login**.

**Fix:**
```bash
# Verify the environment.d config is deployed:
cat ~/.config/environment.d/containers.conf

# Log out and back in to pick up the session-level environment.
# Or source the vars manually in your current terminal:
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
export KIND_EXPERIMENTAL_PROVIDER=podman

# Verify the Podman socket is active:
ls -la "${XDG_RUNTIME_DIR}/podman/podman.sock"

# Then retry:
kind create cluster --config path/to/kind-config.yaml
```

**CSB IT ticket:** No. This is a user-session environment variable issue resolved by re-login.

---

## make test-vm: Group Membership Changes Require Logout

**Symptom:**
`make test-vm` fails with a permission error immediately after `make all` on an existing session (e.g., libvirt socket access denied or `virsh` returns permission errors).

**Cause:**
Group membership changes — such as adding the current user to the `libvirt` group — take effect only in new login sessions. The running shell inherited its group list at login and does not pick up additions made mid-session by the playbook.

**Fix:**
Use `newgrp libvirt` for an immediate in-session reload without logging out:
```bash
newgrp libvirt
make test-vm
```
Or log out and back in, then re-run `make test-vm`.

Verify your active groups after either approach:
```bash
groups | grep libvirt
```

**CSB IT ticket:** No. Group membership is managed by the playbook and requires no IT intervention.

---

## Makefile: ERROR when running as root

**Symptom:**
```
ERROR: Do not run as root. Use -K for privilege escalation (make all).
make: *** [Makefile:<N>: guard-not-root] Error 1
```

**Cause:**
The Makefile has a `guard-not-root` safety check on all targets that install files into `$HOME` (`all`, `minimal`, `dotfiles`, `packages`, `repos`, `ssh`, `claude`, `distrobox`, `container`, etc.). Running `sudo -i && make all` sets `USER=root`, causing all dotfiles, git repos, and configs to be installed to `/root/` instead of the actual user's home directory. The guard prevents this silent misconfiguration.

**Fix:**
Run without sudo at the top level — Ansible handles privilege escalation internally:
```bash
# Correct: Ansible prompts for sudo password via -K
make all
```

The playbook uses `--ask-become-pass` for tasks that need root and `become: true` at the play/task level. The `make all` command itself must run as the actual user.

**CSB IT ticket:** No. This is a local invocation issue.

---

## system: PAM Hardening Silently Inactive (authselect Not on sssd Profile)

**Symptom:**
```
authselect check
```
Fails post-provisioning, or `make smoke-test` reports `authselect-profile: FAIL` or `authselect-check: FAIL`. `faillock.conf` contains the correct values (`deny = 5`, `unlock_time = 900`) but account lockout does not trigger — `pam_faillock.so` is not present in the PAM stack.

**Cause:**
`authselect enable-feature with-faillock` and `authselect enable-feature with-pwhistory` require the `sssd` profile to be active. On systems using the `minimal` or `local` profile (Vagrant boxes, some Fedora minimal installs), `enable-feature` exits 0 without modifying PAM files. On Anaconda-installed systems, manual PAM edits may cause `authselect check` to report drift even when `sssd` is nominally selected.

**Fix:**
The playbook runs `authselect select sssd --force` before the feature-enable tasks. If the smoke test still fails, run manually:
```bash
sudo authselect select sssd --force
sudo authselect enable-feature with-faillock
sudo authselect enable-feature with-pwhistory
sudo authselect check
```
Verify `pam_faillock.so` is wired into the stack:
```bash
grep pam_faillock /etc/pam.d/system-auth
```
If absent, the `--force` flag did not apply — check for other active authselect overrides.

**CSB IT ticket:** No. `authselect` is user-space PAM configuration. On CSB with restricted sudo, contact IT if `authselect select sssd --force` is denied.

---

## system: OVN-K / Submariner Packet Drops ("nf_conntrack: table full")

**Symptom:**
```
kernel: nf_conntrack: table full, dropping packet
```
or intermittent connection failures, retransmits, and `curl` timeouts during OVN-K or Submariner `make kind` test runs. May also appear as `make smoke-test` reporting `sysctl-conntrack-max: FAIL`.

**Cause:**
The kernel auto-sizes `nf_conntrack_max` from RAM at boot. On a 62 GB machine the auto-calculated value is 262144. A playbook value below the auto-calculated default actively shrinks the table. OVN-K and Submariner generate high conntrack state (encapsulated pods, service IPs, gateway routes) and overflow a small table under load.

**Fix:**
The playbook sets `net.netfilter.nf_conntrack_max: 524288` via `90-hardening.conf`. Verify:
```bash
sysctl net.netfilter.nf_conntrack_max
# Expected: >= 524288
```
If the value is lower, the `nf_conntrack` module may not have been loaded when the sysctl was applied. The playbook explicitly loads `nf_conntrack` via `modprobe` before sysctl; re-running `make system` should fix it.

**CSB IT ticket:** No. Sysctl changes are applied by the playbook with sudo.

---

## bpfman-socket: smoke-test check requires manual host verification

**Symptom:** `make smoke-test` reports `bpfman-socket: FAIL` or `bpfman-socket: WARN`.

**Context:** The `bpfman-socket` check in `scripts/smoke-test.sh` is host-only — molecule runs in containers without a live systemd, so this check is not covered by any molecule scenario. The molecule Fedora verify asserts `bpfman.socket` is enabled only when the binary is present; in CI both `packages_networking` and `packages_networking_work` are emptied so the assertion is skipped. This is expected: the check exists to catch regressions on real hosts.

**Manual test (after `make all` on a Fedora host with bpfman installed):**
```bash
systemctl is-enabled bpfman.socket   # expected: enabled
systemctl is-active bpfman.socket    # may be inactive until first client connect — that is OK
bpfman list                          # triggers socket activation; should return without error
```

**Fix:** If `bpfman.socket` is not enabled, re-run the playbook (`make all`) to re-trigger the `Enable bpfman.socket` tasks in both `roles/system/tasks/main.yml` (Play 1, detect-then-enable) and `roles/packages/tasks/main.yml` (Play 2, after package install). Both tasks have `failed_when: false` to allow headless/container provisioning where systemd is absent.

**CSB IT ticket:** No. Socket enable is a user-space systemd unit requiring only sudo.

---

## system: Printing Not Working After Provisioning

**Symptom:** Print dialogs fail to open, CUPS is not running, `lpq` returns an error.

**Root Cause:** The `system` role masks `cups.service`, `cups.socket`, and `cups.path` when `system_disable_printing: true` (the default). Masking prevents socket activation — even Flatpak print dialogs that trigger CUPS via socket will fail silently.

**Fix:** Set in `config.yml`:

```yaml
system_disable_printing: false
```

Then re-run `make system`. This unmasks CUPS and restores printing.

**Note:** `cups-browsed` remains masked regardless (it has CVE-2024-47176 history and no legitimate use on a workstation). Standard printing via `cups.socket` works without it.

---

## system: Shell Sessions Terminate After 10 Minutes of Inactivity

**Symptom:** SSH sessions or terminal emulator shells die after 10 minutes idle. Interactive prompts close unexpectedly. Long-running `ansible-playbook` runs are killed by the shell.

**Root Cause:** The `system` role deploys `TMOUT=600` (600 seconds = 10 minutes) to `/etc/profile.d/tmout.sh` (bash) and `/etc/zshrc` (zsh). This causes the shell to auto-logout when the interactive prompt is idle. Background processes in the shell's job control survive, but the shell itself exits when `TMOUT` fires at the next prompt.

**Fix (permanent):** Set in `config.yml`:

```yaml
system_tmout: 0  # 0 = disabled
```

Then re-run `make system`. Valid values are 0 (disabled) or 1–900 (seconds, CIS max 900).

**Fix (current session):** Open a subshell and unset TMOUT: `bash --norc` or start a `tmux` session (tmux panes are independent processes and are not killed by the parent shell's TMOUT).

---

## system: VS Code Remote SSH Port Forwarding Fails

**Symptom:** The VS Code Remote SSH "Ports" tab shows no ports, or port forwarding (`ssh -L`) fails with `channel 3: open failed: administratively prohibited`.

**Root Cause:** The sshd drop-in sets `AllowTcpForwarding local` (default), which allows `-L` port forwarding (VS Code port panel) but not `-R` remote forwarding. If forwarding is still blocked, the `config.yml` may have overridden the default to `no`.

**Fix:** Verify `config.yml` does not set `system_ssh_allow_tcp_forwarding: "no"`. To disable all forwarding explicitly, set:

```yaml
system_ssh_allow_tcp_forwarding: "no"  # disables both -L and -R forwarding
```

Then re-run `make system`. Valid values: `local` (default — VS Code port panel and ssh -L), `yes` (both -L and -R), `no` (all forwarding disabled), `remote` (-R only).

---

## system: SSH Connection Refused (Port Changed to 722)

**Symptom:** `ssh hostname` or `ssh user@machine` returns `Connection refused` immediately. The machine was reachable via SSH before provisioning.

**Root Cause:** The `system` role deploys a drop-in at `/etc/ssh/sshd_config.d/00-hardening.conf` that changes the SSH port from 22 to `{{ ssh_port }}` (default: 722). Port 22 is no longer open. `ssh` clients default to port 22.

**Fix (connect after provisioning):**

```bash
ssh -p 722 user@hostname
```

**Fix (add to ~/.ssh/config for convenience):**

```
Host myserver
    HostName hostname
    Port 722
    User user
```

**Fix (revert to port 22):** Set in `config.yml`:

```yaml
ssh_port: 22
```

Then re-run `make system`. This redeploys the drop-in with `Port 22` and relabels SELinux.

**Note:** The firewall opens port `{{ ssh_port }}` in the drop zone and SELinux labels it `ssh_port_t`. If you change the port, both are updated automatically.

---

## system: DNS Failures or Slow Resolution After Provisioning

**Symptom:** Websites time out, `curl` hangs, `resolvectl query example.com` is slow or fails with `SERVFAIL`. DNS worked before provisioning.

**Root Cause:** The `system` role deploys `/etc/systemd/resolved.conf.d/99-dot.conf` routing all DNS through Cloudflare (1.1.1.1) with DNS-over-TLS (`DNSOverTLS=opportunistic`). On corporate VPNs, restricted hotel/conference networks, or ISPs that block outbound to 1.1.1.1 or port 853, DNS may be slow (TLS negotiation failing, falling back to plain DNS) or broken entirely.

**Diagnosis:**

```bash
resolvectl status              # shows active DNS servers and DoT status
resolvectl query example.com   # tests resolution with timing
resolvectl query --type=A redhat.com  # tests internal hostname on VPN
```

**Fix (disable DoT):** Set in `config.yml`:

```yaml
system_dot_mode: "no"   # disable DNS-over-TLS entirely
```

**Fix (use different DNS servers):** Set in `config.yml`:

```yaml
system_dns_primary: "192.168.1.1"  # your router or internal DNS
system_dot_mode: "no"
```

**Fix (VPN split-DNS issue):** If internal hostnames fail on VPN, check whether the VPN pushes DNS search domains via `resolvectl status`. VPN-pushed specific domains (e.g., `~redhat.com`) win over the `~.` catch-all automatically — if split-DNS is failing, the VPN may not be integrating with systemd-resolved correctly. Run `make system` with `system_dns_domains: ""` to remove the catch-all if needed.

**CSB note:** This resolved config is skipped entirely on CSB hosts (the task has `when: not csb_detected`).

---

## system: Build Fails with "Permission denied" in /var/tmp

**Symptom:** `pip install`, `cargo build`, `dnf install`, or other build tools fail with `Permission denied` or `EPERM` errors when writing to `/var/tmp`. Some RPM post-install scriptlets fail mid-transaction leaving packages half-installed.

**Root Cause:** Additionally, because /tmp on Fedora is a tmpfs, the bind-mount makes /var/tmp equally volatile: any files written to /var/tmp during a session are silently discarded on the next reboot with no error. This affects staged OCI images, Cargo/pip build caches, and any tool that treats /var/tmp as persistent staging area (Go/Rust builds, pip wheel compilation). The symptom is silent data loss on reboot, not a permission-denied error. The `system` role bind-mounts `/var/tmp` to `/tmp` with `noexec,nosuid,nodev` options (CIS 1.1.8). Some tools use `/var/tmp` as a working directory for executable binaries (pip wheel builds, cargo compilation artifacts, some RPM scriptlets). With `noexec`, anything that writes a binary to `/var/tmp` and then tries to execute it will fail.

**Fix (permanent):** Set in `config.yml`:

```yaml
system_var_tmp_noexec: false
```

Then re-run `make system`. This stops and disables the var-tmp.mount bind-mount unit.

**Fix (per-session):** Override `TMPDIR` for the specific tool:

```bash
TMPDIR=/tmp pip install somepackage
CARGO_TARGET_DIR=/tmp/cargo-build cargo build
```

**Fix (per dnf transaction):** Most dnf/rpm scriptlet issues can be avoided by ensuring the failing package's scriptlet uses `$RPM_BUILD_ROOT` correctly. Contact the package maintainer if `/var/tmp` usage in scriptlets is required.

---

## system: bpf_jit_harden=2 Causes JIT Constant Blinding (bpfman/eBPF Development)

**Symptom:** `bpftool prog dump jited` shows blinded constants (XOR'd with random values) rather than actual literal values. JIT output differs from an unhardened host, making JIT-level debugging difficult.

**Root Cause:** `net.core.bpf_jit_harden=2` applies constant blinding in the BPF JIT to **all users including root**, making JIT-level debugging impossible. The default is 1 (unprivileged-only blinding), which is effectively a no-op since unprivileged BPF is already blocked. Value 2 is only relevant if you explicitly set `system_bpf_jit_harden: 2` in `config.yml` for strict CIS/STIG compliance.

**Practical impact by operation (when harden=2 is set):**
- `bpfman load / list / unload / get` — **not affected** — programs load and run correctly
- `bpftool prog dump jited` — constants are XOR-blinded; harder to read JIT output
- Comparing JIT output to a default Fedora host — output differs due to blinding

**Fix:** Remove or lower `system_bpf_jit_harden` in `config.yml` (default is already 1):

```yaml
system_bpf_jit_harden: 1   # 1 = unprivileged only (default); safe for bpfman JIT debugging
```

Then re-run `make system`. For a non-persistent change: `sudo sysctl -w net.core.bpf_jit_harden=1`.

**CSB IT ticket:** No. Applied by the playbook with sudo.

## system: kernel.unprivileged_bpf_disabled=2 Is Write-Once (bpfman Contributors)

**Symptom:** `sudo sysctl -w kernel.unprivileged_bpf_disabled=0` returns `sysctl: setting key "kernel.unprivileged_bpf_disabled": Operation not permitted` even as root.

**Root Cause:** `kernel.unprivileged_bpf_disabled=2` is write-once: once applied, the kernel locks the sysctl and rejects any subsequent write — including from root — with EPERM. The default is value 1 (root can re-enable at runtime). Value 2 only applies if explicitly set via `system_unprivileged_bpf_disabled: 2` in `config.yml` for strict CIS/STIG lockdown.

**Normal bpfman and OVN-K development is unaffected.** Both workloads use privileged BPF via root-level processes (bpfman daemon with CAP_BPF, ovnkube-node as root), so unprivileged BPF is never needed at runtime.

**If you have set value=2 and need to test BPF rejection:** reboot and override the sysctl via kernel cmdline:

```
kernel.unprivileged_bpf_disabled=0
```

Add to `GRUB_CMDLINE_LINUX` in `/etc/default/grub` (or use `grubby`) for a one-time test, then revert.

**If you only need value=1 (the default):** with `system_unprivileged_bpf_disabled: 1`, root can change the sysctl at runtime without rebooting: `sudo sysctl -w kernel.unprivileged_bpf_disabled=0`.

**CSB IT ticket:** No. This is expected behavior; no provisioning change needed.

## system: SSH Fails to Legacy RHEL 7 / Old Servers After Provisioning

**Symptom:** `ssh user@old-server` fails with `no matching key exchange method found` or `algorithm negotiation failed`. This affects RHEL 7 servers, older network appliances, and any host that only supports SHA-1-based algorithms.

**Root Cause:** The `system` role sets `DEFAULT:NO-SHA1` as the system-wide crypto policy, which removes SHA-1 from all TLS/SSH algorithm negotiations. This is a security improvement (SHA-1 is broken), but RHEL 7 and many legacy systems require SHA-1 key exchange (`diffie-hellman-group14-sha1`) or host key algorithms (`ssh-rsa` with SHA-1).

**Connect to a specific host without changing system policy:**

```bash
ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 user@legacy-server
# Or for old host key type:
ssh -o HostKeyAlgorithms=+ssh-rsa user@legacy-server
```

**Add to `~/.ssh/config` for a permanent per-host workaround:**

```
Host *.rhel7.internal
    KexAlgorithms +diffie-hellman-group14-sha1
    HostKeyAlgorithms +ssh-rsa
```

**Revert to a less strict policy system-wide** (not recommended; reduces security posture):

```bash
sudo update-crypto-policies --set DEFAULT
```

**CSB IT ticket:** No. Applied by the playbook with sudo.
