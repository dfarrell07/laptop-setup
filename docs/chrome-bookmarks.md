# Chrome bookmarks per profile

Each Chrome profile (work, personal) gets its own deliberate bookmark set, kept the same on every machine.

- **Declared** in `local_bookmarks.yml` at the repo root: gitignored, since work bookmarks can name internal hosts
  (copy `local_bookmarks.yml.example`), and outside `group_vars` so Ansible never loads it. Plain data an agent can
  curate; not secret, so not in the vault.
- **Rendered** by `make chrome-bookmarks` (no sudo, Linux and macOS): it validates the file (http and https only, no
  credentials in URLs, folders at most 5 deep, no repeated keys, no empty profile) and writes
  `~/.cache/laptop-setup/chrome-bookmarks-<profile>.html` (0600), each profile's bookmarks inside one `laptop-setup`
  folder. Files of a profile removed from the YAML stay in that directory until deleted.
- **Applied** by the owner, per profile: in that Chrome profile, delete the previous import, then Settings > Import
  bookmarks and settings > Bookmarks HTML file. On an empty bookmarks bar the `laptop-setup` folder lands on the bar;
  otherwise Chrome wraps every import in a new "Imported" folder holding `laptop-setup`, and that wrapper is what to
  delete next time. Chrome Sync carries it to the profile on every other machine, so each change is imported once,
  on one machine.

Nothing here reads or writes Chrome's own profile files or sets Chrome policy. Policy applies to every profile on a
machine (no local per-profile policy exists), and on CSB machines IT owns `/etc/opt/chrome/policies/managed`
(extensions and block lists), so extensions and settings are left to IT and Chrome Sync. Editing the `Bookmarks` file
would fight Sync, which restores what it held.
