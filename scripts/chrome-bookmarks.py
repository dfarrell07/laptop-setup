#!/usr/bin/env python3
"""Render each Chrome profile's declared bookmarks as a bookmarks HTML file to import once (docs/chrome-bookmarks.md).

Reads local_bookmarks.yml at the repo root (gitignored, outside group_vars so Ansible never loads it; see
local_bookmarks.yml.example), validates it and writes ~/.cache/laptop-setup/chrome-bookmarks-<profile>.html (mode 0600),
each profile's tree inside one top-level folder. Never reads or writes Chrome's own profile files.
"""
import argparse
import html
import os
from pathlib import Path
import re
import unicodedata
import sys
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'local_bookmarks.yml'
FOLDER = 'laptop-setup'  # the one top-level folder an import creates; replaced as a whole on update
MAX_DEPTH = 5
MAX_ITEMS = 2000
MAX_NAME = 200


class Invalid(Exception):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    """safe_load that refuses a repeated key instead of keeping only the last one."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        for key in keys:
            if keys.count(key) > 1:
                raise yaml.constructor.ConstructorError(None, None, f'repeated key {key!r}', node.start_mark)
        return super().construct_mapping(node, deep=deep)


def check_items(items, where, depth, count):
    if not isinstance(items, list):
        raise Invalid(f'{where}: must be a list')
    if depth > MAX_DEPTH:
        raise Invalid(f'{where}: folders nest deeper than {MAX_DEPTH}')
    for index, item in enumerate(items):
        place = f'{where}[{index}]'
        count[0] += 1
        if count[0] > MAX_ITEMS:
            raise Invalid(f'more than {MAX_ITEMS} bookmarks and folders')
        if not isinstance(item, dict):
            raise Invalid(f'{place}: must be a mapping')
        if 'folder' in item:
            if set(item) != {'folder', 'items'}:
                raise Invalid(f'{place}: a folder has exactly the keys folder and items')
            check_name(item['folder'], place)
            check_items(item['items'], f"{place} ({item['folder']})", depth + 1, count)
        else:
            if set(item) != {'name', 'url'}:
                raise Invalid(f'{place}: a bookmark has exactly the keys name and url')
            check_name(item['name'], place)
            check_url(item['url'], place)


BAD_CHARS = r'[\x00-\x1f\x7f-\x9f\u2028\u2029]'  # C0/C1 controls and line separators: they break the file or YAML


def is_text(value):
    try:
        value.encode('utf-8')  # a lone surrogate cannot be written
    except UnicodeEncodeError:
        return False
    return True


def check_name(name, place):
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or re.search(BAD_CHARS, name)
            or not is_text(name)):
        raise Invalid(f'{place}: name must be 1-{MAX_NAME} printable characters (quote one that YAML reads as a number)')


def check_url(url, place):
    # http(s) only: javascript:, data: and file: bookmarks run code or read local files when clicked
    if not isinstance(url, str) or re.search(r'[\x00-\x20]', url) or re.search(BAD_CHARS, url):
        raise Invalid(f'{place}: url must be a string without spaces or control characters')
    if not is_text(url):
        raise Invalid(f'{place}: url must be valid text')
    if any(unicodedata.category(c) == 'Cf' for c in url):  # bidi overrides, zero-width marks: a URL that misleads
        raise Invalid(f'{place}: url must not contain invisible formatting characters')
    try:
        parts = urlsplit(url)
        parts.port  # raises on a port that is not a number from 0 to 65535
    except ValueError as error:
        raise Invalid(f'{place}: url does not parse ({error})') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise Invalid(f'{place}: url must be http:// or https:// with a host')
    if parts.username or parts.password:
        raise Invalid(f'{place}: url must not carry credentials')


def load(path):
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise Invalid(f'{path} not found: run make chrome-bookmarks-export, or copy {path.name}.example next to it '
                      'and edit it') from None
    except UnicodeDecodeError:
        raise Invalid(f'{path}: not UTF-8 text') from None
    return load_text(text, path)


def load_text(text, path):
    """Validate the YAML text of a bookmarks file; path is only named in errors."""
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)  # noqa: S506 (a SafeLoader)
    except yaml.YAMLError as error:
        raise Invalid(f'{path}: not valid YAML ({error})') from None
    if not isinstance(data, dict) or set(data) != {'chrome_bookmarks'} or not isinstance(data['chrome_bookmarks'], dict):
        raise Invalid(f'{path}: must hold one key, chrome_bookmarks, mapping each profile to its bookmarks')
    profiles = data['chrome_bookmarks']
    if not profiles:
        raise Invalid(f'{path}: chrome_bookmarks names no profile')
    for profile, items in profiles.items():
        if not isinstance(profile, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', profile):
            raise Invalid(f'profile name {profile!r}: lowercase letters, digits and hyphens')
        check_items(items, profile, 1, [0])
        if not items:
            raise Invalid(f'profile {profile}: no bookmarks')
    return profiles


def escape(text):
    # Chrome's importer decodes only &lt; &gt; &amp; &quot; &#39; (not html.escape's &#x27;)
    return html.escape(text, quote=True).replace('&#x27;', '&#39;')


def render(items, indent=1):
    pad = '    ' * indent
    lines = [f'{pad[:-4]}<DL><p>']
    for item in items:
        if 'folder' in item:
            lines.append(f'{pad}<DT><H3>{escape(item["folder"])}</H3>')
            lines += render(item['items'], indent + 1)
        else:
            lines.append(f'{pad}<DT><A HREF="{escape(item["url"])}">{escape(item["name"])}</A>')
    lines.append(f'{pad[:-4]}</DL><p>')
    return lines


def document(items):
    return '\n'.join(['<!DOCTYPE NETSCAPE-Bookmark-file-1>',
                      '<!-- Generated by laptop-setup make chrome-bookmarks; edit local_bookmarks.yml, not this file -->',
                      '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
                      '<TITLE>Bookmarks</TITLE>', '<H1>Bookmarks</H1>']
                     + render([{'folder': FOLDER, 'items': items}])) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--output-dir', type=Path, default=Path.home() / '.cache/laptop-setup')
    args = parser.parse_args()
    try:
        profiles = load(args.source)
    except Invalid as error:
        print(f'chrome-bookmarks: {error}', file=sys.stderr)
        return 1
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for profile, items in profiles.items():
        target = args.output_dir / f'chrome-bookmarks-{profile}.html'
        temporary = target.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(document(items))
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        print(f'{profile}: {target} — in that Chrome profile, delete the previous import (the "{FOLDER}" folder, or the '
              '"Imported" folder holding it), then Settings > Import bookmarks and settings > Bookmarks HTML file; '
              'Chrome Sync carries it to your other machines')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
