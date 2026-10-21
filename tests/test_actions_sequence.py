"""P5 Section C tests — auto_execute_processes and run_sequence.

The problem these solve: the user needed 10+ confirmations just to open
Notepad, open a tab and type. Two mechanisms fix it structurally:

  * `auto_execute_processes` — a process in BOTH allowed_processes and
    auto_execute_processes runs click_control/type_text in ONE call.
  * `run_sequence` — ONE plan covers an ordered list of steps.

Everything here is hermetic (mocked UIA + temp config); no live desktop.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from tests.test_actions_gate import (  # noqa: E402
    _make_fake_config,
    _make_mock_window,
    _redirect_module_paths,
    _restore_module_paths,
)


def _mock_edit(process_name: str = "notepad.exe"):
    """A mock Notepad document control exposing a writable ValuePattern so
    the test exercises the focus-free tier (no foreground needed)."""
    ctrl = MagicMock()
    ctrl.Name = "Text editor"
    ctrl.AutomationId = ""
    ctrl.ControlTypeName = "DocumentControl"
    ctrl.NativeWindowHandle = 0x00ABCDEF
    ctrl.IsOffscreen = False
    ctrl.IsInvokePatternAvailable = False
    ctrl.IsTogglePatternAvailable = False
    ctrl.IsSelectionItemPatternAvailable = False
    ctrl.IsExpandCollapsePatternAvailable = False
    ctrl.IsValuePatternAvailable = True
    value = MagicMock()
    value.CurrentValue = ""
    value.IsReadOnly = False
    ctrl.GetValuePattern.return_value = value
    ctrl.GetChildren.return_value = []
    return ctrl, process_name


class _P5Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tool_wl_p5_")
        self._tmp_path = Path(self._tmp)
        _make_fake_config(self._tmp_path)
        self._saved = _redirect_module_paths(self._tmp_path)

    def tearDown(self):
        _restore_module_paths(self._saved)
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    def _config(self, **kwargs):
        _make_fake_config(self._tmp_path, **kwargs)


# =====================================================================
# C1 — auto_execute_processes
# =====================================================================

class TestAutoExecuteConfig(_P5Base):
    def test_default_is_empty(self):
        """auto_execute_processes defaults to [] — standing consent is opt-in."""
        import server.actions as amod
        self._config()
        cfg = amod.ActionConfig.load()
        self.assertEqual(cfg.auto_execute_processes, [])

    def test_missing_key_reads_as_empty(self):
        """An existing config without the key must not crash the loader."""
        import server.actions as amod
        self._config()
        raw = json.loads((self._tmp_path / "actions.json").read_text(encoding="utf-8"))
        raw.pop("auto_execute_processes", None)
        (self._tmp_path / "actions.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
        self.assertEqual(amod.ActionConfig.load().auto_execute_processes, [])

    def test_round_trips_through_save(self):
        import server.actions as amod
        cfg = amod.ActionConfig()
        cfg.auto_execute_processes = ["notepad.exe"]
        cfg._save()
        self.assertEqual(
            amod.ActionConfig.load().auto_execute_processes, ["notepad.exe"]
        )

    def test_requires_both_lists(self):
        """Being in auto_execute but NOT allowed_processes must not act."""
        import server.actions as amod
        cfg = amod.ActionConfig(
            allowed_processes=["notepad.exe"],
            auto_execute_processes=["calc.exe"],
        )
        self.assertFalse(cfg.is_auto_executable("calc.exe"))
        self.assertFalse(cfg.is_auto_executable("notepad.exe"))

    def test_auto_executable_when_in_both(self):
        import server.actions as amod
        cfg = amod.ActionConfig(
            allowed_processes=["notepad.exe"],
            auto_execute_processes=["notepad.exe"],
        )
        self.assertTrue(cfg.is_auto_executable("notepad.exe"))

    def test_case_insensitive_membership(self):
        import server.actions as amod
        cfg = amod.ActionConfig(
            allowed_processes=["Notepad.EXE"],
            auto_execute_processes=["notepad.exe"],
        )
        self.assertTrue(cfg.is_auto_executable("NOTEPAD.EXE"))


class TestAutoExecuteSingleCall(_P5Base):
    """The one-call path, and the refusals that must still apply."""

    def _run_click(self, ctrl, process, plan_id=None):
        import server.actions as amod
        with patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value=process):
            return amod.click_control(window="notepad", name="Text editor",
                                      plan_id=plan_id)

    def _run_type(self, ctrl, process, text="hello", plan_id=None):
        import server.actions as amod
        with patch.object(amod, "_resolve_control", return_value=(ctrl, "Notepad")), \
             patch.object(amod, "_process_name_from_window", return_value=process):
            return amod.type_text(window="notepad", text=text,
                                  name="Text editor", plan_id=plan_id)

    def test_click_executes_in_one_call(self):
        import server.actions as amod
        ctrl, proc = _mock_edit()
        ctrl.IsInvokePatternAvailable = True  # focus-free UIA Invoke
        self._config(auto_execute_processes=["notepad.exe"])
        pattern = MagicMock()
        with patch.object(ctrl, "GetInvokePattern", return_value=pattern):
            res = self._run_click(ctrl, proc)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["auto_executed"])
        self.assertEqual(res["phase"], "execute")
        self.assertEqual(res["method"], "invoke")
        self.assertNotIn("plan_id", res)
        pattern.Invoke.assert_called_once()

    def test_type_executes_in_one_call(self):
        ctrl, proc = _mock_edit()
        self._config(auto_execute_processes=["notepad.exe"])
        with patch("server.actions._do_type", return_value=5) as dt:
            res = self._run_type(ctrl, proc, text="hello")
        self.assertTrue(res["ok"])
        self.assertTrue(res["auto_executed"])
        dt.assert_called_once()

    def test_without_auto_config_still_returns_a_plan(self):
        """The one-call path must not engage by accident."""
        ctrl, proc = _mock_edit()
        self._config()  # no auto_execute_processes
        res = self._run_click(ctrl, proc)
        self.assertEqual(res["phase"], "plan")
        self.assertIn("plan_id", res)
        self.assertNotIn("auto_executed", res)

    def test_refused_when_stop_actions_present(self):
        """STOP_ACTIONS beats standing consent — no exceptions."""
        ctrl, proc = _mock_edit()
        self._config(auto_execute_processes=["notepad.exe"])
        (self._tmp_path / "STOP_ACTIONS").write_text("", encoding="utf-8")
        res = self._run_click(ctrl, proc)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "gate_refused")

    def test_refused_when_deny_label_matches(self):
        ctrl, proc = _mock_edit()
        ctrl.Name = "Delete everything"
        self._config(auto_execute_processes=["notepad.exe"])
        res = self._run_click(ctrl, proc)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "gate_refused")
        self.assertIn("deny_label", res["message"])

    def test_refused_when_process_not_allowlisted(self):
        ctrl, proc = _mock_edit(process_name="evil.exe")
        self._config(auto_execute_processes=["evil.exe"])
        res = self._run_click(ctrl, proc)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "gate_refused")
        self.assertIn("allowed_processes", res["message"])

    def test_refused_when_disabled(self):
        ctrl, proc = _mock_edit()
        self._config(enabled=False, auto_execute_processes=["notepad.exe"])
        res = self._run_click(ctrl, proc)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "gate_refused")

    def test_auto_execute_is_logged(self):
        """The audit log must record that no human approved this action."""
        import server.actions as amod
        ctrl, proc = _mock_edit()
        ctrl.IsInvokePatternAvailable = True
        self._config(auto_execute_processes=["notepad.exe"])
        with patch.object(ctrl, "GetInvokePattern", return_value=MagicMock()):
            self._run_click(ctrl, proc)
        log = (self._tmp_path / "actions.log").read_text(encoding="utf-8")
        self.assertIn("auto_executed=true", log)


# =====================================================================
# C2 — run_sequence
# =====================================================================

class TestRunSequenceValidation(_P5Base):
    def test_empty_steps_rejected(self):
        import server.actions as amod
        res = amod.run_sequence([])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "invalid_sequence")

    def test_too_many_steps_rejected(self):
        import server.actions as amod
        steps = [{"action": "focus_window", "window": "Notepad"}] * 11
        res = amod.run_sequence(steps)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "invalid_sequence")
        self.assertIn("maximum", res["message"])

    def test_exactly_ten_steps_accepted(self):
        import server.actions as amod
        steps = [{"action": "focus_window", "window": "Notepad"}] * 10
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(
                              [{"action": "focus_window", "process": "notepad.exe",
                                "window_title": "N", "control_name": "",
                                "method": "focus", "hwnd": 1}] * 10, ""
                          )):
            res = amod.run_sequence(steps)
        self.assertEqual(res.get("phase"), "plan")

    def test_unknown_action_rejected(self):
        import server.actions as amod
        res = amod.run_sequence([{"action": "rm_rf", "window": "Notepad"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "invalid_sequence")

    def test_missing_window_rejected(self):
        import server.actions as amod
        res = amod.run_sequence([{"action": "focus_window"}])
        self.assertFalse(res["ok"])

    def test_type_text_requires_text(self):
        import server.actions as amod
        res = amod.run_sequence([{"action": "type_text", "window": "Notepad"}])
        self.assertFalse(res["ok"])

    def test_click_requires_a_selector(self):
        import server.actions as amod
        res = amod.run_sequence([{"action": "click_control", "window": "Notepad"}])
        self.assertFalse(res["ok"])

    def test_step_number_is_reported_in_the_error(self):
        import server.actions as amod
        steps = [
            {"action": "focus_window", "window": "Notepad"},
            {"action": "click_control", "window": "Notepad"},  # no selector
        ]
        res = amod.run_sequence(steps)
        self.assertIn("step 2", res["message"])

    def test_step_must_be_an_object(self):
        import server.actions as amod
        res = amod.run_sequence(["not a dict"])
        self.assertFalse(res["ok"])


class TestRunSequenceBinding(_P5Base):
    """One token covers the whole sequence and is bound to its content."""

    def _resolved(self):
        return [
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "Untitled - Notepad", "control_name": "",
             "method": "focus", "hwnd": 5},
            {"action": "type_text", "process": "notepad.exe",
             "window_title": "Untitled - Notepad", "control_name": "Text editor",
             "automation_id": "", "control_type": "DocumentControl",
             "hwnd": 6, "method": "wmtext"},
        ]

    def _steps(self, text="hello"):
        return [
            {"action": "focus_window", "window": "Untitled - Notepad"},
            {"action": "type_text", "window": "Untitled - Notepad",
             "text": text, "name": "Text editor"},
        ]

    def _plan(self, text="hello"):
        import server.actions as amod
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(self._resolved(), "")):
            return amod.run_sequence(self._steps(text))

    def test_plan_lists_every_step(self):
        res = self._plan()
        self.assertEqual(res["phase"], "plan")
        self.assertIn("1.", res["description"])
        self.assertIn("2.", res["description"])
        self.assertIn("hello", res["description"])

    def test_execute_with_valid_token_proceeds(self):
        import server.actions as amod
        plan = self._plan()
        resolved = self._resolved()
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(resolved, "")), \
             patch.object(amod, "_execute_sequence",
                          return_value={"ok": True}) as ex:
            amod.run_sequence(self._steps(), plan_id=plan["plan_id"])
        self.assertTrue(ex.called)
        self.assertFalse(ex.call_args.kwargs.get("auto"))

    def test_tampered_step_list_refused(self):
        """Changing the text after the plan invalidates the token."""
        import server.actions as amod
        plan = self._plan("hello")
        resolved = self._resolved()
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(resolved, "")), \
             patch.object(amod, "_execute_sequence") as ex:
            res = amod.run_sequence(self._steps("EVIL"), plan_id=plan["plan_id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "BINDING_MISMATCH")
        ex.assert_not_called()

    def test_reordered_sequence_refused(self):
        import server.actions as amod
        plan = self._plan()
        reordered = list(reversed(self._steps()))
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(list(reversed(self._resolved())), "")):
            res = amod.run_sequence(reordered, plan_id=plan["plan_id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "BINDING_MISMATCH")

    def test_extra_step_appended_refused(self):
        """Appending a step to an approved sequence must not be allowed."""
        import server.actions as amod
        plan = self._plan()
        extended = self._steps() + [{"action": "focus_window",
                                     "window": "Untitled - Notepad"}]
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(self._resolved() + [{
                              "action": "focus_window", "process": "notepad.exe",
                              "window_title": "Untitled - Notepad",
                              "control_name": "", "method": "focus", "hwnd": 7,
                          }], "")):
            res = amod.run_sequence(extended, plan_id=plan["plan_id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "BINDING_MISMATCH")

    def test_garbage_token_refused(self):
        import server.actions as amod
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(self._resolved(), "")):
            res = amod.run_sequence(self._steps(), plan_id="not-a-real-token")
        self.assertFalse(res["ok"])
        self.assertIn(res["error"], {"MALFORMED", "UNKNOWN", "SIGNATURE"})

    def test_binding_hash_changes_with_content(self):
        import server.actions as amod
        a = amod._sequence_binding(self._steps("a"), self._resolved())
        b = amod._sequence_binding(self._steps("b"), self._resolved())
        self.assertNotEqual(a, b)

    def test_binding_hash_changes_with_resolved_method(self):
        """The token is bound to what will HAPPEN, not just what was asked."""
        import server.actions as amod
        steps = self._steps()
        before = self._resolved()
        after = [dict(before[0]), dict(before[1], method="keyboard")]
        self.assertNotEqual(
            amod._sequence_binding(steps, before),
            amod._sequence_binding(steps, after),
        )


class TestRunSequenceAbortSemantics(_P5Base):
    """First failure aborts; later steps are skipped, never silently run."""

    def _run(self, results):
        import server.actions as amod
        steps = [
            {"action": "focus_window", "window": "A"},
            {"action": "click_control", "window": "B", "name": "btn"},
            {"action": "type_text", "window": "C", "text": "x"},
        ]
        resolved = [
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "A", "control_name": "", "method": "focus", "hwnd": 1},
            {"action": "click_control", "process": "notepad.exe",
             "window_title": "B", "control_name": "btn", "method": "invoke",
             "hwnd": 2},
            {"action": "type_text", "process": "notepad.exe",
             "window_title": "C", "control_name": "Text editor",
             "method": "wmtext", "hwnd": 3},
        ]
        seq = iter(results)

        def _fake(action, *a, **kw):
            return next(seq)

        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(resolved, "")), \
             patch.object(amod, "focus_window", side_effect=_fake), \
             patch.object(amod, "click_control", side_effect=_fake), \
             patch.object(amod, "type_text", side_effect=_fake):
            return amod._execute_sequence(steps, resolved, plan_id="t", auto=False)

    def test_all_steps_done(self):
        res = self._run([{"ok": True, "method": "focus"},
                         {"ok": True, "method": "invoke"},
                         {"ok": True, "method": "wmtext"}])
        self.assertTrue(res["ok"])
        self.assertEqual([s["status"] for s in res["steps"]],
                         ["done", "done", "done"])
        self.assertEqual(res["completed"], 3)

    def test_step_two_fails_step_three_skipped(self):
        """The headline requirement: step 2 of 3 fails, step 3 skipped."""
        res = self._run([
            {"ok": True, "method": "focus"},
            {"ok": False, "error": "gate_refused", "message": "nope"},
            {"ok": True, "method": "wmtext"},  # must never be consumed
        ])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "sequence_aborted")
        self.assertEqual([s["status"] for s in res["steps"]],
                         ["done", "failed", "skipped"])
        self.assertEqual(res["failed_step"]["step"], 2)
        self.assertEqual(res["completed"], 1)
        self.assertEqual(res["total"], 3)
        self.assertIn("NOT run", res["message"])

    def test_first_step_failure_skips_the_rest(self):
        res = self._run([
            {"ok": False, "error": "window_not_found", "message": "gone"},
            {"ok": True}, {"ok": True},
        ])
        self.assertEqual([s["status"] for s in res["steps"]],
                         ["failed", "skipped", "skipped"])
        self.assertEqual(res["completed"], 0)

    def test_exception_in_a_step_aborts_not_crashes(self):
        import server.actions as amod
        steps = [{"action": "focus_window", "window": "A"},
                 {"action": "focus_window", "window": "B"}]
        resolved = [
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "A", "control_name": "", "method": "focus", "hwnd": 1},
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "B", "control_name": "", "method": "focus", "hwnd": 2},
        ]
        with patch.object(amod, "focus_window",
                          side_effect=[{"ok": True}, RuntimeError("boom")]):
            res = amod._execute_sequence(steps, resolved, plan_id="t", auto=False)
        self.assertFalse(res["ok"])
        self.assertEqual([s["status"] for s in res["steps"]], ["done", "failed"])
        self.assertEqual(res["failed_step"]["error"], "step_exception")

    def test_auto_flag_is_reported(self):
        res = self._run([{"ok": True}, {"ok": True}, {"ok": True}])
        self.assertIn("auto_executed", res)
        self.assertFalse(res["auto_executed"])


class TestRunSequenceAutoExecute(_P5Base):
    def test_all_auto_processes_runs_without_a_plan(self):
        import server.actions as amod
        self._config(auto_execute_processes=["notepad.exe"])
        resolved = [
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "A", "control_name": "", "method": "focus", "hwnd": 1},
            {"action": "type_text", "process": "notepad.exe",
             "window_title": "A", "control_name": "Text editor",
             "method": "wmtext", "hwnd": 2},
        ]
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(resolved, "")), \
             patch.object(amod, "focus_window", return_value={"ok": True}), \
             patch.object(amod, "type_text", return_value={"ok": True}):
            res = amod.run_sequence([
                {"action": "focus_window", "window": "A"},
                {"action": "type_text", "window": "A", "text": "hi",
                 "name": "Text editor"},
            ])
        self.assertTrue(res["ok"])
        self.assertTrue(res["auto_executed"])
        self.assertEqual(res["phase"], "execute")

    def test_one_non_auto_process_forces_a_plan(self):
        import server.actions as amod
        self._config(auto_execute_processes=["notepad.exe"])
        resolved = [
            {"action": "focus_window", "process": "notepad.exe",
             "window_title": "A", "control_name": "", "method": "focus", "hwnd": 1},
            {"action": "type_text", "process": "calc.exe",
             "window_title": "B", "control_name": "Text editor",
             "method": "wmtext", "hwnd": 2},
        ]
        with patch.object(amod, "_resolve_sequence_steps",
                          return_value=(resolved, "")):
            res = amod.run_sequence([
                {"action": "focus_window", "window": "A"},
                {"action": "type_text", "window": "B", "text": "hi"},
            ])
        self.assertEqual(res["phase"], "plan")
        self.assertIn("plan_id", res)


class TestRunSequenceGate(_P5Base):
    def test_sequence_refused_when_a_step_fails_the_gate(self):
        import server.actions as amod
        with patch.object(
            amod, "_resolve_sequence_steps",
            return_value=([], "step 2: control name 'Delete' contains deny_label"),
        ):
            res = amod.run_sequence([
                {"action": "focus_window", "window": "A"},
                {"action": "click_control", "window": "B", "name": "Delete"},
            ])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "sequence_refused")
        self.assertIn("step 2", res["message"])


if __name__ == "__main__":
    unittest.main()