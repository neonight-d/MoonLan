"""One account's actions in Users: one ⋯ and a menu built from data
(v0.7.8). The page's JavaScript is not run here; what can be held to
is held: the items and their order, the reasons in both languages, and
that the menu is the shared one — the same builder and the same keys
as the header's Actions."""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


def strings(lang):
    body = re.search(rf"^  {lang}: {{$(.*?)^  }},$", I18N, re.S | re.M).group(1)
    return set(re.findall(r'^    "?([\w+.-]+)"?:', body, re.M))


def function(name):
    body = APP[APP.index(f"function {name}("):]
    return body[:body.index("\n}\n")]


class UserMenuTest(unittest.TestCase):
    def test_the_items_in_their_order(self):
        items = function("userMenuItems")
        keys = re.findall(r'\{ key: "(\w+)", group: "(\w+)"', items)
        self.assertEqual(
            [k for k, _ in keys],
            ["password", "totp", "keys", "sessions", "unlock", "enable",
             "disable", "delete"],
        )
        # a line before Enable/Disable, and Delete last on its own
        groups = dict(keys)
        self.assertEqual((groups["sessions"], groups["disable"],
                          groups["delete"]), ("account", "state", "danger"))
        self.assertIn('const USER_GROUPS = ["account", "state", "danger"];', APP)

    def test_what_cannot_be_done_says_why(self):
        items = function("userMenuItems")
        for reason in ("reasonNoTotp", "reasonNoKeys", "reasonNoSessions",
                       "reasonNotLocked", "reasonYourAccount",
                       "reasonLastAdmin"):
            self.assertIn(f't("{reason}")', items)
        # one's own account and the last administrator: neither disabled
        # nor deleted from here
        self.assertEqual(items.count("disabled: kept"), 2)

    def test_the_shared_menu(self):
        opened = function("openUserMenu")
        self.assertIn("menuButtons(userMenuItems(", opened)
        self.assertIn("menuKeys(event, els.userMenu, closeUserMenu)", APP)
        self.assertIn('id="user-menu" class="context-menu hidden" role="menu"',
                      (WEB / "index.html").read_text(encoding="utf-8"))

    def test_no_row_of_buttons_left(self):
        rows = function("renderUsers")
        self.assertNotIn('class: "acts"', rows)
        self.assertIn('class: "user-more"', rows)
        # a key's own "Remove" stays on the key's line
        self.assertIn("keyList(keys, removeKey)", rows)

    def test_words_in_both_languages(self):
        for lang in ("en", "ru"):
            for key in ("userMenuHint", "userMenuFor", "reasonYourAccount",
                        "reasonLastAdmin", "reasonNoTotp", "reasonNoKeys",
                        "reasonNoSessions", "reasonNotLocked"):
                self.assertIn(key, strings(lang), f"{lang}: {key}")


if __name__ == "__main__":
    unittest.main()
