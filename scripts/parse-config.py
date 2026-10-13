#!/usr/bin/env python3
"""Parse raw config YAML, including !unsafe tags, without evaluating templates."""
import datetime
import json
import sys

from ansible.parsing.dataloader import DataLoader


def json_default(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError("config.yml contains a value that cannot be represented as JSON")


def parse_config(content):
    # DataLoader.load parses YAML; unlike include_vars it does not grant trust
    # to template expressions. JSON serialization never invokes the templar.
    data = DataLoader().load(content, show_content=False)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError("config.yml must contain a mapping")
    return json.dumps(data, default=json_default)


if __name__ == "__main__":
    try:
        print(parse_config(sys.stdin.read()))
    except Exception as error:
        print(f"Cannot parse config.yml: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
