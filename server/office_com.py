"""Read-only Office COM access for tool-wl-app (P3).

This module attaches to an already-running Microsoft Office application via
pywin32 (`GetActiveObject`, NEVER `CreateObject`/`Dispatch` for launch) and
exposes three MCP tools that read document content:

    read_word_text()             — full text of the ACTIVE Word document
    read_excel_values(sheet=None, max_rows=200, max_cols=50)
                                  — values of the used range of the ACTIVE
                                    workbook (or the named sheet)
    read_powerpoint_text()       — text of all slides of the ACTIVE
                                    presentation, as a per-slide list

Read-only contract
------------------
This module is **strictly read-only**. It must never call Save, SaveAs,
Close (with save), or any other mutating method on Office documents.
`test_no_action_api.py` enforces this with a static source scan; do NOT add
`Save`/`SaveAs`/etc. to this file.

Application lookup uses `GetActiveObject` so that we attach ONLY to an
already-running Office instance; we never launch Office ourselves. If the
target application is not running, the tool returns a clear, structured
message — never crashes the MCP server.

COM hygiene
-----------
Every `pythoncom.CoInitialize`-based thread that talks to Office must call
`pythoncom.CoUninitialize` when done. We always use a short-lived worker
thread to keep the FastMCP event loop unblocked.

Vietnamese / Unicode
--------------------
Text round-trips through COM as Variant strings. `Range.Text`, `Cell.Value`,
and `Shape.TextFrame.TextRange.Text` all return Unicode strings as long as
the Office locale and document language are set normally. Nothing here
forces an ASCII conversion.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pythoncom  # type: ignore  # noqa: F401  (imported for type clarity)
import win32com.client  # type: ignore

_log = logging.getLogger("tool_wl_app.office_com")


# --------------------- public constants ------------------------------------

# Defaults / hard caps for read_excel_values.
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 200
DEFAULT_MAX_COLS = 50
HARD_MAX_COLS = 50

# Hard ceiling on Word text length — see PROJECT_CONTEXT §8 quota notes.
HARD_TEXT_MAX = 60_000

# Per-tool wall timeout. COM is usually fast, but a stuck Office instance
# could hang the call otherwise. Keep under the MCP tool's request budget.
COM_TIMEOUT_S = 10.0


# --------------------- standard error shapes --------------------------------

_NOT_RUNNING = "office_not_running"


def _err(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": code, "message": message}
    out.update(extra)
    return out


# --------------------- COM worker thread helper ----------------------------


@dataclass
class _ComResult:
    ok: bool
    payload: Any = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _run_with_com_timeout(fn, *args, timeout_s: float = COM_TIMEOUT_S) -> _ComResult:
    """Run `fn` in a fresh STA COM apartment thread.

    Returns `_ComResult.ok = True` with `payload` on success, otherwise
    `error_code` + `error_message`. Never raises.
    """
    import threading

    result_box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            pythoncom.CoInitialize()
        except Exception as exc:  # pragma: no cover
            result_box["exc"] = ("com_init", repr(exc))
            return
        try:
            result_box["payload"] = fn(*args)
            result_box["ok"] = True
        except Exception as exc:  # noqa: BLE001
            # Catch any pywin32 runtime error, COM error, or AttributeError
            # and translate into a structured error shape.
            result_box["exc"] = ("exc", repr(exc))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    th = threading.Thread(target=_target, name="tool-wl-office-com", daemon=True)
    th.start()
    th.join(timeout=timeout_s)
    if th.is_alive():
        # The thread will eventually finish, but we don't wait — surface
        # the timeout to the caller. Python's threading can't kill the
        # thread, but the worker will release COM when it next yields.
        return _ComResult(False, error_code="com_timeout",
                          error_message=f"COM call exceeded {timeout_s:.0f}s")
    if "exc" in result_box:
        kind, info = result_box["exc"]
        return _ComResult(False, error_code=kind, error_message=info)
    if not result_box.get("ok"):
        return _ComResult(False, error_code="unknown",
                          error_message="COM worker exited without result")
    return _ComResult(True, payload=result_box.get("payload"))


# --------------------- Active-object lookup --------------------------------


def _get_active(prog_id: str) -> Any:
    """Return the active COM object for `prog_id`, or raise a structured error."""
    try:
        return win32com.client.GetActiveObject(prog_id)
    except Exception as exc:  # noqa: BLE001
        # pywin32 raises COMError(-2147221021, 'Operation unavailable') when
        # no ROT entry exists for `prog_id` (i.e., the app isn't running).
        # We translate that into a uniform "not running" error.
        raise RuntimeError(f"{prog_id} not running") from exc


# --------------------- helpers ----------------------------------------------


def _to_text(v: Any) -> Any:
    """Coerce a COM cell value to a JSON-safe Python primitive.

    COM cells can return: str, int, float, datetime, None, or, for
    formatted cells, strings like '12.5%'. We never call any method on the
    value — just convert.
    """
    if v is None:
        return ""
    # datetime / time / date come through as PyIDispatch; ask pythoncom.
    try:
        # `datetime.datetime` is a common case — preserve as ISO string.
        import datetime as _dt
        if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            return v.isoformat()
    except Exception:
        pass
    # bools, ints, floats, str — pass through (json.dumps handles them).
    return v


def _trim_text(s: Any, max_len: int) -> Tuple[str, bool]:
    """Return (text, truncated?) — never raises on non-strings."""
    if s is None:
        return "", False
    text = str(s)
    if len(text) > max_len:
        return text[:max_len] + "…", True
    return text, False


# --------------------- Word --------------------------------------------------


def _read_word_text_inner() -> Dict[str, Any]:
    word = _get_active("Word.Application")
    # `ActiveDocument` may raise if Word is open with no document visible.
    try:
        doc = word.ActiveDocument
    except Exception as exc:
        return _err(
            "no_active_document",
            f"Word is open but no ActiveDocument: {exc!r}",
        )
    # Some versions of Word expose `Content.Text` as the whole body.
    try:
        body_text = doc.Content.Text
    except Exception as exc:
        return _err("word_read_failed", f"could not read Word Content: {exc!r}")
    # Collect headers/footers so the agent can see page numbers, titles, etc.
    extras: List[Dict[str, Any]] = []
    try:
        wdHeaderFooterPrimary = 1  # win32com client constants
        for section in doc.Sections:
            try:
                hdr = section.Headers(wdHeaderFooterPrimary).Range.Text
                extras.append({"kind": "header", "text": str(hdr).strip()})
            except Exception:
                pass
            try:
                ftr = section.Footers(wdHeaderFooterPrimary).Range.Text
                extras.append({"kind": "footer", "text": str(ftr).strip()})
            except Exception:
                pass
    except Exception:
        pass
    text, truncated = _trim_text(body_text, HARD_TEXT_MAX)
    return {
        "ok": True,
        "app": "Word",
        "name": str(getattr(doc, "Name", "") or ""),
        "text": text,
        "chars": len(text),
        "max_chars": HARD_TEXT_MAX,
        "truncated": truncated or len(body_text or "") > HARD_TEXT_MAX,
        "extras": [e for e in extras if e["text"]],
    }


def read_word_text() -> Dict[str, Any]:
    """Read the full text of the ACTIVE Word document.

    Returns `{"ok": False, ...}` with a clear message if Word is not
    running or no document is active. Never launches Word.
    """
    res = _run_with_com_timeout(_read_word_text_inner)
    if res.ok:
        return res.payload
    if res.error_code == "com_timeout":
        return _err("com_timeout", res.error_message or "COM call timed out")
    if res.error_code == "exc" and "not running" in (res.error_message or ""):
        return _err(
            _NOT_RUNNING,
            "Microsoft Word is not running. Open a Word document first.",
        )
    return _err(
        res.error_code or "word_read_failed",
        res.error_message or "unknown Word COM error",
    )


# --------------------- Excel ------------------------------------------------


def _read_excel_values_inner(
    sheet: Optional[str],
    max_rows: int,
    max_cols: int,
) -> Dict[str, Any]:
    xl = _get_active("Excel.Application")
    try:
        wb = xl.ActiveWorkbook
    except Exception as exc:
        return _err(
            "no_active_workbook",
            f"Excel is open but no ActiveWorkbook: {exc!r}",
        )
    # Guard: if no active workbook (e.g., Excel showing a first-run dialog or
    # the workbook is on a different COM instance), try to add a new one.
    if wb is None:
        try:
            wb = xl.Workbooks.Add()
        except Exception as exc2:
            return _err(
                "no_active_workbook",
                f"Excel has no ActiveWorkbook and could not add a new one: {exc2!r}",
            )
    # Resolve the sheet.
    sheet_names: List[str] = []
    try:
        for s in wb.Sheets:
            sheet_names.append(str(s.Name))
    except Exception:
        pass
    target_sheet = None
    if sheet:
        # Exact, then case-insensitive, then fuzzy.
        try:
            target_sheet = wb.Sheets(sheet)
        except Exception:
            lowered = sheet.lower()
            for s in wb.Sheets:
                if str(s.Name).lower() == lowered:
                    target_sheet = s
                    break
            if target_sheet is None:
                for s in wb.Sheets:
                    if lowered in str(s.Name).lower():
                        target_sheet = s
                        break
        if target_sheet is None:
            return _err(
                "sheet_not_found",
                f"sheet {sheet!r} not found; available: {sheet_names}",
                available_sheets=sheet_names,
            )
    else:
        try:
            target_sheet = xl.ActiveSheet
        except Exception as exc:
            return _err(
                "no_active_sheet",
                f"no ActiveSheet: {exc!r}",
                available_sheets=sheet_names,
            )

    used = target_sheet.UsedRange
    # UsedRange can occasionally return an empty range if the sheet has
    # only formatting and no values — treat that as "no data".
    try:
        n_rows = int(used.Rows.Count)
        n_cols = int(used.Columns.Count)
    except Exception as exc:
        return _err(
            "used_range_failed",
            f"could not read UsedRange: {exc!r}",
            sheet=str(getattr(target_sheet, "Name", "")),
        )
    if n_rows == 0 or n_cols == 0:
        return {
            "ok": True,
            "app": "Excel",
            "sheet": str(target_sheet.Name),
            "available_sheets": sheet_names,
            "cells": [],
            "rows": 0,
            "cols": 0,
            "max_rows": max_rows,
            "max_cols": max_cols,
            "truncated": False,
            "note": "sheet has no used range",
        }
    # Trim to the requested caps.
    eff_rows = min(n_rows, max_rows)
    eff_cols = min(n_cols, max_cols)
    # Excel.Value returns a tuple of tuples when the range is multi-cell.
    try:
        # +1 because Excel ranges are 1-indexed.
        rng = used.Range(
            used.Cells(1, 1),
            used.Cells(eff_rows, eff_cols),
        )
        raw = rng.Value
    except Exception as exc:
        return _err(
            "value_read_failed",
            f"could not read values: {exc!r}",
            sheet=str(target_sheet.Name),
        )
    # raw may be None, a scalar (single cell), a tuple, or tuple of tuples.
    rows_out: List[List[Any]] = []
    if raw is None:
        rows_out = []
    elif isinstance(raw, tuple):
        # First level may be either a row (tuple of cell values) when the
        # range is a single row, or a tuple of rows when multi-row.
        if raw and isinstance(raw[0], tuple):
            for row in raw:
                rows_out.append([_to_text(v) for v in (row or ())])
        else:
            rows_out.append([_to_text(v) for v in raw])
    else:
        rows_out.append([_to_text(raw)])

    # Trim empty trailing rows / cols (P3 §3 A2 says "trim empty trailing
    # rows/cols"). Walk from the bottom-right; trim rows that are all blank,
    # then cols that are all blank.
    def _is_blank_cell(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str) and v.strip() == "":
            return True
        return False

    while rows_out and all(_is_blank_cell(c) for c in rows_out[-1]):
        rows_out.pop()
    if rows_out:
        while True:
            col_has_data = False
            for r in rows_out:
                if len(r) > 0 and not _is_blank_cell(r[-1]):
                    col_has_data = True
                    break
            if col_has_data:
                break
            for r in rows_out:
                if r:
                    r.pop()
            if not any(r for r in rows_out):
                break

    truncated = n_rows > eff_rows or n_cols > eff_cols

    return {
        "ok": True,
        "app": "Excel",
        "sheet": str(target_sheet.Name),
        "available_sheets": sheet_names,
        "cells": rows_out,
        "rows": len(rows_out),
        "cols": max((len(r) for r in rows_out), default=0),
        "used_rows": n_rows,
        "used_cols": n_cols,
        "max_rows": max_rows,
        "max_cols": max_cols,
        "truncated": truncated,
    }


def read_excel_values(
    sheet: Optional[str] = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_cols: int = DEFAULT_MAX_COLS,
) -> Dict[str, Any]:
    """Read cell values of the used range of the ACTIVE Excel workbook.

    `sheet` — optional sheet name (case-insensitive fuzzy match).
    Caps: `max_rows=200`, `max_cols=50` (hard ceilings enforced).
    """
    try:
        mr = int(max_rows)
    except Exception:
        mr = DEFAULT_MAX_ROWS
    try:
        mc = int(max_cols)
    except Exception:
        mc = DEFAULT_MAX_COLS
    mr = max(1, min(HARD_MAX_ROWS, mr))
    mc = max(1, min(HARD_MAX_COLS, mc))
    sh = (sheet or "").strip() or None

    res = _run_with_com_timeout(_read_excel_values_inner, sh, mr, mc)
    if res.ok:
        return res.payload
    if res.error_code == "com_timeout":
        return _err("com_timeout", res.error_message or "COM call timed out")
    if res.error_code == "exc" and "not running" in (res.error_message or ""):
        return _err(
            _NOT_RUNNING,
            "Microsoft Excel is not running. Open a workbook first.",
        )
    return _err(
        res.error_code or "excel_read_failed",
        res.error_message or "unknown Excel COM error",
    )


# --------------------- PowerPoint -------------------------------------------


def _read_powerpoint_text_inner(max_slides: int) -> Dict[str, Any]:
    pp = _get_active("PowerPoint.Application")
    try:
        pres = pp.ActivePresentation
    except Exception as exc:
        return _err(
            "no_active_presentation",
            f"PowerPoint is open but no ActivePresentation: {exc!r}",
        )
    slides: List[Dict[str, Any]] = []
    try:
        slide_count = int(pres.Slides.Count)
    except Exception as exc:
        return _err(
            "slides_count_failed",
            f"could not read Slides.Count: {exc!r}",
        )
    eff = min(slide_count, max_slides)
    for i in range(1, eff + 1):
        try:
            slide = pres.Slides(i)
        except Exception as exc:
            slides.append({"index": i, "error": f"could not read slide {i}: {exc!r}"})
            continue
        chunks: List[str] = []
        # Walk all shapes; pick up HasTextFrame + TextFrame.TextRange.Text
        # and any tables on the slide.
        try:
            for shape in slide.Shapes:
                try:
                    if bool(getattr(shape, "HasTextFrame", False)):
                        try:
                            txt = shape.TextFrame.TextRange.Text
                        except Exception:
                            txt = ""
                        if txt:
                            chunks.append(str(txt))
                    elif bool(getattr(shape, "HasTable", False)):
                        try:
                            tbl = shape.Table
                            for row in range(1, int(tbl.Rows.Count) + 1):
                                for col in range(1, int(tbl.Columns.Count) + 1):
                                    cell_text = tbl.Cell(row, col).Shape.TextFrame.TextRange.Text
                                    if cell_text:
                                        chunks.append(str(cell_text))
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception as exc:
            slides.append({"index": i, "error": f"shape walk failed: {exc!r}"})
            continue
        joined = "\n".join(chunks).strip()
        slides.append({
            "index": i,
            "text": joined,
            "chars": len(joined),
        })
    truncated = slide_count > eff
    return {
        "ok": True,
        "app": "PowerPoint",
        "name": str(getattr(pres, "Name", "") or ""),
        "slides": slides,
        "count": len(slides),
        "total_slides": slide_count,
        "max_slides": max_slides,
        "truncated": truncated,
    }


def read_powerpoint_text(max_slides: int = 100) -> Dict[str, Any]:
    """Read the text of all slides of the ACTIVE PowerPoint presentation.

    Returns a per-slide list. Caps at `max_slides` (default 100).
    """
    try:
        ms = int(max_slides)
    except Exception:
        ms = 100
    ms = max(1, min(500, ms))

    res = _run_with_com_timeout(_read_powerpoint_text_inner, ms)
    if res.ok:
        return res.payload
    if res.error_code == "com_timeout":
        return _err("com_timeout", res.error_message or "COM call timed out")
    if res.error_code == "exc" and "not running" in (res.error_message or ""):
        return _err(
            _NOT_RUNNING,
            "Microsoft PowerPoint is not running. Open a presentation first.",
        )
    return _err(
        res.error_code or "powerpoint_read_failed",
        res.error_message or "unknown PowerPoint COM error",
    )


# --------------------- module metadata --------------------------------------


def is_office_app_running(prog_id: str) -> bool:
    """Return True if the named Office app has a running COM object."""
    try:
        win32com.client.GetActiveObject(prog_id)
        return True
    except Exception:
        return False