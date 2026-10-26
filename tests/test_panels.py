"""Every panel and dialog follows the language (v0.8.2).

setLang used to name the panels it redrew one by one, and four that
came later — About, notes without a node, one's own account, the export
window — changed their headings and kept their contents in the other
language. Each now registers what redraws it; this holds the
registrations to the page, so a panel added to index.html without one
fails here instead of reaching the operator."""

import re
import unittest

from web_source import APP, INDEX, function


def panel_ids():
    """The ids of every .panel and every dialog of index.html"""
    ids = []
    for tag in re.findall(r"<div\b[^>]*>", INDEX):
        found = re.search(r'\bid="([^"]+)"', tag)
        panel = re.search(r'\bclass="[^"]*\bpanel\b', tag) or 'role="dialog"' in tag
        if found and panel:
            ids.append(found.group(1))
    return ids


def els_key(element_id):
    """The name app.js gives the element in `els`"""
    found = re.search(rf'^  (\w+): document\.getElementById\("{re.escape(element_id)}"\),$',
                      APP, re.M)
    return found and found.group(1)


class RedrawTest(unittest.TestCase):
    def test_every_panel_and_dialog_is_redrawn(self):
        ids = panel_ids()
        # ten panels and two dialogs (sign-in, export) as of v0.8.2
        self.assertGreaterEqual(len(ids), 12)
        for element_id in ids:
            with self.subTest(element_id):
                key = els_key(element_id)
                self.assertTrue(key, f"#{element_id} is not in els")
                self.assertIn(f"redrawOnLanguage(els.{key},", APP)

    def test_set_lang_asks_every_open_one(self):
        body = function("setLang")
        self.assertIn("for (const [panel, redraw] of panelRedraws)", body)
        self.assertIn('if (!panel.classList.contains("hidden")) redraw();', body)
        # no panel named by hand any more
        self.assertNotIn("renderJournal", body)

    def test_the_export_window_keeps_what_was_picked(self):
        redraw = function("redrawExportDialog")
        self.assertIn("renderExportDialog(exportPicked(form))", redraw)
        # a default title is a default in either language
        self.assertIn("Object.values(I18N).some", function("defaultSheetTitle"))


if __name__ == "__main__":
    unittest.main()
