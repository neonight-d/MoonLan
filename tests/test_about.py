"""About (v0.8.1): which build is running, what it runs on, and the
server's own facts for an administrator only."""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import __version__, about, server, signin
from moonlan.access import Principal

ROOT = Path(__file__).resolve().parent.parent
SHA = "317bd0c2f5a1e9d0c4b7a8e6f3d2c1b0a9e8d7c6"
OTHER = "d47f2de0000000000000000000000000000000aa"


class ApiTest(unittest.TestCase):
    def ask(self, who, sign_in_on=True):
        token = signin._current.set(who)
        try:
            with mock.patch.object(server.sign_in, "sign_in_on", lambda: sign_in_on):
                return asyncio.run(server.api_about())
        finally:
            signin._current.reset(token)

    def test_the_server_block_for_an_administrator_only(self):
        for who in (Principal("v", "viewer"), Principal("u", "user")):
            with self.subTest(role=who.role):
                self.assertNotIn("server", self.ask(who))
        block = self.ask(Principal("a", "admin"))["server"]
        self.assertEqual(set(block), {"python", "os", "started_at", "uptime", "sign_in", "https"})
        self.assertIs(block["sign_in"], True)
        self.assertIn(block["https"], ("own", "proxy", "off"))
        # sign-in off: everybody is the administrator
        self.assertIs(self.ask(None, sign_in_on=False)["server"]["sign_in"], False)

    def test_what_everybody_sees(self):
        body = self.ask(Principal("v", "viewer"))
        self.assertEqual(body["version"], __version__)
        self.assertEqual(body["copyright"], "2026 neonight-d")
        self.assertEqual({"loaded", "date", "command"}, set(body["ieee"]))
        names = [c["name"] for c in body["components"]]
        self.assertEqual(names, ["FastAPI", "Starlette", "Uvicorn", "pysnmp", "PyYAML",
                                 "fido2", "cryptography", "vis-network"])
        fastapi = body["components"][0]
        self.assertTrue(fastapi["version"])  # installed: the tests run on it

    def test_a_package_that_is_not_installed(self):
        missing = about.importlib.metadata.PackageNotFoundError
        with mock.patch.object(about.importlib.metadata, "version", side_effect=missing):
            rows = about.components()
        self.assertTrue(all(r["version"] is None for r in rows if r["where"] == "server"))
        self.assertEqual(rows[-1]["version"], "9.1.9")

    def test_the_licence(self):
        answer = call(server.app, "GET", "/api/about/license")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.body.decode("utf-8"), (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertTrue(answer.header("content-type")[0].startswith("text/plain"))


class BuildTest(unittest.TestCase):
    """The commit, read out of .git without running git"""

    def clone(self, head="ref: refs/heads/main\n"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".git" / "refs" / "heads").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(head)
        return root

    def test_a_branch_in_its_own_file(self):
        root = self.clone()
        (root / ".git" / "refs" / "heads" / "main").write_text(SHA + "\n")
        self.assertEqual(about.build_hash(root), SHA[:7])

    def test_a_branch_in_packed_refs(self):
        root = self.clone()
        (root / ".git" / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{OTHER} refs/heads/other\n"
            f"{SHA} refs/heads/main\n"
            f"^{OTHER}\n"
        )
        self.assertEqual(about.build_hash(root), SHA[:7])

    def test_the_branch_file_wins_over_packed_refs(self):
        root = self.clone()
        (root / ".git" / "packed-refs").write_text(f"{OTHER} refs/heads/main\n")
        (root / ".git" / "refs" / "heads" / "main").write_text(SHA + "\n")
        self.assertEqual(about.build_hash(root), SHA[:7])

    def test_a_detached_head(self):
        self.assertEqual(about.build_hash(self.clone(SHA + "\n")), SHA[:7])

    def test_a_worktree(self):
        main = self.clone()
        (main / ".git" / "refs" / "heads" / "feature").write_text(SHA + "\n")
        tree = main / ".git" / "worktrees" / "feature"
        tree.mkdir(parents=True)
        (tree / "HEAD").write_text("ref: refs/heads/feature\n")
        (tree / "commondir").write_text("../..\n")
        checkout = main / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {tree}\n")
        self.assertEqual(about.build_hash(checkout), SHA[:7])

    def test_no_clone_no_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(about.build_hash(Path(tmp)))
        # a branch nobody has committed to yet
        self.assertIsNone(about.build_hash(self.clone()))
        # not a hash
        root = self.clone()
        (root / ".git" / "refs" / "heads" / "main").write_text("garbage\n")
        self.assertIsNone(about.build_hash(root))


class LicenceTableTest(unittest.TestCase):
    def test_every_requirement_has_its_licence(self):
        def key(name):
            return re.sub(r"[-_.]+", "-", name).lower()

        required = set()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                required.add(key(re.match(r"[A-Za-z0-9_.-]+", line).group(0)))
        table = {key(dist): licence for _, dist, licence in about.COMPONENTS}
        self.assertEqual(sorted(required - set(table)), [])
        for licence in table.values():
            self.assertRegex(licence, r"^(MIT|BSD-[23]-Clause|Apache-2\.0|MPL-2\.0)( (OR|AND) |, |$)")

    def test_the_browser_component_is_the_one_the_page_loads(self):
        index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for name, version, _ in about.BROWSER_COMPONENTS:
            # served by MoonLan itself since v0.8.3
            self.assertIn(f'src="vendor/{name}-{version}/', index)
            self.assertTrue((ROOT / "web" / "vendor" / f"{name}-{version}").is_dir())

    def test_the_copyright_line_is_the_licence_file_s(self):
        self.assertEqual(about.copyright_line("MIT License\n\nCopyright (c) 2026 neonight-d\n"),
                         "2026 neonight-d")
        self.assertIsNone(about.copyright_line("no such line"))


if __name__ == "__main__":
    unittest.main()
