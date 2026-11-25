# Chrome configuration

Personal Chrome profiles are configured here, in git, instead of through Chrome Sync: extensions, appearance, startup,
site settings and privacy preferences. `make apply CHUNK=chrome` applies them on Linux and macOS (the `chrome` chunk,
inside the desktop role; `make apply CHUNK=chrome` runs it too), and `make verify CHUNK=chrome` checks them
(`chrome-profiles`, `chrome-policy`; `make smoke-test` adds `chrome-extensions`). Bookmarks are separate:
[chrome-bookmarks.md](chrome-bookmarks.md). The variables are in `roles/desktop/defaults/main.yml` (Chrome section).
Override them in `config.yml`, and put private values (site lists) only there.

## Profiles

Chrome has no per-profile policy: machine policy reaches every profile, a work profile included. So "personal profile"
is decided per profile, from `Local State`: a profile is personal when its signed-in Google account is a consumer
account (`hosted_domain` `NO_HOSTED_DOMAIN`, not managed) in `desktop_chrome_personal_domains` (gmail.com,
googlemail.com) and signed in (`gaia_id`). That is stricter than the bookmarks installer's domain-only match, since
these settings are rewritten on every run: a profile that is signed out or unknown is left alone. Work, signed-out and
other profiles get only the page zoom. The audit output names profile directories, never accounts.

## Three levels

- **Enforced**: mandatory policy, `/etc/opt/chrome/policies/managed/laptop-setup.json` from
  `desktop_chrome_policies_enforced` (empty by default). Nobody can change it in Chrome (greyed out). Linux only, where
  machine policy is ours.
- **Default**: recommended policy from `desktop_chrome_policies_recommended`:
  `/etc/opt/chrome/policies/recommended/laptop-setup.json` on Linux, the user's `com.google.Chrome` defaults on macOS.
  The user can change it in Chrome and the choice persists; a recommended value only applies where the user has not set
  that setting. Where machine policy is ours; it then reaches every profile on that machine, so a work profile there
  gets the same defaults.
- **Reconciled**: each personal profile's `Preferences`, written by `roles/desktop/files/chrome.py` while Chrome is
  closed. The user can change it until the next run sets it back. Every machine.

**Whose policy is it?** Ours only on a personal machine (`profile: personal`) where nobody else writes Chrome policy. On
a work machine policy would reach the work profile, even before IT's files arrive. On a CSB laptop IT owns
`/etc/opt/chrome/policies/managed` (extension settings, a block list, GenAI settings), and two files setting the same
policy are undefined (the last name wins the whole key). So the role writes nothing under `/etc` on a work machine, when
`csb_detected` is true, or when any other file exists under `/etc/opt/chrome/policies` (an enrollment token included;
symlinks, hidden and unreadable entries count against it), and nothing under `/Library` on a Mac that is MDM-enrolled,
enrolled in Chrome Browser Cloud Management or has a configuration profile for Chrome. There, the default-level settings
that have a preference equivalent (`PasswordManagerEnabled`, `AutofillCreditCardEnabled`, `AutofillAddressEnabled`,
`BookmarkBarEnabled`, `BlockThirdPartyCookies`, `SafeBrowsingProtectionLevel`, `RestoreOnStartup` on Linux) are
reconciled into personal profiles instead, so there they are set back on every run; the others (`HttpsOnlyMode`) are not
applied.

**macOS limits.** Without MDM, Chrome reads `defaults` values as recommended policy only, so there is no enforced level,
and it ignores "sensitive" policies such as `RestoreOnStartup` (which is therefore not set; its preference is
tamper-protected on macOS, so the preference is not written there either). Chrome's data folder is protected by macOS
privacy controls: the reconciled settings and the audit need Full Disk Access for the terminal that runs make (System
Settings > Privacy & Security).

**Preferences writes.** Chrome rewrites `Preferences` from memory while it runs, so the helper writes only while Chrome
is closed (no live `SingletonLock`; on macOS quit it with Cmd-Q, closing the windows is not enough), atomically, keeping
the previous file as `Preferences.laptop-setup-bak`, and otherwise warns: quit Chrome and run it again. On macOS only
preferences Chrome does not tamper-protect are written (the `file_system_*` site settings and
`session.restore_on_startup` are, so they are not); Linux does not enforce that protection. The tests run a real
headless Chrome on a throwaway profile to confirm it applies and keeps what the helper writes (page zoom; a JavaScript
block with a per-site exception).

## What is set

"Ours" is a machine whose Chrome policy is ours (the Mac, a personal Linux machine); "CSB" a CSB laptop (see above).
"Before" is what the old laptop's and the Mac's personal profiles had, so the rows marked *change* are new behavior.

| Setting | Value | Ours | CSB | Before | Why |
| --- | --- | --- | --- | --- | --- |
| Password manager | off | default | reconciled | off | Bitwarden holds passwords ([password-migration.md](password-migration.md)) |
| Card autofill | off | default | reconciled | on: *change* | cards live in Bitwarden too (May 2026 hardening notes) |
| Third-party cookies | blocked | default | reconciled | Incognito only: *change* | cross-site tracking; Chrome's default allows them |
| Safe Browsing | enhanced | default | reconciled | enhanced | real-time checks |
| HTTPS-Only mode | balanced | default | not applied | Chrome's default | HTTPS first; internal hostnames exempt |
| On startup | continue where you left off | default (Linux) | reconciled | old laptop: same; Mac: new tab | macOS ignores it without MDM |
| Bookmarks bar | shown | default | reconciled | shown | bookmarks themselves: [chrome-bookmarks.md](chrome-bookmarks.md) |
| Color scheme | dark | reconciled | reconciled | dark | no policy sets it |
| Page zoom | `desktop_chrome_page_zoom` | reconciled, every profile | same | 100% | no policy sets it; fractional display scales |
| Site settings | `config.yml` only | reconciled | reconciled | per site | private |

Not managed:

- **Search engine**: Chrome's default (Google) is what both machines use; `DefaultSearchProvider*` recommended
  policies can be added to `desktop_chrome_policies_recommended_extra` if that changes.
- **Fonts and theme color**: both machines use Chrome's defaults. No policy sets them; the `webkit.webprefs` font
  preferences could be added to the reconciled level when there is something to set.
- **Chrome Sync**: should stay off in personal profiles (Settings > You and Google): Sync would carry settings
  (kept in `account_values`), themes and extensions between machines and fight the ones set here. The
  `chrome-extensions` check reports a personal profile with Sync on (`sync-on`); nothing turns it off.
- **Passwords, cookies, history, sessions**, and anything in a work profile.

## Site settings

`desktop_chrome_site_settings` maps a type to `{site: allow|block|ask}` (images, javascript, popups and sound take allow
or block only); sites are `https://host`, `https://host:port` or `[*.]domain`. `desktop_chrome_default_site_settings`
maps a type to the default for every site. Types: notifications, popups, geolocation, media_stream_camera,
media_stream_mic, javascript, images, sound, automatic_downloads, clipboard, cookies. A site (or default) the role added
is removed from Chrome when it leaves the map; one the user had set in Chrome before is changed to the declared value
but never removed. Example (`config.yml`):

```yaml
desktop_chrome_site_settings:
  notifications: {"https://app.slack.com": allow, "https://calendar.google.com": allow, "https://www.youtube.com": block}
  popups: {"https://bank.example": allow}
```

## Extensions

`desktop_chrome_extensions` (plus `desktop_chrome_extensions_extra` in `config.yml`) lists them by Chrome Web Store ID.
Where machine policy is ours they are installed as external extensions (`/usr/share/google-chrome/extensions` on Linux,
where Chrome adds them to every profile on the machine without asking; `/Library/Application
Support/Google/Chrome/External Extensions` on macOS, where Chrome asks once per profile). The user can remove one, and
it then stays removed in that profile. Dropping an extension from the list deletes its file, and Chrome then uninstalls
it from every profile on the next start, with its local data (uBOL's custom filters, for one). If the machine's policy
stops being ours, the files stay and the run warns. A file a package ships there (Fedora's GNOME Shell integration) is
never written or removed. On a CSB laptop an
external extension would also install into the work profile, so nothing is installed there: `make chrome-extensions`
opens the Web Store page of each missing one in its personal profile to add by hand. Everywhere, the `chrome-extensions`
check reports per personal profile a declared extension that is missing or disabled, and one installed by hand that is
not declared (declare it with `install: false` to allow it without installing it). Extensions that machine policy blocks
(by ID, or by a `*` block list without an allow entry) are not reported missing (Linux; macOS has no policy here). The
check is in `make smoke-test`, not in `make verify CHUNK=chrome`: extensions install at Chrome's next start, or by hand
on a CSB laptop.

Reviewed 2026-11-25 against the old laptop's and this laptop's profiles, the owner's May 2026 browser-hardening notes
and each Chrome Web Store listing. Every extension can read the pages it has access to, and an update can change its
code, so the list stays short and each entry has a reason.

Installed (`desktop_chrome_extensions`):

- **uBlock Origin Lite** `ddkjiahejlhfcafbddmgiahcphecmpfh`: the main content blocker. Manifest V3; it blocks through
  Chrome's declarativeNetRequest without reading page traffic itself and makes no network requests of its own. Full
  uBlock Origin (MV2) no longer runs in Chrome since 139.
- **Privacy Badger** (EFF) `pkehgijcmpdhfbdbbnkijodmdjhbjlgp`: the only way to send Global Privacy Control from Chrome
  (Chrome has none built in, uBOL sends none), plus click-to-load placeholders for embedded widgets. Its tracker lists
  otherwise overlap uBOL's, and it cannot take uBOL's rule capacity; keep its local learning off (the default).
- **Bitwarden** `nngceckbapebfimnlniiiahkandclblb`: the password manager. Keep "auto-fill on page load" off.
- **Claude** (Claude in Chrome) `fcoeoabgfenejglbffodgkkbkcdhcgfn`: agentic browser use, also from Claude Code
  (`claude --chrome`). It asks per site; sign it in with the personal account. Prompt injection from web pages remains
  a risk, so keep sensitive sites (banking) out of its allowed sites.
- **ChatGPT** (OpenAI) `hehggadaopoacecdllhhajmbjkdcmajg`: the owner's choice, for ChatGPT and Codex browser control. It
  has debugger, native-messaging and all-sites access, like Claude's.

Not declared:

- **LastPass** `hdokiejnpimakedhajhdlcegeplioahd`: replaced by Bitwarden; the audit flags it while it is installed.
- **Vimium** `dbepggeogbaibhgnhhndojpepiihcmeb`: the owner's choice; not used today, and another all-sites extension.
- **Google Docs Offline** `ghbmnnjooekpmoecnnnilnnbdlolhkhi`: Chrome installs it itself (external), so it is not
audited.
- **GNOME Shell integration** `gphhapmejobijbbhgpjhcjognlahblep`: installed by Fedora's `fedora-chromium-config-gnome`
  package; it only talks to extensions.gnome.org. Left alone (the package would add it back).
- **Mullvad**: its browser extension is for Firefox and Mullvad Browser only. With the Mullvad app connected, check
  leaks at <https://mullvad.net/check>.
- **Playwright extension, Browser MCP, Chrome DevTools MCP**: they give an agent the whole logged-in profile,
  duplicating Claude in Chrome; use them only with a throwaway profile.
- **Dark Reader, Consent-O-Matic, ClearURLs**: all-sites access for what Chrome's dark scheme and uBOL's annoyance and
  URL-tracking lists already cover (Consent-O-Matic has not been updated since 2024).

Sources: the Chrome Web Store listings; [MV2 deprecation
timeline](https://developer.chrome.com/docs/extensions/develop/migrate/mv2-deprecation-timeline); [uBOL
FAQ](https://github.com/uBlockOrigin/uBOL-home/wiki/Frequently-asked-questions-(FAQ)); [Privacy Badger
FAQ](https://privacybadger.org/); [Claude in
Chrome](https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome); [Mullvad browser
extension](https://mullvad.net/en/help/mullvad-browser-extension). Chrome policy facts: [Linux quick
start](https://www.chromium.org/administrators/linux-quick-start/), [Mac quick
start](https://www.chromium.org/administrators/mac-quick-start/), [policy
precedence](https://support.google.com/chrome/a/answer/9037717), [external
extensions](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions), and the policy
definitions under `components/policy/resources/templates/policy_definitions` in the Chromium source.

## Changing it

Edit the variables (public choices in `roles/desktop/defaults/main.yml` with their reason here, private ones in
`config.yml`), quit Chrome, run `make apply CHUNK=chrome` (the `/etc` and `/Library` files need sudo; `NOSUDO=1`
skips them), then `make verify CHUNK=chrome`. `chrome://policy` lists the policies Chrome loaded, with their
level. Turn it all off with `desktop_chrome_manage: false`; undo steps are in
[rollout-recovery.md](rollout-recovery.md).
