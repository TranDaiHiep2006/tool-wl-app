"""Clipboard helpers (read-only)."""

from __future__ import annotations

import uiautomation as uia


# Format IDs as defined by the Windows clipboard / standard additions.
_LABELS = {
    1: "text",
    2: "bitmap",
    3: "metafile_pict",
    8: "dib",
    13: "unicode_text",
    15: "csv",
    16: "html_format",
    17: "rtf",
}


def _describe_format(fid: int) -> str:
    return _LABELS.get(fid, f"format_{fid}")


def read_clipboard() -> dict:
    """Return clipboard text and detect common non-text formats."""
    fmts = uia.GetClipboardFormats() or {}
    format_ids = sorted(int(k) for k in fmts.keys())
    text = uia.GetClipboardText() or ""

    # If we have text and a known text format id is present, return it.
    if text and (1 in format_ids or 13 in format_ids):
        return {
            "type": "text",
            "text": text,
            "length": len(text),
            "format_ids": format_ids,
        }

    if not format_ids:
        return {"type": "empty", "text": "", "length": 0, "format_ids": []}

    primary_id = format_ids[0]
    primary_type = fmts.get(primary_id, _describe_format(primary_id))
    # uiautomation returns the friendly name in fmts.values()
    if isinstance(primary_type, str):
        primary_type = primary_type.lower()
    else:
        primary_type = _describe_format(primary_id)
    return {
        "type": primary_type if primary_type != "" else _describe_format(primary_id),
        "text": "",
        "format_ids": format_ids,
        "format_names": [str(v) for v in fmts.values()],
    }