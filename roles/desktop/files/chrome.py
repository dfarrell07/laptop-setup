#!/usr/bin/env python3
"""Chrome profile settings that no policy can set, and an audit of the declared extensions (docs/chrome.md).

Usage: chrome.py prefs SPEC [--check] | extensions SPEC | policy SPEC | open SPEC

SPEC is the JSON the desktop role renders from its desktop_chrome_* variables (~/.config/laptop-setup/chrome.json), or -
to read it from stdin.

prefs      Sets each profile's Preferences: the default page zoom in every profile; the color scheme, site settings and,
           where this machine's Chrome policy belongs to someone else, the policy-equivalent preferences in personal
           profiles only. Chrome reads Preferences at start and rewrites it from memory, so nothing is written while
           it runs. Prints one word: changed, unchanged, running (quit Chrome and re-run), absent (no profile yet) or
           blocked (the data dir cannot be read: on macOS, Full Disk Access). A second line, personal-unknown, says
           Local State (the profile list) cannot be read, so no profile was treated as personal (the page zoom still
           applies). --check writes nothing.
extensions Lists, per personal profile, declared extensions that are missing or disabled and Web Store extensions
           installed by hand that are not declared. Prints "ok", "absent", "blocked" (the data dir or Local State
           cannot be read), or one problem per line.
policy     Compares this machine's Chrome policy (the Linux recommended/managed files, or the macOS user defaults)
           with SPEC. Prints "ok" or one difference per line.
open       Opens the Web Store page of each missing declared extension in its personal profile (owner use).

A personal profile is one whose signed-in Google account is in personal_domains, is a consumer account
(hosted_domain NO_HOSTED_DOMAIN) and is not managed. Work, unsigned and other profiles get only the page zoom.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EXTENSION_ID = re.compile(r'[a-p]{32}')  # used with fullmatch: no trailing newline
# Content-setting types the site settings may name. All are untracked prefs, so Chrome on macOS keeps a direct edit
# (the file_system_* types are tracked there and would be reset).
SITE_TYPES = {'notifications', 'popups', 'geolocation', 'media_stream_camera', 'media_stream_mic', 'javascript',
              'images', 'sound', 'automatic_downloads', 'clipboard', 'cookies'}
SITE_VALUES = {'allow': 1, 'block': 2, 'ask': 3}
ALLOW_BLOCK_ONLY = {'images', 'javascript', 'popups', 'sound', 'cookies'}  # Chromium registers no "ask" for these
LABEL = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'  # a DNS label: no hyphen at either end
ORIGIN = re.compile(rf'(https?)://({LABEL}(?:\.{LABEL})*)(?::([0-9]{{1,5}}))?/?')  # used with fullmatch
DOMAIN_PATTERN = re.compile(rf'\[\*\.\]({LABEL}(?:\.{LABEL})+)')
COLOR_SCHEMES = {'system': 0, 'light': 1, 'dark': 2}
POLICY_FILE = 'laptop-setup.json'
EXCEPTIONS = ('profile', 'content_settings', 'exceptions')
DEFAULTS = ('profile', 'default_content_setting_values')


def fail(msg):
    sys.exit(f'chrome.py: {msg}')


# --- Policies applied as preferences where machine policy is not ours (a CSB laptop: it would reach the work
# profile too). Each maps a policy value (of the type given) to the preferences it sets; others are not applied. ---
POLICY_PREFS = {
    'PasswordManagerEnabled': (bool, lambda v: [(('credentials_enable_service',), v)]),
    'AutofillCreditCardEnabled': (bool, lambda v: [(('autofill', 'credit_card_enabled'), v)]),
    'AutofillAddressEnabled': (bool, lambda v: [(('autofill', 'profile_enabled'), v)]),
    'BookmarkBarEnabled': (bool, lambda v: [(('bookmark_bar', 'show_on_all_tabs'), v)]),
    # profile.cookie_controls_mode: 0 off, 1 block third-party, 2 in Incognito only
    'BlockThirdPartyCookies': (bool, lambda v: [(('profile', 'cookie_controls_mode'), 1 if v else 0)]),
    'SafeBrowsingProtectionLevel': (int, lambda v: [(('safebrowsing', 'enabled'), v > 0),
                                                   (('safebrowsing', 'enhanced'), v == 2)]),
    # Tracked (reset by Chrome) on macOS, so skipped there; Linux never enforces tracked prefs.
    'RestoreOnStartup': (int, lambda v: [(('session', 'restore_on_startup'), v)]),
}
# The values Chrome defines for the integer policies above
POLICY_VALUES = {'SafeBrowsingProtectionLevel': {0, 1, 2}, 'RestoreOnStartup': {1, 4, 5}}


def site_key(site):
    """A site as the user writes it (https://host[:port] or [*.]domain) -> Chrome's "primary,secondary" key."""
    text = str(site).strip().lower()
    m = ORIGIN.fullmatch(text)
    if m:
        scheme, host, port = m.groups()
        port = int(port) if port else (443 if scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            fail(f'site {site!r}: port out of range')
        return f'{scheme}://{host}:{port},*'
    m = DOMAIN_PATTERN.fullmatch(text)
    if m:
        return f'[*.]{m.group(1)},*'
    fail(f'site {site!r}: write https://host, https://host:port or [*.]domain (internationalized names in punycode)')
    return None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def load_spec(path):
    try:
        spec = json.loads(sys.stdin.read() if path == '-' else Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as err:
        fail(f'cannot read {path}: {err}')
    if not isinstance(spec, dict) or not isinstance(spec.get('user_data_dir'), str):
        fail(f'{path}: not a settings object with a user_data_dir')
    zoom = spec.get('zoom_percent')
    if zoom is not None and not (is_int(zoom) and 25 <= zoom <= 500):
        fail(f'zoom_percent must be a whole number from 25 to 500, not {zoom!r}')
    if spec.get('color_scheme', '') not in ('', *COLOR_SCHEMES):
        fail(f"color_scheme must be one of {', '.join(COLOR_SCHEMES)} or '', not {spec.get('color_scheme')!r}")
    domains = spec.get('personal_domains', [])
    if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
        fail('personal_domains must be a list of domains')
    spec['personal_domains'] = [d.strip().lower() for d in domains]
    exts = spec.get('extensions', [])
    if not isinstance(exts, list) or not all(isinstance(e, dict) and isinstance(e.get('id'), str)
                                             and EXTENSION_ID.fullmatch(e['id'])
                                             and isinstance(e.get('install', True), bool) for e in exts):
        fail('extensions must be a list of {id, name, install} with Chrome Web Store IDs (32 letters a-p) and install'
             ' true or false')
    defaults = spec.get('default_site_settings') or {}
    sites = spec.get('site_settings') or {}
    if not isinstance(defaults, dict) or not isinstance(sites, dict):
        fail('site_settings and default_site_settings must be maps')
    for kind, value in defaults.items():
        if kind not in SITE_TYPES or not isinstance(value, str) or value not in SITE_VALUES:
            fail(f'default_site_settings: {kind}: {value!r}: types are {", ".join(sorted(SITE_TYPES))}; '
                 'values allow, block or ask')
        if value == 'ask' and kind in ALLOW_BLOCK_ONLY:
            fail(f'default_site_settings: {kind} takes allow or block only')
    canonical = {}
    for kind, entries in sites.items():
        if kind not in SITE_TYPES:
            fail(f"site_settings: {kind!r} is not one of {', '.join(sorted(SITE_TYPES))}")
        if not isinstance(entries, dict):
            fail(f'site_settings.{kind} must map sites to allow, block or ask')
        canonical[kind] = {}
        for site, value in entries.items():
            if not isinstance(value, str) or value not in SITE_VALUES:
                fail(f'site_settings.{kind}.{site}: {value!r} is not allow, block or ask')
            if value == 'ask' and kind in ALLOW_BLOCK_ONLY:
                fail(f'site_settings.{kind}: takes allow or block only')
            key = site_key(site)
            if key in canonical[kind] and canonical[kind][key] != value:
                fail(f'site_settings.{kind}: {site!r} is listed twice with different values')
            canonical[kind][key] = value
    spec['site_settings'] = canonical
    spec['default_site_settings'] = defaults
    policies = spec.get('policies') or {}
    if not isinstance(policies, dict):
        fail('policies must be a map')
    for name, (kind, _) in POLICY_PREFS.items():
        if name in policies and not (is_int(policies[name]) if kind is int else isinstance(policies[name], bool)):
            fail(f'policy {name} must be {"an integer" if kind is int else "true or false"}, not {policies[name]!r}')
        if name in policies and name in POLICY_VALUES and policies[name] not in POLICY_VALUES[name]:
            fail(f'policy {name} must be one of {sorted(POLICY_VALUES[name])}, not {policies[name]!r}')
    spec['policies'] = policies
    return spec


# --- Profiles ---
def chrome_running(data_dir):
    # Chrome holds SingletonLock, a symlink to "<host>-<pid>", while it runs (Linux and macOS)
    try:
        target = os.readlink(data_dir / 'SingletonLock')
    except OSError:
        return False
    host, _, pid = target.rpartition('-')
    if host != os.uname().nodename or not pid.isdigit():
        return True  # another host's lock (shared home) or an unknown form: never write under it
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # a stale lock from a crash
    except PermissionError:
        return True
    return True


def profile_dirs(data_dir):
    return sorted(p for p in data_dir.iterdir()
                  if (p.name == 'Default' or p.name.startswith('Profile ')) and (p / 'Preferences').is_file())


def info_cache(data_dir):
    """Local State's profile list, or None when it cannot be read (then no profile is personal; the zoom still
    applies, and the caller reports it)."""
    try:  # called only once profile directories exist, so a missing Local State is unreadable too
        state = json.loads((data_dir / 'Local State').read_text(encoding='utf-8'))
    except (UnicodeDecodeError, ValueError, OSError) as err:
        print(f'chrome.py: cannot read {data_dir / "Local State"} ({type(err).__name__}); treating no profile as personal',
              file=sys.stderr)
        return None
    if not isinstance(state, dict):
        return {}
    profile = state.get('profile')
    cache = profile.get('info_cache') if isinstance(profile, dict) else None
    return cache if isinstance(cache, dict) else {}


def personal_profiles(cache, domains):
    """Profile dir names (from info_cache) whose signed-in account is a consumer account in domains. Anything unknown
    is not personal."""
    found = set()
    for name, info in (cache.items() if isinstance(cache, dict) else []):
        if not isinstance(info, dict):
            continue
        user = str(info.get('user_name', ''))
        domain = user.rpartition('@')[2].lower() if '@' in user else ''
        if (domain in domains and info.get('gaia_id') and info.get('hosted_domain') == 'NO_HOSTED_DOMAIN'
                and not info.get('is_managed')):
            found.add(name)
    return found


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (UnicodeDecodeError, ValueError) as err:
        fail(f'cannot read {path}: {err}')
    if not isinstance(data, dict):
        fail(f'{path} is not a JSON object')
    return data


def get_path(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def set_path(data, path, value):
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


def same(have, want):
    if isinstance(want, float) and isinstance(have, (int, float)) and not isinstance(have, bool):
        return abs(have - want) <= 1e-9
    return type(have) is type(want) and have == want


# --- prefs ---
def wanted_prefs(spec, personal):
    """(path, value) pairs for one profile."""
    writes = []
    zoom = spec.get('zoom_percent')
    if zoom:
        writes.append((('partition', 'default_zoom_level', 'x'), math.log(zoom / 100) / math.log(1.2)))
    if not personal:
        return writes
    if spec.get('color_scheme'):
        writes.append((('browser', 'theme', 'color_scheme2'), COLOR_SCHEMES[spec['color_scheme']]))
    for kind, value in sorted(spec['default_site_settings'].items()):
        writes.append(((*DEFAULTS, kind), SITE_VALUES[value]))
    for kind, sites in sorted(spec['site_settings'].items()):
        for key, value in sorted(sites.items()):
            writes.append(((*EXCEPTIONS, kind, key, 'setting'), SITE_VALUES[value]))
    if not spec.get('policy_is_ours'):
        for name, value in sorted(spec['policies'].items()):
            if name in POLICY_PREFS and not (name == 'RestoreOnStartup' and sys.platform == 'darwin'):
                writes.extend(POLICY_PREFS[name][1](value))
    return writes


def unit(path):
    """The removable unit a write belongs to: a site exception's entry or a default site setting; else None."""
    if tuple(path[:3]) == EXCEPTIONS and len(path) >= 5:
        return tuple(path[:5])
    if tuple(path[:2]) == DEFAULTS and len(path) >= 3:
        return tuple(path[:3])
    return None


def delete_unit(prefs, path):
    parent = get_path(prefs, path[:-1])
    if isinstance(parent, dict) and path[-1] in parent:
        del parent[path[-1]]


def applied_file(spec):
    return Path(spec.get('state_dir') or Path.home() / '.config/laptop-setup') / 'chrome-site-settings.json'


def write_json(path, data, mode, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, separators=(',', ':') if compact else None, indent=None if compact else 2)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def backup(path):
    """The file as it was before the helper's latest change, as Preferences.laptop-setup-bak (same mode)."""
    bak = path.with_name(path.name + '.laptop-setup-bak')
    write_json(bak, json.loads(path.read_text(encoding='utf-8')), path.stat().st_mode & 0o777, compact=True)


def cmd_prefs(spec, check):
    unreadable = []
    status = apply_prefs(spec, check, unreadable)
    return status + ('\npersonal-unknown' if unreadable and status in ('changed', 'unchanged', 'running') else '')


def apply_prefs(spec, check, unreadable):
    """Site exceptions and default site settings this helper wrote are recorded per profile (state_dir), so one
    removed from the settings is removed from Chrome too; ones the user set in Chrome are never recorded."""
    data_dir = Path(spec['user_data_dir']).expanduser()
    try:
        found = profile_dirs(data_dir) if data_dir.is_dir() else []
        if not found:
            return 'absent'
        cache = info_cache(data_dir)
        if cache is None:
            unreadable.append(data_dir / 'Local State')
        personal = personal_profiles(cache or {}, set(spec['personal_domains']))
        state_file = applied_file(spec)
        applied = read_json(state_file)
        plans = []
        for prof in found:
            prefs = read_json(prof / 'Preferences')
            recorded = applied.get(prof.name)  # the record is user-writable: anything malformed is ignored
            before = {unit(u) for u in (recorded if isinstance(recorded, list) else [])
                      if isinstance(u, list) and all(isinstance(x, str) for x in u)} - {None}
            if prof.name not in personal:
                todo = [(p, v) for p, v in wanted_prefs(spec, False) if not same(get_path(prefs, p), v)]
                plans.append((prof, prefs, todo, [], sorted(before)))  # its record waits until it is personal again
                continue
            writes = wanted_prefs(spec, True)
            todo = [(p, v) for p, v in writes if not same(get_path(prefs, p), v)]
            declared = {unit(p) for p, _ in writes} - {None}
            drop = [u for u in sorted(before - declared) if get_path(prefs, u) is not None]
            # A unit is ours only if it did not exist before the helper set it: a site or default the user had set
            # in Chrome may be changed to the declared value, but is never removed when it leaves the settings.
            created = {unit(p) for p, _ in todo if unit(p) and get_path(prefs, unit(p)) is None}
            record = sorted((before & declared) | created)
            plans.append((prof, prefs, todo, drop, record))
    except PermissionError:
        return 'blocked'
    new_state = {prof.name: [list(u) for u in record] for prof, _, _, _, record in plans if record}
    if not any(todo or drop for _, _, todo, drop, _ in plans):
        if new_state != applied and not check:
            write_json(state_file, new_state, 0o600)
        return 'unchanged'
    if chrome_running(data_dir):
        return 'running'
    if check:
        return 'changed'
    written, complete = set(), False
    try:
        for prof, prefs, todo, drop, _ in plans:
            if not (todo or drop):
                written.add(prof.name)
                continue
            if chrome_running(data_dir):  # Chrome started meanwhile: each file written so far is whole; re-run
                return 'running'
            backup(prof / 'Preferences')
            for path, value in todo:
                set_path(prefs, path, value)
            for path in drop:
                delete_unit(prefs, path)
            write_json(prof / 'Preferences', prefs, (prof / 'Preferences').stat().st_mode & 0o777, compact=True)
            written.add(prof.name)
        complete = True
    finally:  # stopped early: record what was written, keep the old record of profiles not reached
        partial = {name: units for name, units in applied.items() if name not in written}
        partial.update({name: units for name, units in new_state.items() if name in written})
        write_json(state_file, new_state if complete else partial, 0o600)
    return 'changed'


# --- extensions ---
def policy_dir(spec):
    return Path(spec.get('policy_dir') or '/etc/opt/chrome/policies')


def blocked_by_policy(spec):
    """A function telling whether machine policy (IT's, on a CSB laptop) blocks an extension ID (Linux)."""
    blocklist, allowlist, settings = set(), set(), {}
    if sys.platform.startswith('linux'):
        for level in ('recommended', 'managed'):  # managed last: it wins
            for f in sorted((policy_dir(spec) / level).glob('*')):
                try:
                    data = json.loads(f.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                blocklist.update(x for x in data.get('ExtensionInstallBlocklist') or [] if isinstance(x, str))
                allowlist.update(x for x in data.get('ExtensionInstallAllowlist') or [] if isinstance(x, str))
                conf_map = data.get('ExtensionSettings')
                for keys, conf in (conf_map.items() if isinstance(conf_map, dict) else []):
                    for key in str(keys).split(','):
                        if isinstance(conf, dict) and conf.get('installation_mode'):
                            settings[key.strip()] = conf['installation_mode']

    def blocked(ext_id):
        if ext_id in settings:
            return settings[ext_id] in ('blocked', 'removed')
        if ext_id in blocklist:
            return True
        if ext_id in allowlist:
            return False
        return '*' in blocklist or settings.get('*') in ('blocked', 'removed')
    return blocked


def installed(prof):
    settings = {}
    for name in ('Preferences', 'Secure Preferences'):  # macOS and Windows keep them in Secure Preferences
        ext = get_path(read_json(prof / name), ('extensions', 'settings'))
        if isinstance(ext, dict):
            for ext_id, conf in ext.items():
                if isinstance(conf, dict):
                    settings.setdefault(ext_id, {}).update(conf)
    return settings


def enabled(conf):
    reasons = conf.get('disable_reasons')
    if isinstance(reasons, list):
        return not reasons
    if isinstance(reasons, int):
        return reasons == 0
    return conf.get('state', 1) == 1


def audit(spec):
    """(status, [(profile, kind, id, name)])"""
    data_dir = Path(spec['user_data_dir']).expanduser()
    try:
        if not data_dir.is_dir() or not profile_dirs(data_dir):
            return 'absent', []
        cache = info_cache(data_dir)
        if cache is None:  # which profiles are personal is unknown, so nothing can be checked
            return 'blocked', []
        personal = personal_profiles(cache, set(spec['personal_domains']))
        exts = spec.get('extensions', [])
        want = {e['id']: e.get('name', '') for e in exts if e.get('install', True)}
        known = {e['id'] for e in exts}
        blocked = blocked_by_policy(spec)
        problems = []
        for name in sorted(personal):
            if cache.get(name, {}).get('is_consented_primary_account'):
                # Sync would carry settings and extensions from other machines and undo these (docs/chrome.md)
                problems.append((name, 'sync-on', '-', 'turn Chrome Sync off (Settings > You and Google)'))
            have = installed(data_dir / name)
            for ext_id, ext_name in sorted(want.items()):
                if blocked(ext_id):
                    continue
                if ext_id not in have:
                    problems.append((name, 'missing', ext_id, ext_name))
                elif not enabled(have[ext_id]):
                    problems.append((name, 'disabled', ext_id, ext_name))
            for ext_id, conf in sorted(have.items()):
                # location 1: installed by hand from the Web Store (policy, external and component ones are not)
                if ext_id not in known and conf.get('location') == 1 and conf.get('from_webstore'):
                    problems.append((name, 'undeclared', ext_id, get_path(conf, ('manifest', 'name')) or ''))
    except PermissionError:
        return 'blocked', []
    return ('ok' if not problems else 'problems'), problems


def cmd_extensions(spec):
    status, problems = audit(spec)
    if status != 'problems':
        return status
    return '\n'.join(f'{prof}: {kind} {ext_id} {name}'.rstrip() for prof, kind, ext_id, name in problems)


def cmd_open(spec):
    status, problems = audit(spec)
    if status in ('absent', 'blocked'):
        return status
    by_profile = {}
    for prof, kind, ext_id, _ in problems:
        if kind == 'missing':
            by_profile.setdefault(prof, []).append(f'https://chromewebstore.google.com/detail/{ext_id}')
    if not by_profile:
        return 'ok'
    display = sys.platform == 'darwin' or os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
    for prof, urls in sorted(by_profile.items()):
        print('\n'.join(f'{prof}: {url}' for url in urls), flush=True)
        if not display:
            continue
        argv = (['open', '-na', 'Google Chrome', '--args'] if sys.platform == 'darwin'
                else [spec.get('chrome_binary') or 'google-chrome'])
        try:  # not waited for: a Chrome started here runs until it is quit
            subprocess.Popen(argv + [f'--profile-directory={prof}', '--ignore-profile-directory-if-not-exists', *urls],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as err:
            print(f'{prof}: cannot start Chrome ({err}); open the pages above in that profile', flush=True)
    return ''


# --- policy ---
def defaults_value(value):
    """How `defaults read` prints a value osx_defaults wrote."""
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def cmd_policy(spec):
    diffs = []
    ours = spec.get('policy_is_ours')
    if sys.platform.startswith('linux'):
        for level, want in (('recommended', spec['policies']), ('managed', spec.get('policies_enforced') or {})):
            path = policy_dir(spec) / level / POLICY_FILE
            want = want if ours else {}
            try:
                have = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            except (OSError, ValueError):
                have = None
            if have != want:
                diffs.append(f'{path} differs from the desktop_chrome_policies_* settings: make apply CHUNK=chrome (with sudo)')
    elif sys.platform == 'darwin':
        want = {k: v for k, v in spec['policies'].items() if k != 'RestoreOnStartup'} if ours else {}
        for key, value in sorted(want.items()):
            out = subprocess.run(['defaults', 'read', 'com.google.Chrome', key], capture_output=True, text=True)
            have = out.stdout.strip() if out.returncode == 0 else None
            if have != defaults_value(value):
                diffs.append(f'com.google.Chrome {key} is {have!r}, not {defaults_value(value)!r}: make apply CHUNK=chrome')
    return '\n'.join(diffs) if diffs else 'ok'


def main(argv):
    if len(argv) < 2 or argv[0] not in ('prefs', 'extensions', 'policy', 'open'):
        sys.exit(__doc__)
    spec = load_spec(argv[1])
    if argv[0] == 'prefs':
        return cmd_prefs(spec, '--check' in argv[2:])
    if argv[0] == 'extensions':
        return cmd_extensions(spec)
    if argv[0] == 'open':
        return cmd_open(spec)
    return cmd_policy(spec)


if __name__ == '__main__':
    RESULT = main(sys.argv[1:])
    if RESULT:
        print(RESULT)
