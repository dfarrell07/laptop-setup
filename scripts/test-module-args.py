#!/usr/bin/env python3
"""Reject task arguments that the module's argument spec does not define.

ansible-lint does not catch these (e.g. copy follow_symlinks, find maxdepth); they only
fail when the task really runs, which check mode and first-run paths can hide.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DOC = ROOT / '.venv/bin/ansible-doc'
if not ANSIBLE_DOC.exists():
    ANSIBLE_DOC = Path(shutil.which('ansible-doc'))
# Modules whose arguments are free-form variable names, not an argument spec.
FREE_FORM = {'ansible.builtin.set_fact', 'ansible.builtin.add_host'}
NESTED = ('block', 'rescue', 'always', 'tasks', 'pre_tasks', 'post_tasks', 'handlers')
NAMESPACES = ('ansible.', 'community.', 'containers.')


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor('!', lambda loader, suffix, node: None)


def task_files():
    patterns = ['roles/*/tasks/*.yml', 'roles/*/handlers/*.yml', 'common/tasks/*.yml',
                'site.yml', 'molecule/**/*.yml']
    for pattern in patterns:
        yield from sorted(ROOT.glob(pattern))


def module_calls(node, path):
    if isinstance(node, list):
        for item in node:
            yield from module_calls(item, path)
    elif isinstance(node, dict):
        for key in NESTED:
            if key in node:
                yield from module_calls(node[key], path)
        for key, args in node.items():
            if key.startswith(NAMESPACES) and key.count('.') >= 2 and isinstance(args, dict):
                yield path, node.get('name', '?'), key, args


class ModuleArgsTest(unittest.TestCase):
    def test_task_arguments_exist_in_module_specs(self):
        calls = []
        for path in task_files():
            data = yaml.load(path.read_text(), Loader=_Loader)
            calls.extend(module_calls(data, path.relative_to(ROOT)))
        modules = sorted({module for _, _, module, _ in calls} - FREE_FORM)
        docs = json.loads(subprocess.run([str(ANSIBLE_DOC), '-j', *modules], cwd=ROOT, check=True,
                                         capture_output=True, text=True).stdout)
        specs = {}
        for module, doc in docs.items():
            options = doc['doc'].get('options') or {}
            names = set(options)
            for option in options.values():
                names.update(option.get('aliases') or [])
            specs[module] = names
        self.assertEqual(sorted(set(modules) - set(specs)), [], 'modules without documentation')
        unknown = [f'{path}: {name}: {module}: {key}'
                   for path, name, module, args in calls if module not in FREE_FORM
                   for key in args if key not in specs[module]]
        self.assertEqual(unknown, [])

    def test_no_yaml_comments_inside_conditionals(self):
        """A '# ...' written inside a folded when/failed_when becomes Jinja text (syntax error)."""
        keys = ('when', 'failed_when', 'changed_when', 'until')
        found = []

        def walk(node, path):
            if isinstance(node, list):
                for item in node:
                    walk(item, path)
            elif isinstance(node, dict):
                for key in keys:
                    values = node.get(key)
                    for value in values if isinstance(values, list) else [values]:
                        if isinstance(value, str) and ' # ' in value:
                            found.append(f"{path}: {node.get('name', '?')}: {key}")
                for value in node.values():
                    walk(value, path)

        for path in task_files():
            walk(yaml.load(path.read_text(), Loader=_Loader), path.relative_to(ROOT))
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
