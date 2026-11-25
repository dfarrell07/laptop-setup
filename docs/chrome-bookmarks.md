# Chrome bookmarks per profile

Each Chrome profile (work, personal) gets its own deliberate bookmark set, kept the same on every machine.

- **Exported** by `make chrome-bookmarks-export` (no sudo, Linux and macOS): it reads each Chrome profile's
  `Bookmarks` and `AccountBookmarks` files and profile names from `Local State` (read-only; Chrome may run) and writes
  `local_bookmarks.yml`, one entry per profile named after it, keeping the newest 5 previous files as
  `local_bookmarks.yml.bak-<time>` (gitignored too). The bookmark bar becomes the top level, other and mobile bookmarks
  become folders. Left out, and only counted: links that are not http or https, links whose query or fragment names a
  credential (token, sig, key, code, auth, session, ...), and the `laptop-setup` folder an earlier import created.
  Folders deeper than 5 levels move their links up. To re-add a link wrongly taken for a secret, see its host with
  `.venv/bin/python3 scripts/chrome-bookmarks-export.py --show-left-out-hosts`.
- **Declared** in `local_bookmarks.yml` at the repo root: gitignored, since bookmarks are private and work ones can name
  internal hosts (copy `local_bookmarks.yml.example`), and outside `group_vars` so Ansible never loads it. Plain data
  an agent can curate; keep it out of shared places, and check a link before adding one that carries a token (the
  export leaves the obvious ones out, but a URL can still hold one under another name).
- **Rendered** by `make chrome-bookmarks` (no sudo, Linux and macOS): it validates the file (http and https only, no
  credentials in URLs, folders at most 5 deep, no repeated keys, no empty profile) and writes
  `~/.cache/laptop-setup/chrome-bookmarks-<profile>.html` (0600), each profile's bookmarks inside one `laptop-setup`
  folder. Files of a profile removed from the YAML stay in that directory until deleted.
- **Backed up** by `make chrome-bookmarks-backup MACHINE=<name>` (no sudo, no key; Chrome closed, or
  `ALLOW_RUNNING=1` and it refuses if a file changes meanwhile): every profile's `Bookmarks` and `AccountBookmarks`
  files verbatim, plus the sets `work` and `personal` built from them with every link (bookmarklets and links carrying
  a token included), keyed by Chrome root, go into `chrome-bookmarks/<name>.tar.age`, encrypted with age to the
  YubiKeys in `chrome-bookmarks/recipients` (at least two). `<name>` is public: use a neutral one such as `old-laptop`,
  not the hostname. Before encrypting it checks that every `url` field of the raw JSON was read as a link, that the
  sets hold exactly the profiles' links (each as often), and that the files read again from disk are unchanged and
  equal to the archive; afterwards, that the archive is encrypted to exactly the listed keys. `<name>.counts.json` holds
  totals only. It never replaces an archive without `FORCE=1`. Work is a profile signed in to redhat.com (or signed out
  and named like redhat) plus links on redhat.com and red.ht hosts from the others; the rest is personal. Commit both
  files: the repository is public, so GitHub sees only ciphertext, its size and the totals.
- **Two keys at least**: one lost YubiKey must not lose the backup. `make chrome-bookmarks-keys` lists every plugged-in
  YubiKey by serial and whether `recipients` has it; `SERIAL=<n>` appends that key (no PIN or touch; any number of keys
  may be plugged in, and only the named one is added, so a borrowed key never slips in). Then back up again (`FORCE=1`).
- **Verified** by `make chrome-bookmarks-verify` (`MACHINE=` when there are several archives): it checks the archive is
  encrypted to exactly the listed keys and matches the SHA-256 in its counts file, then decrypts it once with each
  plugged-in listed YubiKey in turn, naming it by serial before its touch (and its PIN, when its policy asks for one),
  and checks every file and link against the manifest, touching no Chrome file. Plug in every key at once; it says
  which listed keys it could not check. When every listed key opens it, the backup is safe to rely on (for example
  before resetting Chrome Sync).
- **Installed** by `make chrome-bookmarks-install` (Chrome closed; one touch): `work` goes into the one profile signed
  in to a redhat.com account and `personal` into the one signed in to a gmail.com account, whatever the profiles are
  named. Each root's items are merged into that root: folders matched by name (and order among same-named ones) or
  created, a link added unless its folder already holds it as often; links of a signed-out profile go in a folder named
  after it. A second run adds nothing, nothing is removed or renamed, Sync's copy (account storage) is not counted, the
  previous file is kept as `Bookmarks.laptop-setup-bak-<time>`, and afterwards every link is checked in the file.
- **macOS**: Chrome's data folder is protected app data. Until the terminal app that runs these targets (Terminal,
  Ghostty) is allowed in System Settings > Privacy & Security > Full Disk Access, backup and install stop with a
  message saying so; verify needs no Chrome access.
- **Restored by hand**: `.venv/bin/python3 scripts/chrome-bookmarks-install.py --extract DIR` writes the raw files out;
  copy one over a closed profile's `Bookmarks` to get that profile back exactly as it was.
- **Applied by hand** (fallback): `make chrome-bookmarks` renders one HTML file per profile for Settings > Import
  bookmarks and settings > Bookmarks HTML file; delete the previous import first.

The installer writes only the `Bookmarks` file of the matched profiles, while Chrome is closed: tested against Chrome
157, which loads it, gives the new nodes GUIDs and rewrites it with a checksum. It only adds missing links. Nothing
here sets Chrome policy, which applies to every profile on a machine (no local per-profile policy exists); on CSB
machines IT owns `/etc/opt/chrome/policies/managed`, so extensions and settings are left to IT and Chrome Sync.
