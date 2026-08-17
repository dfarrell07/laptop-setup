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
make setup-yubikeys
```

The script (`scripts/setup-yubikeys.sh`) generates a fresh HMAC secret, programs
each YubiKey slot 2, verifies outputs match, and writes `vault-pass.sh`. The HMAC
secret never touches disk — keep all YubiKeys in separate physical locations.

**If you need to write vault-pass.sh manually**:

```bash
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykman (yubikey-manager package on Fedora/RHEL; brew install ykman on macOS)
set -euo pipefail
_HEX=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
timeout 20 ykman otp calculate 2 "$_HEX" 2>/dev/null || {
  echo "ERROR: YubiKey not available or touch timed out" >&2; exit 1
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
_HEX=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
ykman otp calculate 2 "$_HEX"
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

## FIDO2 SSH Key Best Practices

Always generate `ed25519-sk` keys with `-O verify-required`:

```bash
# Set FIDO2 PIN first (prevents silent use if key is stolen):
ykman fido access change-pin

# Generate auth key — verify-required = PIN + touch on every use:
ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk
# Or as a resident/discoverable credential:
ssh-keygen -t ed25519-sk -O resident -O verify-required -f ~/.ssh/id_ed25519_sk

# Same for signing key:
ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk_signing
```

`-O verify-required` (user-verification flag) gates every SSH operation on
PIN + touch. Without it, physical touch alone is sufficient — a stolen,
unpinned YubiKey silently authenticates. A FIDO2 PIN with at least 8
characters provides the second factor; `ykman fido info` shows current PIN
state.

**SSH key rotation** should also add `-O verify-required` to both keygen
commands in the SSH Key Rotation section above.

## Quantum Security Posture

| Component | Algorithm | Quantum status | Effective security |
|---|---|---|---|
| SSH key exchange | mlkem768x25519-sha256 | **Quantum-safe** (hybrid ML-KEM + X25519) | 128-bit post-quantum |
| SSH auth keys | ed25519-sk (FIDO2) | **Classical only** — no PQ hardware key support yet | 128-bit classical |
| Vault encryption | AES-256-CTR (ansible-vault) | **Quantum-safe** — Grover halves bits: 128-bit effective | 128-bit post-quantum |
| Vault password | HMAC-SHA1 (YubiKey OTP slot) | **Marginal** — Grover: 80-bit effective security | 80-bit post-quantum |
| Vault password (migration path) | PIV P-384 (age-plugin-yubikey 0.5.1) | **Classical only** — production-ready; recommended 2026 migration | 96-bit post-quantum |
| Git commit signing | ed25519-sk (SSH signing) | **Classical only** | 128-bit classical |

**SSH key exchange is already quantum-safe** — `mlkem768x25519-sha256` is
first in `KexAlgorithms`. Data in transit is protected against harvest-now/
decrypt-later attacks today.

**The vault password (HMAC-SHA1) is the weakest link at 80-bit post-quantum
security.** YubiKey OTP slots are hardware-limited to HMAC-SHA1; HMAC-SHA256
is not available in firmware. 80-bit security is currently considered
sufficient (the largest quantum computers in 2025 have ~2,000 noisy qubits,
far short of the millions needed for Grover attacks).

**August 2026 status:**
- **YubiKey 5.8** (released July 2026): no PQ algorithms in production; a PQ
  prototype was demonstrated Oct 2025; hardware PQ support is estimated ~2027.
- **ykpersonalize** reached EOL in Feb 2026; new scripts should prefer `ykman`.
  `setup-yubikeys.sh` uses `ykman otp chalresp` for HMAC-SHA1 slot programming.
- **age-plugin-yubikey 0.5.1**: production-ready; 2026 recommended migration path
  from `ansible-vault`. See *Future Migration to SOPS + age* below.
- **SOPS + age-plugin-yubikey**: 2026 community consensus for hardware-key-backed
  secrets in Ansible automation tooling.

**Migration path when hardware PQ support arrives:**
1. Yubico ships a YubiKey with HMAC-SHA256 (or ML-DSA) support
2. Re-run `make setup-yubikeys` (new secret; new algorithm)
3. `ansible-vault rekey group_vars/all/vault.yml`
4. Regenerate SSH keys with PQ hardware key if available

## Future Migration to SOPS + age

2026 community consensus for hardware-key-backed secrets in Ansible tooling.
Recommended when migrating away from `ansible-vault`'s HMAC-SHA1 vault password.

**Prerequisites (Fedora):**

```bash
sudo dnf install pcsc-lite-devel pcscd age sops
sudo systemctl enable --now pcscd
# age-plugin-yubikey is not in Fedora repos; build from crates.io:
cargo install age-plugin-yubikey   # lands in ~/.cargo/bin/
ansible-galaxy collection install community.sops
```

**PIV key setup (EC P-384, slot 9a):**

```bash
ykman piv keys generate --algorithm ECCP384 9a
ykman piv certificates generate --subject 'age-yubikey' 9a
age-plugin-yubikey          # follow prompts to generate key and get recipient string
age-plugin-yubikey --identity --slot 1 > yubikey-identity.txt
age-plugin-yubikey --list   # prints age1yubikey1… recipient string
```

**Configure SOPS (`.sops.yaml` at repo root):**

```yaml
creation_rules:
  - path_regex: group_vars/.*\.sops\.ya?ml
    key_groups:
      - age:
          - age1yubikey1<RECIPIENT>
```

**`ansible.cfg` update:**

```ini
[defaults]
vars_plugins_enabled = host_group_vars,community.sops.sops
```

**Migrate `vault.yml` (one-way; irreversible):**

```bash
ansible-vault decrypt group_vars/all/vault.yml
mv group_vars/all/vault.yml group_vars/all/vault.sops.yml
sops -i --encrypt group_vars/all/vault.sops.yml
sops -d group_vars/all/vault.sops.yml   # verify decryption works
git rm group_vars/all/vault.yml
git add group_vars/all/vault.sops.yml
```

**Runtime:** set `SOPS_AGE_KEY_FILE=/path/to/yubikey-identity.txt` before
running Ansible. Each decryption requires PIN entry + YubiKey touch. The
`community.sops.sops` vars plugin auto-decrypts `*.sops.yml` files in
`group_vars`/`host_vars`.

SOPS encrypts per-value — variable names (e.g. `vault_ssh_private_key:`)
remain plaintext in the committed file; only values are ciphertext. This
differs from `ansible-vault`, which encrypts the entire file as a single blob.
