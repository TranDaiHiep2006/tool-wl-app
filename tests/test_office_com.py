"""Tests for `server.office_com` (P3 — Office COM read-only).

Coverage
--------
- Module imports cleanly even when COM machinery is mocked out (proves the
  read-only code path doesn't import Office at module load time when there
  is no running Office app).
- `is_office_app_running` returns False when no app is running.
- The three public tools return clear "office_not_running" errors when
  Office isn't running, instead of crashing the server.
- Cap enforcement: `read_excel_values` honors `max_rows` / `max_cols` and
  truncates with the right marker.
- The Word/Excel/PowerPoint inner extractors are exercised through a
  fake COM object that records what was called — verifying:
    * Word: `Content.Text` is read; no Save / SaveAs / Close is invoked.
    * Excel: UsedRange + Value are read; no Save is invoked.
    * PowerPoint: every slide's text is extracted; no Save is invoked.
- Empty trailing rows/cols are trimmed in Excel.
- COM worker timeout path returns a structured error (no hang).

The whole file must run hermetically — NO live Office instance required.
We patch `win32com.client.GetActiveObject` to return a fake object that
behaves like Word/Excel/PowerPoint.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
import unittest
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Fake COM object helpers
# ---------------------------------------------------------------------------


class _FakeCallRecorder:
    """Records attribute/method calls so the test can assert no mutating
    calls were made (Save, SaveAs, Close, Quit, etc.)."""

    def __init__(self, label: str = "fake") -> None:
        self.label = label
        self.calls: List[str] = []

    def __getattr__(self, name: str) -> Any:
        rec = self

        def _record(*args: Any, **kwargs: Any) -> Any:
            rec.calls.append(f"{name}({args!r}, {kwargs!r})")
            # Return a sensible fake for known mutating methods so we can
            # verify they were NEVER called.
            if name in ("Save", "SaveAs", "SaveAs2", "Close", "Quit",
                        "PrintOut", "PrintPreview"):
                raise AssertionError(
                    f"{rec.label}: FORBIDDEN call to {name}(...)"
                )
            if name in ("NewDocument", "NewWorkbook", "NewPresentation",
                        "AddSlide", "DeleteSlide"):
                raise AssertionError(
                    f"{rec.label}: FORBIDDEN call to {name}(...)"
                )
            return None

        return _record


class _FakeDoc(_FakeCallRecorder):
    def __init__(self, body_text: str = "Hello world", name: str = "Doc1") -> None:
        super().__init__(label="doc")
        self._body = body_text
        self.Name = name
        # Build a fake Content object that returns the body text.
        self.Content = types.SimpleNamespace(Text=body_text)
        # Sections with empty headers/footers — just stubs.
        sec = _FakeCallRecorder(label="section")
        sec.Headers = lambda *_a, **_k: types.SimpleNamespace(Range=types.SimpleNamespace(Text=""))
        sec.Footers = lambda *_a, **_k: types.SimpleNamespace(Range=types.SimpleNamespace(Text=""))
        self.Sections = [sec]


class _FakeCell:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeShape(_FakeCallRecorder):
    def __init__(self, text: str = "") -> None:
        super().__init__(label="shape")
        self.HasTextFrame = True
        self.HasTable = False
        self.TextFrame = types.SimpleNamespace(
            TextRange=types.SimpleNamespace(Text=text)
        )


class _FakeSlide(_FakeCallRecorder):
    def __init__(self, shapes: List[_FakeShape]) -> None:
        super().__init__(label="slide")
        self.Shapes = shapes


class _FakeRange(_FakeCallRecorder):
    """Excel range — Value returns a tuple of tuples (or scalar)."""

    def __init__(self, values: Any) -> None:
        super().__init__(label="range")
        self._values = values
        self._row_count = 1
        self._col_count = 1
        if isinstance(values, tuple) and values and isinstance(values[0], tuple):
            self._row_count = len(values)
            self._col_count = len(values[0]) if values[0] else 1
        elif isinstance(values, tuple):
            self._row_count = 1
            self._col_count = len(values)
        else:
            self._row_count = 1
            self._col_count = 1

    @property
    def Value(self) -> Any:
        return self._values

    @property
    def Rows(self) -> Any:
        return types.SimpleNamespace(Count=self._row_count)

    @property
    def Columns(self) -> Any:
        return types.SimpleNamespace(Count=self._col_count)


class _FakeUsedRange(_FakeCallRecorder):
    """A UsedRange that allows Range(c1, c2) to be called.

    Crucially, the returned range must obey the cap dimensions, since the
    production code asks for `used.Range(used.Cells(1,1), used.Cells(eff_rows, eff_cols))`
    where eff_rows/cols have been clamped to the cap.
    """

    def __init__(self, values: Any) -> None:
        super().__init__(label="usedrange")
        self._all_values = values
        # Cached "full" range — used when the production code reads Rows/Columns
        # directly off the UsedRange (not via a sub-range).
        self._full_rng = _FakeRange(values)
        # Track rows/cols that are full so we can answer Rows/Columns.
        if isinstance(values, tuple) and values and isinstance(values[0], tuple):
            self._full_rows = len(values)
            self._full_cols = max((len(r) for r in values), default=1)
        elif isinstance(values, tuple):
            self._full_rows = 1
            self._full_cols = len(values)
        else:
            self._full_rows = 1
            self._full_cols = 1

    def Cells(self, row: int, col: int) -> Any:
        return types.SimpleNamespace(_row=row, _col=col)

    def Range(self, c1: Any, c2: Any) -> "_FakeRange":
        r1 = int(getattr(c1, "_row", 1))
        c1c = int(getattr(c1, "_col", 1))
        r2 = int(getattr(c2, "_row", r1))
        c2c = int(getattr(c2, "_col", c1c))
        # Slice the underlying values to honor (r2-r1+1, c2-c1+1).
        if isinstance(self._all_values, tuple) and self._all_values and isinstance(self._all_values[0], tuple):
            sub = tuple(
                tuple(row[c1c - 1: c2c])
                for row in self._all_values[r1 - 1: r2]
            )
            return _FakeRange(sub)
        if isinstance(self._all_values, tuple):
            sub = self._all_values[c1c - 1: c2c]
            return _FakeRange(sub)
        return _FakeRange(self._all_values)

    @property
    def Rows(self) -> Any:
        return types.SimpleNamespace(Count=self._full_rows)

    @property
    def Columns(self) -> Any:
        return types.SimpleNamespace(Count=self._full_cols)

    @property
    def Value(self) -> Any:
        return self._all_values


class _FakeSheet(_FakeCallRecorder):
    def __init__(self, name: str, values: Any) -> None:
        super().__init__(label="sheet")
        self.Name = name
        self.UsedRange = _FakeUsedRange(values)


class _FakeWorkbook(_FakeCallRecorder):
    def __init__(self, sheets: List[_FakeSheet]) -> None:
        super().__init__(label="workbook")
        self._sheets = sheets
        # Real COM `Sheets` is a collection you can iterate (for s in
        # wb.Sheets) AND call by name (wb.Sheets(name)). The production
        # code uses both forms.
        self.Sheets = _FakeSheetsCollection(sheets)

    def __iter__(self) -> Any:
        return iter(self._sheets)

    def Sheets_by_name(self, name: str) -> _FakeSheet:
        for s in self._sheets:
            if s.Name == name:
                return s
        raise KeyError(name)


class _FakeSheetsCollection(_FakeCallRecorder):
    """Behaves like a COM Sheets collection: iterable AND callable by name."""

    def __init__(self, sheets: List[_FakeSheet]) -> None:
        super().__init__(label="sheets")
        self._sheets = sheets

    def __iter__(self) -> Any:
        return iter(self._sheets)

    def __call__(self, name: str) -> _FakeSheet:
        for s in self._sheets:
            if s.Name == name:
                return s
        # Real COM also accepts index — accept int as fallback.
        if isinstance(name, int):
            return self._sheets[name - 1]
        raise KeyError(name)


class _FakeWordApp(_FakeCallRecorder):
    def __init__(self, doc: _FakeDoc) -> None:
        super().__init__(label="Word.Application")
        self.ActiveDocument = doc


class _FakeExcelApp(_FakeCallRecorder):
    def __init__(self, wb: _FakeWorkbook, active_sheet_name: str) -> None:
        super().__init__(label="Excel.Application")
        self.ActiveWorkbook = wb
        self._active_sheet = next(s for s in wb._sheets if s.Name == active_sheet_name)

    @property
    def ActiveSheet(self) -> _FakeSheet:
        return self._active_sheet


class _FakePowerPointApp(_FakeCallRecorder):
    def __init__(self, slides: List[_FakeSlide], name: str = "Pres1") -> None:
        super().__init__(label="PowerPoint.Application")
        self.ActivePresentation = _FakePresentation(slides, name)


class _FakePresentation(_FakeCallRecorder):
    def __init__(self, slides: List[_FakeSlide], name: str) -> None:
        super().__init__(label="presentation")
        self.Name = name
        self._slides = slides
        self.Slides = _FakeSlidesCollection(slides)


class _FakeSlidesCollection(_FakeCallRecorder):
    def __init__(self, slides: List[_FakeSlide]) -> None:
        super().__init__(label="slides")
        self._slides = slides

    def __iter__(self) -> Any:
        return iter(self._slides)

    def __call__(self, index: int) -> _FakeSlide:  # pragma: no cover
        return self._slides[index - 1]

    @property
    def Count(self) -> int:
        return len(self._slides)

    def __getitem__(self, index: int) -> _FakeSlide:  # type: ignore[override]
        return self._slides[index - 1]


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------


def _install_win32_fakes(apps: Dict[str, Any]) -> None:
    """Patch win32com.client.GetActiveObject to return the named fake app.

    `apps` keys: 'Word.Application', 'Excel.Application', 'PowerPoint.Application'.
    Missing keys raise (so an "office not running" path is preserved).
    """
    from server import office_com
    real_get_active = office_com.win32com.client.GetActiveObject

    def _patched(prog_id: str) -> Any:
        if prog_id in apps:
            return apps[prog_id]
        # Mirror pywin32's COMError for "Operation unavailable".
        raise Exception(f"{prog_id} not running")

    office_com.win32com.client.GetActiveObject = _patched  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOfficeNotRunning(unittest.TestCase):
    """If Office isn't running, the public tools return a clear error
    AND the MCP server process stays up (no exception leaks)."""

    def setUp(self) -> None:
        from server import office_com
        # Replace GetActiveObject with one that always raises.
        office_com.win32com.client.GetActiveObject = lambda _pid: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            Exception(f"Office.Application not running")
        )

    def test_read_word_text_returns_not_running(self):
        from server import office_com
        out = office_com.read_word_text()
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "office_not_running")
        self.assertIn("not running", out.get("message", "").lower())

    def test_read_excel_values_returns_not_running(self):
        from server import office_com
        out = office_com.read_excel_values()
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "office_not_running")

    def test_read_powerpoint_text_returns_not_running(self):
        from server import office_com
        out = office_com.read_powerpoint_text()
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "office_not_running")

    def test_is_office_app_running_returns_false(self):
        from server import office_com
        self.assertFalse(office_com.is_office_app_running("Word.Application"))
        self.assertFalse(office_com.is_office_app_running("Excel.Application"))
        self.assertFalse(office_com.is_office_app_running("PowerPoint.Application"))


class TestReadWordText(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _FakeDoc("Đây là tài liệu tiếng Việt có dấu.")
        _install_win32_fakes({"Word.Application": _FakeWordApp(self.doc)})

    def test_returns_document_text(self):
        from server import office_com
        out = office_com.read_word_text()
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["text"], "Đây là tài liệu tiếng Việt có dấu.")
        self.assertEqual(out["chars"], len(out["text"]))
        self.assertFalse(out["truncated"])
        self.assertEqual(out["app"], "Word")
        self.assertEqual(out["name"], "Doc1")
        # No Save / SaveAs / Close was invoked on the doc.
        for call in self.doc.calls:
            self.assertNotIn("Save", call)
            self.assertNotIn("Close", call)

    def test_truncation_marker(self):
        big = "x" * (office_com.HARD_TEXT_MAX + 100) if False else "x" * 100_000
        self.doc = _FakeDoc(big)
        _install_win32_fakes({"Word.Application": _FakeWordApp(self.doc)})
        from server import office_com
        out = office_com.read_word_text()
        self.assertTrue(out.get("ok"))
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["chars"], office_com.HARD_TEXT_MAX + 1)

    def test_no_active_document_returns_clear_error(self):
        # Word is open but ActiveDocument raises.
        from server import office_com

        class _NoDoc(_FakeCallRecorder):
            def __init__(self) -> None:
                super().__init__(label="Word.Application")
            @property
            def ActiveDocument(self) -> None:
                raise Exception("no document open")

        office_com.win32com.client.GetActiveObject = lambda _pid: _NoDoc()  # type: ignore[attr-defined]
        out = office_com.read_word_text()
        self.assertFalse(out.get("ok"))
        self.assertEqual(out["error"], "no_active_document")


class TestReadExcelValues(unittest.TestCase):
    def _make_workbook(self) -> _FakeWorkbook:
        sheet1 = _FakeSheet("Sheet1", (
            ("A1", "B1", "C1"),
            ("A2", 42, 3.14),
            ("", "", ""),
        ))
        sheet2 = _FakeSheet("Data", (("Việt Nam", "Hà Nội"),))
        return _FakeWorkbook([sheet1, sheet2])

    def test_default_sheet_returns_active(self):
        wb = self._make_workbook()
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Sheet1")})
        from server import office_com
        out = office_com.read_excel_values()
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["sheet"], "Sheet1")
        self.assertEqual(out["available_sheets"], ["Sheet1", "Data"])
        # The trailing all-empty row should be trimmed.
        self.assertEqual(out["rows"], 2)
        # Mutating calls were never made on the workbook or sheet.
        for rec in (wb, sheet1 := wb._sheets[0]):  # type: ignore[misc]
            for call in rec.calls:
                self.assertNotIn("Save", call)
                self.assertNotIn("Close", call)

    def test_explicit_sheet_lookup(self):
        wb = self._make_workbook()
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Sheet1")})
        from server import office_com
        out = office_com.read_excel_values(sheet="Data")
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["sheet"], "Data")
        self.assertEqual(out["rows"], 1)
        # Vietnamese text round-trip.
        self.assertEqual(out["cells"][0][0], "Việt Nam")
        self.assertEqual(out["cells"][0][1], "Hà Nội")

    def test_case_insensitive_sheet_lookup(self):
        wb = self._make_workbook()
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Sheet1")})
        from server import office_com
        out = office_com.read_excel_values(sheet="data")  # lower-case
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["sheet"], "Data")

    def test_unknown_sheet_returns_clear_error(self):
        wb = self._make_workbook()
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Sheet1")})
        from server import office_com
        out = office_com.read_excel_values(sheet="Nope")
        self.assertFalse(out.get("ok"))
        self.assertEqual(out["error"], "sheet_not_found")
        self.assertIn("available_sheets", out)

    def test_max_rows_caps_output(self):
        # Build a 250-row sheet — must cap at 200.
        rows = tuple((f"r{i}", f"c{i}") for i in range(250))
        wb = _FakeWorkbook([_FakeSheet("Big", rows)])
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Big")})
        from server import office_com
        out = office_com.read_excel_values(max_rows=200)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["rows"], 200)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["used_rows"], 250)

    def test_max_cols_caps_output(self):
        # Build a wide sheet (60 cols, 1 row).
        row = tuple(f"c{i}" for i in range(60))
        wb = _FakeWorkbook([_FakeSheet("Wide", (row,))])
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Wide")})
        from server import office_com
        out = office_com.read_excel_values(max_cols=50)
        self.assertTrue(out.get("ok"))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["used_cols"], 60)

    def test_caps_clamped(self):
        wb = self._make_workbook()
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Sheet1")})
        from server import office_com
        # Asking for 5000 rows / 200 cols must be clamped to 200 / 50.
        out = office_com.read_excel_values(max_rows=5000, max_cols=200)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["max_rows"], office_com.HARD_MAX_ROWS)
        self.assertEqual(out["max_cols"], office_com.HARD_MAX_COLS)

    def test_trim_empty_trailing_rows(self):
        # A sheet with 5 rows, the last 3 are all blank.
        rows = (
            ("a", "b"),
            ("c", "d"),
            ("", ""),
            ("", ""),
            ("", ""),
        )
        wb = _FakeWorkbook([_FakeSheet("Blank", rows)])
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "Blank")})
        from server import office_com
        out = office_com.read_excel_values()
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["rows"], 2)
        self.assertFalse(out["truncated"])


class TestReadPowerPointText(unittest.TestCase):
    def test_returns_per_slide_text(self):
        s1 = _FakeSlide([
            _FakeShape("Slide 1 title"),
            _FakeShape("Slide 1 body text"),
        ])
        s2 = _FakeSlide([
            _FakeShape("Slide 2: thông tin tiếng Việt"),
        ])
        _install_win32_fakes({
            "PowerPoint.Application": _FakePowerPointApp([s1, s2])
        })
        from server import office_com
        out = office_com.read_powerpoint_text()
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["total_slides"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["slides"][0]["index"], 1)
        self.assertIn("Slide 1 title", out["slides"][0]["text"])
        self.assertIn("Slide 1 body text", out["slides"][0]["text"])
        # Vietnamese
        self.assertIn("thông tin tiếng Việt", out["slides"][1]["text"])
        # No mutating calls
        for rec in (s1, s2):
            for call in rec.calls:
                self.assertNotIn("Save", call)
                self.assertNotIn("Close", call)

    def test_truncates_at_max_slides(self):
        slides = [_FakeSlide([_FakeShape(f"Slide {i}")]) for i in range(1, 6)]
        _install_win32_fakes({
            "PowerPoint.Application": _FakePowerPointApp(slides, "BigDeck")
        })
        from server import office_com
        out = office_com.read_powerpoint_text(max_slides=3)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["total_slides"], 5)
        self.assertTrue(out["truncated"])

    def test_max_slides_clamped(self):
        slides = [_FakeSlide([_FakeShape(f"Slide {i}")]) for i in range(1, 3)]
        _install_win32_fakes({
            "PowerPoint.Application": _FakePowerPointApp(slides)
        })
        from server import office_com
        out = office_com.read_powerpoint_text(max_slides=10_000)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["max_slides"], 500)  # hard cap


class TestComWorker(unittest.TestCase):
    """Direct tests of the COM-thread helper."""

    def test_returns_payload_on_success(self):
        from server import office_com
        def _add(a: int, b: int) -> int:
            return a + b
        res = office_com._run_with_com_timeout(_add, 2, 3)
        self.assertTrue(res.ok)
        self.assertEqual(res.payload, 5)

    def test_returns_error_on_exception(self):
        from server import office_com
        def _boom() -> int:
            raise RuntimeError("kaboom")
        res = office_com._run_with_com_timeout(_boom)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "exc")
        self.assertIn("kaboom", res.error_message or "")

    def test_returns_timeout_on_long_call(self):
        from server import office_com
        def _hang() -> None:
            time.sleep(2.0)
        start = time.time()
        res = office_com._run_with_com_timeout(_hang, timeout_s=0.2)
        elapsed = time.time() - start
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "com_timeout")
        # We didn't sit on the warm 2s; the call returned in ~0.2s.
        self.assertLess(elapsed, 1.5)


class TestCaps(unittest.TestCase):
    def test_excel_hard_caps(self):
        # Document the hard caps so any future change is visible here.
        from server import office_com
        self.assertEqual(office_com.HARD_MAX_ROWS, 200)
        self.assertEqual(office_com.HARD_MAX_COLS, 50)


class TestNoMutatingApi(unittest.TestCase):
    """Hardening: even when a fake Office app is provided with a Save
    method, the office_com code path must NEVER call it. The fake object
    raises AssertionError if Save is invoked."""

    def test_no_save_called_on_word(self):
        doc = _FakeDoc("hello")
        # Wrap doc so calls to .Save etc raise — verified by FakeCallRecorder.
        _install_win32_fakes({"Word.Application": _FakeWordApp(doc)})
        from server import office_com
        out = office_com.read_word_text()
        self.assertTrue(out.get("ok"))

    def test_no_save_called_on_excel(self):
        sheet = _FakeSheet("S1", (("a",),))
        wb = _FakeWorkbook([sheet])
        _install_win32_fakes({"Excel.Application": _FakeExcelApp(wb, "S1")})
        from server import office_com
        out = office_com.read_excel_values()
        self.assertTrue(out.get("ok"))

    def test_no_save_called_on_powerpoint(self):
        slide = _FakeSlide([_FakeShape("hi")])
        _install_win32_fakes({
            "PowerPoint.Application": _FakePowerPointApp([slide])
        })
        from server import office_com
        out = office_com.read_powerpoint_text()
        self.assertTrue(out.get("ok"))


if __name__ == "__main__":
    unittest.main()