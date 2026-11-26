#!/usr/bin/env python3
"""Parse raw config YAML, including !unsafe tags, without evaluating templates.

With --policy, print the config.yml keys and characters that would change how the play runs
(key paths and code points only, never values) and exit 3 when there are any.

With --vars-files [--mode provision|molecule|check] [--remove-bytecode] [--inventory PATH ...], check the checkout
against HEAD before the make wrapper starts Ansible (see checkout_violations): prints file paths and key names
(never values), exits 3 when anything is refused, and 4 (provision mode) with the tracked files that differ
from HEAD, which the wrapper asks to confirm on a terminal. The wrapper runs it with -I -S: no .pth file in
.venv runs first (one calling os._exit(0) would end the check with success), and site_violations() refuses
.pth and sitecustomize files no installed distribution's RECORD lists, since ansible-playbook does run them.
"""
import base64
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import site  # with -S only the module: site.main(), which runs .pth files, is not called


def _site_packages():
    return site.getsitepackages([sys.prefix])  # .venv's, or the system's (/usr/lib64/...) without one


if sys.flags.no_site:  # -S: site-packages on the path for yaml, without running any .pth file in them
    sys.path.extend(path for path in _site_packages() if path not in sys.path)

import yaml  # noqa: E402 pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parent.parent

# Magic variables a config.yml key could shadow. include_vars overrides some of them (omit turns
# every `default(omit)` into a real argument, e.g. a file mode; inventory_hostname, group_names,
# inventory_dir); the rest are ignored today but are never configuration.
MAGIC_KEYS = frozenset({
    'omit', 'hostvars', 'groups', 'group_names', 'inventory_hostname', 'inventory_hostname_short',
    'inventory_dir', 'inventory_file', 'playbook_dir', 'play_hosts', 'role_name', 'role_names',
    'role_path', 'environment',
    # vars is reserved, item the default loop variable, cacheable a set_fact option
    'vars', 'item', 'cacheable',
    # Jinja and Ansible template globals: a key named lookup makes every later lookup() call fail
    'cycler', 'dict', 'joiner', 'lipsum', 'lookup', 'namespace', 'now', 'q', 'query', 'range', 'undef', 'zip',
})
# Facts Play 0 sets before pre-flight loads config.yml (pre-flight.yml). config.yml values are set
# with set_fact, so they would overwrite these; include_vars used to lose to them.
FACTS_SET_BEFORE_CONFIG = frozenset({'real_user_home', 'real_user_id', 'is_linux', 'is_macos'})
# Facts common/tasks/csb_detect.yml derives on localhost after the load, which Play 2's add_host hands
# to the dev container (proxy_url): on the container host a config.yml value would win over them.
DERIVED_FACTS = frozenset({
    'csb_detected', 'fapolicyd_enforcing', 'fapolicyd_installed', 'internal_ca_present', 'is_apt', 'is_dnf',
    'is_dnf5', 'is_fedora', 'is_rhel', 'needs_container_tier', 'network_proxied', 'proxy_url',
})
# The wrapper protects rollout_enable from -e only; config.yml is where it is set.
CONFIG_SETTABLE_PROTECTED = frozenset({'rollout_enable'})
# Executables and install locations the roles run from a fixed place (SECURITY.md, "Executable paths"):
# config.yml cannot point them elsewhere. transcrypt receives the notes vault password, codex the Codex API key.
FIXED_EXECUTABLE_KEYS = frozenset({
    'notes_transcrypt_bin_path', 'notes_transcrypt_linux_bin_path', 'containers_podman_bin',
    'containers_macos_machine_ensure_script', 'claude_bin_path', 'claude_queue_poller_path', 'codex_npm_prefix',
    'antigravity_bin_dir',
})
# Executable locations config.yml may choose, from an allowlist: the Homebrew prefix (PATH of launch agents,
# the git credential helper's gh) and the polkit agent sway/i3 start (it receives the user's password).
HOMEBREW_PREFIXES = frozenset({'/opt/homebrew', '/usr/local'})
# A system binary: /usr/bin/<name>, or any depth under /usr/libexec, /usr/lib or /usr/lib64 (Plasma 6's
# /usr/libexec/kf6/polkit-kde-authentication-agent-1). No spaces (sway's exec takes a command line), no . or ..
SYSTEM_BINARY = re.compile(r'/usr/(bin|libexec(/[A-Za-z0-9_.+-]+)*|lib(64)?(/[A-Za-z0-9_.+-]+)*)/[A-Za-z0-9_.+-]+')


def _system_binary(value):
    return bool(SYSTEM_BINARY.fullmatch(value)) and not set(value.split('/')) & {'.', '..'}


LOCALE = re.compile(r'[A-Za-z]{2,3}(_[A-Z]{2})?(\.[A-Za-z0-9-]+)?(@[A-Za-z0-9]+)?|C|C\.UTF-8|C\.utf8|POSIX')
PLATFORM_PROFILE_CHOICES = ('low-power', 'cool', 'quiet', 'balanced', 'balanced-performance', 'performance', 'custom')
ENERGY_PERF_POLICIES = ('default', 'performance', 'balance_performance', 'balance_power', 'power')
# acli logs in to acli_site with the vault's Atlassian token: an Atlassian Cloud site, or '' to turn it off.
ATLASSIAN_SITE = re.compile(r'[a-z0-9][a-z0-9-]*\.atlassian\.net')
ALLOWED_VALUES = {
    'acli_site': lambda value: value == '' or bool(ATLASSIAN_SITE.fullmatch(value)),
    # rendered unquoted into the macOS shell start files (zshenv, bashrc): a Homebrew formula name
    'dotfiles_macos_python': lambda value: bool(re.fullmatch(r'[a-z0-9@.+-]+', value)),
    # /etc/locale.conf (evaluated by /etc/profile.d/lang.sh in root login shells) and /etc/vconsole.conf
    'system_locale': lambda value: bool(LOCALE.fullmatch(value)),
    'system_keymap': lambda value: bool(re.fullmatch(r'[a-zA-Z0-9_-]+', value)),
    # /etc/tlp.d, read by TLP as root (roles/system/tasks/power_management.yml has the same lists)
    'system_tlp_platform_profile_ac': lambda value: value in PLATFORM_PROFILE_CHOICES,
    'system_tlp_platform_profile_bat': lambda value: value in PLATFORM_PROFILE_CHOICES,
    'system_tlp_cpu_energy_policy_ac': lambda value: value in ENERGY_PERF_POLICIES,
    'system_tlp_cpu_energy_policy_bat': lambda value: value in ENERGY_PERF_POLICIES,
}
ALLOWED_EXECUTABLE_VALUES = {
    'dotfiles_homebrew_prefix': lambda value: value in HOMEBREW_PREFIXES,
    'desktop_sway_polkit_agent': _system_binary,
    'desktop_i3_polkit_agent': _system_binary,
}
# Sysctls whose value the kernel runs as root or treats as a path (usermode helpers, the core dump pipe, binfmt
# handlers) and the helpers' capability masks. A leading '-' only tells systemd-sysctl to ignore errors.
# roles/system/tasks/main.yml refuses the same pattern at the point of use; a test keeps the two equal.
SYSCTL_EXEC_KEYS = re.compile(
    r'-?(kernel\.(modprobe|poweroff_cmd|reboot_cmd|core_pattern|hotplug|usermodehelper(\..*)?)|fs\.binfmt_misc(\..*)?)')
SYSCTL_DICT_KEYS = frozenset({'system_sysctl_extra', 'system_sysctl_extra_additional'})
# A well-formed sysctl key: dot-separated lowercase segments, none empty (systemd-sysctl and procps both reduce
# kernel..modprobe or .kernel.modprobe to kernel.modprobe). The same pattern as the role's _sysctl_key_re.
SYSCTL_KEY = re.compile(r'-?[a-z0-9_-]+(\.[a-z0-9_-]+)*')


def sysctl_name(key):
    """The sysctl a key writes: no leading '-', '/' as '.', repeated dots collapsed, no leading or trailing dot."""
    return re.sub(r'\.+', '.', key.lstrip('-').replace('/', '.')).strip('.')
# C0 controls (tab and newline included: no option takes them), DEL, C1 controls and the Unicode
# line and paragraph separators. A NUL reaches files such as /etc/sysctl.d, where systemd treats it
# as a line break.
CONTROL_CHARS = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]')
JINJA_EXPRESSION = re.compile(r'\{\{.*?\}\}', re.DOTALL)
VAULT_NAME = re.compile(r'\bvault_[A-Za-z0-9_]+')


def json_default(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError("config.yml contains a value that cannot be represented as JSON")


def load(content):
    # DataLoader.load parses YAML; unlike include_vars it does not grant trust
    # to template expressions. JSON serialization never invokes the templar.
    # Imported here: --vars-files runs in the make wrapper, where Ansible may not be importable.
    from ansible.parsing.dataloader import DataLoader  # pylint: disable=import-outside-toplevel
    data = DataLoader().load(content, show_content=False)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError("config.yml must contain a mapping")
    return data


def parse_config(content):
    return json.dumps(load(content), default=json_default)


class PolicySourceError(Exception):
    """A source of the policy lists cannot be read; the message names a file, never its content."""


class _VarsLoader(yaml.SafeLoader):
    """SafeLoader that accepts the tags Ansible allows in vars files: !unsafe as plain data, !vault as opaque."""


def _construct_unsafe(loader, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_VarsLoader.add_constructor('!unsafe', _construct_unsafe)
_VarsLoader.add_constructor('!vault', lambda loader, node: '')


def protected_keys_pattern(root=ROOT):
    """The -e protected-key regex of scripts/verify-ansible-args.sh, the single list of protected keys."""
    text = (root / 'scripts/verify-ansible-args.sh').read_text()
    match = re.search(r"^_protected_keys='([^']+)'$", text, re.MULTILINE)
    if not match:
        raise PolicySourceError('_protected_keys not found in verify-ansible-args.sh')
    return re.compile(match.group(1))


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def vault_mappings(root=ROOT):
    """{variable: vault_* names its Jinja value reads}: a config.yml key of that name shadows the vault."""
    files = [p for p in sorted((root / 'group_vars/all').glob('*.yml')) if p.name != 'vault.yml']
    files += sorted(root.glob('roles/*/defaults/main.yml')) + sorted(root.glob('roles/*/vars/main.yml'))
    mapped = {}
    for path in files:
        try:
            data = yaml.load(path.read_text(), Loader=_VarsLoader)  # noqa: S506 - SafeLoader subclass
        except Exception as error:  # the message would quote the offending line
            raise PolicySourceError('%s in %s' % (type(error).__name__, path.name)) from None
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            names = {name for text in _strings(value) for expression in JINJA_EXPRESSION.findall(text)
                     for name in VAULT_NAME.findall(expression)}
            if names and not str(key).startswith('vault_'):
                mapped[str(key)] = names
    if not mapped:
        raise PolicySourceError('no vault_* mappings found under group_vars/all or roles/*/defaults')
    return mapped


def _control_chars(value, path):
    if isinstance(value, dict):
        for key, item in value.items():
            here = path + [str(key)]
            yield from _control_chars(str(key), here)
            yield from _control_chars(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _control_chars(item, path + [str(index)])
    elif isinstance(value, str):
        for char in sorted(set(CONTROL_CHARS.findall(value))):
            yield '%s: control character U+%04X' % (ascii('.'.join(path)), ord(char))


def policy_violations(data, root=ROOT):
    protected = protected_keys_pattern(root)
    vault_mapped = vault_mappings(root)
    found = []
    for key in map(str, data):
        if key.lower().startswith('ansible_'):
            found.append('%s: ansible_* connection and behaviour variables cannot be set in config.yml' % ascii(key))
        elif key.startswith('_'):
            found.append('%s: internal (underscore) variables cannot be set in config.yml' % ascii(key))
        elif key in MAGIC_KEYS:
            found.append('%s: Ansible magic variable' % ascii(key))
        elif key in FACTS_SET_BEFORE_CONFIG or key in DERIVED_FACTS:
            found.append('%s: derived at run time, not configuration' % ascii(key))
        elif protected.fullmatch(key) and key not in CONFIG_SETTABLE_PROTECTED:
            found.append('%s: protected variable (_protected_keys in scripts/verify-ansible-args.sh)' % ascii(key))
        elif key in vault_mapped:
            found.append('%s: comes from the vault; set it in group_vars/all/vault.yml (make vault-edit)' % ascii(key))
        elif key in FIXED_EXECUTABLE_KEYS:
            found.append('%s: an executable the roles run from a fixed location' % ascii(key))
        elif key in ALLOWED_EXECUTABLE_VALUES and not (
                isinstance(data[key], str) and ALLOWED_EXECUTABLE_VALUES[key](data[key])):
            found.append('%s: not an allowed executable location (SECURITY.md, "Executable paths")' % ascii(key))
        elif key in ALLOWED_VALUES and not (isinstance(data[key], str) and ALLOWED_VALUES[key](data[key])):
            found.append('%s: not an allowed value (see its default.config.yml comment and SECURITY.md)' % ascii(key))
        elif key in SYSCTL_DICT_KEYS and isinstance(data[key], dict):
            for name in map(str, data[key]):
                if not SYSCTL_KEY.fullmatch(name):
                    found.append('%s.%s: not a well-formed sysctl key' % (ascii(key), ascii(name)))
                elif SYSCTL_EXEC_KEYS.fullmatch(sysctl_name(name)):
                    found.append('%s.%s: a sysctl the kernel runs or reads as a path' % (ascii(key), ascii(name)))
    found += _control_chars(data, [])
    return found


# --vars-files: a tripwire for files in the checkout that git does not account for, which Ansible would load or
# run before any task could check them: group_vars/, host_vars/ and inventory files outrank ansible.cfg and the
# wrapper's pins; role vars/, defaults/ and meta/ dependencies, task, template and file search paths, and the
# *_plugins/, library/ and module_utils/ dirs beside any playbook or role are read whatever ANSIBLE_*_PLUGINS
# says. Rather than model every search path, git's own walk of the whole tree is checked against HEAD. It is a
# tripwire for stray or planted files, not an anchor against an attacker running as this user, who could edit
# .git or .venv directly (SECURITY.md).
LOCAL_REPOS = 'group_vars/all/local_repos.yml'
LOCAL_REPOS_KEYS = frozenset({'git_repos_local'})  # local_repos.yml.example documents no other key
VAULT = 'group_vars/all/vault.yml'  # tracked, skip-worktree once encrypted (make vault-init)
# Untracked or gitignored paths a normal checkout has: configuration (config.yml, CLAUDE.local.md, the Chrome
# bookmark list), local_repos.yml (checked below), the side files the vault scripts leave beside vault.yml and
# vault-pass.sh, and generated or scratch directories: .venv/ (Ansible's own interpreter), collections/
# (verified by verify-collections.sh), node_modules/, galaxy_cache/, the gitignored tmp/ and agent state in .claude/.
UNTRACKED_ALLOWED = re.compile(r'''(?x)
    config\.yml | CLAUDE\.local\.md
  | local_bookmarks\.yml | local_bookmarks\.yml\.bak-[^/]+ | \.local_bookmarks\.yml\.[^/]+
  | group_vars/all/(?: local_repos\.yml | vault\.yml\.pre-shared | vault\.yml\.shared\.tmp
                      | vault\.yml\.import\.[A-Za-z0-9]{6} )
  | scripts/vault-pass\.sh (?: \.sha256 | \.pre-shared[^/]* | \.new\.[^/]+ | \.[A-Za-z0-9]{6} )?
  | (?: \.venv | node_modules | collections | galaxy_cache | tmp | \.claude ) (?: /.* )?
''')
PYCACHE = re.compile(r'((?:[^/]+/)*?__pycache__)(?:/.*)?')
# Directory names Ansible searches; an untracked case variant is also what macOS would load under that name.
SEARCHED_NAMES = frozenset({'group_vars', 'host_vars', 'inventory', 'roles', 'library', 'module_utils', 'plugins',
                            'files', 'templates', 'vars', 'defaults', 'meta', 'tasks', 'handlers', 'collections'})
BYTECODE = ('.pyc', '.pyo')
TEMPLATE_MARKERS = ('{' + '{', '{' + '%', '#' + 'jinja2')  # the config_ssti_guard.yml scan


# Every git call (_git_runner, the only place this file runs git) sees the checkout as it is, whatever .git/config
# says: full stat checks (core.trustctime=false plus core.checkStat=minimal let an edit that keeps size and mtime pass
# as unchanged), no fsmonitor or untracked cache to answer for git's own walk, and exact names (core.ignorecase=true
# made a planted Vars.yml beside the tracked vars.yml count as that file; core.precomposeunicode does the same for
# Unicode forms). core.protectHFS and core.protectNTFS only guard what checkout writes, so they do not matter here.
GIT_OVERRIDES = ('core.fsmonitor=false', 'core.untrackedCache=false', 'core.trustctime=true', 'core.checkStat=default',
                 'core.ignoreStat=false', 'core.ignorecase=false', 'core.precomposeunicode=false')


def _git_runner(root):
    """A git command runner for the checkout at root; fails closed unless root is the top of a checkout."""
    git = shutil.which('git')
    if git is None:
        raise PolicySourceError('git is not installed')
    base = [git, '-C', str(root)] + [arg for setting in GIT_OVERRIDES for arg in ('-c', setting)]
    # GIT_NO_REPLACE_OBJECTS: a refs/replace/ entry for HEAD would make ls-tree show another commit's tree.
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CEILING_DIRECTORIES'):
        env.pop(name, None)
    top = subprocess.run(base + ['rev-parse', '--show-toplevel'], capture_output=True, text=True, env=env, check=False)
    if top.returncode != 0:
        detail = (top.stderr.strip().splitlines() or ['git rev-parse failed'])[-1]
        raise PolicySourceError('%s is not a usable git checkout (%s)' % (root, detail))
    if Path(top.stdout.strip()).resolve() != Path(root).resolve():
        raise PolicySourceError('%s is not the top of a git checkout (git found %s)' % (root, top.stdout.strip()))

    def run(*args, status=False):
        result = subprocess.run(base + list(args), capture_output=True, env=env, check=False)
        if status:
            return result.returncode
        if result.returncode != 0:
            raise PolicySourceError('git %s failed in %s' % (args[0], root))
        return result.stdout
    return run


def _outside_inventories(root, inventories):
    """Inventory sources outside the checkout (those inside are covered by the whole-tree walk)."""
    outside = []
    for source in inventories:
        path = Path(source)
        if not path.is_absolute():
            path = root / path
        if not os.path.lexists(path):
            continue  # a host list such as 'localhost,', or a missing file Ansible reports itself
        if root not in path.resolve().parents:  # the checkout itself as inventory would parse every file in it
            outside.append(str(path))
    return outside


def _paths(output):
    return [os.fsdecode(path) for path in output.split(b'\0') if path]


def local_repos_violations(data):
    """local_repos.yml: only git_repos_local, and the config.yml template and control-character checks."""
    if not isinstance(data, dict):
        return ['%s: must be a mapping with the single key git_repos_local' % ascii(LOCAL_REPOS)]
    found = ['%s: %s: only git_repos_local may be set here (configuration belongs in config.yml)'
             % (ascii(LOCAL_REPOS), ascii(str(key))) for key in data if str(key) not in LOCAL_REPOS_KEYS]
    serialized = json.dumps(data, default=json_default)
    found += ['%s: template marker %s (Ansible would evaluate it; mark nothing here as a template)'
              % (ascii(LOCAL_REPOS), ascii(marker)) for marker in TEMPLATE_MARKERS if marker in serialized]
    found += ['%s: %s' % (ascii(LOCAL_REPOS), line) for line in _control_chars(data, [])]
    found += _local_repos_dest_violations(data.get('git_repos_local', []))
    return found


def _local_repos_dest_violations(repos):
    """Each repo clones to git_repos_base_dir/<dest>: dest must stay below it (no .., not absolute, not empty)."""
    if not isinstance(repos, list):
        return ['%s: git_repos_local must be a list of repos' % ascii(LOCAL_REPOS)]
    found = []
    for index, repo in enumerate(repos):
        dest = repo.get('dest') if isinstance(repo, dict) else None
        parts = PurePosixPath(dest).parts if isinstance(dest, str) else ()
        if not isinstance(dest, str) or not dest.strip() or dest.startswith(('/', '~')) or '\\' in dest \
                or '..' in parts or parts in ((), ('.',)):
            found.append('%s: git_repos_local.%d.dest: must be a relative path below the repos directory (no .., '
                         'not absolute)' % (ascii(LOCAL_REPOS), index))
    return found


def _load_local_repos(path):
    raw = path.read_bytes()
    if raw.lstrip().startswith(b'$ANSIBLE_VAULT'):
        return None, ['%s: vault-encrypted; keep it plaintext (it is gitignored) so it can be checked' % ascii(LOCAL_REPOS)]
    try:
        data = yaml.load(raw.decode('utf-8'), Loader=_VarsLoader)  # noqa: S506 - SafeLoader subclass
    except Exception as error:  # the message would quote the offending line
        return None, ['%s: cannot be parsed (%s)' % (ascii(LOCAL_REPOS), type(error).__name__)]
    return ({} if data is None else data), []


def _remove_bytecode_cache(root, directory):
    """Delete one __pycache__ directory; a symlink, or one reached through a symlink, is refused instead."""
    path = root / directory
    if path.is_symlink():
        return ['%s: __pycache__ is a symlink; remove it (Python would load byte code from its target)' % ascii(directory)]
    if path.parent.resolve() != path.parent or not path.is_dir():
        return ['%s: not a plain directory inside the checkout; remove it' % ascii(directory)]
    try:
        shutil.rmtree(path)  # fd-based on Linux (rmtree.avoids_symlink_attacks): never follows a link inside
    except OSError as error:
        return ['%s: could not be removed (%s); remove it' % (ascii(directory), type(error).__name__)]
    return []


def _head_blobs(git):
    """{path: (mode, object id)} for every file in HEAD."""
    blobs = {}
    for entry in _paths(git('ls-tree', '-r', '-z', '--full-tree', 'HEAD')):
        meta, name = entry.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind == 'blob':
            blobs[name] = (mode, oid)
    return blobs


VAULT_HEADER = re.compile(rb'\$ANSIBLE_VAULT;1\.[12];AES256(?:;[^\s;]+)?')
VAULT_BODY_LINE = re.compile(rb'[0-9a-fA-F]+')


def _blob_id(algorithm, data):
    return hashlib.new(algorithm, b'blob %d\0' % len(data) + data).hexdigest()


def _vault_violations(root, head_blobs, algorithm):
    """vault.yml is the only tracked file whose content may differ from HEAD, and only as one whole Ansible Vault
    envelope: plaintext appended to it (ansible_become_exe, ansible_shell_executable) would load like any group_vars."""
    path = root / VAULT
    if VAULT not in head_blobs:
        return []
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return ['%s: not a regular file; restore it with git checkout -- %s' % (ascii(VAULT), VAULT)]
        data = path.read_bytes()
    except OSError:
        return []  # a missing vault.yml is the pre-flight's to report
    if _blob_id(algorithm, data) == head_blobs[VAULT][1]:
        return []  # the plaintext stub as committed
    text = data[3:] if data.startswith(b'\xef\xbb\xbf') else data
    lines = [line.rstrip() for line in text.lstrip().splitlines()]
    while lines and not lines[-1]:
        lines.pop()  # trailing blank lines; a blank line inside the body is refused below
    if len(lines) > 1 and VAULT_HEADER.fullmatch(lines[0]) and all(VAULT_BODY_LINE.fullmatch(line) for line in lines[1:]):
        return []
    return ['%s: neither the committed stub nor a whole Ansible Vault file (plaintext beside or instead of the '
            'encrypted vault would load like any group_vars); restore it with make vault-edit or git checkout'
            % ascii(VAULT)]


def _differs_from_head(root, git, head_blobs):
    """Tracked paths whose worktree content differs from HEAD, vault.yml aside. Each file is hashed as a git blob
    rather than trusting the index's stat cache: .git/config settings (core.trustctime, core.checkStat), an edit
    within the same second as the last index refresh, skip-worktree bits or a clean filter could each make
    git diff report an edited file as unchanged."""
    algorithm = git('rev-parse', '--show-object-format').decode().strip() or 'sha1'
    differs = []
    for name, (mode, oid) in sorted(head_blobs.items()):
        if name == VAULT:
            continue
        path = root / name
        try:
            data = os.fsencode(os.readlink(path)) if mode == '120000' else (
                path.read_bytes() if stat.S_ISREG(os.lstat(path).st_mode) else None)
        except OSError:
            data = None  # deleted, or unreadable
        if data is None:
            differs.append(name)
            continue
        digest = hashlib.new(algorithm, b'blob %d\0' % len(data) + data).hexdigest()
        executable = mode != '120000' and bool(os.lstat(path).st_mode & 0o100)
        if digest != oid or (mode in ('100644', '100755') and executable != (mode == '100755')):
            differs.append(name)
    return differs


def _toolbox_lock_only(root, name):
    """toolbox/migrate.lock, an empty file Fedora's toolbox writes relative to the working directory when
    XDG_RUNTIME_DIR is unset, and nothing else under toolbox/ (git reports the directory as one entry)."""
    if name not in ('toolbox', 'toolbox/migrate.lock'):
        return False
    directory = root / 'toolbox'
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode) or os.listdir(directory) != ['migrate.lock']:
            return False
        lock = os.lstat(directory / 'migrate.lock')
    except OSError:
        return False
    return stat.S_ISREG(lock.st_mode) and lock.st_size == 0


def _untracked_message(name, ignored):
    case = [part for part in name.split('/') if part != part.lower()
            and (part.lower() in SEARCHED_NAMES or part.lower().endswith('_plugins'))]
    note = ' (%s is a case variant of a name Ansible searches)' % ascii(case[0]) if case else ''
    if name.endswith(BYTECODE):
        return '%s: byte code Python would import as a module; remove it' % ascii(name)
    if ignored:
        return '%s: gitignored, so git status hides it; Ansible could load or run it%s' % (ascii(name), note)
    return '%s: not tracked by git; Ansible could load or run it%s' % (ascii(name), note)


def _case_duplicates(other, tracked):
    """Untracked paths that differ from a tracked file or directory only in case: on a case-insensitive filesystem
    (macOS) or with core.ignorecase they pass for it, and Ansible loads both where names are distinct."""
    folded = {}
    for name in tracked:
        parts = name.split('/')
        for end in range(1, len(parts) + 1):
            folded.setdefault('/'.join(parts[:end]).casefold(), '/'.join(parts[:end]))
    found = []
    for name, _ in other:
        match = folded.get(name.casefold())
        if match is not None and match != name:
            found.append('%s: differs only in case from the tracked %s; remove it' % (ascii(name), ascii(match)))
    return found


def _is_plugin_dir(name):
    lowered = name.lower()
    return lowered.endswith('_plugins') or lowered in ('library', 'module_utils')


def _holds_plugin_code(root, name):
    """An untracked path in, or a collapsed untracked directory holding, a *_plugins/, library/ or module_utils/ dir."""
    if any(_is_plugin_dir(part) for part in name.split('/')):
        return True
    path = root / name
    if path.is_symlink() or not path.is_dir():
        return False
    return any(_is_plugin_dir(part) for _, dirs, _ in os.walk(path) for part in dirs)


def _replaced_history(root, git):
    """refs/replace/ entries and info/grafts make git show other commits than HEAD's own: refused, as they would make
    planted files look committed (the checker also sets GIT_NO_REPLACE_OBJECTS)."""
    found = ['%s: a git replace ref, which would show another commit in place of the real one; remove it with '
             'git replace -d' % ascii(ref)
             for ref in git('for-each-ref', '--format=%(refname)', 'refs/replace/').decode().splitlines() if ref]
    common = Path(git('rev-parse', '--git-common-dir').decode().strip())
    grafts = (common if common.is_absolute() else root / common) / 'info' / 'grafts'
    if os.path.lexists(grafts):
        found.append('%s: git grafts rewrite history; remove the file' % ascii(str(grafts)))
    return found


def site_violations(directories=None):
    """.pth, sitecustomize.py and usercustomize.py files in .venv's site-packages that no installed distribution's
    RECORD lists with their hash: Python runs them at startup, so ansible-playbook would. Only a virtualenv is
    checked: the system site-packages is root-owned. This does not make .venv trustworthy (SECURITY.md)."""
    if directories is None:  # the checkout's .venv, and this interpreter's if it is a venv inside the checkout
        directories = [str(path) for path in ROOT.glob('.venv/lib*/python3*/site-packages')]
        # A venv outside the checkout is the user's own install, already trusted for ansible-playbook itself: on macOS
        # make runs Homebrew's ansible, whose packages carry no RECORD, so each of its .pth files would be refused.
        root = os.path.realpath(ROOT)
        if sys.prefix != sys.base_prefix and os.path.commonpath([root, os.path.realpath(sys.prefix)]) == root:
            directories += _site_packages()
    found = []
    for directory in sorted({os.path.realpath(path) for path in directories if os.path.isdir(path)}):
        recorded = {}
        for record in sorted(Path(directory).glob('*.dist-info/RECORD')):
            with open(record, newline='', encoding='utf-8', errors='replace') as stream:
                for row in csv.reader(stream):
                    if len(row) >= 2:
                        recorded[os.path.normpath(os.path.join(directory, row[0]))] = row[1]
        for name in sorted(os.listdir(directory)):
            if not (name.endswith('.pth') or name in ('sitecustomize.py', 'usercustomize.py')):
                continue
            path = os.path.join(directory, name)
            algorithm, _, expected = recorded.get(path, '').partition('=')
            try:
                with open(path, 'rb') as stream:
                    actual = base64.urlsafe_b64encode(hashlib.new(algorithm, stream.read()).digest()).rstrip(b'=')
            except (OSError, ValueError):  # unreadable, or no (or an unknown) hash in any RECORD
                actual = None
            if actual is None or actual.decode() != expected:
                found.append('%s: runs at Python startup and no installed package lists it with this content; remove '
                             'it, or rebuild .venv (rm -r .venv, then make bootstrap)' % ascii(path))
    return found


def checkout_violations(root=ROOT, inventories=(), mode='provision', remove_bytecode=False):
    """(refusals, modified tracked paths) for the checkout at root.

    Refused in every mode, as git status hides them: gitignored files outside the allowlist, skip-worktree or
    assume-unchanged flags (vault.yml aside), tracked byte code, a symlinked __pycache__, local_repos.yml keys
    and characters, and inventories outside the checkout. In provision and molecule modes (runs that execute
    plays) also untracked files outside the allowlist and index entries missing from HEAD (git add -f, git add -N).
    In provision mode the tracked files whose content differs from HEAD are returned for confirmation; Molecule
    tests uncommitted edits, so molecule mode accepts them. Check mode (syntax checks, task listings) accepts what
    git status shows, the normal state of a branch before its commit, except untracked plugin code: a vars plugin
    beside a playbook runs even for --syntax-check. Replace refs and grafts are refused in every mode.

    With remove_bytecode, untracked __pycache__ directories are deleted instead of refused: Python runs a cached
    .pyc whose header matches the source in place of the tracked source, and a cache is never needed (the
    wrapper sets PYTHONDONTWRITEBYTECODE).
    """
    root = Path(root).resolve()
    git = _git_runner(root)
    strict = mode != 'check'  # provision and molecule: both execute plays
    provision = mode == 'provision'
    found = ['%s: inventory outside the checkout; its files cannot be checked' % ascii(path)
             for path in _outside_inventories(root, inventories)]
    try:
        git('rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
    except PolicySourceError:
        raise PolicySourceError('%s has no commit to compare with' % root) from None
    # git's own walk of the whole tree (no untracked cache, no fsmonitor): untracked dirs collapse to "dir/"
    entries = _paths(git('status', '--porcelain=v1', '-z', '--ignored',
                         '--untracked-files=normal', '--no-renames'))
    other = sorted((entry[3:].rstrip('/'), entry[:2] == '!!') for entry in entries if entry[:2] in ('??', '!!'))
    caches = sorted({match.group(1) for match in (PYCACHE.fullmatch(name) for name, _ in other) if match})
    for directory in caches:
        if remove_bytecode:
            found += _remove_bytecode_cache(root, directory)
        else:
            found.append('%s: byte code Python would run in place of the tracked source; make deletes it, or delete '
                         'it yourself' % ascii(directory))
    for name, ignored in other:
        if UNTRACKED_ALLOWED.fullmatch(name) or any(name == d or name.startswith(d + '/') for d in caches):
            continue
        if _toolbox_lock_only(root, name):
            continue
        if ignored or strict:
            found.append(_untracked_message(name, ignored))
        elif _holds_plugin_code(root, name):  # check mode: plugins beside a playbook load even for --syntax-check
            found.append('%s: untracked plugin code; Ansible loads a playbook- or role-adjacent plugin dir even for '
                         'a syntax check: commit it or move it out of the checkout' % ascii(name))
    found += _replaced_history(root, git)
    for entry in _paths(git('ls-files', '-v', '-z')):
        tag, name = entry[:1], entry[2:]
        if tag != 'H' and not (name == VAULT and tag == 'S'):
            found.append('%s: marked %s, which hides its changes from git; run git update-index --no-skip-worktree '
                         '--no-assume-unchanged -- %s' % (ascii(name), 'skip-worktree' if tag in 'Ss'
                                                          else 'assume-unchanged', name))
    head_blobs = _head_blobs(git)
    head = set(head_blobs)
    found += _vault_violations(root, head_blobs, git('rev-parse', '--show-object-format').decode().strip() or 'sha1')
    index = set(_paths(git('ls-files', '-z')))
    found += ['%s: tracked byte code; commit the source instead' % ascii(name)
              for name in sorted(index | head) if name.endswith(BYTECODE)]
    if strict:
        found += ['%s: in the index but not in HEAD (git add); commit it, or git rm --cached it' % ascii(name)
                  for name in sorted(index - head)]
    found += _case_duplicates(other, head | index)
    modified = _differs_from_head(root, git, head_blobs) if provision else []
    local = root / LOCAL_REPOS
    if local.is_symlink():
        found.append('%s: is a symlink; replace it with the file itself' % ascii(LOCAL_REPOS))
    elif local.is_file():
        data, errors = _load_local_repos(local)
        found += errors or local_repos_violations(data)
    return found, modified


def vars_file_violations(root=ROOT, inventories=(), remove_bytecode=False):
    """Every refusal of a provisioning run, modified tracked files included (they need a typed confirmation)."""
    found, modified = checkout_violations(root, inventories, 'provision', remove_bytecode)
    return found + ['%s: modified since HEAD' % ascii(name) for name in modified]


def _vars_files_main(args):
    inventories = []
    remove_bytecode = warn_unpublished = False
    mode = 'provision'
    while args:
        if args[0] == '--remove-bytecode':
            remove_bytecode, args = True, args[1:]
        elif args[0] == '--warn-unpublished':
            warn_unpublished, args = True, args[1:]
        elif args[0] == '--mode' and len(args) > 1 and args[1] in ('provision', 'molecule', 'check'):
            mode, args = args[1], args[2:]
        elif args[0] == '--inventory' and len(args) > 1:
            inventories.append(args[1])
            args = args[2:]
        else:
            print('usage: parse-config.py --vars-files [--mode provision|molecule|check] [--remove-bytecode] '
                  '[--warn-unpublished] [--inventory PATH ...]', file=sys.stderr)
            return 2
    modified = []
    try:
        found = site_violations()
        more, modified = checkout_violations(ROOT, inventories, mode, remove_bytecode)
        found += more
        if warn_unpublished:
            _warn_unpublished(_git_runner(ROOT))
    except PolicySourceError as error:  # fail closed: without git nothing says which files belong here
        found = ['cannot check the checkout: %s' % error]
    except Exception as error:
        found = ['cannot check the checkout: %s' % type(error).__name__]
    if found:
        print('\n'.join(found))
        return 3
    if modified:  # exit 4: the wrapper lists them (with their content ids) and asks for a typed yes on a terminal
        print('\n'.join('%s (%s)' % (ascii(name), _content_id(ROOT / name)) for name in modified))
        return 4
    return 0


def _content_id(path):
    """sha256 of a file's bytes, so the wrapper's last check can tell an edit made after the confirmation."""
    try:
        return hashlib.sha256(os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()).hexdigest()
    except OSError:
        return 'deleted'


def _warn_unpublished(git):
    """A local commit makes whatever it holds tracked; say so when HEAD is not on origin/main (feature branches are
    fine, so this only warns)."""
    code = git('merge-base', '--is-ancestor', 'HEAD', 'refs/remotes/origin/main', status=True)
    if code == 0:
        return
    detail = 'is not on origin/main' if code == 1 else 'cannot be compared with origin/main'
    print('NOTE: HEAD %s: commits only in this checkout count as tracked. Review them (git log origin/main..HEAD) '
          'if you did not make them.' % detail, file=sys.stderr)


def main(argv):
    if argv[1:2] == ['--vars-files']:
        return _vars_files_main(argv[2:])
    try:
        # strict UTF-8 whatever the locale (a C locale reads stdin with surrogateescape): the error type only
        data = load(sys.stdin.buffer.read().decode('utf-8'))
        output = json.dumps(data, default=json_default)
    except Exception as error:
        print(f"Cannot parse config.yml: {type(error).__name__}", file=sys.stderr)
        return 1
    if argv[1:] == ['--policy']:
        try:
            found = policy_violations(data)
        except Exception as error:  # fail closed: an unreadable source of truth rejects the config
            detail = f": {error}" if isinstance(error, PolicySourceError) else ""
            print(f"Cannot check config.yml keys: {type(error).__name__}{detail}", file=sys.stderr)
            return 3
        print('\n'.join(found))
        return 3 if found else 0
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
