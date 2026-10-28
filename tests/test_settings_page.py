"""Settings and My notifications in the page (v0.8.4), as far as the
source shows them: where they are, that they follow the language, ask
the server no more than they should, and that every word they use is
there in both languages."""

import re
import unittest
from pathlib import Path

from moonlan import subscriptions
from web_source import APP, INDEX, STYLE, function, strings

ROOT = Path(__file__).resolve().parent.parent


def section(start, end):
    """The page's code from one heading to the next"""
    at = APP.index(start)
    return APP[at:APP.index(end, at)]


SETTINGS = section("/* ---------- Settings (v0.8.4) ----------",
                   "/* ---------- My notifications (v0.8.4) ----------")
MINE = section("/* ---------- My notifications (v0.8.4) ----------",
               "/* ---------- a node's name and note (v0.8.0) ----------")


class PlaceTest(unittest.TestCase):
    def test_in_the_actions_menu_above_about_for_everybody(self):
        items = function("moreItems")
        settings_at = items.index('key: "settings"')
        self.assertLess(settings_at, items.index('key: "about"'))
        line = items[settings_at:items.index("\n", settings_at)]
        self.assertNotIn("disabled", line)
        self.assertIn('<div id="settings" class="panel hidden">', INDEX)

    def test_redrawn_in_the_other_language(self):
        self.assertIn("redrawOnLanguage(els.settings, renderSettings);", APP)
        # what was typed is kept: the fields are drawn from the drafts
        self.assertIn("draftOf(s)", function("settingField"))

    def test_my_notifications_first(self):
        render = function("renderSettings")
        self.assertIn('foldSection("mine", open, myNotificationsParts(), false)', render)
        self.assertLess(render.index('"mine"'), render.index("settingsData.sections.map"))

    def test_closing_with_changes_asks(self):
        self.assertIn('window.confirm(t("confirmDiscardSettings"))', function("closeSettings"))

    def test_a_section_saves_in_one_request_once_at_a_time(self):
        save = function("saveSettingsSection")
        self.assertIn('return busy("settings:" + section', save)
        self.assertEqual(save.count('api("/api/settings"'), 1)

    def test_scan_now_after_a_change_a_scan_applies(self):
        save = function("saveSettingsSection")
        self.assertIn('s.applies === "next_scan" || s.section === "polling"', save)
        self.assertIn("rescan();", SETTINGS)

    def test_phone_fields_are_16px(self):
        rule = '.set-input input[type="text"], .set-input input[type="email"] { font-size: 16px; }'
        self.assertIn(rule, STYLE)
        # after the field's own rule, which it has to win over
        self.assertGreater(STYLE.index(rule), STYLE.index('.set-input input[type="text"], .set-input input[type="email"] {\n'))


class LinkTest(unittest.TestCase):
    def test_asked_every_three_seconds_only_while_open(self):
        watch = function("watchMyLink")
        self.assertIn("}, 3000);", watch)
        self.assertEqual(watch.count('els.settings.classList.contains("hidden")'), 2)
        self.assertIn('state !== "waiting" && state !== "group"', watch)

    def test_requests_carry_the_language(self):
        self.assertIn("return jsonPost(\"\", { lang: lang, ...extra });", function("myBody"))


class WordsTest(unittest.TestCase):
    def keys_used(self, code):
        # a key built at run time ("al_" + type) is checked by its values
        return {k for k in re.findall(r'\b(?:t|fmt)\("(\w+)"', code)
                if not k.endswith("_")}

    def test_every_word_in_both_languages(self):
        used = self.keys_used(SETTINGS + MINE + function("settingsEventText"))
        used |= {f"channelWhy_{why}" for why in ("off", "no_server", "no_token")}
        used |= {f"setErrType_{kind}" for kind in ("int", "float", "bool", "list", "table")}
        used |= {f"ch_{c}" for c in ("email", "telegram", "syslog")}
        used |= {"setSection_mine"}
        # every refusal of the personal API is worded
        source = (ROOT / "moonlan" / "subscriptions.py").read_text(encoding="utf-8")
        for code in set(re.findall(r'Refused\("(\w+)"', source)) - {"channel_off", "unknown_channel"}:
            used.add("myErr_" + code)
        for lang in ("en", "ru"):
            words = strings(lang)
            with self.subTest(lang):
                self.assertEqual(sorted(k for k in used if not words.get(k)), [])

    def test_the_mail_and_the_bot_speak_both(self):
        self.assertEqual(set(subscriptions.MESSAGES["en"]), set(subscriptions.MESSAGES["ru"]))
        self.assertIn("просто удалите письмо", subscriptions.MESSAGES["ru"]["code_body"])


if __name__ == "__main__":
    unittest.main()
