# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately via
[GitHub Security Advisories](https://github.com/dfarrell07/laptop-setup/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

This is a personal workstation provisioning playbook. Security-relevant areas:

- **Ansible Vault** — secrets template in `group_vars/all/vault.yml` (encrypt after populating)
- **SSH key management** — YubiKey FIDO2 key deployment, post-quantum key exchange
- **System hardening** — the defaults are in `roles/system/defaults/main.yml`; highlights: firewalld drop zone, sysctl hardening set,
  auditd (immutable rules, user persistence monitoring), kernel lockdown, SELinux enforcing, USBGuard, crypto policy `DEFAULT:NO-SHA1`,
  DNS-over-TLS/DNSSEC, core dump disable, io_uring restricted, kernel module blacklist, mount options
  (`/tmp`, `/dev/shm`, `/home`, `/boot`),
  0700 home directories, `pam_wheel` for `su`, root account locked, inactive account lockout, cups-browsed/avahi/NFS masking, optional AIDE
  and weekly setuid scan, chrony NTS, dnf-automatic. Corporate-managed (CSB-detected) hosts skip chrony time sync and dnf-automatic
  updates; RHEL CSB also skips boot config and firewall zones (Fedora hybrid CSB keeps them). Opportunistic DoT with `allow-downgrade`
  DNSSEC lets an on-path attacker force plain DNS; set `system_dot_mode: yes` and `system_dnssec_mode: yes` for strict enforcement (breaks
  captive portals)
- **Git security** — `core.fsmonitor=false`, `safe.bareRepository=explicit`, `transfer.fsckObjects=true`, `protocol.file.allow=user`,
  SSH commit signing, gitleaks pre-commit, `core.hooksPath` guard (CI and hooks)
- **Claude Code isolation** — instance separation, sandbox config, deny lists for credential paths and other instances' data,
  settings self-modify protection, API endpoint guard, telemetry controls, `enableAllProjectMcpServers=false`, auditd monitoring
- **Supply chain** — `.npmrc ignore-scripts=true`, vendored and verified Ansible collections, SHA256-verified
  binary downloads, oh-my-zsh pinned to a commit, Go module checksum verification (see "Go Module Supply Chain")
- **CI supply chain** — actionlint, zizmor and gitleaks run as SHA256-verified binary downloads (no third-party node actions); all GitHub
  Actions are SHA-pinned; OSSF Scorecard runs weekly
- **Privilege escalation** — `become_exe = /usr/bin/sudo` in `ansible.cfg` prevents PATH-based sudo trojans
- **Desktop hardening** — lock screen notifications hidden, USB automount disabled, idle lock (GNOME dconf, i3lock,
  swaylock; timeouts in
  `roles/desktop/defaults/main.yml` and `system_logind_idle_sec`)
- **Secret scanning** — GitHub secret scanning and push protection (server-side complement to the gitleaks hook)

Audit rules watch credential directories; absent credential files stay absent so the kernel can watch their later creation. Changing
immutable audit rules (`-e 2`) requires a reboot; restarting auditd cannot unlock them.

## Cryptographic Identities Registry

| Identity | Purpose | Key ID / Comment |
| --- | --- | --- |
| Collections signing | Owner signature of vendored checksums | Placeholder; no active public key is bundled |
| Commit signing key | Commit signatures (when enforced) | SSH `ed25519-sk` signing key (default) or a GPG key |
| SSH auth key | GitHub/internal git authentication | `ed25519-sk` (§ SSH Key Rotation) |
| Vault password | Ansible Vault | Derived from YubiKey HMAC-SHA1 (§ Vault Security) |

## Git Hooks Protection (core.hooksPath)

Hooks live in `.githooks/` and run with user privileges; `make hooks` (called by `make bootstrap`) sets
`git config --local core.hooksPath .githooks`.

- `pre-commit` blocks unencrypted vault files, runs gitleaks (blocks if gitleaks is missing), lints staged YAML/shell/JSON, and checks the
  SHA256 baselines for hooks, scripts and `collections-dist/`
- `commit-msg` requires `Signed-off-by:`, enforces the `Scope: noun-phrase` convention, and requires `commit.gpgsign=true` for
  core supply-chain files when `.gpg-enforcement` is `enabled`
- `pre-push` re-checks vault encryption, signoff, signing policy and gitleaks for every pushed ref
- `prepare-commit-msg` fails when `core.hooksPath` points at a missing directory

A local `git config --local core.hooksPath /dev/null` silences hooks. CI cannot see a developer's local config; the `git-config-guards` job
only verifies that the `make hooks` recipe still sets `.githooks` and that no hook or commit in a PR writes `core.hooksPath`.
**Do not override this setting.**

### Hook bypass prohibition

**Never use `git commit --no-verify` or `git push --no-verify`.** Git does not invoke hooks at all with `--no-verify`, so no hook can detect
it. The pre-commit hook guards vault encryption, secret scanning and collections integrity; tarballs modified without a matching
`collections-dist/SHA256SUMS` would otherwise reach a PR branch before CI runs. Unencrypted vault files pushed with `--no-verify` land in
history on the server before CI can reject the PR.

Compensating controls:

1. CI job `vault-encryption` runs on pull requests to main and on pushes to main (feature-branch pushes without a PR are not scanned)
2. It is a required status check on main, so unencrypted secrets cannot merge
3. Other required checks (`Secret Detection`, `Git Config Guards`) cover secrets and hook tampering

There is no safe bypass: no hook honours an environment override, and the only CI environment override is `SKIP_GPG_VERIFY=1`, which skips
just the missing-key GPG check in collection verification. Fix the failing check instead. The only exception is a CI-only
workflow step, and only when the commit is entirely CI-generated, the workflow is sealed, and earlier gates validated the content.

### Hook changes require review

Changes to `.githooks/pre-commit`, `.githooks/commit-msg` and `.githooks/pre-push` are validated three ways: shellcheck (syntax), a CI
behavioral scan, and owner review. `.githooks/prepare-commit-msg` is not shellcheck-ed or behaviorally scanned in CI; its changes get only
the CI `core.hooksPath` write check and owner review.

- The CI scan rejects `curl`/`wget`/`nc` with URLs, `cat` of `~/.ssh`, `~/.gnupg`, `~/.aws` or gcloud paths, and credential exports
  (`AWS_*`, `GOOGLE_*`, `GCP_*`, `GITHUB_TOKEN`)
- `.github/CODEOWNERS` lists the owner. Branch protection does not require approving reviews, so review is the owner's discipline;
  required status checks, linear history, no force push and no deletion are enforced

## Supply-Chain Validation

Branch protection on main requires every job in the `needs:` list of `all-checks-pass` in `.github/workflows/linting.yml`, the
per-platform Molecule jobs, and the aggregates `All Checks Pass`, `All Molecule Checks Pass` and `Supply-Chain Validation Gate`. Renaming
or deleting a job blocks merges until branch protection is updated. The supply-chain checks are `GPG Signature Verification` (signoff, plus
signatures when enforced), `Node Lint` (signoff, commit conventions, npm lockfile), `Secret Detection`, `Script Integrity Check` and the
collections jobs.

## Supply-Chain Commits: -s vs -S

| Flag | Full name | Cryptographic? | Purpose | Verification |
| --- | --- | --- | --- | --- |
| `-s` | `--signoff` | No | Adds a `Signed-off-by:` text trailer | commit-msg hook and CI regex |
| `-S` | `--gpg-sign` | Yes | Signs the commit object | `git verify-commit`, GitHub verification |

`-s` alone is only text. When `.gpg-enforcement` is `enabled`, commits touching supply-chain paths (`scripts/`, `.githooks/`,
`collections-dist/`, `.github/`, `Makefile`, `Makefile.sha256`, `roles/packages/defaults/main.yml`,
`roles/repos_dnf/defaults/main.yml`, root-level `requirements-test.lock`, `requirements-bootstrap.txt`, `package.json`,
`package-lock.json`, `.gitleaks.toml`, `.gpg-enforcement`) need both:

```bash
git commit -s -S -m "Scripts: update verify-collections.sh"
git verify-commit HEAD
git log -1 --format=%B HEAD | grep '^Signed-off-by:'
```

With `commit.gpgsign=true` configured, `git commit -s` is enough because `-S` comes from config.

## Temporary Repository GPG Policy

`.gpg-enforcement` is currently `disabled`. Commit and push hooks and CI keep requiring `Signed-off-by:` and secret scans, but not
cryptographic commit signatures. Collection verification skips GPG while still enforcing archive SHA256, Galaxy API digests, installed
source
comparison and the complete Python manifest. Package repository signature checks and binary download verification stay active. No
authenticated owner signature is claimed while the policy is disabled.

With the policy disabled, the `GPG Signature Verification` check verifies only the `Signed-off-by:` trailer on commits touching supply-chain
files. The `*.sha256` baselines (for example `.githooks/pre-commit.sha256`, `Makefile.sha256`) catch accidental edits only: they change in
the same commit as the file they cover, so an attacker with write access can update both.

`scripts/gpg-policy.sh` parses the tracked file as data; invalid values fail checks and a missing file means `enabled`. `pre-commit` and
`commit-msg` read the staged policy (`--staged`), so an unstaged edit cannot disable signing for a commit. `pre-push` reads the
working-tree policy, so an unstaged edit to `.gpg-enforcement` can skip its push-time signature check. Disabling is an explicit repository
choice, not a `--no-verify` bypass.

To re-enable enforcement:

1. Configure a commit-signing key and register its public key on GitHub (see "GPG Key Requirements for Supply-Chain Commits")
2. Replace the collection placeholder key and signature (see "SHA256SUMS Signing Key Management")
3. Write `enabled` to `.gpg-enforcement` and stage it
4. Run `git config --local commit.gpgsign true`
5. Commit with `git commit -s -S`, then run the signature and collection checks

## GPG Key Requirements for Supply-Chain Commits

When `.gpg-enforcement` is enabled, supply-chain commits must carry a signature GitHub verifies. The dotfiles role configures SSH signing
(`gpg.format=ssh`, key `~/.ssh/id_ed25519_sk_signing.pub`); a GPG key (RSA 4096 or ed25519) also works. Set `commit.gpgsign true` and
`user.signingkey`, and register the public key on GitHub as a signing key. For GPG setup see the
[GnuPG manual](https://www.gnupg.org/documentation/manuals/gnupg/) and
[GitHub's signature verification docs](https://docs.github.com/en/authentication/managing-commit-signature-verification).

## SHA256SUMS Signing Key Management

The bundled `collections-dist/signing-key.asc` is an invalid placeholder and `SHA256SUMS.asc` is a test signature, not upstream provenance.
Importing the placeholder cannot enable verification. Replace both with a real owner public key and a signature of `SHA256SUMS` before
claiming authenticated owner signatures.

When `.gpg-enforcement` is `enabled`, `scripts/verify-collections.sh` fails if the signing key is missing. `SKIP_GPG_VERIFY=1` (CI and other
non-production checks) permits a missing key but still fails on an invalid signature when the key is present.

`scripts/verify-collections.sh` hashes the `.py` and `.so` files under `plugins/`, `module_utils/` and `roles/` and compares them with the
vendored tarballs. Collections installed in `~/.ansible/collections` or `/usr/share/ansible/collections` are not verified. It also fails on
any symlink (including a symlinked or broken `collections/` root) and on an `__init__.py` in the collections root, namespace or collection
directories, and warns on stderr when a vendored collection is not installed (partial bootstrap).

To configure a real owner signature:

1. Verify the archives against Galaxy with `scripts/verify-collections-galaxy.sh`
2. Export the owner's public signing key to `collections-dist/signing-key.asc`
3. Sign `collections-dist/SHA256SUMS` with it and replace `SHA256SUMS.asc`
4. Record the verified fingerprint in the identities registry
5. Commit the key, signature and checksums together in a signed commit

Rotate a compromised owner key by publishing its revocation, replacing the public key and signature, and updating the pinned fingerprint. A
signature authenticates owner-approved checksums; it does not establish provenance from a Galaxy maintainer. A compromised GPG
commit-signing
key is handled the same way: revoke it, remove it from GitHub, and re-sign.

## Ansible Playbook Safety

**Do not use `--start-at-task`.** It skips play-level `pre_tasks`, including the security validations, so
`-e claude_install_url=https://evil.example/x.sh` could reach the installer. Use the scoped make targets (`make dotfiles`, `make repos`,
`make ssh`, `make claude`, ...); they run through `scripts/verify-ansible-args.sh`. Do not run `ansible-playbook` directly.

The wrapper (`VERIFY_AND_RUN` in the Makefile):

- rejects `--start-at-task` (and abbreviations), `--vault-id`, `--vault-password-file`, `--skip-tags always`, `@file` extra-vars,
  backslash-escaped structured extra-vars, and `-e` overrides of protected variables (`csb_rhel`, `_pf_*`, `system_sysctl_hardening`,
  `common_project_root`, `rollout_enable`, `rollout_catalog`, `_rollout_*`)
- canonicalizes abbreviated long options before scanning
- turns shell tracing off and clears `BASH_XTRACEFD`, `PS4`, `SHELLOPTS`, `BASHOPTS` and 90+ other environment variables
  (`ANSIBLE_*`, `LD_*`, `PYTHON*`, `GIT_*`, Go, container and registry variables), pins `ANSIBLE_VAULT_PASSWORD_FILE`, and checks `HOME`
- runs `scripts/verify-collections.sh` before exec

Each role also re-validates its critical inputs at the start of `main.yml` (for example, `roles/claude/tasks/main.yml` constrains
`claude_install_url` to `claude.ai` or `anthropic.com`), so a skipped `pre_tasks` does not reach the installer.

**Chunk selection** (`rollout_enable` in `config.yml`, registry `group_vars/all/rollout.yml`): a chunk's name is its Ansible tag. The
wrapper
turns every chunk that is not enabled into `--skip-tags` for `site.yml` runs and rejects an invalid `rollout_enable` before any sudo prompt.
Play 0 (`pre-flight.yml`) has an `always`-tagged assert that every disabled chunk is in `ansible_skip_tags`, so a direct `ansible-playbook`
run cannot apply a disabled chunk. The enabled list can only come from `config.yml`. Skipping a chunk's tag also skips `always` guards
inside
that chunk's tasks (they only protect that chunk); top-level guards stay.

## Ansible Collections Supply Chain

Four collections are vendored in `collections-dist/` (versions pinned in `requirements.yml`; archives listed in `SHA256SUMS`).

### Ansible Collections Maintainer Identity Verification

Galaxy publishes no GPG signatures, Sigstore certificates or SLSA provenance for community collections. If a maintainer's Galaxy account is
compromised, the attacker controls both the CDN tarball and the API `artifact.sha256` field, so hash verification passes. Until a real owner
key is configured, protection is: version pinning, the independent Galaxy API cross-check (catches CDN corruption only), local archive and
manifest checks, and review of every collection bump. These checks must not be described as upstream signature verification. Report a
suspected compromise to the [Galaxy security team](https://galaxy.ansible.com/security).

### Verification layers

1. **Pre-commit hook** blocks `collections-dist/*.tar.gz` changes without a `SHA256SUMS` update
2. **`make bootstrap`** runs `sha256sum -c SHA256SUMS` and `scripts/verify-collections-galaxy.sh`
3. **Runtime** `scripts/verify-collections.sh` re-verifies before every wrapped run (TOCTOU guard)
4. **CI** `collections-integrity` runs the Galaxy and runtime checks; `collections-manifest` checks the Python manifest;
   `galaxy-recheck` and `upstream-collections-check` (`scripts/verify-vendor-tarball.sh`) compare against Galaxy and upstream release tags
5. **Audit trail** — tarballs are committed to git, not ignored

### Upgrade procedure

```bash
# 1. Bump the version in requirements.yml AND in the spec list inside the vendor-collections recipe in the Makefile, regenerate the
#    Makefile baseline with `sha256sum Makefile > Makefile.sha256` (the pre-commit hook rejects a changed Makefile without it), then:
make vendor-collections

# 2. Review the upstream changelog for new tasks, plugins, network calls and privilege escalation
tar -xzOf collections-dist/community-general-<new>.tar.gz CHANGELOG.rst | head -100

# 3. Compare vendored runtime files against the upstream release tag (needs network and git; CI runs the same check)
scripts/verify-vendor-tarball.sh

# 4. Diff old and new plugins
d=$(mktemp -d)
mkdir "$d/old" "$d/new"
tar -xzf collections-dist/community-general-<old>.tar.gz -C "$d/old"
tar -xzf collections-dist/community-general-<new>.tar.gz -C "$d/new"
diff -r "$d/old/plugins" "$d/new/plugins" | head -50

# 5. Commit with signoff on a branch and open a PR; review the diff before merging
git add requirements.yml Makefile Makefile.sha256 collections-dist
git commit -s -m "Collections: bump community.general to <new>"
```

When enforcement is enabled, re-sign `SHA256SUMS` and replace `SHA256SUMS.asc` in the same commit (the pre-commit hook rejects a
stale signature), and verify the signature before trusting the checksums:

```bash
gpg --keyserver keys.openpgp.org --recv-keys <fingerprint-from-registry>
gpg --list-key <fingerprint-from-registry>
gpg --verify collections-dist/SHA256SUMS.asc collections-dist/SHA256SUMS
```

## Known Limitations

- **Bluetooth** — `system_disable_bluetooth` defaults to `true` (CIS 2.1.5, blacklists `btusb`/`bluetooth`). Set it to `false` if you rely
  on Bluetooth peripherals.
- **ptrace_scope (CIS 1.6.3)** — `system_ptrace_scope` defaults to 1 (parent-only). Set 0 in `config.yml` on developer machines that need
  `gdb -p` or `dlv attach`.
- **claude.ai install.sh** — downloaded with `get_url` and run only when it matches `claude_install_sha256` (`claude_pinned_version` is a
  minimum version); the binaries it fetches come from Anthropic's rolling release channel and are not pinned by this repo.
- **SHA256-only release binaries** — tkn, ec, operator-sdk, sops, actionlint and zizmor use pinned SHA256 checksums without signature or
  attestation checks (tkn and ec upstreams publish no Sigstore attestations; see the TODOs in
  `roles/packages/tasks/install_standalone_binaries.yml`). This defends against CDN corruption but not a compromised release or maintainer
  account. Review upstream releases before updating hashes in `roles/packages/defaults/main.yml`. cosign upgrades verify the Sigstore
  bundle.
- **lockdown=integrity is not userspace integrity** — it restricts kernel self-modification (unsigned modules, `/dev/mem` writes, kexec of
  unverified kernels) and does nothing about executed binaries. No IMA appraisal policy is deployed (Fedora's default is measure-only).
  Mitigations: SELinux enforcing, optional AIDE (`system_aide_enabled: true`), or a full IMA appraisal setup this playbook does not provide.
- **Swap and vault memory remanence** — Ansible decrypts the vault into process memory; under memory pressure the pages can be swapped to a
  block device and survive a reboot. `vm.swappiness: 1` minimizes swapping (override to 0 via `system_sysctl_extra_additional` on LUKS
  hosts); zram-only hosts keep pages in RAM and use `system_zram_swappiness`. Use LUKS on swap partitions, run `make all` on a local console
  and reboot promptly afterwards.
- **LUKS TRIM/discard** — `system_luks_discards_enabled: false` (default) avoids SSD wear-pattern leakage. Enable only where SSD longevity
  outweighs that risk; smoke-test warns when discard is active.
- **Vault password strength** — the vault password is the 160-bit YubiKey HMAC-SHA1 output, about 80-bit strength against Grover-style
  attacks (YubiKey firmware has no HMAC-SHA256). Accepted risk; revisit when hardware supports a stronger primitive.
- **Vault and CI** — CI copies `scripts/vault-pass-ci.sh` over `vault-pass.sh`; the committed `vault.yml` is a plaintext stub.
  Committing a vault encrypted with the real password breaks CI jobs that decrypt with the dummy password, and CI rejects a vault
  encrypted with the dummy password.

## Secure Boot + GRUB bootloader password

`lockdown=integrity` without Secure Boot is weakened by an unprotected GRUB: a physical attacker can edit the kernel command line before
lockdown activates. Both protections are needed.

- Secure Boot is a firmware setting (out of scope for Ansible); `make smoke-test` reports its state.
- The GRUB superuser password is opt-in: set `system_grub_password_enabled: true` in `config.yml` and `vault_grub_password_hash` in the
  vault (generate with `grub2-mkpasswd-pbkdf2`; never put the hash in `config.yml`). Leaving the hash empty while
  `system_grub_password_enabled` is true fails provisioning at the hash assert (it does not disable the GRUB password). The default,
  `system_grub_password_enabled: false`, sets no password. RHEL CSB skips it (IT manages the bootloader).
- A GRUB password does not stop a physical attacker from entering firmware setup to change boot order or disable Secure Boot. Set a
  UEFI/BIOS admin password in firmware (not configurable via Ansible). The playbook warns when it cannot confirm one via
  `fwupdmgr get-bios-setting`.

## Vault Security

- Vault ships as a plaintext stub (`vault_placeholder`); encrypt after adding real secrets
- The production vault password is derived from YubiKey HMAC-SHA1 challenge-response
- `vault-pass.sh` is gitignored; `make bootstrap` creates a stub from `vault-pass-ci.sh` (integrity-checked)
- CI uses `vault-pass-ci.sh` with a dummy password (no real secrets); a CI job rejects vaults encrypted with it
- The pre-commit hook blocks committing unencrypted vault files that contain real variables
- Rotate the password with `ansible-vault rekey group_vars/all/vault.yml`

### Setting Up vault-pass.sh

Use the script. It programs all YubiKeys in one run, verifies each produces identical output, and writes `vault-pass.sh` atomically:

```bash
make setup-yubikeys
```

`scripts/setup-yubikeys.sh` generates a fresh HMAC secret, programs slot 2 of each key, and verifies the outputs match. The secret never
touches disk and is never displayed. Keep the YubiKeys in separate physical locations.

To write `vault-pass.sh` manually:

```bash
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykman (yubikey-manager package on Fedora/RHEL; brew install ykman on macOS)
# SECURITY: this script prints the vault password; never let xtrace record it.
unset BASH_ENV
{ set +x; } 2>/dev/null
set -euo pipefail
_CHALRESP_HEX=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
_to=timeout; command -v timeout >/dev/null 2>&1 || _to=gtimeout
"$_to" 20 ykman otp calculate 2 "$_CHALRESP_HEX" 2>/dev/null || {
  echo "ERROR: YubiKey not available or touch timed out — insert YubiKey and retry" >&2; exit 1
}
```

Then:

```bash
chmod 700 scripts/vault-pass.sh
(cd scripts && sha256sum vault-pass.sh > vault-pass.sh.sha256)  # make checks this; relative path required
ansible-vault encrypt group_vars/all/vault.yml
```

### Future Migration to SOPS + age

HMAC-SHA1 is the only challenge-response primitive the YubiKey OTP applet offers. If stronger hardware support arrives, migrate secrets to
SOPS with `age-plugin-yubikey` and retire `vault-pass.sh`. No migration is scheduled; see "Known Limitations" for the strength trade-off.

### Lost or Compromised YubiKey

`make setup-yubikeys` enforces a recovery path:

1. **Hardware redundancy (recommended)** — program 2+ YubiKeys in one session with the same secret and store them separately; any one
   unlocks the vault.
2. **Password manager backup (single YubiKey)** — the script requires you to save the vault password to your password manager during setup.

The HMAC secret cannot be read back from a YubiKey and the script never displays it, so a replacement key cannot be cloned afterwards.
To rotate to new keys, decrypt with a surviving key (or the saved password), re-run `make setup-yubikeys` (which generates a new secret and
so
changes the vault password), then re-encrypt the decrypted vault with `ansible-vault encrypt group_vars/all/vault.yml`. (`rekey` needs an
encrypted file and fails on plaintext.)

**Recovering with the saved password:**

1. Retrieve the password and read it into the shell without recording it in history: `read -rs VAULT_PASS && export VAULT_PASS`
2. Create a temporary password script:

   ```bash
   umask 077
   tmp=$(mktemp -d)
   printf '#!/bin/bash\necho "%s"\n' "$VAULT_PASS" > "$tmp/vault-pass-temp.sh"
   chmod 700 "$tmp/vault-pass-temp.sh"
   ```

3. Call `ansible-vault` directly. Do not use `make vault-*`, because the wrapper pins `scripts/vault-pass.sh`:

   ```bash
   ansible-vault view --vault-password-file "$tmp/vault-pass-temp.sh" group_vars/all/vault.yml
   ansible-vault rekey --vault-password-file "$tmp/vault-pass-temp.sh" group_vars/all/vault.yml
   ```

4. Shred the temporary script and unset the variable: `shred -u "$tmp/vault-pass-temp.sh"; rmdir "$tmp"; unset VAULT_PASS`

**No YubiKey and no saved password** — the vault is permanently inaccessible. Re-create the secrets, replace `group_vars/all/vault.yml`
with a fresh plaintext template, then run `make setup-yubikeys` and `ansible-vault encrypt group_vars/all/vault.yml`.

## Internal SSH Git Host Setup

For repositories on internal SSH git servers (not GitHub), pre-seed host keys with optional fingerprint pinning to prevent MITM at first
connection. Configure `ssh_work_keyscan_hosts` in `config.yml` (work profile only):

```yaml
ssh_work_keyscan_hosts:
  - {host: internal-git.example.com, fingerprint: "SHA256:..."}
```

1. Request each server's SSH host key fingerprint from its administrators over a secure out-of-band channel (phone, in person, verified
   email)
2. Confirm it: `ssh-keyscan -t ed25519 internal-git.example.com | ssh-keygen -lf /dev/stdin`
3. Add it as the `fingerprint` field. Omitting `fingerprint` means trust on first use (less secure). A placeholder that is not a full
   `SHA256:` value defers the host: nothing is seeded and its SSH repos are skipped until you set the real value

`roles/ssh/tasks/keyscan_host.yml` seeds `~/.ssh/known_hosts` (work profile only). The `git_repos` role does not seed hosts: it clones SSH
repos only from hosts already in `known_hosts` and defers the rest to `make ssh repos`. GitHub keys are seeded from a pinned key.

## SSH Key Rotation

Generate `ed25519-sk` keys with `-O verify-required`, and set a FIDO2 PIN first so a stolen key cannot be used by touch alone:

```bash
ykman fido access change-pin
ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk
ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk_signing
# Or add -O resident to store a discoverable credential on the key
```

Then:

1. Run `make vault-edit` and update `vault_ssh_auth_key`, `vault_ssh_auth_key_pub`, `vault_ssh_signing_key`, `vault_ssh_signing_key_pub`
2. Run `make ssh` to deploy the keys; `authorized_keys` is written with `exclusive: true`, so the old key is removed from this machine
3. Remove the old public key from `authorized_keys` on every other machine that trusted it

`ykman fido info` shows PIN state. sshd hardening accepts only ed25519 and FIDO2 (`sk-ssh-ed25519`) keys.

## Go Module Supply Chain

Go tools installed with `go install` (gofumpt, gopls, golangci-lint, govulncheck, gci, stern, controller-gen, the k8s code-generator
tools, subctl; work profile only) pull dozens of transitive dependencies each.

**Threat model.** `go install` compiles and does not run package `init()` code; injected code runs the first time the installed tool is
executed, with the user's privileges. Go's module checksum database (GOSUMDB) detects tampered module content and proxy tampering. It does
not detect a malicious release published by a compromised maintainer, GOPROXY or TLS hijack of the first fetch, typosquatting, or a
backdoored
dependency that already has a valid checksum. It covers the whole build list, not only direct modules. Built binaries are not hash-pinned:
their bytes depend on the local toolchain and flags, so no upstream binary hash exists.

**Mitigations** (`roles/packages/tasks/install_go_tools.yml`):

1. **Version pinning** in `roles/packages/defaults/main.yml` (`packages_*_version`); bumps need deliberate review
2. **GONOSUMDB validation** — the effective `GONOSUMDB`/`GOPRIVATE` value may contain only private patterns; public hosts, wildcard
   owners, patterns covering the installed public tools and unsupported glob syntax are rejected
3. **GOSUMDB enforcement** — set to `sum.golang.org` (default; override with `packages_gosumdb`), must not be `off` or empty, and the
   server must be reachable for Go tool installs to run (if unreachable, they are skipped with a warning and the play does not fail); CI
   also rejects a disabled GOSUMDB
4. **No retry on integrity failures** — only transient `permission denied` is retried; checksum mismatches fail fast
5. **Sanity checks** — `scripts/smoke-test.sh` runs each Go tool's version command (`--version`; `gopls` and `subctl` use `version`,
   `govulncheck` uses `-version`); this does not catch stealthy payloads
6. **Isolation** — `make container` provisions the dev tier in a distrobox, which bind-mounts `$HOME` (same UID) and is not a credential
   boundary: host `~/.ssh`, `~/.gnupg`, and `scripts/vault-pass.sh` (when the checkout is under `$HOME`) are readable inside it. Mount-level
   isolation is not configured by this repo.

**Hardening options by risk:**

- Baseline: pinned versions, GOSUMDB, `govulncheck` on your own projects, periodic `make smoke-test`
- Monitoring: enable AIDE (`system_aide_enabled: true`) and audit rules for credential paths; inspect `journalctl` after installs
- Isolation: do untrusted code review and testing only inside the `make container` environment
- Strict: install pre-built release binaries with verified checksums or signatures where upstreams publish them, use an internal vetted
  proxy, or provision from an air-gapped mirror

**Configuration** (`config.yml`):

```yaml
packages_gosumdb: "sum.example.com"                          # corporate sumdb (FQDN or FQDN:port)
dotfiles_gonosumdb: "*.internal.example.com"                  # private module domains only
packages_goproxy: "https://goproxy.example.com,direct"        # corporate proxy with direct fallback
```

For air-gapped environments set `packages_goproxy: 'off'` or point it at an internal proxy only.

**Corporate builds with fapolicyd** may block `go install` compilation in `/tmp`. Options: set the proxy variables above, ask your IT team
to allow `proxy.golang.org` and `sum.golang.org`, use pre-built binaries where the repo provides them (`install_binary_downloads.yml`:
kind, helm, kustomize, k9s; oc/kubectl/opm via `install_oc_opm.yml`; standalone tools via `install_standalone_binaries.yml`; none of the
Go tools listed above), or run the tools inside the container (`make container`, then
`distrobox enter <container-name> -- gofumpt --version`).

Report supply chain issues in Go itself to <security@golang.org>.
