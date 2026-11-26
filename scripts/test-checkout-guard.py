#!/usr/bin/env python3
"""The checkout tripwire: files git does not account for never reach Ansible (parse-config.py --vars-files)."""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import pty
import shutil
import site
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = max(60, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))
TRUE = shutil.which('true') or '/usr/bin/true'  # /bin/true on Linux, /usr/bin/true on macOS

spec = importlib.util.spec_from_file_location("config_parser", Path(__file__).with_name("parse-config.py"))
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false',
                    '-c', 'core.hooksPath=/dev/null', *args], check=True, capture_output=True, env=GIT_ENV)


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def make_checkout(root, files):
    """A git checkout with the given files committed."""
    write(root, files)
    git(root, 'init', '-q')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'init', '--no-verify')


def commit(root, files):
    write(root, files)
    git(root, 'add', '-f', *files)
    git(root, 'commit', '-qm', 'more', '--no-verify')


TRACKED = {'group_vars/all/vars.yml': 'x: 1\n', 'group_vars/all/vault.yml': 'vault_x: ""\n',
           'group_vars/all/local_repos.yml.example': 'git_repos_local: []\n',
           'inventory/localhost.yml': 'all:\n  hosts:\n    localhost: {}\n',
           'roles/r/tasks/main.yml': '[]\n', 'roles/r/filter_plugins/ok.py': 'x = 1\n', 'filter_plugins/ok.py': 'x = 1\n',
           'molecule/s/converge.yml': '[]\n', 'scripts/tool.py': 'x = 1\n', '.gitignore': 'ignored-dir/\n*.secret\n'}
LOCAL_REPOS_OK = "git_repos_local:\n  - url: 'git@git.example.internal:group/repo.git'\n    dest: rh-context/group/repo\n"


def folds_case():
    """Whether the temporary directory's filesystem treats names differing only in case as one file (APFS)."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'probe').write_text('')
        return (Path(directory) / 'PROBE').exists()


def reported(found, name):
    """The line reporting name, or the untracked directory git collapsed it into."""
    parts = name.split('/')
    candidates = {repr('/'.join(parts[:end])) + ':' for end in range(1, len(parts) + 1)}
    return [line for line in found if any(line.startswith(candidate) for candidate in candidates)]


class CheckoutTests(unittest.TestCase):
    def check(self, planted=None, inventories=('inventory',), setup=None, mode='provision', remove_bytecode=False,
              after=None):
        """(refusals, modified tracked paths) for a fresh checkout with planted files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_checkout(root, TRACKED)
            write(root, planted or {})
            if setup:
                setup(root)
            result = parser.checkout_violations(root, inventories, mode, remove_bytecode)
            if after:
                after(root)
            return result

    def refused(self, planted=None, **kwargs):
        found, modified = self.check(planted, **kwargs)
        self.assertEqual(modified, [])
        return found

    def assertRefusedAlone(self, found, name, needle):
        lines = reported(found, name)
        self.assertEqual((len(found), len(lines)), (1, 1), found)
        self.assertIn(needle, lines[0])

    def test_clean_checkout_and_allowlist_pass(self):
        allowed = {'config.yml': 'x: 1\n', 'CLAUDE.local.md': 'x\n', 'local_bookmarks.yml': 'x: 1\n',
                   'local_bookmarks.yml.bak-1': 'x\n', 'group_vars/all/local_repos.yml': LOCAL_REPOS_OK,
                   'group_vars/all/vault.yml.pre-shared': 'x\n', 'group_vars/all/vault.yml.import.Ab3xYz': 'x\n',
                   'group_vars/all/vault.yml.shared.tmp': 'x\n', 'scripts/vault-pass.sh': 'x\n',
                   'scripts/vault-pass.sh.sha256': 'x\n', 'scripts/vault-pass.sh.pre-shared': 'x\n',
                   'scripts/vault-pass.sh.new.abc': 'x\n', '.venv/bin/python3': 'x\n', 'node_modules/a/b.js': 'x\n',
                   'collections/ansible_collections/a/b/c.py': 'x\n', 'galaxy_cache/x': 'x\n', 'tmp/x.py': 'x\n',
                   '.claude/settings.local.json': '{}\n'}
        for mode in ('provision', 'check'):
            with self.subTest(mode=mode):
                self.assertEqual(self.check(allowed, mode=mode), ([], []))

    def test_toolbox_migrate_lock_alone_is_allowed(self):
        """Fedora's toolbox leaves an empty toolbox/migrate.lock in the working directory; only that file passes."""
        self.assertEqual(self.check({'toolbox/migrate.lock': ''}), ([], []))

        def as_link(root):
            write(root, {'tmp/lock': ''})
            (root / 'toolbox').mkdir()
            (root / 'toolbox/migrate.lock').symlink_to(root / 'tmp/lock')

        def as_dir(root):
            write(root, {'toolbox/migrate.lock/x.py': ''})  # git never lists an empty directory, which holds nothing

        def dir_link(root):
            write(root, {'tmp/box/migrate.lock': ''})
            (root / 'toolbox').symlink_to(root / 'tmp/box')
        for case, kwargs in [('not empty', dict(planted={'toolbox/migrate.lock': 'x'})),
                             ('another file', dict(planted={'toolbox/migrate.lock': '', 'toolbox/x.py': ''})),
                             ('hidden file', dict(planted={'toolbox/migrate.lock': '', 'toolbox/.x': ''})),
                             ('symlink', dict(setup=as_link)), ('directory', dict(setup=as_dir)),
                             ('symlinked toolbox', dict(setup=dir_link))]:
            with self.subTest(case=case):
                self.assertRefusedAlone(self.refused(**kwargs), 'toolbox/migrate.lock', 'not tracked by git')

    def test_untracked_files_anywhere_are_refused(self):
        """Bypasses 1, 2, 3, 4, 7 and the search paths: the whole tree is walked, not a list of directories."""
        for name in ['group_vars/all/evil.yml', 'group_vars/all/evil', 'group_vars/all/sub/evil.yml',
                     'group_vars/localhost', 'host_vars/localhost.yml', 'inventory/group_vars/all.yml',
                     'inventory/extra.yml', 'inventory/sub/hosts.yml', 'group_vars/all/.hidden.yml',
                     'group_vars/all/x.yml~', 'inventory/README.md', 'group_vars/all/vault.yml.import.Ab3xYz.yml',
                     'roles/r/vars/main.yml', 'roles/r/defaults/main.yml', 'roles/r/meta/main.yml',
                     'roles/evil/tasks/main.yml', 'roles/r/tasks/templates/t.j2', 'roles/r/filter_plugins/evil.py',
                     'roles/r/library/evil', 'roles/r/module_utils/x/evil.py', 'filter_plugins/evil.py',
                     'lookup_plugins/evil.py', 'library/evil.py', 'module_utils/evil.py', 'plugins/test/evil.py',
                     'plugins/cache/memory.py', 'vars/main.yml', 'templates/x.j2', 'molecule/s/filter_plugins/x.py',
                     'molecule/s/host_vars/instance.yml', 'sub/filter_plugins/e.py', 'toolbox/x.py', 'config.yml.orig',
                     'CONFIG.yml', 'scripts/evil.py']:
            with self.subTest(name=name):
                # 'not tracked by git', or 'gitignored' where the user's global excludes cover the name (x.yml~)
                self.assertRefusedAlone(self.refused({name: 'ansible_python_interpreter: /tmp/x\n'}), name,
                                        'Ansible could load or run it')

    def test_case_variants_are_named(self):
        """Bypass 9: macOS would load HOST_VARS/ as host_vars/; git's own walk reports it, and the message says why."""
        for name in ['HOST_VARS/localhost.yml', 'Lookup_Plugins/x.py', 'roles/r/Vars/main.yml']:
            with self.subTest(name=name):
                found = self.refused({name: 'x\n'})
                self.assertEqual(reported(found, name), found)
                self.assertTrue(any('case variant' in line for line in found), found)

    def test_case_duplicates_of_tracked_paths_are_refused(self):
        """core.ignorecase=true made a planted Vars.yml beside the tracked vars.yml pass for it; Ansible loads both."""
        if folds_case():
            self.skipTest('a case-folding filesystem (APFS) writes the plant into the tracked file: no second file exists')
        for mode in ('provision', 'check'):
            for name, tracked in [('group_vars/all/Vars.yml', 'group_vars/all/vars.yml'),
                                  ('Filter_Plugins/x.py', 'filter_plugins'), ('ROLES/r/vars/main.yml', 'roles')]:
                with self.subTest(mode=mode, name=name):
                    found, _ = self.check({name: 'ansible_become_exe: /tmp/sudo\n'}, mode=mode,
                                          setup=lambda root: git(root, 'config', 'core.ignorecase', 'true'))
                    self.assertTrue(any(line.endswith('differs only in case from the tracked %r; remove it' % tracked)
                                        for line in found), found)

    def test_replace_refs_and_grafts_are_refused(self):
        """A refs/replace/ entry for HEAD made ls-tree show a commit holding a planted, staged Evil.yml."""
        def replace(root):
            write(root, {'group_vars/all/Evil.yml': 'ansible_become_exe: /tmp/sudo\n'})
            git(root, 'add', '-f', 'group_vars/all/Evil.yml')
            run = dict(cwd=root, check=True, capture_output=True, text=True, env=GIT_ENV)
            tree = subprocess.run(['git', 'write-tree'], **run).stdout.strip()
            fake = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit-tree', tree, '-m', 'x'],
                                  **run).stdout.strip()
            git(root, 'replace', 'HEAD', fake)
        for mode in ('provision', 'check'):
            with self.subTest(mode=mode):
                found, _ = self.check(setup=replace, mode=mode)
                self.assertTrue(any(line.startswith("'refs/replace/") for line in found), found)
                if mode == 'provision':  # without the replacement, the staged file is not in HEAD
                    self.assertTrue(any('in the index but not in HEAD' in line for line in found), found)
        found, _ = self.check(setup=lambda root: write(root, {'.git/info/grafts': ''}), mode='check')
        self.assertTrue(any('git grafts rewrite history' in line for line in found), found)

    def test_check_mode_refuses_untracked_plugin_code(self):
        """A vars plugin beside a playbook runs even for --syntax-check: plugin dirs are refused in every mode."""
        for name in ['vars_plugins/evil.py', 'roles/new/library/x.py', 'roles/r/lookup_plugins/x.py',
                     'molecule/s/filter_plugins/x.py', 'module_utils/x.py', 'sub/Callback_Plugins/x.py']:
            with self.subTest(name=name):
                found = self.refused({name: 'x\n'}, mode='check')
                self.assertEqual(len(found), 1, found)
                self.assertIn('untracked plugin code', found[0])
        self.assertEqual(self.check({'roles/new/tasks/main.yml': '[]\n', 'docs/x.md': 'x\n'}, mode='check'), ([], []))

    def test_every_git_call_goes_through_the_one_runner(self):
        """Only _git_runner starts processes, and it passes every override in GIT_OVERRIDES."""
        import inspect  # pylint: disable=import-outside-toplevel
        source = (ROOT / 'scripts/parse-config.py').read_text()
        runner = inspect.getsource(parser._git_runner)
        self.assertEqual(source.count('subprocess.'), runner.count('subprocess.'))
        self.assertNotIn('os.system', source)
        self.assertNotIn('os.popen', source)
        for setting in ('core.ignorecase=false', 'core.precomposeunicode=false', 'core.trustctime=true',
                        'core.checkStat=default', 'core.fsmonitor=false', 'core.untrackedCache=false'):
            self.assertIn(setting, parser.GIT_OVERRIDES)
        calls = []
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            calls.append(list(argv))
            return real(argv, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            make_checkout(directory, TRACKED)
            parser.subprocess.run = spy
            try:
                parser.checkout_violations(Path(directory))
            finally:
                parser.subprocess.run = real
        self.assertTrue(calls)
        for argv in calls:
            overrides = {argv[index + 1] for index, arg in enumerate(argv) if arg == '-c'}
            self.assertTrue(set(parser.GIT_OVERRIDES) <= overrides, argv)

    def test_local_repos_dest_stays_below_the_repos_directory(self):
        for dest in ['../../.config/git', '/etc/x', '~/.ssh', '', '.', 'a/../../b', 'a\\..\\b', 5]:
            with self.subTest(dest=dest):
                text = yaml.safe_dump({'git_repos_local': [{'url': 'git@h:a/b.git', 'dest': dest}]})
                found = self.refused({'group_vars/all/local_repos.yml': text})
                self.assertEqual(len(found), 1, found)
                self.assertIn('git_repos_local.0.dest: must be a relative path', found[0])
        for text in ['git_repos_local: {url: x}\n', 'git_repos_local: [{url: x}]\n', 'git_repos_local: [x]\n']:
            with self.subTest(text=text):
                self.assertEqual(len(self.refused({'group_vars/all/local_repos.yml': text})), 1)
        self.assertEqual(self.check({'group_vars/all/local_repos.yml': LOCAL_REPOS_OK}), ([], []))

    def test_unlisted_startup_files_in_site_packages_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            site_dir = Path(directory)
            ok = b'import os\n'
            digest = base64.urlsafe_b64encode(hashlib.sha256(ok).digest()).rstrip(b'=').decode()
            write(site_dir, {'pkg-1.dist-info/RECORD': 'ok.pth,sha256=%s,%d\npkg/__init__.py,,\n' % (digest, len(ok))})
            (site_dir / 'ok.pth').write_bytes(ok)
            self.assertEqual(parser.site_violations([str(site_dir)]), [])
            (site_dir / 'ok.pth').write_bytes(b'import os; os._exit(0)\n')
            (site_dir / 'zz.pth').write_text('import os; os._exit(0)\n')
            (site_dir / 'sitecustomize.py').write_text('x = 1\n')
            found = parser.site_violations([str(site_dir)])
            self.assertEqual([line.split("'")[1].rsplit('/', 1)[-1] for line in found],
                             ['ok.pth', 'sitecustomize.py', 'zz.pth'])
        self.assertEqual(parser.site_violations(), [])  # this .venv as installed

    def test_only_a_venv_inside_the_checkout_is_audited(self):
        """macOS: make runs Homebrew's ansible, a venv outside the checkout whose packages carry no RECORD; its .pth
        files (setuptools' distutils-precedence.pth) are not refused. The interpreter's venv inside the checkout is."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            # checkout-evil: a sibling whose name starts with the checkout's is outside it (whole path components)
            for prefix in (base / 'brew/libexec', base / 'checkout/.wt-venv', base / 'checkout-evil/venv'):
                site_dir = Path(site.getsitepackages([str(prefix)])[0])
                site_dir.mkdir(parents=True)
                (site_dir / 'distutils-precedence.pth').write_text('import os\n')
            (base / 'linked-venv').symlink_to(base / 'checkout/.wt-venv')  # resolves inside the checkout
            with mock.patch.object(parser, 'ROOT', base / 'checkout'), \
                    mock.patch.object(sys, 'base_prefix', str(base / 'python')):
                for outside in ('brew/libexec', 'checkout-evil/venv'):
                    with self.subTest(prefix=outside), mock.patch.object(sys, 'prefix', str(base / outside)):
                        self.assertEqual(parser.site_violations(), [])
                for inside in ('checkout/.wt-venv', 'linked-venv'):
                    with self.subTest(prefix=inside), mock.patch.object(sys, 'prefix', str(base / inside)):
                        self.assertEqual(len(parser.site_violations()), 1)

    def test_ignored_files_are_refused_in_every_mode(self):
        for mode in ('provision', 'check'):
            for name in ['ignored-dir/x.py', 'roles/r/vars/main.secret', 'host_vars/localhost.secret']:
                with self.subTest(mode=mode, name=name):
                    self.assertRefusedAlone(self.refused({name: 'x\n'}, mode=mode), name, 'gitignored')

    def test_index_entries_missing_from_head_are_refused(self):
        """Bypass 5: git add -f or -N does not make a planted file part of the commit."""
        for flag in ('-f', '-N'):
            with self.subTest(flag=flag):
                found = self.refused({'host_vars/localhost.yml': 'x: 1\n'},
                                     setup=lambda root, flag=flag: git(root, 'add', flag, 'host_vars/localhost.yml'))
                self.assertRefusedAlone(found, 'host_vars/localhost.yml', 'in the index but not in HEAD')

    def test_hidden_flags_are_refused_in_every_mode(self):
        """Bypass 6: skip-worktree or assume-unchanged hides an edit from git diff and git status."""
        for flag in ('--skip-worktree', '--assume-unchanged'):
            for mode in ('provision', 'check'):
                with self.subTest(flag=flag, mode=mode):
                    def hide(root, flag=flag):
                        git(root, 'update-index', flag, 'inventory/localhost.yml')
                        (root / 'inventory/localhost.yml').write_text('all: {hosts: {localhost: {x: 1}}}\n')
                    found, modified = self.check(setup=hide, mode=mode)
                    self.assertRefusedAlone(found, 'inventory/localhost.yml', flag[2:])
                    # content is hashed, not taken from the index, so the hidden edit is listed too
                    self.assertEqual(modified, ['inventory/localhost.yml'] if mode == 'provision' else [])

    def test_encrypted_vault_may_be_skip_worktree(self):
        def encrypt(root):
            git(root, 'update-index', '--skip-worktree', 'group_vars/all/vault.yml')
            (root / 'group_vars/all/vault.yml').write_text('$ANSIBLE_VAULT;1.1;AES256\n3132\n')
        self.assertEqual(self.check(setup=encrypt), ([], []))

    ENVELOPE = '$ANSIBLE_VAULT;1.1;AES256\n6162630a\n3132\n'

    def vault(self, text, skip_worktree=False):
        def setup(root):
            if skip_worktree:
                git(root, 'update-index', '--skip-worktree', 'group_vars/all/vault.yml')
            (root / 'group_vars/all/vault.yml').write_bytes(text if isinstance(text, bytes) else text.encode())
        return self.check(setup=setup)

    def test_vault_may_only_be_the_stub_or_a_whole_vault_file(self):
        """Round 3: vault.yml was exempt from the HEAD check, so plaintext appended to it loaded like group_vars."""
        for text in [TRACKED['group_vars/all/vault.yml'], self.ENVELOPE, '﻿  \n' + self.ENVELOPE,
                     '$ANSIBLE_VAULT;1.2;AES256;yubikey\n6162\n\n', self.ENVELOPE.replace('\n', '\r\n')]:
            for skip in (False, True):
                with self.subTest(text=text, skip_worktree=skip):
                    self.assertEqual(self.vault(text, skip), ([], []))
        stub = TRACKED['group_vars/all/vault.yml']
        for text in [stub + 'ansible_become_exe: /tmp/sudo\n', 'ansible_shell_executable: /tmp/sh\n',
                     self.ENVELOPE + 'ansible_become_exe: /tmp/sudo\n', self.ENVELOPE.replace('\n3132', '\n\n3132'),
                     'secret: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\n', '$ANSIBLE_VAULT;1.1;AES256\n',
                     '$ANSIBLE_VAULT;1.1;AES256\nnot-hex\n', '$ANSIBLE_VAULT;1.3;AES256\n3132\n',
                     'x: 1\n$ANSIBLE_VAULT;1.1;AES256\n3132\n', b'\xff\xfe']:
            for skip in (False, True):
                with self.subTest(text=text, skip_worktree=skip):
                    found, _ = self.vault(text, skip)
                    self.assertTrue(any(line.startswith("'group_vars/all/vault.yml': neither") for line in found), found)

        def link(root):
            write(root, {'tmp/vault.yml': self.ENVELOPE})
            (root / 'group_vars/all/vault.yml').unlink()
            (root / 'group_vars/all/vault.yml').symlink_to(root / 'tmp/vault.yml')
        found, _ = self.check(setup=link)
        self.assertTrue(any('not a regular file' in line for line in found), found)

    def test_modified_tracked_files_need_confirmation(self):
        """Bypass 6 without the flag: the edit is visible, so it is listed for a typed confirmation, not refused."""
        edits = {'group_vars/all/vars.yml': 'ansible_python_interpreter: /tmp/x\n', 'group_vars/all/vault.yml': self.ENVELOPE}
        self.assertEqual(self.check(edits), ([], ['group_vars/all/vars.yml']))
        self.assertEqual(self.check(edits, setup=lambda root: git(root, 'add', '-u')), ([], ['group_vars/all/vars.yml']))
        found, modified = self.check(setup=lambda root: (root / 'scripts/tool.py').unlink())
        self.assertEqual((found, modified), ([], ['scripts/tool.py']))

    def test_edit_hidden_by_weakened_stat_config_is_seen(self):
        """core.trustctime=false and core.checkStat=minimal in .git/config would pass a same-size, same-mtime edit."""
        def edit(root):
            path = root / 'inventory/localhost.yml'
            past = path.stat().st_mtime_ns - 3600 * 10**9
            os.utime(path, ns=(past, past))
            git(root, 'add', '-u')  # the index records the old mtime; the index file itself is newer (not racy)
            git(root, 'config', 'core.trustctime', 'false')
            git(root, 'config', 'core.checkStat', 'minimal')
            original = path.read_bytes()
            path.write_bytes(original.replace(b'localhost', b'evilhosts'))
            os.utime(path, ns=(past, past))
            self.assertEqual(len(path.read_bytes()), len(original))
        self.assertEqual(self.check(setup=edit), ([], ['inventory/localhost.yml']))

    def test_molecule_mode_refuses_untracked_and_staged_files(self):
        """Molecule converges roles with its own plugin paths: what git does not account for is refused there too."""
        for name, setup in [('molecule/s/filter_plugins/e.py', None),
                            ('roles/r/vars/main.yml', lambda root: git(root, 'add', '-f', 'roles/r/vars/main.yml'))]:
            with self.subTest(name=name):
                found, modified = self.check({name: 'x\n'}, setup=setup, mode='molecule')
                self.assertEqual((len(found), modified), (1, []), found)
        edits = {'group_vars/all/vars.yml': 'x: 2\n'}  # uncommitted edits are what Molecule tests
        self.assertEqual(self.check(edits, mode='molecule'), ([], []))

    def test_check_mode_accepts_what_git_status_shows(self):
        """Syntax checks and Molecule run on uncommitted work: new files, staged files and edits pass there."""
        def work(root):
            write(root, {'roles/r/tasks/new.yml': '[]\n', 'roles/r/tasks/staged.yml': '[]\n',
                         'group_vars/all/vars.yml': 'x: 2\n'})
            git(root, 'add', 'roles/r/tasks/staged.yml')
        self.assertEqual(self.check(setup=work, mode='check'), ([], []))
        found, modified = self.check(setup=work)
        self.assertEqual(len(found), 2, found)
        self.assertEqual(modified, ['group_vars/all/vars.yml'])

    def test_bytecode_caches_are_removed(self):
        for cache in ['filter_plugins/__pycache__', 'roles/r/filter_plugins/__pycache__', 'scripts/__pycache__',
                      'molecule/s/__pycache__', 'group_vars/all/__pycache__']:
            planted = {cache + '/ok.cpython-314.pyc': 'x', cache + '/sub/y.pyc': 'y'}
            with self.subTest(cache=cache, remove=True):
                def gone(root, cache=cache):
                    self.assertFalse(os.path.lexists(root / cache))
                    self.assertTrue((root / cache).parent.is_dir())
                self.assertEqual(self.check(planted, remove_bytecode=True, after=gone), ([], []))
            with self.subTest(cache=cache, remove=False):
                def kept(root, cache=cache):
                    self.assertTrue((root / cache).is_dir())
                self.assertRefusedAlone(self.refused(planted, after=kept), cache, 'make deletes it')

    def test_symlinked_pycache_is_refused_and_its_target_kept(self):
        for cache in ['roles/r/filter_plugins/__pycache__', 'filter_plugins/__pycache__']:
            with self.subTest(cache=cache):
                def plant(root, cache=cache):
                    write(root, {'tmp/elsewhere/ok.cpython-314.pyc': 'x'})
                    (root / cache).symlink_to(root / 'tmp/elsewhere')

                def kept(root, cache=cache):
                    self.assertTrue((root / cache).is_symlink())
                    self.assertTrue((root / 'tmp/elsewhere/ok.cpython-314.pyc').exists())
                self.assertRefusedAlone(self.refused(setup=plant, remove_bytecode=True, after=kept), cache, 'symlink')

    def test_bare_and_tracked_bytecode_is_refused(self):
        for name in ['filter_plugins/x.pyc', 'roles/r/filter_plugins/go_checksum.pyc', 'roles/r/tasks/x.pyo',
                     'scripts/x.pyc']:
            with self.subTest(name=name, tracked=False):
                found = self.refused({name: 'x'}, remove_bytecode=True)
                self.assertRefusedAlone(found, name, 'byte code Python would import')
            with self.subTest(name=name, tracked=True):
                found = self.refused(setup=lambda root, name=name: commit(root, {name: 'x'}), remove_bytecode=True)
                self.assertRefusedAlone(found, name, 'tracked byte code')

    def test_repository_tracks_no_bytecode(self):
        listed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '*.pyc', '*.pyo'], capture_output=True, text=True,
                                check=True, env=GIT_ENV)
        self.assertEqual(listed.stdout, '')

    def test_symlinks_are_refused(self):
        for name in ['group_vars/all/linked', 'host_vars', 'filter_plugins/linked']:
            with self.subTest(name=name):
                def link(root, name=name):
                    (root / 'tmp/elsewhere').mkdir(parents=True)
                    (root / name).symlink_to(root / 'tmp/elsewhere')
                self.assertRefusedAlone(self.refused(setup=link), name, 'not tracked by git')

        def link_local_repos(root):
            write(root, {'tmp/repos.yml': LOCAL_REPOS_OK})
            (root / 'group_vars/all/local_repos.yml').symlink_to(root / 'tmp/repos.yml')
        self.assertRefusedAlone(self.refused(setup=link_local_repos), 'group_vars/all/local_repos.yml', 'symlink')

    def test_local_repos_may_set_only_git_repos_local(self):
        for extra in ['ansible_python_interpreter: /tmp/x\n', 'ansible_become_exe: /tmp/sudo\n',
                      'claude_install_url: https://evil.example/x.sh\n', '_pf_config_raw: {}\n']:
            with self.subTest(extra=extra):
                found = self.refused({'group_vars/all/local_repos.yml': LOCAL_REPOS_OK + extra})
                self.assertEqual(len(found), 1, found)
                self.assertIn(repr(extra.split(':')[0]), found[0])
                self.assertIn('only git_repos_local', found[0])

    def test_local_repos_values_pass_the_config_checks(self):
        for value, needle in [("\"{{ lookup('pipe', 'id') }}\"", "'{{'"), ("'{% if true %}x{% endif %}'", "'{%'"),
                              ("\"#jinja2: variable_start_string:'[%'\"", "'#jinja2'"),
                              ('!unsafe "{{ x }}"', "'{{'"), ('"a\\0b"', 'U+0000'), ('"a\\tb"', 'U+0009')]:
            with self.subTest(value=value):
                found = self.refused({'group_vars/all/local_repos.yml': 'git_repos_local:\n  - url: ' + value + '\n'})
                self.assertTrue(any(needle in line for line in found), found)
                self.assertNotIn('lookup', ''.join(found))  # messages name markers and code points, never values

    def test_local_repos_errors_never_quote_content(self):
        for text, needle in [('git_repos_local: [internal-secret-host.example\n  bad: {\n', 'cannot be parsed'),
                             ('- internal-secret-host.example\n', 'must be a mapping'),
                             ('$ANSIBLE_VAULT;1.1;AES256\n3132\n', 'vault-encrypted')]:
            with self.subTest(needle=needle):
                found = self.refused({'group_vars/all/local_repos.yml': text})
                self.assertIn(needle, ''.join(found))
                self.assertNotIn('internal-secret-host', ''.join(found))

    def test_without_a_checkout_or_commit_it_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, TRACKED)
            with self.assertRaises(parser.PolicySourceError):
                parser.checkout_violations(Path(directory))
            git(directory, 'init', '-q')
            git(directory, 'add', '-A')
            with self.assertRaises(parser.PolicySourceError):  # no commit to compare with
                parser.checkout_violations(Path(directory))
        with tempfile.TemporaryDirectory() as directory:  # a copy inside some other checkout (e.g. a home repo)
            outer = Path(directory)
            make_checkout(outer, {'README': 'x\n'})
            write(outer / 'copy', TRACKED)
            with self.assertRaises(parser.PolicySourceError):
                parser.checkout_violations(outer / 'copy')
        cli = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/parse-config.py'), '--vars-files'],
                             env=dict(os.environ, PATH='/nonexistent'), capture_output=True, text=True, timeout=60)
        self.assertEqual(cli.returncode, 3)
        self.assertIn('git is not installed', cli.stdout)

    def test_inventory_outside_the_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            found = self.refused(inventories=('inventory', other))
            self.assertEqual(len(found), 1, found)
            self.assertIn('inventory outside the checkout', found[0])
        self.assertEqual(self.check(inventories=('inventory', 'localhost,')), ([], []))

    def test_this_checkout_passes_the_check_mode(self):
        """The repository (with whatever uncommitted work a gate runs on) passes what syntax checks and Molecule get."""
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/parse-config.py'), '--vars-files', '--mode',
                                 'check', '--remove-bytecode', '--inventory', str(ROOT / 'inventory')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)


FAKE_INTERPRETER = '#!/bin/sh\ntouch "$(dirname "$0")/fake-ran"\nexec {real} "$@"\n'
TOUCH = "import pathlib\npathlib.Path(r'{marker}').touch()\n"
FAKE_FILTER_PLUGIN = TOUCH + "\n\nclass FilterModule:\n    def filters(self):\n        return dict()\n"
FAKE_TEST_PLUGIN = TOUCH + "\n\nclass TestModule:\n    def tests(self):\n        return dict(string=lambda v: True)\n"
FAKE_CACHE_PLUGIN = TOUCH + "from ansible.plugins.cache.memory import CacheModule as _Cache\n\n\n" \
                            "class CacheModule(_Cache):\n    pass\n"
INTERPRETER = 'ansible_python_interpreter: "{python}"\n'
# Writes a timestamp .pyc for argv[1] that Python accepts in place of the source: the header holds the source's
# mtime and size, and the code touches argv[2].
CRAFT_PYC = '\n'.join([
    'import importlib._bootstrap_external as bootstrap, importlib.util, os, sys',
    'source, marker = sys.argv[1], sys.argv[2]',
    'status = os.stat(source)',
    'body = "import pathlib\\npathlib.Path(%r).touch()\\n" % marker + open(source).read()',
    'data = bootstrap._code_to_timestamp_pyc(compile(body, source, "exec"), status.st_mtime, status.st_size)',
    'target = importlib.util.cache_from_source(source)',
    'os.makedirs(os.path.dirname(target), exist_ok=True)',
    'open(target, "wb").write(data)',
])
DEFAULT_TASKS = [{'name': 'Template', 'ansible.builtin.debug': {'msg': "{{ 'x' | upper }} {{ 'x' is string }}"}},
                 {'name': 'Run a module', 'ansible.builtin.command': TRUE, 'changed_when': False}]
ROLE_TASKS = "- ansible.builtin.command: %s\n  changed_when: false\n" % TRUE


class WrapperTests(unittest.TestCase):
    """scripts/verify-ansible-args.sh refuses before Ansible starts, so planted code never runs."""

    def run_wrapper(self, planted=None, tasks=None, direct=False, tracked=None, setup=None, after=None, args=(),
                    roles=None, playbook='play.yml', answer=None, command=None, real_venv=False, on_prompt=None):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            fakes = Path(directory) / 'fakes'
            fakes.mkdir()
            python, shell = fakes / 'python', fakes / 'sh'
            python.write_text(FAKE_INTERPRETER.format(real=Path(ANSIBLE).parent / 'python3'))
            shell.write_text(FAKE_INTERPRETER.format(real='/bin/sh'))
            for path in (python, shell):
                path.chmod(0o755)
            play = {'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks or DEFAULT_TASKS}
            if roles:
                play['roles'] = roles
            write(repo, dict(tracked or {}, **{
                'ansible.cfg': '[defaults]\ninventory = inventory/\ninterpreter_python = auto_silent\n',
                'inventory/localhost.yml': (ROOT / 'inventory/localhost.yml').read_text(),
                'group_vars/all/vars.yml': 'harmless: true\n',
                'scripts/parse-config.py': (ROOT / 'scripts/parse-config.py').read_text(),
                'scripts/verify-ansible-args.sh': (ROOT / 'scripts/verify-ansible-args.sh').read_text(),
                'scripts/verify-collections.sh': '#!/bin/sh\nexit 0\n',
                'scripts/vault-pass.sh': (ROOT / 'scripts/vault-pass-ci.sh').read_text(),
                'play.yml': yaml.safe_dump([play])}))
            for name in ['scripts/verify-ansible-args.sh', 'scripts/verify-collections.sh', 'scripts/vault-pass.sh']:
                (repo / name).chmod(0o755)
            make_checkout(repo, {})  # committed with their modes
            # The wrapper runs the check with the checkout's .venv python, as on a provisioned machine.
            if real_venv:  # a virtualenv of its own, with PyYAML copied in, for planting site-packages files
                subprocess.run([str(Path(ANSIBLE).parent / 'python3'), '-m', 'venv', '--without-pip', str(repo / '.venv')],
                               check=True, capture_output=True)
                site_dir = Path(subprocess.run([str(repo / '.venv/bin/python3'), '-I', '-S', '-c',
                                                'import site, sys; print(site.getsitepackages([sys.prefix])[0])'],
                                               check=True, capture_output=True, text=True).stdout.strip())
                shutil.copytree(Path(yaml.__file__).parent, site_dir / 'yaml')
            else:
                (repo / '.venv/bin').mkdir(parents=True)
                (repo / '.venv/bin/python3').write_text('#!/bin/sh\nexec ' + str(Path(ANSIBLE).parent / 'python3')
                                                        + ' "$@"\n')
                (repo / '.venv/bin/python3').chmod(0o755)
            (repo / '.git/info/exclude').write_text('.venv/\n')
            write(repo, {name: text.format(python=python, shell=shell, marker=fakes / 'fake-ran')
                         for name, text in (planted or {}).items()})
            if setup:
                setup(repo, fakes / 'fake-ran')
            command = list(command or [str(ANSIBLE), playbook, *args])
            env = {key: value for key, value in os.environ.items() if not key.startswith('ANSIBLE_')}
            env.update(ANSIBLE_NOCOLOR='1')
            if direct:  # Ansible as the wrapper used to set it up: ANSIBLE_HOME at the checkout
                env.update(ANSIBLE_CONFIG=str(repo / 'ansible.cfg'), ANSIBLE_HOME=str(repo),
                           ANSIBLE_VAULT_PASSWORD_FILE=str(repo / 'scripts/vault-pass.sh'))
            else:
                command = [str(repo / 'scripts/verify-ansible-args.sh')] + command
            if answer is None:
                result = subprocess.run(command, cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=TEST_TIMEOUT)
            else:
                master, slave = pty.openpty()
                try:
                    process = subprocess.Popen(command, cwd=repo, env=env, stdin=slave, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE)
                    before = b''
                    if on_prompt:  # act while the prompt waits, as someone planting files during the typing would
                        while b'Type yes' not in before:
                            chunk = os.read(process.stderr.fileno(), 4096)
                            self.assertTrue(chunk, 'no prompt: ' + before.decode(errors='replace'))
                            before += chunk
                        on_prompt(repo, fakes / 'fake-ran')
                    os.write(master, (answer + '\n').encode())
                    out, err = process.communicate(timeout=TEST_TIMEOUT)
                    result = subprocess.CompletedProcess(command, process.returncode, out.decode(errors='replace'),
                                                         (before + err).decode(errors='replace'))
                finally:
                    os.close(master)
                    os.close(slave)
            if after:
                after(repo)
            return result, (fakes / 'fake-ran').exists()

    PLANTS = {
        'host_vars/localhost.yml': dict(planted={'host_vars/localhost.yml': INTERPRETER}),
        'group_vars/all/evil.yml': dict(planted={'group_vars/all/evil.yml': INTERPRETER
                                                 + 'ansible_shell_executable: "{shell}"\n'}),
        'inventory/group_vars/all.yml': dict(planted={'inventory/group_vars/all.yml': 'ansible_shell_executable: "{shell}"\n'}),
        # the allowed file with a key it may not set (ansible_become_exe would replace sudo the same way)
        'local_repos.yml': dict(planted={'group_vars/all/local_repos.yml': 'git_repos_local: []\n' + INTERPRETER
                                         + 'ansible_shell_executable: "{shell}"\n'}),
        'filter_plugins/evil.py': dict(planted={'filter_plugins/evil.py': FAKE_FILTER_PLUGIN}),
        # the review's bypasses of 248ca8e7
        '1 role vars': dict(tracked={'roles/r/tasks/main.yml': ROLE_TASKS}, roles=['r'],
                            planted={'roles/r/vars/main.yml': INTERPRETER}),
        '2 role meta': dict(tracked={'roles/r/tasks/main.yml': ROLE_TASKS}, roles=['r'],
                            planted={'roles/r/meta/main.yml': 'dependencies: [evil]\n',
                                     'roles/evil/tasks/main.yml': '- ansible.builtin.command: touch {marker}\n'}),
        '3 plugins/test': dict(planted={'plugins/test/evil.py': FAKE_TEST_PLUGIN}),
        '4 plugins/cache': dict(planted={'plugins/cache/memory.py': FAKE_CACHE_PLUGIN}),
        '5 git add -f': dict(planted={'host_vars/localhost.yml': INTERPRETER},
                             setup=lambda repo, _: git(repo, 'add', '-f', 'host_vars/localhost.yml')),
        '5 git add -N': dict(planted={'host_vars/localhost.yml': INTERPRETER},
                             setup=lambda repo, _: git(repo, 'add', '-N', 'host_vars/localhost.yml')),
        '6 skip-worktree': dict(planted={'group_vars/all/vars.yml': INTERPRETER + 'ansible_shell_executable: "{shell}"\n'},
                                setup=lambda repo, _: git(repo, 'update-index', '--skip-worktree',
                                                          'group_vars/all/vars.yml')),
        '7 other playbook dir': dict(tracked={'sub/play.yml': yaml.safe_dump([{
            'hosts': 'localhost', 'gather_facts': False,
            'tasks': [{'ansible.builtin.debug': {'msg': "{{ 'x' | upper }}"}}]}])},
            planted={'sub/filter_plugins/e.py': FAKE_FILTER_PLUGIN}, playbook='sub/play.yml'),
        'round 3 vault plaintext': dict(tracked={'group_vars/all/vault.yml': 'vault_x: ""\n'},
                                        planted={'group_vars/all/vault.yml': 'vault_x: ""\nansible_shell_executable: "{shell}"\n'}),
        'round 3 vault plaintext, skip-worktree': dict(
            tracked={'group_vars/all/vault.yml': 'vault_x: ""\n'},
            planted={'group_vars/all/vault.yml': 'vault_x: ""\nansible_shell_executable: "{shell}"\n'},
            setup=lambda repo, _: git(repo, 'update-index', '--skip-worktree', 'group_vars/all/vault.yml')),
        '8 -i': dict(planted={'tmp/evil_inv.yml': 'all:\n  hosts:\n    localhost:\n      ansible_connection: local\n'
                                                  '      ' + INTERPRETER}, args=('-i', 'tmp/evil_inv.yml')),
    }

    def test_molecule_with_untracked_plugin_is_refused(self):
        """Round 2, bypass 1: molecule ran with check mode; an untracked scenario filter plugin ran on converge."""
        def fake_molecule(repo, marker):
            tool = repo / 'tmp/bin/molecule'
            write(repo, {'tmp/bin/molecule': '#!/bin/sh\ntouch %s\n' % marker})
            tool.chmod(0o755)
        result, ran = self.run_wrapper({'molecule/x/filter_plugins/e.py': FAKE_FILTER_PLUGIN}, setup=fake_molecule,
                                       command=['tmp/bin/molecule', 'converge', '-s', 'x'])
        self.assertFalse(ran, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("'molecule", result.stderr)  # the untracked scenario, as git collapses it
        result, ran = self.run_wrapper(self.MODIFIED['planted'], setup=fake_molecule,
                                       command=['tmp/bin/molecule', 'converge', '-s', 'x'])
        self.assertTrue(ran, result.stdout + result.stderr)  # uncommitted edits are what Molecule tests

    def test_double_dash_cannot_disguise_a_run_as_a_syntax_check(self):
        """Round 2, bypass 2: after --, --syntax-check is a playbook name and the run executes plays."""
        play = yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'tasks': DEFAULT_TASKS}])
        result, ran = self.run_wrapper({'host_vars/localhost.yml': INTERPRETER, '--syntax-check': play},
                                       args=('--', '--syntax-check'))
        self.assertFalse(ran, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('-- rejected by VERIFY_AND_RUN', result.stderr)

    def test_planted_code_never_runs(self):
        for name, case in self.PLANTS.items():
            with self.subTest(name=name):
                result, ran = self.run_wrapper(**case)
                self.assertFalse(ran, 'the planted code ran: ' + result.stdout + result.stderr)
                self.assertIn(result.returncode, (1, 2), result.stdout + result.stderr)
                self.assertRegex(result.stderr, 'SECURITY: refusing to start Ansible|rejected by VERIFY_AND_RUN')
                self.assertNotIn('PLAY [', result.stdout)  # Ansible never started

    def test_planted_files_are_not_inert(self):
        """Run directly (as the old wrapper set up Ansible), each plant does run: the test above is not vacuous."""
        for name, case in self.PLANTS.items():
            with self.subTest(name=name):
                result, ran = self.run_wrapper(direct=True, **case)
                self.assertTrue(ran, result.stdout + result.stderr)

    def test_clean_checkout_runs(self):
        result, ran = self.run_wrapper({'group_vars/all/local_repos.yml': LOCAL_REPOS_OK})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PLAY [', result.stdout)
        self.assertFalse(ran)

    def test_plugin_paths_are_pinned(self):
        """ANSIBLE_HOME is root-owned and every plugin path is pinned, so only checked paths are searched."""
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'ansible-playbook'  # prints the environment the real one would get
            fake.write_text('#!/bin/sh\nenv\n')
            fake.chmod(0o755)
            result = subprocess.run([str(ROOT / 'scripts/verify-ansible-args.sh'), str(fake), 'other.yml',
                                     '--syntax-check'], cwd=ROOT, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                    env=dict(os.environ, ANSIBLE_HOME=directory, ANSIBLE_TEST_PLUGINS=directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        self.assertEqual(env['ANSIBLE_HOME'], '/')
        self.assertEqual(os.stat('/').st_uid, 0)
        self.assertEqual(env['ANSIBLE_ROLES_PATH'], str(ROOT / 'roles'))
        self.assertEqual(env['ANSIBLE_COLLECTIONS_PATH'], str(ROOT / 'collections') + ':/usr/share/ansible/collections')
        for name in ['ACTION', 'BECOME', 'CACHE', 'CALLBACK', 'CLICONF', 'CONNECTION', 'DOC_FRAGMENT', 'FILTER',
                     'HTTPAPI', 'INVENTORY', 'LOOKUP', 'NETCONF', 'STRATEGY', 'TERMINAL', 'TEST', 'VARS']:
            with self.subTest(plugin=name):
                self.assertEqual(env['ANSIBLE_%s_PLUGINS' % name], '/dev/null')
        self.assertEqual((env['ANSIBLE_LIBRARY'], env['ANSIBLE_MODULE_UTILS']), ('/dev/null', '/dev/null'))

    MODIFIED = dict(planted={'group_vars/all/vars.yml': 'harmless: false\n'})

    def test_modified_tracked_files_without_a_terminal_are_refused(self):
        result, _ = self.run_wrapper(**self.MODIFIED)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("'group_vars/all/vars.yml'", result.stderr)
        self.assertIn('without a terminal', result.stderr)
        self.assertNotIn('PLAY [', result.stdout)

    def test_modified_tracked_files_need_a_typed_yes(self):
        result, _ = self.run_wrapper(answer='yes', **self.MODIFIED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PLAY [', result.stdout)
        for answer in ('y', 'no', ''):
            with self.subTest(answer=answer):
                result, _ = self.run_wrapper(answer=answer, **self.MODIFIED)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('not confirmed', result.stderr)
                self.assertNotIn('PLAY [', result.stdout)

    def test_syntax_check_runs_on_uncommitted_work(self):
        """make gate checks branches before their commit: new, staged and edited files pass a syntax check."""
        def work(repo, _):
            write(repo, {'roles/r/tasks/main.yml': '[]\n', 'staged.yml': '[]\n'})
            git(repo, 'add', 'staged.yml')
        for args in (('--syntax-check',), ('--syntax',), ('--list-tasks',)):
            with self.subTest(args=args):
                result, _ = self.run_wrapper(setup=work, args=args, **self.MODIFIED)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, _ = self.run_wrapper(setup=work, **self.MODIFIED)  # a run that executes plays refuses
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_inventory_and_module_path_options_are_rejected(self):
        for args in (('-i', 'x'), ('-ix',), ('-vi', 'x'), ('--inventory=x',), ('--inv', 'x'), ('--inventory-file', 'x'),
                     ('-M', 'x'), ('--module-path', 'x'), ('--mod=x',)):
            with self.subTest(args=args):
                result, _ = self.run_wrapper(args=args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('rejected by VERIFY_AND_RUN', result.stderr)

    def test_a_pth_file_in_venv_cannot_end_the_check(self):
        """A .venv zz.pth calling os._exit(0) ended the check with success; with -S it never runs, and it is refused."""
        def plant(repo, _):
            site_dir = next((repo / '.venv').glob('lib*/python3*/site-packages'))
            (site_dir / 'zz.pth').write_text('import os; os._exit(0)\n')
        result, ran = self.run_wrapper({'host_vars/localhost.yml': INTERPRETER}, real_venv=True, setup=plant)
        self.assertFalse(ran, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("zz.pth': runs at Python startup", result.stderr)
        self.assertIn("'host_vars': not tracked by git", result.stderr)

    def test_files_planted_while_the_prompt_waits_are_refused(self):
        """The check runs again after every prompt, right before exec: typing time is no window."""
        def plant(repo, marker):
            write(repo, {'host_vars/localhost.yml': INTERPRETER.format(python=marker.parent / 'python')})
        result, ran = self.run_wrapper(answer='yes', on_prompt=plant, **self.MODIFIED)
        self.assertFalse(ran, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('the checkout changed after the first check', result.stderr)

        def edit_again(repo, _):  # the confirmed file, edited once more
            write(repo, {'group_vars/all/vars.yml': 'harmless: maybe\n'})
        result, _ = self.run_wrapper(answer='yes', on_prompt=edit_again, **self.MODIFIED)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('PLAY [', result.stdout)

    def test_head_outside_origin_main_is_only_noted(self):
        result, _ = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('NOTE: HEAD cannot be compared with origin/main', result.stderr)

    def test_planted_vars_plugin_never_runs_under_a_syntax_check(self):
        result, ran = self.run_wrapper({'vars_plugins/evil.py': FAKE_FILTER_PLUGIN}, args=('--syntax-check',))
        self.assertFalse(ran, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("'vars_plugins': untracked plugin code", result.stderr)
        result, ran = self.run_wrapper({'vars_plugins/evil.py': FAKE_FILTER_PLUGIN}, args=('--syntax-check',),
                                       direct=True)
        self.assertTrue(ran, 'a syntax check did not load the planted vars plugin: ' + result.stdout + result.stderr)

    def test_wrapper_tripwire_has_no_array_slices(self):
        """Under IFS=$'\\n\\t' bash 3.2 (macOS /bin/bash) joins an array slice into one word, so a loop over one would
        see a single argument and miss -i, -M, -- and --syntax-check."""
        source = (ROOT / 'scripts/verify-ansible-args.sh').read_text()
        for begin, end in (('# vars-files: begin', '# vars-files: end'),
                           ('# vars-files-final: begin', '# vars-files-final: end')):
            block = source[source.index(begin):source.index(end)]
            self.assertNotIn('[@]:', block)
            self.assertNotIn('[*]:', block)

    def test_extra_vars_value_is_not_taken_for_an_option(self):
        """The argument after -e is its value: -e -ix=1 is not an -i option (Ansible's own parser rejects it)."""
        for args in (('-e', '-ix=1'), ('--extra-vars', '-Mx=1'), ('-ve', '-ix=1')):
            with self.subTest(args=args):
                result, _ = self.run_wrapper(args=args)
                self.assertNotIn('rejected by VERIFY_AND_RUN', result.stderr)
        result, _ = self.run_wrapper(args=('-e', 'x=1', '-i', 'x'))  # an -i after the value still is one
        self.assertIn('-i rejected by VERIFY_AND_RUN', result.stderr)

    def run_with_crafted_bytecode(self, plugin_dir, direct):
        """A tracked filter plugin with a planted .pyc whose header matches the source (mtime and size)."""
        tracked = {plugin_dir + '/ok.py': 'class FilterModule:\n    def filters(self):\n        return dict()\n'}
        tasks = [{'name': 'Template', 'ansible.builtin.debug': {'msg': "{{ 'x' | upper }}"}}]
        if plugin_dir.startswith('roles/'):
            tracked['roles/r/tasks/main.yml'] = yaml.safe_dump(tasks)
            tasks = [{'name': 'Use the role', 'ansible.builtin.include_role': {'name': 'r'}}]

        def plant(repo, marker):
            subprocess.run([str(Path(ANSIBLE).parent / 'python3'), '-I', '-c', CRAFT_PYC, str(repo / plugin_dir / 'ok.py'),
                            str(marker)], check=True)
            self.assertTrue((repo / plugin_dir / '__pycache__').is_dir())
        state = {}

        def after(repo):
            state['cache'] = os.path.lexists(repo / plugin_dir / '__pycache__')
        result, ran = self.run_wrapper(tasks=tasks, direct=direct, tracked=tracked, setup=plant, after=after)
        return result, ran, state['cache']

    def test_crafted_plugin_bytecode_is_removed_and_never_runs(self):
        for plugin_dir in ['filter_plugins', 'roles/r/filter_plugins']:
            with self.subTest(plugin_dir=plugin_dir, direct=True):  # the crafted .pyc is not inert
                result, ran, _ = self.run_with_crafted_bytecode(plugin_dir, direct=True)
                self.assertTrue(ran, result.stdout + result.stderr)
            with self.subTest(plugin_dir=plugin_dir, direct=False):
                result, ran, cache = self.run_with_crafted_bytecode(plugin_dir, direct=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(ran, 'the planted byte code ran')
                self.assertFalse(cache, 'the wrapper left __pycache__ (or Ansible wrote it again)')

    def test_play_zero_task_refuses_before_any_module(self):
        """The pre-flight.yml copy of the check (direct runs) is the first task and runs no module."""
        tasks = yaml.safe_load((ROOT / 'pre-flight.yml').read_text())[0]['tasks']
        self.assertTrue(tasks[0]['name'].startswith('SECURITY CRITICAL — Refuse untracked group_vars'))
        self.assertIn('ansible.builtin.assert', tasks[0])
        self.assertEqual(tasks[0]['tags'], ['always'])
        for name in tasks[0]['vars']:  # -e outranks task vars: the wrapper must refuse -e for each
            self.assertRegex(name + '=exit=0', parser.protected_keys_pattern())
        probe = [tasks[0], {'name': 'Run a module', 'ansible.builtin.command': TRUE, 'changed_when': False}]
        result, _ = self.run_wrapper(tasks=probe, direct=True, **self.MODIFIED)  # edits are the wrapper's to confirm
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, ran = self.run_wrapper(tasks=probe, direct=True, **self.PLANTS['host_vars/localhost.yml'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("'host_vars': not tracked by git", result.stdout)
        self.assertFalse(ran)


if __name__ == "__main__":
    unittest.main()
