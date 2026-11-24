"""The log in UTF-8 whatever the locale (v0.8.9).

Python writes stderr in the locale's encoding with `backslashreplace`,
so under an 8-bit locale (LANG=en_US is ISO-8859-1) an em dash in
MoonLan's lines becomes the text `\\u2014` and a section sign the byte
0xA7, which journalctl shows as `\\xa7`. PEP 538 and PEP 540 act only
on the C and POSIX locales. The service and diag make their streams
UTF-8 before the first line.

(v0.8.10: the `\\u2014` that started this came from copying a
terminal's output, not from the journal, which runs under
LANG=en_US.UTF-8 there. The case is real under LANG=en_US all the same,
and the first test below shows it.)

Each case runs in a process of its own with PYTHONIOENCODING=ascii,
which is what such a locale amounts to — and which a test can set on
any machine, where a missing locale would fall back to C.UTF-8.

Run with:  python -m unittest discover -s tests
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = "—".encode("utf-8")
SECTION = "§".encode("utf-8")


def run(code_or_args, tmp, extra_env=None):
    config = Path(tmp) / "config.yaml"
    config.write_text(f"db_path: {Path(tmp) / 'moonlan.db'}\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "ascii", "MOONLAN_CONFIG": str(config),
           "PYTHONPATH": str(ROOT), **(extra_env or {})}
    env.pop("PYTHONUTF8", None)
    args = [sys.executable, *code_or_args] if isinstance(code_or_args, list) else \
        [sys.executable, "-c", code_or_args]
    return subprocess.run(args, cwd=tmp, env=env, capture_output=True, timeout=120)


class HelperTest(unittest.TestCase):
    def test_a_stream_in_ascii_becomes_utf8(self):
        from moonlan.textio import use_utf8
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="backslashreplace")
        self.assertEqual(use_utf8(stream), ["ascii"])
        stream.write("a — b § c")
        stream.flush()
        self.assertEqual(raw.getvalue(), "a — b § c".encode("utf-8"))
        self.assertEqual(stream.errors, "backslashreplace")
        # already UTF-8, or not a text stream: left alone
        self.assertEqual(use_utf8(stream, object()), [])


class ProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_without_the_fix_it_is_what_the_journal_showed(self):
        done = run("import logging; logging.basicConfig(); "
                   "logging.getLogger('moonlan').warning('a %s b %s c', chr(0x2014), chr(0xa7))",
                   self.tmp)
        self.assertIn(b"\\u2014", done.stderr)
        self.assertNotIn(DASH, done.stderr)

    def test_the_service_log(self):
        done = run("from moonlan import server; import logging; server.configure_logging(); "
                   "logging.getLogger('moonlan').warning('a %s b %s c', chr(0x2014), chr(0xa7))",
                   self.tmp, {"MOONLAN_DEMO": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"a " + DASH + b" b " + SECTION + b" c", done.stderr)
        self.assertNotIn(b"\\u2014", done.stderr)
        self.assertNotIn(b"\\xa7", done.stderr)

    def test_run_py_before_anything(self):
        done = run("import sys; sys.path.insert(0, %r); import run; "
                   "print(sys.stdout.encoding, sys.stderr.encoding)" % str(ROOT), self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), [b"utf-8", b"utf-8"])

    def test_diag(self):
        done = run(["-m", "moonlan.diag", "--oui", "02:00:00:00:00:01"], self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"none " + DASH + b" a random", done.stdout)
        self.assertNotIn(b"\\u2014", done.stdout)


if __name__ == "__main__":
    unittest.main()
