"""The release number: VERSION is a valid version, /api/status reports it to members/admin/the panel
and hides it from visitors (it would help an attacker pick exploits).
Run: python3.11 tests/test_version.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))


class Version(unittest.TestCase):
    def test_version_file_is_semver(self):
        v = (REPO / "VERSION").read_text().strip()
        self.assertRegex(v, r"^[0-9]+(?:\.[0-9]+){2,3}(?:[-+][0-9A-Za-z.-]+)?$")   # 3 numbers, or 4 for follow-ups

    def test_server_reads_it(self):
        import server
        self.assertEqual(server.software_version(), (REPO / "VERSION").read_text().strip())

    def test_visitors_do_not_get_it(self):
        import visitor
        self.assertIn("version", visitor.PRIVATE_STATUS_KEYS)

    def test_changelog_mentions_it(self):
        v = (REPO / "VERSION").read_text().strip()
        major_minor = ".".join(v.split(".")[:2])
        text = (REPO / "CHANGELOG.md").read_text()
        self.assertTrue(re.search(r"^## v" + re.escape(major_minor) + r"\b", text, re.M),
                        "CHANGELOG.md has no section for this VERSION")


if __name__ == "__main__":
    unittest.main(verbosity=1)
