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
- **Applied** by the owner, per profile: in that Chrome profile, delete the previous import, then Settings > Import
  bookmarks and settings > Bookmarks HTML file. On an empty bookmarks bar the `laptop-setup` folder lands on the bar;
  otherwise Chrome wraps every import in a new "Imported" folder holding `laptop-setup`, and that wrapper is what to
  delete next time. Chrome Sync carries it to the profile on every other machine, so each change is imported once,
  on one machine.

Nothing here writes Chrome's own profile files or sets Chrome policy; only the export reads them. Policy applies to every profile on a
machine (no local per-profile policy exists), and on CSB machines IT owns `/etc/opt/chrome/policies/managed`
(extensions and block lists), so extensions and settings are left to IT and Chrome Sync. Editing the `Bookmarks` file
would fight Sync, which restores what it held.
