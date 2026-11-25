# Drive sync for large artifacts

Status: slice 1 is implemented (the `drive_sync` role and chunk, Linux). Slice 2 (macOS, vault-held tokens) is listed
at the end.

## Goal

Every machine gets the same local directory per Google account, `~/drive/<account>/`, whose contents are kept on
Google Drive. Agents put there what must outlive the machine but does not belong in Git: large binary artifacts (a
voice-model training run writes ~15 GB checkpoints), datasets, exported results, backups. Ansible sets it up; the
manual step is one browser consent per account.

Not goals: two-way sync, editing one file on two machines, Google Docs, work (Red Hat) Drive, sharing between accounts.

## Decision: one-way push per machine, pull on request

Each machine **pushes** `~/drive/<account>/` to its own Drive folder every 30 minutes (`rclone sync`). Another
machine's files arrive only when asked: `drive-sync pull <account> <host>/<path>`. Every Drive path has one writer, so
there is nothing to reconcile. A machine does not see another machine's new files until it pulls them.

| Option | Why not |
| --- | --- |
| `rclone bisync` | An advanced command, long in beta; its listing state needs `--resync` after errors; reported silent misses. |
| `rclone mount` | Uploads after close only, 15 GB of cache per file, reads fail offline, a kernel extension on macOS. |
| Google Drive for desktop | No Linux client and interactive sign-in only, so the Mac would behave differently from Fedora. |
| GNOME Online Accounts | Drive file access was removed in GNOME 50. |

rclone runs on both (Fedora package, Homebrew). Drive takes up to 5 TB per file and 750 GB of uploads per user per
day; rclone uploads in resumable 64 MiB chunks. Drive has no partial update: a changed 15 GB file uploads in full, and an
upload interrupted by sleep starts over on the next run.

## Layout and push rules

| Local | Drive (`<remote>` is `drive-<account>`) |
| --- | --- |
| `~/drive/<account>/` | `<remote>:laptop-drive/<host>/`, pushed by this machine only |
| `~/drive/<account>/.pulled/<host>/<path>` | a pulled copy of another machine's files; never pushed (this host: restored into the root) |

- `<host>` is `drive_sync_host`, set once per machine in `config.yml` (required once an account is listed; generic
  names such as `fedora` or `localhost` are rejected), so two machines never share a folder and a DHCP name change
  does not start a new one. The folder holds `.drive-sync-machine`, a hash of the pushing machine's `/etc/machine-id`;
  a push refuses a folder another machine owns (the same `drive_sync_host` in a copied `config.yml`), since each
  machine's push would delete the other's files. After a reinstall (a new machine-id) run `drive-sync adopt <account>` in a terminal:
  it asks you to type the host name, claims the folder and restores it; pushes refuse until the restore finishes. An
  empty or garbled marker is refused, never treated as unowned.
- Skipped: `/.pulled/**`, `*.partial` and `*.partial/**`, temp files (`*.tmp`, `*.incomplete`, `.~lock*`, `.DS_Store`),
  and secret-shaped files (`.env*`, `*.pem`, `*.key`, SSH key names `id_rsa*`, `id_ed25519*` and the like). Symlinks
  inside the tree are refused (outside the skipped paths): rclone skips them, so a file or directory replaced by a link,
  or a link to an unmounted disk, would be deleted from Drive. A symlinked account root is followed by both the scan
  and the push.
- A push refuses when the local tree holds no pushable files (excluded paths and `.gitleaksignore` do not count) but
  the Drive copy does (a wiped or re-created directory, or a new machine reusing a host name), so it cannot empty
  Drive. The way out: `drive-sync pull <account> <this-host>` restores this machine's Drive copy into the account
  root, or pick a new host name.
  Deleted and overwritten files go to Drive's trash (30 days) and file revisions, which is the undo.
- A push refuses when the Drive's free space (`rclone about`) is less than the bytes it would upload (local files missing
  on Drive or of another size, from `rclone lsjson` on both sides; deletions free nothing for 30 days), so a full Drive does not re-upload
  gigabytes every run; `--drive-stop-on-upload-limit` stops at the
  750 GB daily upload limit.
- Before uploading, a gitleaks scan (files up to 10 MB, same exclusions as the push, plus a `.gitleaksignore` at the
  account root for false positives) stops the push if it finds a secret. gitleaks comes from the packages role. Larger
  files (the artifacts themselves), the inside of archives, and what gitleaks' default allowlist skips (such as
  `node_modules/`, lock files and binary formats like `.pdf`) are not scanned, and the scan covers the files present
  when it starts (write with `.partial` names so nothing half-written lands in between).
- One push or pull at a time (`flock`): the timer's run exits 0 when one is already running; a manual push or pull
  waits. A restore of this host's copy leaves `.drive-sync-restoring` until a full restore completes (a subpath
  pull never clears it), and pushes refuse while it is there, so a restore that failed halfway is never pushed over
  Drive (run the pull again). `--contimeout 60s --timeout 5m` end a hung connection;
  `--transfers 2` bounds memory to about 128 MB.
- Every refusal (empty source, quota, a gitleaks finding, gitleaks missing, a token Drive refuses, a symlink,
  a folder another machine owns) exits non-zero,
  so the push unit shows as failed; the smoke test reads that state, never a status file. A timer run skips an
  account that is not set up yet (no remote or no token), which `drive-sync-auth` reports instead.

## Accounts and credentials

- `config.yml`: `drive_sync_accounts: [<name>, ...]` (short names, e.g. `[owner, partner]`; no addresses in the repo)
  and `drive_sync_host`. Empty accounts (the default) means the chunk does nothing; rclone itself comes from the
  packages role on Fedora and Debian (IT's CSB backup client also relies on it); elsewhere install it by hand, and
  until then the chunk leaves the push timer off and warns.
- One OAuth client (Desktop app) in a Google Cloud project the owner keeps, publishing status **In production**:
  Testing-status clients get refresh tokens that expire after 7 days, and rclone's shared client is retired during 2026.
  Scope `drive.file` (rclone sees only files it created) is non-sensitive, so the client needs no verification. All
  machines must use this one client: files are visible per account and client, and deleting the client or project
  cuts rclone off from everything already uploaded. Client ID and secret live in the vault
  (`vault_drive_oauth_client_id`, `vault_drive_oauth_client_secret`); Google treats a Desktop client's secret as not
  confidential, so the vault only keeps it out of the public repo.
- Tokens: `drive-sync auth <account>` runs the browser consent and rclone stores the token in
  `~/.config/rclone/drive-sync.conf` (0600, refreshed in place by rclone; separate from IT's backup configuration).
- What protects the token: not the agent deny lists. Any agent running as the user can run `rclone` and read the
  config. The limits are the `drive.file` scope and revocation: Google Account > Security > Third-party access
  revokes the client for that account on every machine (rclone's Drive backend cannot revoke one token); deleting the
  remote from one machine's config only forgets it there. Encrypting the config would not help: the timer must
  decrypt it unattended, so an agent could too.
- A partner account: she consents on her own device, not in the owner's browser, and knows that the owner's machines
  and agents can create and read files in `laptop-drive/` of her Drive. The owner runs `drive-sync auth partner` and
  answers `n` to "Use web browser"; rclone prints an `rclone authorize "drive" "…"` command. She runs it on her device
  (rclone installed), signs in, and sends back the token it prints, which the owner pastes at the `config_token>`
  prompt. A free account has 15 GB in total, about one checkpoint, and trash and revisions count against it for 30 days.

### Creating the OAuth client (once)

1. In the Google Cloud console (owner's account), create a project, then enable the **Google Drive API** in it.
2. OAuth consent screen: user type **External**; add the `.../auth/drive.file` scope; set the publishing status to
   **In production** (Testing makes tokens expire after 7 days).
3. Credentials: create an **OAuth client ID** of type **Desktop app**.
4. `make vault-edit`: set `vault_drive_oauth_client_id` and `vault_drive_oauth_client_secret` from it.
5. In `config.yml` set `drive_sync_accounts` and `drive_sync_host`, run `make apply CHUNK=drive_sync`, then
   `drive-sync auth <account>` for each account (the push timer starts once the client is in the vault).

## Work (Red Hat) Drive

Not configured. No Red Hat statement was found either way, and Workspace admins commonly block third-party OAuth
apps for Drive scopes. Whether a personal OAuth app may hold a token for a redhat.com account, and whether work data
may sync this way, are policy questions for Red Hat IT; trying the consent screen to find out is not the way. The
same goes for the other direction on the managed laptop: pushing to a personal Drive from a CSB machine is data
egress that IT's tooling may flag. The chunk is opt-in per machine (no accounts, nothing happens), and the owner's
decision for the work laptop belongs in its gitignored local notes.

## Sharing between accounts

Out of scope. With `drive.file`, rclone cannot see a folder the other account shares; it would need the full `drive`
scope on every token.

## Agent convention

`~/drive/README.md`, written by the role where an account is configured, carries the rules: where artifacts go, the
`.partial` then rename convention, new names over rewriting 15 GB files, push and pull, no secrets, no sharing,
cleanup. Its source is `roles/drive_sync/templates/README.md.j2`. The managed agent baseline says, in one line, to
follow it when it exists.

## Rollout

New chunk `drive_sync` (low risk, no sudo, Linux only in slice 1, after `agents`, requires `packages`).
`make verify CHUNK=drive_sync` checks each account has a token and the last push did not fail (a token Drive refuses
fails the push); with no accounts it
passes as not applicable.

## Slices

1. This branch, Linux: rclone in the packages lists; role `drive_sync` (directories, `drive-sync.conf` remotes from the
   vault client, the `drive-sync` command, a systemd user timer); smoke checks; the README and baseline line; the
   recovery sheet. Molecule: Fedora converge (the role itself never runs rclone); the command's guards in unit
   tests, with the real rclone filters and gitleaks rules.
2. Next: macOS (launchd agent, Homebrew rclone and flock); `make vault-drive-token ACCOUNT=<a>` copies an authorized
   token into the vault (as `make vault-signing-key` does) so other machines are seeded from it and the partner
   consents once; skip pushes on metered networks.
