# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately via
[GitHub Security Advisories](https://github.com/dfarrell07/laptop-setup/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

This is a personal workstation provisioning playbook. Security-relevant areas:

- **Ansible Vault** — secrets template in `group_vars/all/vault.yml`
  (encrypt after populating)
- **SSH key management** — YubiKey FIDO2 key deployment, post-quantum
  key exchange
- **System hardening** — firewall (drop zone), sysctl (49 settings),
  auditd (user persistence monitoring, immutable rules), kernel lockdown,
  SELinux enforcing (targeted), USBGuard, crypto policy DEFAULT:NO-SHA1,
  DNS-over-TLS/DNSSEC (FallbackDNS: Quad9 + Google DoT), core dump disable,
  io_uring restricted to CAP_SYS_ADMIN (kernel.io_uring_disabled=1; set 2 in config.yml to disable for all users), kernel
  module blacklist (incl. usb-storage/uas when system_disable_usb_storage: true), cups-browsed/avahi masking,
  dnf-automatic, /tmp nosuid/nodev/noexec (CIS 1.1.2.x), /var/tmp nosuid/nodev/noexec (CIS 1.1.8, bind-mount), /dev/shm nosuid/nodev/noexec (CIS 1.1.7.x), /home nosuid/nodev (CIS 1.1.9), /boot nosuid/nodev/noexec, /boot/efi nosuid/noexec (vfat — nodev not applicable), home directory 0750 (CIS 6.2.x, non-RHEL-CSB), NFS server /
  rpcbind masked (CIS 2.2.7), auditd user-mgmt tool watches — chsh/chfn/newgrp/su
  (CIS 4.1.3.8), pam_wheel.so (su restricted to wheel),
  root account locked, inactive account lockout chage -I 30 (CIS 5.5.1.5),
  AIDE file integrity monitoring (daily; disabled by default — set system_aide_enabled: true in config.yml only when a log consumer is in place), chrony NTS
  (authenticated time sync), DHCP hostname privacy (NM conf.d 99-dhcp-privacy.conf — suppresses hostname advertisement to DHCP servers on non-CSB hosts)
- **Git security** — `core.fsmonitor=false`, `safe.bareRepository=explicit`,
  `transfer.fsckObjects=true`,
  `protocol.file.allow=user`, SSH commit signing, gitleaks pre-commit
- **Claude Code isolation** — instance separation, sandbox config, file
  access deny lists (43 read + 44 write paths incl. /proc/environ,
  .git/config, settings.json self-modify protection), API endpoint guard,
  telemetry controls, `enableAllProjectMcpServers=false`, auditd monitoring
- **Supply chain** — `.npmrc ignore-scripts=true`, Ansible collection
  verification, Chrome extension allowlisting, binary SHA256 verification,
  oh-my-zsh pinned to commit SHA
- **CI supply chain** — actionlint, zizmor, and gitleaks run as
  SHA256-verified binary downloads instead of third-party node actions;
  all GHA actions SHA-pinned by commit hash; OSSF Scorecard weekly
- **Privilege escalation** — `become_exe = /usr/bin/sudo` in ansible.cfg
  prevents PATH-based sudo trojan attacks
- **Branch protection** — main requires 4 status checks (Ansible Lint,
  Vault Encryption Check, Secret Detection, Ansible Syntax Check);
  force push and deletion blocked; linear history enforced
- **Secret scanning** — GitHub secret scanning and push protection
  enabled (server-side complement to gitleaks pre-commit hook)
- **Browser hardening** — 41 Chrome/Chromium managed policies (HTTPS-only,
  site isolation, WebRTC, download restrictions, remote debugging blocked)
- **Desktop hardening** — GNOME lock screen notifications hidden, USB
  automount disabled, RDP/VNC disabled, idle lock at 300s (GNOME dconf,
  i3 via xss-lock + i3lock, Sway via swayidle + swaylock at 300s lock /
  600s display off)

## Known Limitations

- **Bluetooth enabled (CIS RHEL 9 2.1.5)** — CIS 2.1.5 recommends disabling
  Bluetooth by blacklisting the `btusb`/`bluetooth` kernel modules.
  `system_disable_bluetooth` defaults to `false` (enabled) because this
  ThinkPad P16v Gen 1 AMD requires Bluetooth for its Bluetooth mouse.
  Set `system_disable_bluetooth: true` in `config.yml` on machines that
  have no Bluetooth peripherals.
- **ptrace_scope=0 (CIS 1.6.3)** — `kernel.yama.ptrace_scope` defaults to 0 (Fedora
  workstation default), allowing a user to trace any of their own processes with `gdb -p`
  or `dlv attach`. CIS Level 1 recommends 1 (parent-only). Set
  `system_ptrace_scope: 1` in `config.yml` on machines where developer attach workflows
  are not needed.
- **claude.ai install.sh** — installed via `curl | bash` with no SHA256
  verification; Anthropic uses a rolling installer without pinned releases
  (note: binary SHA256 verification in the Scope section refers to other
  tools; the Claude Code installer is the deliberate exception)
- **Secure Boot** — not managed by Ansible (BIOS/firmware setting); kernel
  `lockdown=integrity` is weakened without a Secure Boot chain of trust
- **GRUB bootloader password** — not set (CIS 1.4.2); an attacker with
  physical console access can edit kernel boot parameters
- **LUKS TRIM/discard** — not managed by this playbook; if `rd.luks.options=discard`
  was set at OS install time, the storage controller can infer free blocks
  (SSD longevity vs. data remanence tradeoff)
- **Vault + CI** — encrypting vault.yml with a real YubiKey-derived password
  will break CI syntax-check (which uses the dummy password stub); this is
  a known design tradeoff, not a bug

## Vault Security

- Vault ships as a plaintext stub (`vault_placeholder`) — encrypt after
  adding real secrets
- Production vault password should be derived from YubiKey HMAC-SHA1
  challenge-response
- `vault-pass.sh` is gitignored; `make bootstrap` creates a stub for
  development
- CI uses `vault-pass-ci.sh` with a dummy password (no real secrets)
- Pre-commit hook blocks committing unencrypted vault files containing
  real variables
- Rotate vault password: re-key with
  `ansible-vault rekey group_vars/all/vault.yml`

### Setting Up vault-pass.sh

**Use the script** — programs all YubiKeys in one run, verifies each produces
identical output, and writes `vault-pass.sh` automatically:

```bash
make all          # installs ykpers + yubikey-manager first
make setup-yubikeys
```

The script (`scripts/setup-yubikeys.sh`) generates a fresh HMAC secret, programs
each YubiKey slot 2, verifies outputs match, and writes `vault-pass.sh`. The HMAC
secret never touches disk — keep all YubiKeys in separate physical locations.

**If you need to write vault-pass.sh manually** (Fedora/RHEL):

```bash
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykchalresp (ykpers package)
set -euo pipefail
CHALLENGE="ansible-vault-laptop-setup"
ykchalresp -2 "$CHALLENGE" 2>/dev/null || {
  echo "ERROR: YubiKey not available — insert YubiKey and retry" >&2
  exit 1
}
```

For macOS (`ykpers`/`ykchalresp` unavailable on Homebrew; use `ykman`):

```bash
#!/bin/bash
# scripts/vault-pass.sh — YubiKey vault password derivation (macOS)
# Requires: ykman (brew install ykman)
set -euo pipefail
CHALLENGE=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
ykman otp calculate 2 "$CHALLENGE" 2>/dev/null || {
  echo "ERROR: YubiKey not available — insert YubiKey and retry" >&2
  exit 1
}
```

After writing vault-pass.sh, encrypt the vault:

```bash
chmod 700 scripts/vault-pass.sh
ansible-vault encrypt group_vars/all/vault.yml
```

### Lost or Compromised YubiKey

**Backup YubiKey** — `make setup-yubikeys` programs all keys in one session with
the same secret. Run it with 2+ YubiKeys before storing backups separately.

**Manual re-programming** — if you have the original 40-character hex key from
a previous run, program a replacement YubiKey:

```bash
ykpersonalize -2 -ochal-resp -ochal-hmac -ohmac-lt64 -oserial-api-visible \
  -a <original-40-char-hex-key>
```

Without `-a <hexkey>`, `ykpersonalize` generates a new random secret and the
backup will not produce the same vault password. The HMAC secret cannot be read
back from a YubiKey after programming, so retroactive backup is impossible.

**Password manager fallback** — store the literal output of

```bash
ykchalresp -2 "ansible-vault-laptop-setup"
```

On macOS (`ykchalresp` is unavailable; use `ykman`):

```bash
CHALLENGE=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n') && ykman otp calculate 2 "$CHALLENGE"
```

in a hardware-backed password manager (e.g., Bitwarden) as a plaintext emergency
copy. This is the most practical single-YubiKey recovery path.

**YubiKey already lost with no backup** — if neither a backup YubiKey nor the
stored response exists, the vault is permanently inaccessible. The only path is
to re-encrypt from scratch: recover the secret values from memory or other
records, edit `group_vars/all/vault.yml` with known secrets, then re-key with a
new password source:

```bash
ansible-vault rekey group_vars/all/vault.yml
```

## SSH Key Rotation

1. Generate a new auth key pair:
   ```bash
   ssh-keygen -t ed25519-sk -f ~/.ssh/id_ed25519_sk
   ```
2. Generate a new signing key pair:
   ```bash
   ssh-keygen -t ed25519-sk -f ~/.ssh/id_ed25519_sk_signing
   ```
3. Run `make vault-edit` to update all four vault variables:
   `vault_ssh_auth_key`, `vault_ssh_auth_key_pub`,
   `vault_ssh_signing_key`, `vault_ssh_signing_key_pub`
4. Run `make ssh` to deploy the new keys on this machine and update
   `authorized_keys` (the role uses `exclusive: true`, so the old key
   is automatically removed from this machine's `authorized_keys`).
5. Remove the old public key from `authorized_keys` on every other
   machine that previously trusted it.
