# LastPass to Bitwarden

The owner's choice of Bitwarden (with KeePassXC for offline recovery secrets) and the order of the move are in the
owner's private notes. This page covers the one step automation does: importing the LastPass vault into Bitwarden
with `make pw-migrate`, without the plaintext export ever reaching persistent storage, a log, git or an agent.

## What you do, what the target does

1. **You** create the Bitwarden account, add its passkey or TOTP, switch the KDF to Argon2id, and store its recovery
   code in KeePassXC (owner's migration order).
2. **You** install the Bitwarden CLI (`bw`) and sign in: `bw login`, then `export BW_SESSION="$(bw unlock --raw)"`
   in the same shell. On macOS laptop-setup installs the Bitwarden app; `bw` itself comes from Bitwarden's release
   page or Homebrew (`bitwarden-cli`). Avoid the npm package version that shipped a trojan (2026.4.0).
3. **You** export the LastPass vault as CSV straight into memory and decrypt it there:
   - Linux: save it under `/dev/shm` or `$XDG_RUNTIME_DIR` (both tmpfs), e.g. `/dev/shm/lastpass.csv`.
   - macOS: create a RAM disk first, `diskutil erasevolume APFS PWMIGRATE $(hdiutil attach -nomount ram://262144)`
     (128 MB), and save it in `/Volumes/PWMIGRATE`. Eject it afterwards: `diskutil eject PWMIGRATE`.
   - Do not open the file in an editor or a viewer, and do not save it anywhere else first: browsers' download
     folders and editors' swap files are on disk.
4. **Dry run**: `make pw-migrate EXPORT=/dev/shm/lastpass.csv DRY_RUN=1 KEEP_EXPORT=1` prints only counts (items,
   logins, secure notes, folders, shared-folder items, TOTP seeds, attachments) and keeps the file for the real run.
5. **Import**: `make pw-migrate EXPORT=/dev/shm/lastpass.csv` prints the counts and the vault's current counts,
   asks before importing, runs `bw import lastpasscsv`, then verifies: the vault's item, login and TOTP counts
   must grow by exactly the export's, and new folders must not exceed the export's. It removes the export (and the
   TOTP-stripped copy it made next to it) whether it succeeds, fails, is interrupted or you decline (add
   `KEEP_EXPORT=1` to keep it on decline).
6. **You** check a few entries in the Bitwarden app, then finish the steps in the table below.

The target refuses an export that is not a regular file on tmpfs/ramfs (Linux) or a `ram://` RAM disk (macOS), and
leaves such a file untouched so you can move it. It never prints the file's contents, and hides `bw`'s output on
failure because it can echo rows; only `bw`'s exit status is shown. Agents must not read the export: run the target
yourself, or let an agent run it, but never `cat`, `head` or open the file.

## Options

| Variable | Default | Effect |
| --- | --- | --- |
| `EXPORT` | (required) | Path of the decrypted LastPass CSV |
| `DRY_RUN=1` | off | Counts only; removes the export unless `KEEP_EXPORT=1` |
| `STRIP_TOTP=0` | strip | Import TOTP seeds too (the owner's plan keeps them on the YubiKey, not in Bitwarden) |
| `ASSUME_YES=1` | ask | Skip the confirmation |

## What does not migrate cleanly, and what to do by hand

| Item | Why | By hand |
| --- | --- | --- |
| Attachments | Not in the CSV export | Download each into memory, attach it in Bitwarden, delete the download |
| TOTP seeds | Stripped by default: they belong on the YubiKey | Re-enroll 2FA on both YubiKeys (new seed) before closing LastPass |
| Shared folders | Become ordinary `Shared-...` folders; sharing is lost | Re-share via a Bitwarden organization if needed |
| Passkeys | LastPass passkeys do not export | Register new passkeys (both YubiKeys) on each site |
| Folder nesting | LastPass `a\b` folders become Bitwarden `a/b` names | Tidy in the Bitwarden app if needed |
| Form fills | Not in the CSV export | Re-enter by hand, or skip |
| Payment cards, addresses | Imported as Bitwarden card and identity items (from LastPass typed notes) | Check them in the app |
| Other typed notes (bank, passport, licence) | Become typed items or secure notes, depending on Bitwarden | Check them in the app |
| Password history, favourites | History is not exported; `fav` maps to favourite | None needed |
| Old passwords | The 2022 LastPass breach exposed the vaults | Rotate every password after the move (full credential refresh) |

After the move: confirm the counts in the Bitwarden app, rotate credentials, then delete the LastPass account.
