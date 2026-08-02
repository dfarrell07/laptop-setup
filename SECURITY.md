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
- **System hardening** — firewall (drop zone), sysctl (48 settings),
  auditd (user persistence monitoring, immutable rules), kernel lockdown,
  SELinux enforcing (targeted), USBGuard, crypto policy DEFAULT:NO-SHA1,
  DNS-over-TLS/DNSSEC (empty FallbackDNS), core dump disable, kernel
  module blacklist (incl. usb-storage/uas), cups-browsed/avahi masking,
  dnf-automatic, /dev/shm noexec, /home nosuid (CIS 1.1.9), NFS server /
  rpcbind masked (CIS 2.2.7), auditd user-mgmt tool watches — chsh/chfn/newgrp/su
  (CIS 4.1.3.8), pam_wheel.so (su restricted to wheel),
  root account locked, inactive account lockout chage -I 30 (CIS 5.5.1.5),
  AIDE file integrity monitoring (daily; disabled by default — set system_aide_enabled: true in config.yml only when a log consumer is in place), chrony NTS
  (authenticated time sync)
- **Git security** — `core.fsmonitor=false`, `safe.bareRepository=explicit`,
  `transfer.fsckObjects=true`, `core.symlinks=false`,
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

The real vault password script (gitignored) should derive the password
from YubiKey HMAC-SHA1 challenge-response:

```bash
#!/bin/bash
# scripts/vault-pass.sh — YubiKey vault password derivation
# Requires: ykpers package (ykchalresp command)
CHALLENGE="ansible-vault-laptop-setup"
ykchalresp -2 "$CHALLENGE" 2>/dev/null || {
  echo "ERROR: YubiKey not available" >&2
  exit 1
}
```

Create this file, then encrypt the vault:

```bash
chmod 700 scripts/vault-pass.sh
ansible-vault encrypt group_vars/all/vault.yml
```
