"""Unit tests for the P4 action safety gate.

Covers all gate rules:
- execute without plan_id → refused
- expired plan → refused
- reuse consumed plan → refused
- executing plan with different text → refused
- non-allowlisted process → refused
- STOP_ACTIONS present → refused
- deny-label control → refused
- enabled=false → refused
- focus_window without allowlist → refused

Uses hermetic mocking so no live desktop is needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


# ---- helpers ---------------------------------------------------------------

def _make_fake_config(tmp_path: Path, **kwargs) -> None:
    cfg_file = tmp_path / "actions.json"
    defaults = {
        "enabled": True,
        "allowed_processes": ["notepad.exe"],
        "deny_labels": ["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
    }
    defaults.update(kwargs)
    cfg_file.write_text(json.dumps(defaults), encoding="utf-8")


def _make_mock_window(name: str = "Untitled - Notepad",
                       aid: str = "notepad_main",
                       process_name: str = "notepad.exe") -> tuple[MagicMock, dict]:
    mock_win = MagicMock()
    mock_win.Name = name
    mock_win.ProcessId = 1234
    mock_win.NativeWindowHandle = 0x00120ABC
    mock_win.IsOffscreen = False
    mock_win.IsInvokePatternAvailable = True
    mock_win.IsTogglePatternAvailable = False
    mock_win.IsSelectionItemPatternAvailable = False
    mock_win.IsExpandCollapsePatternAvailable = False
    mock_win.IsValuePatternAvailable = True
    mock_win.AutomationId = aid
    mock_win.GetChildren.return_value = []

    mock_invoke = MagicMock()
    mock_value = MagicMock()
    mock_value.CurrentValue = "hello world"
    mock_value.IsReadOnly = False
    mock_value.SetValue = MagicMock()
    mock_win.GetInvokePattern.return_value = mock_invoke
    mock_win.GetValuePattern.return_value = mock_value

    return mock_win, {"invoke": mock_invoke, "value": mock_value}


# ---- test class ------------------------------------------------------------

def _redirect_module_paths(tmp_path: Path) -> dict:
    """Point actions/action_token at `tmp_path` and return the saved globals
    so tearDown can put them back (prevents cross-file test pollution)."""
    import server.actions as amod
    import server.action_token as tok_mod
    attrs = ("CONFIG_DIR", "STOP_FILE", "ACTIONS_LOG", "ACTION_CONFIG")
    tok_attrs = ("CONFIG_DIR", "SECRET_FILE", "CONSUMED_DIR", "_SECRET")
    saved = {f"amod.{a}": getattr(amod, a) for a in attrs}
    saved.update({f"tok.{a}": getattr(tok_mod, a) for a in tok_attrs})
    amod.CONFIG_DIR = tmp_path
    amod.STOP_FILE = tmp_path / "STOP_ACTIONS"
    amod.ACTIONS_LOG = tmp_path / "actions.log"
    amod.ACTION_CONFIG = tmp_path / "actions.json"
    tok_mod.CONFIG_DIR = tmp_path
    tok_mod.SECRET_FILE = tmp_path / ".action_token_secret.key"
    tok_mod.CONSUMED_DIR = tmp_path / "consumed"
    tok_mod._ensure_dirs()
    # Create a fresh secret for each test so token tests are independent.
    tok_mod._SECRET = tok_mod._load_or_create_secret()
    # Invalidate ActionConfig class-level cache so it re-reads from new path.
    amod.ActionConfig._instance = None  # type: ignore[attr-defined]
    return saved


def _restore_module_paths(saved: dict) -> None:
    import server.actions as amod
    import server.action_token as tok_mod
    for key, val in saved.items():
        mod, attr = key.split(".", 1)
        (amod if mod == "amod" else tok_mod).__setattr__(attr, val)


# ---- test class ------------------------------------------------------------

class TestGateRefusals(unittest.TestCase):
    """Gate rules that block actions regardless of phase."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tool_wl_act_")
        self._tmp_path = Path(self._tmp)
        _make_fake_config(self._tmp_path)
        self._saved = _redirect_module_paths(self._tmp_path)

    def tearDown(self):
        _restore_module_paths(self._saved)
        import shutil
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    # ---- click_control / type_text tests (use _resolve_control) ------------

    def test_execute_without_plan_id_returns_phase_plan(self):
        """click_control without plan_id must return phase=plan, not execute."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.click_control(window="notepad", name="test")

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("phase"), "plan")
        self.assertIn("plan_id", result)
        self.assertIn("description", result)
        self.assertEqual(result.get("expires_in_s"), 300)

    def test_execute_without_plan_id_type_text_returns_phase_plan(self):
        """type_text without plan_id must return phase=plan, not execute."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.type_text(window="notepad", text="new text", name="test")

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("phase"), "plan")
        self.assertIn("plan_id", result)
        self.assertIn("current_value", result)

    def test_expired_plan_refused(self):
        """A token whose TTL has elapsed must be refused with EXPIRED."""
        import server.actions as amod
        import server.action_token as tok_mod
        mock_win, _ = _make_mock_window()

        # Patch _tok.verify_token to return EXPIRED.
        import server.action_token as t
        with patch.object(amod, "_tok") as mock_tok:
            mock_tok.verify_token.return_value = t.VerifyResult(
                False, "EXPIRED",
                plan_id="ts-x",
                message="token expired",
            )
            with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.click_control(window="notepad", name="test", plan_id="ts-x-bad")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "EXPIRED")

    def test_consumed_plan_refused(self):
        """A token that has already been consumed must be refused with CONSUME_RACE."""
        import server.actions as amod
        import server.action_token as tok_mod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_tok") as mock_tok:
            mock_tok.verify_token.return_value = tok_mod.VerifyResult(
                False, "CONSUME_RACE",
                plan_id="ts-x",
                message="token already consumed",
            )
            with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.click_control(window="notepad", name="test", plan_id="ts-x-bad")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "CONSUME_RACE")

    def test_plan_with_different_text_refused(self):
        """type_text plan is only valid for the exact text it was created with.

        The token binding includes args_hash (a hash of the text argument).
        Calling with different text produces a different args_hash → BINDING_MISMATCH.
        """
        import server.actions as amod
        import server.action_token as tok_mod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_tok") as mock_tok:
            mock_tok.verify_token.return_value = tok_mod.VerifyResult(
                False, "BINDING_MISMATCH",
                plan_id="ts-x",
                message="args_hash mismatch",
            )
            with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.type_text(
                        window="notepad", text="changed", name="test",
                        plan_id="FAKE_TOKEN"
                    )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "BINDING_MISMATCH")

    def test_non_allowlisted_process_refused(self):
        """A process not in allowed_processes must be refused."""
        import server.actions as amod
        mock_win, _ = _make_mock_window(process_name="badapp.exe")

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Bad App")):
            with patch.object(amod, "_process_name_from_window", return_value="badapp.exe"):
                result = amod.click_control(window="badapp", name="test")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")
        self.assertIn("not in allowed_processes", result.get("message", ""))

    def test_stop_actions_file_refused(self):
        """Presence of config/STOP_ACTIONS must refuse all actions."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()
        (self._tmp_path / "STOP_ACTIONS").touch()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.click_control(window="notepad", name="test")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")
        self.assertIn("STOP_ACTIONS", result.get("message", ""))

    def test_deny_label_control_refused(self):
        """A control whose Name contains a deny_label must be refused."""
        import server.actions as amod
        deny_win, _ = _make_mock_window(name="Delete Everything Button", aid="deletebtn")

        with patch.object(amod, "_resolve_control", return_value=(deny_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.click_control(window="notepad", name="Delete Everything Button")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")
        self.assertIn("deny_label", result.get("message", ""))

    def test_enabled_false_refused(self):
        """enabled=false in config must refuse all actions."""
        import server.actions as amod
        # Patch _load_config to return the fresh (enabled=False) config.
        fresh_cfg = amod.ActionConfig(
            enabled=False,
            allowed_processes=["notepad.exe"],
            deny_labels=["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
        )
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_load_config", return_value=fresh_cfg):
            with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.click_control(window="notepad", name="test")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")
        self.assertIn("enabled=false", result.get("message", ""))

    def test_plan_contains_current_and_new_value(self):
        """type_text plan must include current_value and new_text."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.type_text(window="notepad", text="new", name="test")

        self.assertIn("current_value", result)
        self.assertIn("new_text", result)
        self.assertEqual(result["current_value"], "hello world")
        self.assertEqual(result["new_text"], "new")

    def test_type_text_execute_consumes_plan(self):
        """type_text execute must consume the plan (single-use)."""
        import server.actions as amod
        mock_win, mock_pats = _make_mock_window()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan_id = amod.type_text(window="notepad", text="hello", name="test")["plan_id"]
                result = amod.type_text(window="notepad", text="hello", name="test", plan_id=plan_id)

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("phase"), "execute")
        # P4 fix: type_text routes through _do_type which uses ValuePattern
        # when one is available — the SetValue call still happens, but it is
        # one level deeper in the stack.
        mock_pats["value"].SetValue.assert_called_once_with("hello")
        self.assertEqual(result.get("method"), "value")
        self.assertEqual(result.get("method_label"),
                         "via ValuePattern")

    def test_plan_id_is_unique_per_call(self):
        """Each plan has a unique ID."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                r1 = amod.click_control(window="notepad", name="test")
                r2 = amod.click_control(window="notepad", name="test")

        self.assertNotEqual(r1["plan_id"], r2["plan_id"])

    def test_control_not_found_returns_error(self):
        """Unknown window must return control_not_found."""
        import server.actions as amod

        with patch.object(amod, "_resolve_control", return_value=(None, "window not found")):
            result = amod.click_control(window="notexist", name="test")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "control_not_found")

    # ---- logging tests ----------------------------------------------------

    def test_plan_logged_to_actions_log(self):
        """A plan event must be written to logs/actions.log."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()
        log_path = self._tmp_path / "actions.log"

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                amod.click_control(window="notepad", name="test")

        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(len(lines) >= 1)
        import json as _json
        entry = _json.loads(lines[-1])
        self.assertEqual(entry["event"], "plan")
        self.assertEqual(entry["tool"], "click_control")

    def test_refusal_logged_to_actions_log(self):
        """A refusal event must be written to logs/actions.log."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()
        log_path = self._tmp_path / "actions.log"
        fresh_cfg = amod.ActionConfig(
            enabled=False,
            allowed_processes=["notepad.exe"],
            deny_labels=["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
        )

        with patch.object(amod, "_load_config", return_value=fresh_cfg):
            with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    amod.click_control(window="notepad", name="test")

        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        import json as _json
        entry = _json.loads(lines[-1])
        self.assertEqual(entry["event"], "refuse")
        self.assertEqual(entry["tool"], "click_control")

    def test_execute_logged_to_actions_log(self):
        """An execute event must be written to logs/actions.log."""
        import server.actions as amod
        mock_win, _ = _make_mock_window()
        log_path = self._tmp_path / "actions.log"

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan_id = amod.click_control(window="notepad", name="test")["plan_id"]
                amod.click_control(window="notepad", name="test", plan_id=plan_id)

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        execute_lines = [l for l in lines if '"event": "execute"' in l]
        self.assertTrue(len(execute_lines) >= 1)
        import json as _json
        entry = _json.loads(execute_lines[-1])
        self.assertEqual(entry["tool"], "click_control")

    # ---- focus_window tests (uses _ui_mod.find_window) --------------------

    def test_focus_window_non_allowlisted_refused(self):
        """focus_window to a non-allowlisted process must be refused."""
        import server.actions as amod
        import server.ui_automation as ui_mod
        mock_win, _ = _make_mock_window(process_name="badsite.exe")

        with patch.object(ui_mod, "find_window", return_value=mock_win):
            with patch.object(amod, "_process_name_from_window", return_value="badsite.exe"):
                result = amod.focus_window("badsite")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")

    def test_focus_window_stop_actions_refused(self):
        """focus_window must also obey STOP_ACTIONS."""
        import server.actions as amod
        import server.ui_automation as ui_mod
        mock_win, _ = _make_mock_window()
        (self._tmp_path / "STOP_ACTIONS").touch()

        with patch.object(ui_mod, "find_window", return_value=mock_win):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                result = amod.focus_window("notepad")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")

    def test_focus_window_enabled_false_refused(self):
        """focus_window must also obey enabled=false."""
        import server.actions as amod
        import server.ui_automation as ui_mod
        mock_win, _ = _make_mock_window()
        fresh_cfg = amod.ActionConfig(
            enabled=False,
            allowed_processes=["notepad.exe"],
            deny_labels=["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
        )

        with patch.object(amod, "_load_config", return_value=fresh_cfg):
            with patch.object(ui_mod, "find_window", return_value=mock_win):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.focus_window("notepad")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "gate_refused")

    def test_focus_window_setforegroundwindow_failure_returns_error(self):
        """If SetForegroundWindow fails, focus_window returns an honest error."""
        import server.actions as amod
        import server.ui_automation as ui_mod
        mock_win, _ = _make_mock_window()

        # Mock _SetForegroundWindow to return 0 (failure).
        orig_sfw = amod._SetForegroundWindow
        fake_sfw = MagicMock(return_value=0)
        fake_sfw.argtypes = [amod.ctypes.c_void_p]
        fake_sfw.restype = amod.ctypes.wintypes.BOOL
        amod._SetForegroundWindow = fake_sfw
        # Mock get_last_error.
        orig_err = amod.ctypes.get_last_error
        amod.ctypes.get_last_error = MagicMock(return_value=5)

        try:
            with patch.object(ui_mod, "find_window", return_value=mock_win):
                with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                    result = amod.focus_window("notepad")
        finally:
            amod._SetForegroundWindow = orig_sfw
            amod.ctypes.get_last_error = orig_err

        self.assertFalse(result.get("ok"))
        self.assertIn("foreground_refused", result.get("error", ""))


class TestMethodSelection(unittest.TestCase):
    """P4-fix tests: confirm the plan/EXECUTE pair selects and enforces the
    correct execution method for each control shape, including the
    SendInput fallbacks. No real desktop is touched."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tool_wl_act_method_")
        self._tmp_path = Path(self._tmp)
        _make_fake_config(self._tmp_path)
        self._saved = _redirect_module_paths(self._tmp_path)

    def tearDown(self):
        _restore_module_paths(self._saved)
        import shutil
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    def test_click_selects_invoke_when_invokepattern_available(self):
        import server.actions as amod
        mock_win, _ = _make_mock_window()
        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.click_control(window="notepad", name="test")
        self.assertEqual(plan["method"], "invoke")
        self.assertEqual(plan["method_label"], "via UIA Invoke")
        self.assertIn("via UIA Invoke", plan["description"])

    def test_click_fallback_to_coordinate_when_no_pattern(self):
        """A control with no UIA click pattern and no Button HWND must select
        the guarded coordinate fallback and explicitly state it in the plan."""
        import server.actions as amod
        mock_win = MagicMock()
        mock_win.Name = "Text editor"
        mock_win.AutomationId = ""
        mock_win.NativeWindowHandle = 0
        mock_win.GetParentControl.return_value = None
        mock_win.IsInvokePatternAvailable = False
        mock_win.IsTogglePatternAvailable = False
        mock_win.IsSelectionItemPatternAvailable = False
        mock_win.IsExpandCollapsePatternAvailable = False

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.click_control(window="notepad", name="test")

        self.assertEqual(plan["method"], "coordinate")
        self.assertIn(
            "via guarded coordinate click on the resolved control",
            plan["description"],
        )
        # A SendInput method's plan MUST carry the "hands off" warning.
        self.assertIn(amod.SENDINPUT_WARNING, plan["description"])
        self.assertIn("method_label", plan)

        # And execute MUST take the coordinate path — no Pattern.Invoke call.
        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                # Stub the focus guard + SendInput click: no desktop touched.
                with patch.object(amod, "_send_click_at", return_value=None) as sca, \
                     patch.object(amod, "_click_guarded",
                                  return_value=(10, 20)) as cguard, \
                     patch.object(amod, "_force_foreground", return_value=True):
                    res = amod.click_control(
                        window="notepad", name="test", plan_id=plan["plan_id"]
                    )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["method"], "coordinate")
        # The click went through the guarded helper, and the raw SendInput
        # helper was never reached directly (the guard calls it internally).
        cguard.assert_called_once()
        sca.assert_not_called()

    def test_type_selects_keyboard_when_no_value_pattern(self):
        """A control without ValuePattern and without a WM_SETTEXT-capable
        HWND must select the guarded keyboard fallback and state it."""
        import server.actions as amod
        mock_win = MagicMock()
        mock_win.Name = "Text editor"
        mock_win.AutomationId = ""
        mock_win.NativeWindowHandle = 0
        mock_win.IsValuePatternAvailable = False

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.type_text(window="notepad", text="hi", name="Text editor")

        self.assertEqual(plan["method"], "keyboard")
        self.assertIn(
            "via guarded keyboard input into the focused control",
            plan["description"],
        )
        # The SendInput warning is mandatory in the plan text.
        self.assertIn(amod.SENDINPUT_WARNING, plan["description"])

    def test_type_fallback_calls_send_input_focus_then_unicode(self):
        """Execute of a keyboard-fallback plan must focus, verify, then
        type through the guarded chunked path."""
        import server.actions as amod
        mock_win = MagicMock()
        mock_win.Name = "Text editor"
        mock_win.AutomationId = ""
        mock_win.NativeWindowHandle = 0
        mock_win.GetParentControl.return_value = None
        mock_win.IsValuePatternAvailable = False

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.type_text(window="notepad", text="hi", name="Text editor")

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                with patch.object(amod, "_send_unicode_text_guarded",
                                  return_value=2) as sut, \
                     patch.object(amod, "_uia_set_focus", return_value=None) as fcs, \
                     patch.object(amod, "_verify_typing_focus", return_value=None), \
                     patch.object(amod, "_force_foreground", return_value=True):
                    res = amod.type_text(
                        window="notepad",
                        text="hi",
                        name="Text editor",
                        plan_id=plan["plan_id"],
                    )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["method"], "keyboard")
        fcs.assert_called_once_with(mock_win)
        sut.assert_called_once()
        # What was passed to the guarded typer equals the requested text.
        self.assertEqual(sut.call_args[0][0], "hi")

    def test_type_value_path_still_calls_setvalue(self):
        """When a writable ValuePattern is present, the planner picks value
        and the executor routes to SetValue (no SendInput)."""
        import server.actions as amod
        mock_win, mock_pats = _make_mock_window()
        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.type_text(window="notepad", text="hello", name="test")
        self.assertEqual(plan["method"], "value")

        with patch.object(amod, "_resolve_control", return_value=(mock_win, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                with patch.object(amod, "_send_unicode_text_guarded",
                                  return_value=99) as sut:
                    res = amod.type_text(
                        window="notepad",
                        text="hello",
                        name="test",
                        plan_id=plan["plan_id"],
                    )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["method"], "value")
        mock_pats["value"].SetValue.assert_called_once_with("hello")
        sut.assert_not_called()

    def test_method_locks_in_plan_and_refuses_different_one(self):
        """A plan must refuse to execute if the resolved method flips
        between PLAN and EXECUTE — no silent escalation."""
        import server.actions as amod
        # PLAN: control has no value pattern -> keyboard method.
        no_vp = MagicMock()
        no_vp.Name = "Text editor"
        no_vp.AutomationId = ""
        no_vp.NativeWindowHandle = 0
        no_vp.GetParentControl.return_value = None
        no_vp.IsValuePatternAvailable = False
        with patch.object(amod, "_resolve_control", return_value=(no_vp, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                plan = amod.type_text(window="notepad", text="hi", name="Text editor")
        self.assertEqual(plan["method"], "keyboard")

        # EXECUTE: the control magically now exposes a writable ValuePattern.
        # The plan was bound to "keyboard", so execute must refuse.
        with_vp = MagicMock()
        with_vp.Name = "Text editor"
        with_vp.AutomationId = ""
        with_vp.NativeWindowHandle = 0
        with_vp.GetParentControl.return_value = None
        with_vp.IsValuePatternAvailable = True
        vp = MagicMock()
        vp.IsReadOnly = False
        with_vp.GetValuePattern.return_value = vp
        with patch.object(amod, "_resolve_control", return_value=(with_vp, "Notepad")):
            with patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
                res = amod.type_text(
                    window="notepad",
                    text="hi",
                    name="Text editor",
                    plan_id=plan["plan_id"],
                )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "plan_method_changed")

    def test_sendinput_symbols_confined_to_actions_module(self):
        """Static sanity: SendInput is used ONLY in server/actions.py."""
        root = HERE.parent / "server"
        offenders = []
        for fp in sorted(root.glob("*.py")):
            text = fp.read_text(encoding="utf-8")
            if "SendInput" in text and fp.name != "actions.py":
                offenders.append(str(fp))
        self.assertEqual(offenders, [], f"SendInput leaked: {offenders}")
        # And actions.py itself uses the symbol — the only allowed home.
        self.assertIn("SendInput",
                      (root / "actions.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
