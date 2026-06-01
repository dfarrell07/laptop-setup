# Troubleshooting

Known failure patterns organized by Ansible role. Each entry includes the symptom, root cause, fix, and whether an IT ticket is required on RHEL CSB.

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
- Wrap firewall tasks in `block/rescue` and record failures for the CSB compatibility report.
- Do not attempt to set the default zone or add custom rules without confirmed sudo access.
- For Tailscale: userspace networking mode avoids all firewall changes.
- On non-CSB machines (Fedora, macOS), firewall tasks should work normally with `--ask-become-pass`.

**CSB IT ticket:** Yes. Request permission to add a non-default SSH port and any required service exceptions to the drop zone.

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
The native binary is code-signed, GPG-verified, and has zero npm dependencies. It installs to `~/.claude/bin/` (add to PATH).

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
1. Disable the conflicting agent:
   - GNOME < 46 (RHEL 9): create `~/.config/autostart/gnome-keyring-ssh.desktop` with `Hidden=true`
   - GNOME 46+ (Fedora 42, RHEL 10): `systemctl --user disable --now gcr-ssh-agent.socket gcr-ssh-agent.service`
2. Start OpenSSH ssh-agent via systemd user service:
   ```ini
   # ~/.config/systemd/user/ssh-agent.service
   [Unit]
   Description=OpenSSH Agent
   [Service]
   Type=simple
   ExecStart=/usr/bin/ssh-agent -D -a %t/ssh-agent.socket
   [Install]
   WantedBy=default.target
   ```
3. Set `SSH_AUTH_SOCK` in `~/.config/environment.d/ssh_auth_sock.conf`:
   ```
   SSH_AUTH_SOCK=${XDG_RUNTIME_DIR}/ssh-agent.socket
   ```
4. Verify after login: `echo $SSH_AUTH_SOCK` should point to the OpenSSH socket, not a GNOME/gcr path.

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

---

## vault: Password File Not Found on First Run

**Symptom:**
```
ERROR! The vault password file /home/user/laptop-setup/scripts/vault-pass.sh was not found
```
Running `make all` fails immediately before any task executes.

**Cause:**
`ansible.cfg` references `vault_password_file = scripts/vault-pass.sh`. On a fresh machine, the vault password script does not exist yet because it depends on `ykpers` being installed (for YubiKey HMAC-SHA1 challenge-response).

**Fix:**
Bootstrap procedure for first run:
1. Install `ykpers` first (`sudo dnf install ykpers`), then the vault password script works automatically.
2. If YubiKey is not available yet, create a temporary password file:
   ```bash
   echo "your-vault-password" > ~/.vault_pass && chmod 0600 ~/.vault_pass
   ```
3. Run `make all`.
4. After setup completes, delete the temporary password file (`rm ~/.vault_pass`) and use the vault password script that derives the password from YubiKey challenge-response.

The `scripts/preflight.sh` should detect missing vault password sources and guide the user through bootstrap.

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
