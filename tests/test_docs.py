"""The documentation against the code it describes (v0.7.5).

A table written by hand next to a table in the code drifts apart
within a version; these tests make the drift a failing build instead of
a wrong page nobody notices.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moonlan import access
from moonlan.config import load_config

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# how the docs write the one key that is not "METHOD /path"
STATIC_ROW = ("GET", "/{file}")


def api_table() -> dict[str, str]:
    """"METHOD /path" -> role, from the table between the markers in
    docs/API.md."""
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    block = text.split("<!-- routes:begin -->", 1)[1].split(
        "<!-- routes:end -->", 1
    )[0]
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in ("Method", "---"):
            continue
        method, route, role = cells
        path = re.search(r"`([^`]+)`", route).group(1)
        key = (access.STATIC if (method, path) == STATIC_ROW
               else f"{method} {path}")
        rows[key] = role
    return rows


class ApiTableTest(unittest.TestCase):
    def test_the_table_in_the_docs_is_the_table_in_the_code(self):
        documented = api_table()
        self.assertEqual(
            sorted(set(access.RULES) - set(documented)), [],
            "routes in moonlan/access.py missing from docs/API.md",
        )
        self.assertEqual(
            sorted(set(documented) - set(access.RULES)), [],
            "routes in docs/API.md that moonlan/access.py does not have",
        )
        wrong = {
            key: (documented[key], access.RULES[key])
            for key in access.RULES if documented[key] != access.RULES[key]
        }
        self.assertEqual(wrong, {}, "docs role != code role")

    def test_no_page_says_there_is_no_sign_in(self):
        for page in [ROOT / "README.md", ROOT / "SECURITY.md",
                     *DOCS.glob("*.md")]:
            text = page.read_text(encoding="utf-8").lower()
            self.assertNotIn("no authentication layer", text, page.name)


class ConfigurationKeysTest(unittest.TestCase):
    def test_every_key_the_loader_reads_is_documented(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "config.yaml"
            empty.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(empty)}):
                cfg = load_config()
        keys = [key for key, _, _ in cfg.report.values]
        self.assertGreater(len(keys), 50)
        text = (DOCS / "CONFIGURATION.md").read_text(encoding="utf-8")
        missing = [key for key in keys if f"`{key}`" not in text]
        self.assertEqual(
            missing, [],
            "config keys docs/CONFIGURATION.md does not describe",
        )


class LinksTest(unittest.TestCase):
    """Every relative link in the Markdown pages points at a file that
    exists: moving a section out of the README must not leave an
    address behind that leads nowhere."""

    LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

    def test_relative_links_resolve(self):
        pages = [*ROOT.glob("*.md"), *DOCS.glob("*.md")]
        broken = []
        for page in pages:
            for target in self.LINK.findall(page.read_text(encoding="utf-8")):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                if not (page.parent / path).exists():
                    broken.append(f"{page.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
