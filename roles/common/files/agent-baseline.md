# Working agreements

- Carry authorized work through implementation and verification; for a bug fix, first
  reproduce it with a failing test where a test can reasonably capture it. Settle reversible
  choices yourself and say which you chose; ask when the choice is the user's or costly to
  reverse.
- Be concise; lead with the outcome and state material limitations.
- Verify plans against current code and primary sources.
- Keep diffs to the task: no drive-by refactors or reformatting. Mention unrelated problems
  instead of fixing them; remove only what your own change made unused.
- Use each repository's Make targets and required checks, including slow prerequisites.
- Use feature branches by default and commit logical changes as you go with `--signoff`
  (`-s`). Publishing beyond pushes (PRs, comments, releases) needs user authorization; a
  printed deploy command is not permission to run it.
- Push only to remotes the user controls (their own repos and their forks; check that the
  URL's owner is the user), never to a repo someone else owns, even with write access. Push
  only after the user, or instructions they wrote, approve pushing that work; that covers
  later pushes until a PR has been opened from the branch (check before each push, in the
  upstream repo too for a fork), after which pushes need a new approval for that PR, which
  covers its later pushes. With other agent sessions in the repo, push only your own branch,
  warn them before a force-push and use `--force-with-lease`. SSH pushes need a YubiKey
  touch: say so first; a hang or timeout means an untouched key, not an auth failure.
- Preserve existing local work. Keep credentials, private infrastructure details,
  conversation history and personal attribution out of public commits and PR prose.
- Capture durable machine changes in the laptop-setup automation; locate its checkout rather
  than assuming its path.
- Keep reusable skills in their owning repositories. Promote verified lessons into
  instructions or tests, linking instead of repeating; keep project facts and publishing
  exceptions local.
- Before large downloads/builds, check free space; scratch dirs may be tmpfs, so put large
  caches and artifacts on disk.
