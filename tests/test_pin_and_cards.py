"""Pin and Unpin, and short cards (v0.7.9).

One verb for one action: a node is pinned and unpinned, in the menu,
the header, the hints and the journal. A card's explanation is one
line that names the fact; the full text is its tooltip and, behind ⓘ,
a paragraph that opens in the card."""

import re
import unittest

from web_source import APP, INDEX, STYLE, function, strings

EN = strings("en")
RU = strings("ru")
PLACEHOLDER = re.compile(r"\{\w+\}")


class PinWordsTest(unittest.TestCase):
    def test_no_placing_or_releasing_in_russian(self):
        for key, text in RU.items():
            for word in ("Поставить здесь", "Отпустить", "Отпущено",
                         "поставлено руками", "Поставлено руками",
                         "поставленные руками", "поставил человек"):
                self.assertNotIn(word, text, key)

    def test_no_placing_or_releasing_in_english(self):
        for key, text in EN.items():
            for word in ("Place here", "placed by hand", "Placed by hand",
                         "Release this node"):
                self.assertNotIn(word, text, key)

    def test_the_words(self):
        self.assertEqual((EN["menuPin"], EN["menuUnpin"]), ("Pin", "Unpin"))
        self.assertEqual((RU["menuPin"], RU["menuUnpin"]),
                         ("Закрепить", "Открепить"))
        self.assertEqual((RU["ev_layout_pinned"], RU["ev_layout_released"]),
                         ("Закреплено", "Откреплено"))
        self.assertEqual(RU["arrangePinned"], "Закреплено: {n}.")
        self.assertEqual(RU["layoutPinnedMark"], "закреплено: {n}")


def hint_keys():
    """Every key a card shows through hintKey(), and the one it builds
    by hand (the approximate place, with its "why" joined on)"""
    keys = set(re.findall(r'hintKey\(\s*"(\w+)"', APP))
    # the switch card picks one of two by the number of slow scans, and
    # the bridge card one of four: the keys are names in a ternary
    keys |= {"staleSwitchHint", "overBudgetHint", "externalHint",
             "bridgeAssumedHint", "bridgeSharesPortHint", "bridgeHint"}
    keys.add("approximateHint")
    return keys


class ShortCardsTest(unittest.TestCase):
    def test_the_keys(self):
        # the operator's list, all of them on hintKey/hintBlock now
        self.assertTrue({
            "pinnedHint", "loopUnsupportedHint", "staleSwitchHint",
            "overBudgetHint", "hostFromSavedHint", "linkOrderUnknown",
            "linkCycleUnresolved", "bridgeAssumedHint", "externalHint",
            "lldpForwardedHint", "bridgeSharesPortHint", "lldpCrowdedHint",
            "offlineGroupGuessHint", "uplinkOnlyHint", "nameFromLldpHint",
            "approximateHint", "rememberedHint", "bridgeHint",
            "unlocatedHint", "stpBlockingHint", "staleButAliveHint",
        } <= hint_keys())

    def test_every_hint_has_a_short_line_in_both_languages(self):
        for lang, texts in (("en", EN), ("ru", RU)):
            for key in sorted(hint_keys()):
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, texts)
                    self.assertIn(key + "Short", texts)
                    short, full = texts[key + "Short"], texts[key]
                    self.assertLess(len(short), len(full))
                    # the same values are substituted into both
                    self.assertLessEqual(set(PLACEHOLDER.findall(short)),
                                         set(PLACEHOLDER.findall(full)))

    def test_the_pinned_card(self):
        self.assertEqual(RU["pinnedHintShort"], "Этот узел закреплён")
        self.assertEqual(
            RU["pinnedHint"],
            "Этот узел закреплён вручную пользователем. Он не подвергается "
            "физике, а его расположение сохраняется на сервере карты.",
        )
        self.assertIn('hintKey("pinnedHint")', function("showDetails"))

    def test_the_markup(self):
        block = function("hintBlock")
        # the tooltip is plain text, escaped by html`…` (v0.8.3)
        self.assertIn('return html`<p class="hint" title="${longText}">', block)
        self.assertIn('class="hint-more" aria-expanded="false"', block)
        self.assertIn('aria-label="${t("hintMore")}"', block)
        self.assertIn('class="hint hint-full hidden"', block)
        self.assertIn('type="button"', block)  # Enter and Space for free
        for texts in (EN, RU):
            self.assertIn("hintMore", texts)
        # one listener on the card opens and closes the full text
        self.assertIn('event.target.closest(".hint-more")', APP)
        self.assertIn('more.setAttribute("aria-expanded", String(open))', APP)
        self.assertIn("#details .hint-more", STYLE)

    def test_no_long_paragraph_left_on_the_cards(self):
        cards = function("showDetails") + function("showLinkDetails")
        for key in hint_keys() - {"approximateHint"}:
            self.assertNotIn(f'<p class="hint">${{t("{key}")}}', cards, key)
            self.assertNotIn(f'<p class="hint">${{fmt("{key}"', cards, key)


class PhoneListTest(unittest.TestCase):
    def test_the_bar(self):
        self.assertIn('id="sidebar-toggle"', INDEX)
        self.assertIn('aria-controls="sidebar" aria-expanded="true"', INDEX)
        # before the list, so between the header and the map on a phone
        self.assertLess(INDEX.index('id="sidebar-toggle"'),
                        INDEX.index('<aside id="sidebar">'))
        for texts in (EN, RU):
            self.assertIn("sidebarToggle", texts)
        self.assertEqual(RU["sidebarToggle"], "Список и поиск")

    def test_only_on_a_phone(self):
        self.assertIn(".sidebar-toggle { display: none; }", STYLE)
        phone = STYLE[STYLE.index("@media (max-width: 720px)"):]
        phone = phone[:phone.index("\n}\n")]
        self.assertIn(".sidebar-toggle {", phone)
        self.assertIn("main.sidebar-collapsed #sidebar { display: none; }", phone)

    def test_kept_and_resized(self):
        self.assertIn('const SIDEBAR_KEY = "moonlan-sidebar-collapsed";', APP)
        toggle = function("toggleSidebar")
        self.assertIn('network.setSize("100%", "100%")', toggle)
        self.assertIn("network.getViewPosition()", toggle)
        apply = function("applySidebarCollapsed")
        self.assertIn('"aria-expanded"', apply)


if __name__ == "__main__":
    unittest.main()
