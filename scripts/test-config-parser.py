#!/usr/bin/env python3
"""Regression coverage for raw YAML parsing before template-injection checks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("config_parser", Path(__file__).with_name("parse-config.py"))
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class ConfigParserTests(unittest.TestCase):
    def parse(self, content):
        return json.loads(parser.parse_config(content))

    def test_preserves_yaml_types_and_unsafe_literals(self):
        self.assertEqual(
            self.parse("name: !unsafe 'Valid User'\nenabled: true\ncount: 3\npaths: [one, two]\n"),
            {"name": "Valid User", "enabled": True, "count": 3, "paths": ["one", "two"]},
        )

    def test_keeps_template_markers_without_executing_lookups(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            payload = "{{ lookup('pipe', 'touch " + str(marker) + "') }}"
            for content in (
                'value: "' + payload + '"\n',
                'value: !unsafe "' + payload + '"\n',
                'value: ["' + payload + '"]\n',
                'nested:\n  value: "' + payload + '"\n',
            ):
                with self.subTest(content=content):
                    self.assertIn(payload, json.dumps(self.parse(content)))
                    self.assertFalse(marker.exists())

    def test_rejects_python_object_tags(self):
        with self.assertRaises(Exception):
            self.parse("value: !!python/object/apply:os.getpid []\n")

    def test_preserves_literal_unsafe_word_inside_a_string(self):
        self.assertEqual(self.parse("value: 'literal !unsafe text'"), {"value": "literal !unsafe text"})

    def test_empty_document_is_an_empty_mapping(self):
        self.assertEqual(self.parse(""), {})

    def test_rejects_non_mapping_documents(self):
        for content in ("[one, two]", "123", "some text"):
            with self.subTest(content=content), self.assertRaises(TypeError):
                self.parse(content)


if __name__ == "__main__":
    unittest.main()
