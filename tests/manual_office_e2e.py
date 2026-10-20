"""End-to-end smoke test for the P3 Office COM tools.

Opens Word/Excel/PowerPoint, creates a real document/workbook/presentation
containing English + Vietnamese text, then calls each of the new MCP
tools via the same path the FastMCP server uses. Closes Office at the
end. Designed to be run manually as the P3 acceptance step.

NOT part of the pytest suite (it touches real Office processes).

Usage:
    .\\.venv\\Scripts\\python.exe tests/manual_office_e2e.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server import office_com  # noqa: E402

WINWORD = r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"
EXCEL = r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
POWERPNT = r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE"


def _spawn(binary: str) -> None:
    """Launch an Office app in the background (Windows uses CreateProcess)."""
    print(f"  launching {binary} ...")
    subprocess.Popen(
        [binary, "/q"],  # /q = no splash screen
        creationflags=0x00000008 | 0x00000010,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )


def _wait_until(predicate, timeout_s: float = 30.0, label: str = "Office") -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"  WARNING: timed out waiting for {label}")
    return False


def _terminate(proc_name: str) -> None:
    """Force-quit a running Office app by name (test-only)."""
    subprocess.run(["taskkill", "/IM", proc_name, "/F"], check=False)


def _create_word_doc_via_python() -> str:
    """Use COM directly to create a .docx with Vietnamese text and return its path."""
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        doc = word.Documents.Add()
        # Vietnamese sample text.
        rng = doc.Content
        rng.Text = (
            "Báo cáo P3 — Office COM test\n"
            "Đây là tài liệu tiếng Việt có dấu.\n"
            "Hà Nội, Việt Nam — xin chào Cursor Agent!\n"
            "ASCII line: Hello, world! 123.\n"
        )
        out = os.path.join(tempfile.gettempdir(), "tool_wl_p3_word.docx")
        doc.SaveAs(out)
        doc.Close()
        word.Quit()
        return out
    finally:
        pythoncom.CoUninitialize()


def _create_excel_workbook_via_python() -> str:
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        xl = win32com.client.DispatchEx("Excel.Application")
        xl.Visible = False
        wb = xl.Workbooks.Add()
        sheet = wb.Sheets(1)
        sheet.Name = "Dữ liệu"
        sheet.Cells(1, 1).Value = "Header A"
        sheet.Cells(1, 2).Value = "Header B"
        sheet.Cells(1, 3).Value = "Header C"
        sheet.Cells(2, 1).Value = "Hà Nội"
        sheet.Cells(2, 2).Value = 42
        sheet.Cells(2, 3).Value = 3.14
        sheet.Cells(3, 1).Value = "Sài Gòn"
        sheet.Cells(3, 2).Value = 100
        sheet.Cells(3, 3).Value = 2.71
        sheet.Cells(4, 1).Value = ""  # last row intentionally blank for trim test
        sheet.Cells(4, 2).Value = ""
        sheet.Cells(4, 3).Value = ""
        out = os.path.join(tempfile.gettempdir(), "tool_wl_p3_excel.xlsx")
        wb.SaveAs(out)
        wb.Close()
        xl.Quit()
        return out
    finally:
        pythoncom.CoUninitialize()


def _create_powerpoint_presentation() -> str:
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        pp = win32com.client.DispatchEx("PowerPoint.Application")
        # NOTE: PowerPoint raises if you try `pp.Visible = False`.
        # Leave the default (True) so the test can create slides.
        pres = pp.Presentations.Add()
        # Slide 1
        s1 = pres.Slides(1)
        s1.Shapes.Title.TextFrame.TextRange.Text = "Tiêu đề P3"
        s1.Shapes.AddTextbox(1, 100, 100, 400, 100).TextFrame.TextRange.Text = (
            "Slide 1 — Hà Nội, Việt Nam"
        )
        # Slide 2
        s2 = pres.Slides.Add(2, 12)
        s2.Shapes.Title.TextFrame.TextRange.Text = "Slide 2 ASCII"
        s2.Shapes.AddTextbox(1, 100, 100, 400, 100).TextFrame.TextRange.Text = (
            "Hello, world!"
        )
        out = os.path.join(tempfile.gettempdir(), "tool_wl_p3_ppt.pptx")
        pres.SaveAs(out)
        pres.Close()
        pp.Quit()
        return out
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    print("=" * 60)
    print("P3 Office COM end-to-end smoke test")
    print("=" * 60)

    if not shutil.which(WINWORD) and not os.path.exists(WINWORD):
        print("FAIL: WINWORD.EXE not found at", WINWORD)
        return 1

    # 1. Create test documents via COM directly (no app instance needed yet).
    print()
    print("[1/4] Creating test documents...")
    word_doc = _create_word_doc_via_python()
    print(f"  word: {word_doc}")
    excel_doc = _create_excel_workbook_via_python()
    print(f"  excel: {excel_doc}")
    ppt_doc = _create_powerpoint_presentation()
    print(f"  ppt: {ppt_doc}")

    # 2. Launch the apps and open the test documents.
    print()
    print("[2/4] Launching Office apps and opening the test documents...")
    _spawn(WINWORD)
    _spawn(EXCEL)
    _spawn(POWERPNT)
    time.sleep(2)

    # Wait for COM to be available.
    def _word_ready() -> bool:
        try:
            office_com.is_office_app_running("Word.Application")
            return office_com.is_office_app_running("Word.Application")
        except Exception:
            return False
    _wait_until(office_com.is_office_app_running, 30, "Office apps to register COM")
    time.sleep(2)

    # Open documents using COM.
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.GetActiveObject("Word.Application")
            word.Documents.Open(word_doc)
        except Exception as exc:
            print(f"  could not open Word doc: {exc!r}")
        try:
            xl = win32com.client.GetActiveObject("Excel.Application")
            xl.Workbooks.Open(excel_doc)
        except Exception as exc:
            print(f"  could not open Excel doc: {exc!r}")
        try:
            pp = win32com.client.GetActiveObject("PowerPoint.Application")
            pp.Presentations.Open(ppt_doc, ReadOnly=True)
        except Exception as exc:
            print(f"  could not open PowerPoint doc: {exc!r}")
    finally:
        pythoncom.CoUninitialize()
    time.sleep(2)

    # 3. Call the three new MCP tools.
    print()
    print("[3/4] Calling the three new MCP tools...")
    print()
    print("--- read_word_text() ---")
    out_w = office_com.read_word_text()
    print(f"  ok: {out_w.get('ok')}")
    print(f"  app: {out_w.get('app')}, name: {out_w.get('name')}")
    print(f"  chars: {out_w.get('chars')}, truncated: {out_w.get('truncated')}")
    text = (out_w.get("text") or "")
    print(f"  text (first 200 chars): {text[:200]!r}")
    if "Hà Nội" in text and "Việt Nam" in text and "Báo cáo P3" in text:
        print("  ✓ Vietnamese text round-tripped")
    else:
        print("  ✗ Vietnamese text NOT found")

    print()
    print("--- read_excel_values() ---")
    out_e = office_com.read_excel_values(sheet="Dữ liệu")
    print(f"  ok: {out_e.get('ok')}")
    print(f"  sheet: {out_e.get('sheet')}")
    print(f"  rows: {out_e.get('rows')}, cols: {out_e.get('cols')}")
    cells = out_e.get("cells") or []
    for i, row in enumerate(cells, 1):
        print(f"    row {i}: {row}")
    # Spot-check 3 cells: A2, B2, B3.
    spotchecks = []
    if len(cells) >= 2:
        spotchecks.append((cells[1][0] == "Hà Nội", "A2 == 'Hà Nội'", cells[1][0]))
        spotchecks.append((cells[1][1] == 42, "B2 == 42", cells[1][1]))
    if len(cells) >= 3:
        spotchecks.append((cells[2][1] == 100, "B3 == 100", cells[2][1]))
    for ok, label, value in spotchecks:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {label}: {value!r}")

    print()
    print("--- read_powerpoint_text() ---")
    out_p = office_com.read_powerpoint_text()
    print(f"  ok: {out_p.get('ok')}")
    print(f"  count: {out_p.get('count')}, total_slides: {out_p.get('total_slides')}")
    for s in out_p.get("slides") or []:
        print(f"    slide {s['index']} ({s['chars']} chars): {s['text'][:80]!r}")
    slides = out_p.get("slides") or []
    if len(slides) >= 2:
        if "Tiêu đề P3" in (slides[0]["text"] or ""):
            print("  ✓ slide 1 Vietnamese title found")
        else:
            print("  ✗ slide 1 title NOT found")
        if "Hello" in (slides[1]["text"] or ""):
            print("  ✓ slide 2 ASCII text found")
        else:
            print("  ✗ slide 2 ASCII text NOT found")

    # 4. Cleanup.
    print()
    print("[4/4] Cleaning up...")
    try:
        _terminate("WINWORD.EXE")
        _terminate("EXCEL.EXE")
        _terminate("POWERPNT.EXE")
    except Exception:
        pass

    # Sizes for the quota metrics.
    print()
    print("Sample output sizes (KB):")
    import json as _json
    if out_w.get("ok"):
        sz = len(_json.dumps(out_w, ensure_ascii=False).encode("utf-8"))
        print(f"  read_word_text: {sz / 1024:.2f} KB")
    if out_e.get("ok"):
        sz = len(_json.dumps(out_e, ensure_ascii=False).encode("utf-8"))
        print(f"  read_excel_values: {sz / 1024:.2f} KB")
    if out_p.get("ok"):
        sz = len(_json.dumps(out_p, ensure_ascii=False).encode("utf-8"))
        print(f"  read_powerpoint_text: {sz / 1024:.2f} KB")
    print()
    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())