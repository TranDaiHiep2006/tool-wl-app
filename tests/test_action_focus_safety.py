"""Unit tests for the P4 fix-round-3 focus-safety method ladder.

Covers:
- The method ladder ORDER (focus-free first, SendInput last) for both
  type_text and click_control.
- The new window-message tier (WM_SETTEXT / BM_CLICK) is preferred over
  SendInput whenever the control's class supports it.
- WM_SETTEXT is VERIFIED by read-back; a mismatch is a method FAILURE and
  never silently escalates to keyboard input.
- The focus guard aborts BEFORE any input is sent when the foreground is
  not the target.
- The guard re-verifies before every ≤20-character chunk and reports the
  honest partial character count when focus is lost mid-type.
- The coordinate-click guard hit-tests WindowFromPoint and re-checks the
  foreground immediately before clicking.
- SendInput plans carry the "hands off the keyboard" warning; focus-free
  plans do not.

No live desktop is required: all Win32 reads are stubbed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import server.actions as amod  # noqa: E402


# ---- helpers ---------------------------------------------------------------

def _ctrl(hwnd: int = 0, **patterns) -> MagicMock:
    """A UIA control mock with a native HWND and the given pattern flags."""
    c = MagicMock()
    c.Name = "Some control"
    c.AutomationId = ""
    c.NativeWindowHandle = hwnd
    c.GetParentControl.return_value = None
    c.IsValuePatternAvailable = False
    c.IsInvokePatternAvailable = False
    c.IsTogglePatternAvailable = False
    c.IsSelectionItemPatternAvailable = False
    c.IsExpandCollapsePatternAvailable = False
    for k, v in patterns.items():
        setattr(c, k, v)
    return c


def _with_value_pattern() -> MagicMock:
    c = _ctrl()
    c.IsValuePatternAvailable = True
    vp = MagicMock()
    vp.IsReadOnly = False
    c.GetValuePattern.return_value = vp
    return c


# ---- ladder order ----------------------------------------------------------

class TestMethodLadderOrder(unittest.TestCase):
    """Focus-free methods must always be preferred over SendInput."""

    def test_type_ladder_puts_value_first(self):
        c = _with_value_pattern()
        self.assertEqual(amod.type_method_ladder(c)[0], "value")
        self.assertEqual(amod._select_type_method(c), "value")

    def test_type_ladder_prefers_wmtext_over_keyboard(self):
        """A control with a plain Win32 Edit HWND must use WM_SETTEXT, never
        SendInput, even though it has no ValuePattern."""
        c = _ctrl(hwnd=1000)
        with patch.object(amod, "_class_of", return_value="Edit"):
            self.assertEqual(amod._select_type_method(c), "wmtext")
            self.assertEqual(amod.type_method_ladder(c), ["wmtext", "keyboard"])

    def test_type_ladder_falls_to_keyboard_for_non_text_classes(self):
        """Modern XAML/Document controls do not honour WM_SETTEXT, so the
        ladder must fall through to the guarded keyboard tier."""
        c = _ctrl(hwnd=2000)
        with patch.object(amod, "_class_of", return_value="RichEditD2DPT"):
            self.assertEqual(amod._select_type_method(c), "keyboard")
            self.assertEqual(amod.type_method_ladder(c), ["keyboard"])

    def test_type_ladder_falls_to_keyboard_without_hwnd(self):
        c = _ctrl(hwnd=0)
        with patch.object(amod, "_class_of", return_value=""):
            self.assertEqual(amod._select_type_method(c), "keyboard")

    def test_click_ladder_uia_patterns_precede_bmclick(self):
        c = _ctrl(hwnd=3000, IsInvokePatternAvailable=True)
        with patch.object(amod, "_class_of", return_value="Button"):
            self.assertEqual(amod._select_click_method(c), "invoke")
            self.assertEqual(amod.click_method_ladder(c)[0], "invoke")

    def test_click_ladder_prefers_bmclick_over_coordinate(self):
        """A Button-class control with no UIA pattern must use BM_CLICK
        (focus-free), not a coordinate click."""
        c = _ctrl(hwnd=3000)
        with patch.object(amod, "_class_of", return_value="Button"), \
             patch.object(amod, "_GetWindowLongW", return_value=0):
            self.assertEqual(amod._select_click_method(c), "bmclick")
            self.assertEqual(amod.click_method_ladder(c), ["bmclick", "coordinate"])

    def test_click_ladder_skips_bmclick_for_ownerdraw(self):
        """MSDN: BM_CLICK is ignored by owner-drawn buttons."""
        c = _ctrl(hwnd=3000)
        with patch.object(amod, "_class_of", return_value="Button"), \
             patch.object(amod, "_GetWindowLongW", return_value=amod._BS_OWNERDRAW):
            self.assertEqual(amod._select_click_method(c), "coordinate")

    def test_click_ladder_skips_bmclick_for_non_button_classes(self):
        c = _ctrl(hwnd=3000)
        with patch.object(amod, "_class_of", return_value="CustomWidget"):
            self.assertEqual(amod._select_click_method(c), "coordinate")

    def test_sendinput_methods_are_exactly_keyboard_and_coordinate(self):
        self.assertEqual(amod.SENDINPUT_METHODS, frozenset({"keyboard", "coordinate"}))
        # Everything else is focus-free.
        for m in ("value", "wmtext", "invoke", "toggle", "select", "expand", "bmclick"):
            self.assertNotIn(m, amod.SENDINPUT_METHODS)


# ---- WM_SETTEXT verification ----------------------------------------------

class TestWmSetTextVerification(unittest.TestCase):
    """A WM_SETTEXT write that does not stick is a FAILURE, not a reason to
    escalate to SendInput."""

    def test_verified_write_succeeds(self):
        c = _ctrl(hwnd=4000)
        with patch.object(amod, "_wm_set_text", return_value=True) as setter, \
             patch.object(amod, "_wm_get_text", return_value="hello"):
            n = amod._do_wm_set_text(c, "hello")
        self.assertEqual(n, 5)
        setter.assert_called_once_with(4000, "hello")

    def test_mismatched_readback_raises_and_does_not_escalate(self):
        """Read-back mismatch must raise, and nothing may fall through to the
        keyboard tier."""
        c = _ctrl(hwnd=4000)
        with patch.object(amod, "_wm_set_text", return_value=True), \
             patch.object(amod, "_wm_get_text", return_value="something else"), \
             patch.object(amod, "_send_unicode_text_guarded") as keyboard:
            with self.assertRaises(RuntimeError) as ctx:
                amod._do_type(c, "hello", method="wmtext")
        self.assertIn("verification failed", str(ctx.exception))
        self.assertIn("Refusing to escalate", str(ctx.exception))
        keyboard.assert_not_called()

    def test_rejected_wm_settext_raises(self):
        c = _ctrl(hwnd=4000)
        with patch.object(amod, "_wm_set_text", return_value=False), \
             patch.object(amod, "_send_unicode_text_guarded") as keyboard:
            with self.assertRaises(RuntimeError):
                amod._do_type(c, "hello", method="wmtext")
        keyboard.assert_not_called()

    def test_control_without_hwnd_cannot_use_wmtext(self):
        c = _ctrl(hwnd=0)
        with self.assertRaises(RuntimeError):
            amod._do_type(c, "hi", method="wmtext")


# ---- focus guard -----------------------------------------------------------

class TestFocusVerify(unittest.TestCase):
    """_verify_typing_focus is the gate that stands between SendInput and
    the user's foreground window."""

    def test_ok_when_foreground_is_target_and_focus_is_inside(self):
        with patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_foreground_matches", return_value=True), \
             patch.object(amod, "_pid_of", return_value=42), \
             patch.object(amod, "_focused_child_hwnd", return_value=501), \
             patch.object(amod, "_hwnd_owns", return_value=True):
            self.assertIsNone(
                amod._verify_typing_focus(500, 42, None, chars_typed=0)
            )

    def test_aborts_when_foreground_is_a_different_window(self):
        with patch.object(amod, "_foreground_hwnd", return_value=999), \
             patch.object(amod, "_foreground_matches", return_value=False), \
             patch.object(amod, "_class_of", return_value="SomeOtherApp"):
            reason = amod._verify_typing_focus(500, 42, None, chars_typed=20)
        self.assertIsNotNone(reason)
        self.assertIn("foreground window changed", reason)
        self.assertIn("20", reason)
        self.assertIn("Aborting", reason)

    def test_aborts_when_foreground_pid_differs(self):
        """Even if the hwnd comparison is inconclusive, a foreign PID must
        abort."""
        with patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_foreground_matches", return_value=True), \
             patch.object(amod, "_pid_of", return_value=777), \
             patch.object(amod, "_focused_child_hwnd", return_value=501), \
             patch.object(amod, "_hwnd_owns", return_value=True):
            reason = amod._verify_typing_focus(500, 42, None, chars_typed=5)
        self.assertIsNotNone(reason)
        self.assertIn("pid=777", reason)
        self.assertIn("target pid=42", reason)

    def test_aborts_when_focused_control_is_outside_target(self):
        with patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_foreground_matches", return_value=True), \
             patch.object(amod, "_pid_of", return_value=42), \
             patch.object(amod, "_focused_child_hwnd", return_value=8888), \
             patch.object(amod, "_hwnd_owns", return_value=False), \
             patch.object(amod, "uia") as fake_uia:
            fake_uia.GetFocusedControl.return_value = MagicMock(
                NativeWindowHandle=8888, ProcessId=99
            )
            reason = amod._verify_typing_focus(500, 42, _ctrl(), chars_typed=10)
        self.assertIsNotNone(reason)
        self.assertIn("could not confirm keyboard focus", reason)
        self.assertIn("8888", reason)

    def test_uia_focused_element_can_confirm_target(self):
        """When Win32 focus is unavailable, a UIA focused element in the
        target process is accepted as sufficient proof."""
        with patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_foreground_matches", return_value=True), \
             patch.object(amod, "_pid_of", return_value=42), \
             patch.object(amod, "_focused_child_hwnd", return_value=0), \
             patch.object(amod, "uia") as fake_uia:
            fake_uia.GetFocusedControl.return_value = MagicMock(
                NativeWindowHandle=0, ProcessId=42
            )
            self.assertIsNone(
                amod._verify_typing_focus(500, 42, _ctrl(), chars_typed=0)
            )

    def test_aborts_when_no_foreground_window_at_all(self):
        with patch.object(amod, "_foreground_hwnd", return_value=0):
            reason = amod._verify_typing_focus(500, 42, None, chars_typed=0)
        self.assertIsNotNone(reason)
        self.assertIn("no foreground window", reason)


class TestGuardedTyping(unittest.TestCase):
    """Chunked, re-verified typing."""

    def test_chunks_are_at_most_20_chars(self):
        # Mirror the real contract: _send_unicode_text returns len(text).
        with patch.object(amod, "_verify_typing_focus", return_value=None), \
             patch.object(amod, "_send_unicode_text",
                          side_effect=lambda t: len(t)) as send:
            total = amod._send_unicode_text_guarded(
                "x" * 45, 500, 42, ctrl=None
            )
        self.assertEqual(total, 45)
        lengths = [len(c[0][0]) for c in send.call_args_list]
        self.assertEqual(lengths, [20, 20, 5])
        self.assertTrue(all(n <= amod.TYPING_CHUNK_CHARS for n in lengths))

    def test_focus_verified_before_every_chunk(self):
        with patch.object(amod, "_verify_typing_focus", return_value=None) as ver, \
             patch.object(amod, "_send_unicode_text", side_effect=lambda t: len(t)), \
             patch.object(amod, "_verify_typing_focus", return_value=None) as ver:
            amod._send_unicode_text_guarded("x" * 60, 500, 42, ctrl=None)
        self.assertEqual(ver.call_count, 3)
        # Each verification carries the running character count.
        self.assertEqual(
            [c[1]["chars_typed"] for c in ver.call_args_list],
            [0, 20, 40],
        )

    def test_focus_lost_mid_type_raises_with_partial_count(self):
        """The core incident: the user steals focus after some characters
        have already landed. Typing must stop and report the exact count."""
        calls = {"n": 0}

        def _verify(target_win, target_pid, ctrl, chars_typed=0):
            # Focus is fine for the first chunk, stolen before the second.
            return None if calls["n"] == 0 else "foreground window changed"

        def _send(text):
            calls["n"] += 1
            return len(text)

        with patch.object(amod, "_verify_typing_focus", side_effect=_verify), \
             patch.object(amod, "_send_unicode_text", side_effect=_send):
            with self.assertRaises(amod.FocusGuardAbort) as ctx:
                amod._send_unicode_text_guarded("x" * 100, 500, 42, ctrl=None)
        abort = ctx.exception
        self.assertEqual(abort.chars_typed, 20)
        self.assertIn("foreground window changed", str(abort))
        # Crucially: exactly ONE chunk was delivered, nothing after.
        self.assertEqual(calls["n"], 1)

    def test_no_input_sent_when_first_verification_fails(self):
        with patch.object(amod, "_verify_typing_focus",
                          return_value="wrong window") as ver, \
             patch.object(amod, "_send_unicode_text") as send:
            with self.assertRaises(amod.FocusGuardAbort) as ctx:
                amod._send_unicode_text_guarded("hello", 500, 42, ctrl=None)
        self.assertEqual(ctx.exception.chars_typed, 0)
        send.assert_not_called()  # ZERO characters delivered
        ver.assert_called_once()

    def test_midtype_hook_can_steal_focus_for_e2e(self):
        """The test seam used by tests/manual_focus_safety_e2e.py: the hook
        runs immediately BEFORE each chunk, so it can steal focus mid-type."""
        state = {"n": 0}
        with patch.object(amod, "_verify_typing_focus",
                          side_effect=lambda *a, **k: None), \
             patch.object(amod, "_send_unicode_text",
                          side_effect=lambda t: state.__setitem__("n", state["n"] + 1) or len(t)):
            def _hook(typed):
                if typed >= 20:
                    raise amod.FocusGuardAbort(
                        "focus stolen by test hook", chars_typed=typed
                    )
            amod._MIDTYPE_INTERRUPT_HOOK = _hook
            try:
                with self.assertRaises(amod.FocusGuardAbort):
                    amod._send_unicode_text_guarded("z" * 100, 500, 42, ctrl=None)
            finally:
                amod._MIDTYPE_INTERRUPT_HOOK = None
        self.assertEqual(state["n"], 1)


class TestGuardedClick(unittest.TestCase):
    """Coordinate clicks must re-resolve, hit-test, and re-verify."""

    def test_click_aborts_when_point_resolves_to_foreign_process(self):
        ctrl = _ctrl()
        ctrl.BoundingRectangle = MagicMock(left=10, top=20)
        ctrl.BoundingRectangle.width.return_value = 100
        ctrl.BoundingRectangle.height.return_value = 50
        with patch.object(amod, "_center_of", return_value=(60, 45)), \
             patch.object(amod, "_window_from_point", return_value=1234), \
             patch.object(amod, "_pid_of", return_value=999), \
             patch.object(amod, "_send_click_at") as click:
            with self.assertRaises(amod.FocusGuardAbort) as ctx:
                amod._click_guarded(ctrl, 500, 42)
        self.assertEqual(ctx.exception.phase, "hit_test")
        self.assertIn("NOT the target process", str(ctx.exception))
        click.assert_not_called()  # nothing was clicked

    def test_click_aborts_when_foreground_is_not_target(self):
        ctrl = _ctrl()
        with patch.object(amod, "_center_of", return_value=(60, 45)), \
             patch.object(amod, "_window_from_point", return_value=1234), \
             patch.object(amod, "_pid_of", return_value=42), \
             patch.object(amod, "_foreground_hwnd", return_value=8888), \
             patch.object(amod, "_foreground_matches", return_value=False), \
             patch.object(amod, "_send_click_at") as click:
            with self.assertRaises(amod.FocusGuardAbort) as ctx:
                amod._click_guarded(ctrl, 500, 42)
        self.assertEqual(ctx.exception.phase, "foreground_verify")
        click.assert_not_called()

    def test_click_proceeds_when_all_three_checks_pass(self):
        ctrl = _ctrl()
        with patch.object(amod, "_center_of", return_value=(60, 45)), \
             patch.object(amod, "_window_from_point", return_value=1234), \
             patch.object(amod, "_pid_of", return_value=42), \
             patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_foreground_matches", return_value=True), \
             patch.object(amod, "_send_click_at") as click:
            x, y = amod._click_guarded(ctrl, 500, 42)
        self.assertEqual((x, y), (60, 45))
        click.assert_called_once_with(60, 45)


class TestForegroundRestore(unittest.TestCase):
    def test_restore_is_a_noop_when_already_foreground(self):
        """No work when the user's window is still the foreground window."""
        with patch.object(amod, "_foreground_hwnd", return_value=500), \
             patch.object(amod, "_force_foreground") as force:
            self.assertTrue(amod._restore_foreground(500))
        force.assert_not_called()

    def test_restore_forces_when_foreground_differs(self):
        with patch.object(amod, "_foreground_hwnd", return_value=7777), \
             patch.object(amod, "_force_foreground", return_value=True) as force:
            amod._restore_foreground(500)
        force.assert_called_once_with(500)

    def test_restore_never_focuses_a_null_hwnd(self):
        with patch.object(amod, "_foreground_hwnd", return_value=7777), \
             patch.object(amod, "_force_foreground") as force:
            self.assertFalse(amod._restore_foreground(0))
        force.assert_not_called()

    def test_force_foreground_returns_true_only_when_confirmed(self):
        """SetForegroundWindow returning True is not enough — we re-read
        GetForegroundWindow to confirm."""
        with patch.object(amod, "_root_hwnd", return_value=500), \
             patch.object(amod, "_SetForegroundWindow", return_value=1), \
             patch.object(amod, "_foreground_matches", return_value=False), \
             patch.object(amod, "_GetWindowThreadProcessId", return_value=0), \
             patch.object(amod, "_GetCurrentThreadId", return_value=1), \
             patch.object(amod, "_foreground_hwnd", return_value=999):
            self.assertFalse(amod._force_foreground(500))
        with patch.object(amod, "_root_hwnd", return_value=500), \
             patch.object(amod, "_SetForegroundWindow", return_value=1), \
             patch.object(amod, "_foreground_matches", return_value=True):
            self.assertTrue(amod._force_foreground(500))


# ---- plan warnings ---------------------------------------------------------

class _TempModulePaths:
    """Context helper that points actions/action_token at a throwaway config
    dir and restores the real module globals afterwards, so this file cannot
    pollute tests that run after it."""

    def __init__(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tool_wl_focus_"))
        (self._tmp / "actions.json").write_text(
            '{"enabled": true, "allowed_processes": ["notepad.exe"], '
            '"deny_labels": ["delete"]}',
            encoding="utf-8",
        )
        self._saved = {
            attr: getattr(amod, attr)
            for attr in ("CONFIG_DIR", "STOP_FILE", "ACTIONS_LOG", "ACTION_CONFIG")
        }
        import server.action_token as tok
        self._tok = tok
        self._tok_saved = {
            attr: getattr(tok, attr)
            for attr in ("CONFIG_DIR", "SECRET_FILE", "CONSUMED_DIR", "_SECRET")
        }
        amod.CONFIG_DIR = self._tmp
        amod.STOP_FILE = self._tmp / "STOP_ACTIONS"
        amod.ACTIONS_LOG = self._tmp / "actions.log"
        amod.ACTION_CONFIG = self._tmp / "actions.json"
        tok.CONFIG_DIR = self._tmp
        tok.SECRET_FILE = self._tmp / ".secret.key"
        tok.CONSUMED_DIR = self._tmp / "consumed"
        tok._ensure_dirs()
        tok._SECRET = tok._load_or_create_secret()

    def restore(self):
        for attr, val in self._saved.items():
            setattr(amod, attr, val)
        for attr, val in self._tok_saved.items():
            setattr(self._tok, attr, val)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestPlanWarnings(unittest.TestCase):
    """SendInput plans must warn; focus-free plans must not."""

    def setUp(self):
        self._paths = _TempModulePaths()

    def tearDown(self):
        self._paths.restore()

    def _plan_type(self, ctrl) -> dict:
        with patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            return amod.type_text(window="notepad", text="hi", name="x")

    def _plan_click(self, ctrl) -> dict:
        with patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            return amod.click_control(window="notepad", name="x")

    def test_keyboard_plan_carries_the_warning(self):
        ctrl = _ctrl(hwnd=0)
        with patch.object(amod, "_class_of", return_value="RichEditD2DPT"):
            plan = self._plan_type(ctrl)
        self.assertEqual(plan["method"], "keyboard")
        self.assertIn(amod.SENDINPUT_WARNING, plan["description"])
        self.assertIn("needs the target app in front", plan["description"])
        self.assertIn("Do not touch the keyboard or mouse", plan["description"])

    def test_coordinate_click_plan_carries_the_warning(self):
        ctrl = _ctrl(hwnd=0)
        with patch.object(amod, "_class_of", return_value="Custom"):
            plan = self._plan_click(ctrl)
        self.assertEqual(plan["method"], "coordinate")
        self.assertIn(amod.SENDINPUT_WARNING, plan["description"])

    def test_value_plan_has_no_warning(self):
        plan = self._plan_type(_with_value_pattern())
        self.assertEqual(plan["method"], "value")
        self.assertNotIn(amod.SENDINPUT_WARNING, plan["description"])

    def test_wmtext_plan_has_no_warning(self):
        ctrl = _ctrl(hwnd=1000)
        with patch.object(amod, "_class_of", return_value="Edit"):
            plan = self._plan_type(ctrl)
        self.assertEqual(plan["method"], "wmtext")
        self.assertNotIn(amod.SENDINPUT_WARNING, plan["description"])

    def test_invoke_click_plan_has_no_warning(self):
        plan = self._plan_click(_ctrl(hwnd=3000, IsInvokePatternAvailable=True))
        self.assertEqual(plan["method"], "invoke")
        self.assertNotIn(amod.SENDINPUT_WARNING, plan["description"])

    def test_bmclick_plan_has_no_warning(self):
        ctrl = _ctrl(hwnd=3000)
        with patch.object(amod, "_class_of", return_value="Button"), \
             patch.object(amod, "_GetWindowLongW", return_value=0):
            plan = self._plan_click(ctrl)
        self.assertEqual(plan["method"], "bmclick")
        self.assertNotIn(amod.SENDINPUT_WARNING, plan["description"])


# ---- method locking (no silent escalation) --------------------------------

class TestNoSilentEscalation(unittest.TestCase):
    """The method is chosen at PLAN, recorded in the token, and re-verified
    at EXECUTE. A change must abort, never silently escalate."""

    def setUp(self):
        self._paths = _TempModulePaths()

    def tearDown(self):
        self._paths.restore()

    def test_keyboard_plan_refuses_when_control_gains_wmtext(self):
        """PLAN picked keyboard (no Edit class); at EXECUTE the control is an
        Edit, so the ladder now says wmtext. That is a method change and must
        be refused, NOT silently upgraded/downgraded."""
        plan_ctrl = _ctrl(hwnd=0)
        with patch.object(amod, "_class_of", return_value="RichEditD2DPT"), \
             patch.object(amod, "_resolve_control", return_value=(plan_ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            plan = amod.type_text(window="notepad", text="hi", name="x")
        self.assertEqual(plan["method"], "keyboard")

        exec_ctrl = _ctrl(hwnd=1000)
        with patch.object(amod, "_class_of", return_value="Edit"), \
             patch.object(amod, "_resolve_control", return_value=(exec_ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            res = amod.type_text(
                window="notepad", text="hi", name="x", plan_id=plan["plan_id"]
            )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "plan_method_changed")
        self.assertIn("keyboard", res.get("message", ""))
        self.assertIn("wmtext", res.get("message", ""))

    def test_execute_uses_exactly_the_recorded_method(self):
        """Even with the ladder able to pick something else, execute uses the
        recorded method verbatim."""
        ctrl = _ctrl(hwnd=1000)
        with patch.object(amod, "_class_of", return_value="Edit"), \
             patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            plan = amod.type_text(window="notepad", text="hi", name="x")

        with patch.object(amod, "_class_of", return_value="Edit"), \
             patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"), \
             patch.object(amod, "_wm_set_text", return_value=True), \
             patch.object(amod, "_wm_get_text", return_value="hi"), \
             patch.object(amod, "_send_unicode_text_guarded") as keyboard:
            res = amod.type_text(
                window="notepad", text="hi", name="x", plan_id=plan["plan_id"]
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["method"], "wmtext")
        keyboard.assert_not_called()

    def test_focus_guard_abort_surfaces_partial_count(self):
        """A guarded-type abort must reach the caller as an honest partial
        result, not a generic failure."""
        ctrl = _ctrl(hwnd=0)
        with patch.object(amod, "_class_of", return_value="RichEditD2DPT"), \
             patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"):
            plan = amod.type_text(window="notepad", text="y" * 50, name="x")

        with patch.object(amod, "_class_of", return_value="RichEditD2DPT"), \
             patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value="notepad.exe"), \
             patch.object(amod, "_force_foreground", return_value=True), \
             patch.object(amod, "_uia_set_focus", return_value=None), \
             patch.object(amod, "_restore_foreground", return_value=True) as restore, \
             patch.object(amod, "_send_unicode_text",
                          side_effect=lambda t: len(t)), \
             patch.object(amod, "_verify_typing_focus",
                          side_effect=[
                              None,    # preflight before any input
                              None,    # chunk 1 (20 chars)
                              None,    # chunk 2 (20 chars)
                              "foreground changed",  # chunk 3 -> abort
                          ]) as ver:
            res = amod.type_text(
                window="notepad", text="y" * 50, name="x", plan_id=plan["plan_id"]
            )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "focus_guard_abort")
        self.assertTrue(res.get("aborted"))
        self.assertEqual(res.get("chars_typed"), 40)
        self.assertEqual(res.get("requested_chars"), 50)
        self.assertFalse(res.get("delivered_to_other_window"))
        # 1 preflight + 3 chunk verifications; the 3rd chunk never sent.
        self.assertEqual(ver.call_count, 4)
        # The user's foreground window was handed back even on abort.
        restore.assert_called()


if __name__ == "__main__":
    unittest.main()