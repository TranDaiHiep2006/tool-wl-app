"""Tests for `server.textnorm` — Vietnamese-safe string matching (P5).

The concrete bug this module prevents: a window titled
"lịch thi.xlsx - Excel" can come back from the OS in NFD (decomposed,
with combining diacritics) while the agent types the NFC spelling.
Naive `==` / `.lower()` comparison then fails and the tool reports
"window not found" for a window that is plainly open in front of the user.
"""

from __future__ import annotations

import os
import sys
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server.textnorm import (  # noqa: E402
    contains_text,
    nfc,
    nfc_fold,
    same_text,
)

# Real-world samples from the P5 brief.
TITLE_EXCEL = "lịch thi.xlsx - Excel"
TITLE_WORD = "zhezhi.docx - Word"
SENTENCE = "Hà Nội những ngày đầu đông, sương mù giăng kín lối cũ."


class TestNFC(unittest.TestCase):
    def test_ascii_is_unchanged(self):
        self.assertEqual(nfc("l?ch thi.xlsx - Excel"), "l?ch thi.xlsx - Excel")

    def test_none_is_empty_string(self):
        self.assertEqual(nfc(None), "")
        self.assertEqual(nfc(""), "")

    def test_composes_decomposed_text(self):
        decomposed = unicodedata.normalize("NFD", TITLE_EXCEL)
        self.assertNotEqual(decomposed, TITLE_EXCEL)
        self.assertEqual(nfc(decomposed), TITLE_EXCEL)

    def test_is_idempotent(self):
        once = nfc(unicodedata.normalize("NFD", SENTENCE))
        self.assertEqual(nfc(once), once)

    def test_vietnamese_sentence_round_trips_through_nfc(self):
        decomposed = unicodedata.normalize("NFD", SENTENCE)
        self.assertEqual(nfc(decomposed), SENTENCE)


class TestNFCFold(unittest.TestCase):
    def test_case_insensitive_ascii(self):
        self.assertEqual(nfc_fold("NOTEPAD"), nfc_fold("notepad"))

    def test_case_insensitive_vietnamese(self):
        self.assertEqual(nfc_fold("LỊCH THI"), nfc_fold("lịch thi"))

    def test_nfc_applied_before_casefold(self):
        # The order matters: fold the decomposed haystack to the same key.
        decomposed = unicodedata.normalize("NFD", TITLE_EXCEL)
        self.assertEqual(nfc_fold(decomposed), nfc_fold(TITLE_EXCEL))

    def test_none_and_empty(self):
        self.assertEqual(nfc_fold(None), "")
        self.assertEqual(nfc_fold(""), "")

    def test_full_casefolding_not_just_lower(self):
        # casefold is the Unicode-correct full mapping (ß -> ss).
        self.assertEqual(nfc_fold("Straße"), "strasse")

    def test_process_names_match_case_insensitively(self):
        # The gate compares a process name from the OS against user config.
        self.assertEqual(nfc_fold("Notepad.EXE"), nfc_fold("notepad.exe"))


class TestSameText(unittest.TestCase):
    def test_exact_vietnamese_title(self):
        self.assertTrue(same_text(TITLE_EXCEL, TITLE_EXCEL))

    def test_decomposed_vs_precomposed_title(self):
        self.assertTrue(
            same_text(unicodedata.normalize("NFD", TITLE_EXCEL), TITLE_EXCEL)
        )

    def test_case_and_composition_combined(self):
        self.assertTrue(
            same_text(
                unicodedata.normalize("NFD", TITLE_EXCEL).upper(),
                TITLE_EXCEL,
            )
        )

    def test_different_titles_are_not_same(self):
        self.assertFalse(same_text(TITLE_EXCEL, TITLE_WORD))

    def test_none_matches_empty(self):
        self.assertTrue(same_text(None, ""))


class TestContainsText(unittest.TestCase):
    def test_substring_of_title(self):
        self.assertTrue(contains_text(TITLE_EXCEL, "lịch thi"))

    def test_substring_decomposed_haystack(self):
        hay = unicodedata.normalize("NFD", TITLE_EXCEL)
        self.assertTrue(contains_text(hay, "lịch thi"))

    def test_substring_case_insensitive(self):
        self.assertTrue(contains_text(TITLE_EXCEL, "EXCEL"))

    def test_empty_needle_never_matches(self):
        self.assertFalse(contains_text(TITLE_EXCEL, ""))
        self.assertFalse(contains_text(TITLE_EXCEL, None))

    def test_missing_substring(self):
        self.assertFalse(contains_text(TITLE_EXCEL, "PowerPoint"))

    def test_deny_label_check_catches_decomposed_control_name(self):
        # A control named with decomposed diacritics must still trip a
        # deny_label spelled in NFC form.
        control = unicodedata.normalize("NFD", "Xoá nhóm người dùng")
        self.assertTrue(contains_text(control, "xoá"))

    def test_deny_label_ascii_case_insensitive(self):
        self.assertTrue(contains_text("Delete file", "delete"))
        self.assertTrue(contains_text("DELETE FILE", "delete"))


if __name__ == "__main__":
    unittest.main()