# Shared vault

One Ansible vault for every machine (Linux laptops, the Mac), so a secret added once (`make vault-edit`) reaches all of
them. It lives in the owner's private `laptop-secrets` repository, next to the other clones
(`~/src/<dotfiles_github_user>/laptop-secrets`):

| File | What it is |
| --- | --- |
| `vault.yml` | The Ansible vault, encrypted with a random 48-byte password |
| `vault-pass.age` | That password, encrypted with [age](https://age-encryption.org) to every enrolled YubiKey |
| `recipients` | The enrolled YubiKeys' public age recipients (`age-plugin-yubikey`, PIV), one per line |

`group_vars/all/vault.yml` stays the working copy that every play and check reads; `scripts/vault-shared.sh` keeps it in
step. `scripts/vault-pass.sh` becomes `age -d` of `vault-pass.age`: a plugged-in enrolled YubiKey, its PIV PIN and a
touch. GitHub holds only ciphertext and sees file sizes, commit times and messages.

The design follows the Health repository's: each key is its own recipient, so a key is added or dropped without
reprogramming the others, and a spare can open by touch alone.

## Set up

Before anything else on each machine: `gh auth login` (the repository is private), and commit signing working
(`make dotfiles` after the vault's signing key is in place; every commit to the shared repository is signed, a touch of
the signing YubiKey).

1. **First machine, once** (the one with the current vault and a YubiKey):
   `gh repo create <user>/laptop-secrets --private --clone` (run in `~/src/<user>`), then `make vault-share-init`. It
   enrols the plugged-in YubiKey if none is listed, seals a new random password to it, re-encrypts the vault with that
   password, switches `vault-pass.sh` and pushes. The old helper and vault stay beside the originals
   (`scripts/vault-pass.sh.pre-shared.*`, `group_vars/all/vault.yml.pre-shared`) until every machine has joined.
2. **Every other YubiKey**: plug in only that key and run `make vault-add-key` (`NOPIN=1` for a spare kept somewhere safe: touch
   only). The first use on a key makes its age slot; a key with firmware 5.7 or later first gets a PIN-protected TDES
   management key, because the plugin cannot use the factory AES one. Re-encrypting the password needs a key that is
   already enrolled; the plugin asks for it.
3. **Every other machine**: `make packages` (age and the plugin), then `make vault-join` with an enrolled key plugged
   in. A machine that had its own encrypted vault keeps it as `group_vars/all/vault.yml.pre-shared` (and its old helper
   as `scripts/vault-pass.sh.pre-shared`): read it with
   `.venv/bin/ansible-vault view --vault-password-file scripts/vault-pass.sh.pre-shared group_vars/all/vault.yml.pre-shared`
   (the make wrapper refuses that option on purpose), move any values only it has with `make vault-edit`, then delete
   both files.

## Day to day

- `make vault-edit` pulls, edits, commits and pushes; other machines pick it up with `make vault-pull` (or their next
  `make vault-edit`/`vault-view`). `make vault-status` shows the state without opening anything.
- Plays run as before: Ansible runs `vault-pass.sh`, so a run asks for the YubiKey PIN (once per key session) and a touch.

## Losing a key

`make vault-remove-key RECIPIENT=age1yubikey1...` drops it and rotates: a new random password sealed to the remaining
keys and the vault re-encrypted with it (plug in a key you keep, not the one you remove). The removed key still opens
old commits, so if a key was stolen, or a machine that had the vault was compromised, rotate the secrets themselves
too (new tokens and keys, then `make vault-edit`); the new password protects only what is written after it.

## Trust

- **Signatures are the trust anchor.** age does not say who encrypted a file, so anyone who can push could seal a
  password of their choosing to your keys and plant their own vault. Every command that reads the repository fetches
  first, resolves GitHub's head once, and refuses, before merging anything, unless every commit in its history is
  signed by one of your SSH signing keys (a clone made by hand is checked the same way). The keys come from
  `~/.config/git/allowed_signers`, or, on a machine without them yet, from the signing keys GitHub lists for your
  account after you confirm their fingerprints (kept in `~/.config/laptop-setup/vault-allowed-signers`). Only SSH
  signatures count. Pinning removes the trust-on-first-use step: put your signing key's fingerprint in `config.yml`
  as `secrets_signing_fingerprints: "SHA256:..."` (several space-separated; `ssh-keygen -lf <key>.pub`), and only
  keys with those fingerprints are trusted, from any source. When you rotate the signing key, keep the old public
  key in `allowed_signers` (with `valid-before`) so older commits still verify. The repository may hold only regular
  files (no symlinks).
- **No rollback**: each machine records the last commit it accepted and refuses a history that no longer contains it
  (a force-push back to an older signed state could bring back a removed key). A brand-new machine has no record yet:
  `make vault-join` shows GitHub's newest vault commit (date and subject) and asks you to confirm it is your latest
  change before trusting it.
- **Recipients**: anyone who can push could add their own key, and the next re-encryption would include it. Each
  machine keeps the list it approved (`~/.config/laptop-setup/age-recipients-approved`); `make vault-pull` warns when
  the list changed, and `vault-add-key`/`vault-remove-key` refuse a list with keys this machine never approved until
  `FORCE=1` (after checking `git -C ~/src/<user>/laptop-secrets log -p recipients`).
- **Failures**: `vault-share-init`, `vault-add-key`, `vault-remove-key` and `vault-edit` change nothing unless they
  push: an error, a failed signature or push, or Ctrl-C at a PIN or touch prompt puts the previous files, commit and
  helper back, so the command can simply be run again. A failed `vault-edit` keeps the edit, still encrypted, in
  `~/.config/laptop-setup/vault.yml.unpushed`.
- **No plaintext on disk**: the password moves only through pipes; the vault's plaintext appears only inside
  `ansible-vault edit` and `view`, as before.
- **CI** keeps the plaintext stub and `vault-pass-ci.sh`; nothing here changes it.

## Platform notes

- Linux x86_64 gets `age-plugin-yubikey` 0.5.0, the newest release with a Linux binary on GitHub (SHA256-pinned; upstream
  publishes no digest, so the pin is from two downloads); macOS gets 0.5.1 from Homebrew. Both read the same keys and
  files.
- `make vault-view` works offline from the working copy; changes need the network.
- The plugin talks to the key through `pcscd` (installed and enabled with the YubiKey packages).
