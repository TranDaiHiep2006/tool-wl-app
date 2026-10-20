"""Focused end-to-end smoke test for the P3 Office COM tools.

Launches Excel, creates a workbook with cell content, then calls
read_excel_values via the same path the FastMCP server uses.
Cleans up at the end.

Not part of pytest (touches real Office processes).

Usage:
    .\\.venv\\Scripts\\python.exe tests/manual_office_e2e_excel.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server import office_com  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("P3 Office COM end-to-end smoke test (Excel)")
    print("=" * 60)

    # NOTE: we call the inner function DIRECTLY (not via the public tool API)
    # because the tool spawns a worker thread that cannot see Excel's ROT entry
    # created by a different COM apartment. For the real use case, the user
    # has Excel open and the MCP server calls the tool in its own apartment.
    # Here we simulate that by calling the inner function from the SAME apartment.
    print("\n[1/4] Launching Excel and creating workbook via COM...")
    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()
    try:
        xl = win32com.client.DispatchEx("Excel.Application")
        wb = xl.Workbooks.Add()
        sheet = wb.Sheets(1)
        sheet.Name = "Data"
        sheet.Cells(1, 1).Value = "Header A"
        sheet.Cells(1, 2).Value = "Header B"
        sheet.Cells(1, 3).Value = "Header C"
        sheet.Cells(2, 1).Value = "Ha Noi"
        sheet.Cells(2, 2).Value = 42
        sheet.Cells(2, 3).Value = 3.14
        sheet.Cells(3, 1).Value = "Sai Gon"
        sheet.Cells(3, 2).Value = 100
        sheet.Cells(3, 3).Value = 2.71
        sheet.Cells(4, 1).Value = ""
        sheet.Cells(4, 2).Value = ""
        sheet.Cells(4, 3).Value = ""
        print("  workbook created in memory.")
        print(f"  Excel: {xl.Name}, ActiveSheet: {xl.ActiveSheet.Name}")

        # Call the inner function directly (same apartment as the Excel instance).
        print("\n[2/4] Calling _read_excel_values_inner(sheet='Data')...")
        out = office_com._read_excel_values_inner("Data", 200, 50)
    finally:
        pythoncom.CoUninitialize()

    print(f"  ok: {out.get('ok')}")
    if not out.get("ok"):
        err_msg = str(out.get("message") or "")
        # Encode safely for cp1252 console.
        safe_msg = err_msg.encode("cp1252", errors="replace").decode("cp1252")
        print(f"  error: {out.get('error')}, message: {safe_msg[:200]}")
        return 1

    print(f"  sheet: {out.get('sheet')}")
    print(f"  available_sheets: {out.get('available_sheets')}")
    print(f"  rows: {out.get('rows')}, cols: {out.get('cols')}")
    print(f"  truncated: {out.get('truncated')}")
    print("  cells:")
    cells = out.get("cells") or []
    for i, row in enumerate(cells, 1):
        print(f"    row {i}: {row}")

    # Spot-check 3 cells.
    print("\n[3/4] Spot-checks:")
    spotchecks = []
    if len(cells) >= 2:
        spotchecks.append(("A2 == 'Ha Noi'", cells[1][0], "Ha Noi"))
        spotchecks.append(("B2 == 42", cells[1][1], 42))
    if len(cells) >= 3:
        spotchecks.append(("B3 == 100", cells[2][1], 100))
    all_pass = True
    for label, got, want in spotchecks:
        ok = got == want
        all_pass = all_pass and ok
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {label}: got={got!r}, want={want!r}")

    # Trailing blank rows trimmed?
    shape_ok = out.get("rows") == 3  # 4th row is all blank, should be trimmed
    print(f"  [{'PASS' if shape_ok else 'FAIL'}] empty trailing rows trimmed: rows={out.get('rows')}")
    all_pass = all_pass and shape_ok

    # Quota metric.
    size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    print(f"\n  read_excel_values envelope: {size} bytes ({size / 1024:.2f} KB)")

    # Cleanup.
    print("\n[4/4] Cleanup...")
    pythoncom.CoInitialize()
    try:
        x2 = win32com.client.GetActiveObject("Excel.Application")
        x2.Quit()
        print("  Excel quit cleanly.")
    except Exception as exc:
        print(f"  warning: could not quit Excel: {exc!r}")
    finally:
        pythoncom.CoUninitialize()

    print()
    if all_pass:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
