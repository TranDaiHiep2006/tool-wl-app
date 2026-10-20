"""Unit tests for the stateless action-plan token module.

Tests cover:
- Token round-trip (issue → decode → verify → OK)
- Each refusal bucket: MALFORMED, UNKNOWN, SIGNATURE, EXPIRED, CONSUME_RACE,
  BINDING_MISMATCH, METHOD_MISMATCH
- cross-process simulation: plan from server A, execute in server B
  (same HMAC secret + consumed-on-disk set)
- Same-process still works (token verified + executed)
- tamper, expiry, and reuse all refused with specific reasons
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestTokenRoundTrip(unittest.TestCase):
    """Token can be issued, decoded, and verified in one round-trip."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tok_round_"))
        import server.action_token as t
        t.CONFIG_DIR = self._tmp
        t.SECRET_FILE = self._tmp / ".secret.key"
        t.CONSUMED_DIR = self._tmp / "consumed"
        t._ensure_dirs()
        t._SECRET = t._load_or_create_secret()
        self._tok = t
        self._tok._gc_consumed(0)  # clean slate

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    def test_issue_and_decode_round_trip(self):
        t = self._tok
        binding = {
            "tool": "type_text",
            "process": "notepad.exe",
            "window": "Untitled - Notepad",
            "hwnd": 123456,
            "control_name": "Text editor",
            "control_type": "DocumentControl",
            "automation_id": "",
            "args_hash": t._hash_args({"text": "hello"}),
        }
        token = t.issue_token(tool="type_text", method="keyboard", binding=binding)
        self.assertIn(".", token)
        payload = t.decode_token(token)
        self.assertEqual(payload["tool"], "type_text")
        self.assertEqual(payload["method"], "keyboard")
        self.assertIn("plan_id", payload)
        self.assertEqual(payload["binding"]["tool"], "type_text")

    def test_verify_ok_same_binding(self):
        t = self._tok
        binding = {
            "tool": "type_text",
            "process": "notepad.exe",
            "window": "Untitled - Notepad",
            "hwnd": 123456,
            "control_name": "Text editor",
            "control_type": "DocumentControl",
            "automation_id": "",
            "args_hash": t._hash_args({"text": "hello"}),
        }
        token = t.issue_token(tool="type_text", method="keyboard", binding=binding)
        args = {
            "window": "Untitled - Notepad",
            "automation_id": "",
            "name": "Text editor",
            "text": "hello",
        }
        vr = t.verify_token(token, args)
        self.assertTrue(vr.ok)
        self.assertEqual(vr.reason, "")
        self.assertIsInstance(vr.plan_id, str)
        self.assertTrue(vr.plan_id.startswith("ts-"))


class TestRefusalBuckets(unittest.TestCase):
    """Every refusal bucket is reachable and returns its specific reason."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tok_refusal_"))
        import server.action_token as t
        t.CONFIG_DIR = self._tmp
        t.SECRET_FILE = self._tmp / ".secret.key"
        t.CONSUMED_DIR = self._tmp / "consumed"
        t._ensure_dirs()
        t._SECRET = t._load_or_create_secret()
        self._tok = t
        self._tok._gc_consumed(0)

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    def _binding(self, **kw):
        defaults = dict(
            tool="type_text",
            process="notepad.exe",
            window="Untitled - Notepad",
            hwnd=123,
            control_name="Text editor",
            control_type="DocumentControl",
            automation_id="",
            args_hash=self._tok._hash_args({"text": "hello"}),
        )
        defaults.update(kw)
        return defaults

    def test_malformed_empty(self):
        vr = self._tok.verify_token("", {})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "MALFORMED")

    def test_malformed_no_sig(self):
        # A token with no "." separator has no signature segment at all.
        vr = self._tok.verify_token("eyJ2IjoxfQ", {})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "MALFORMED")

    def test_unknown_wrong_version(self):
        t = self._tok
        # Encode a payload with version 99.
        import base64, hmac as _hmac, hashlib as _hl
        payload = {"v": 99, "plan_id": "x", "tool": "t", "method": "v",
                   "issued_at": time.time(), "ttl": 300,
                   "binding": self._binding()}
        msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = _hmac.new(t._SECRET, msg, _hl.sha256).digest()
        token = base64.urlsafe_b64encode(msg).rstrip(b"=").decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        vr = t.verify_token(token, {})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "UNKNOWN")

    def test_signature_mismatch(self):
        t = self._tok
        # Use a different secret to sign.
        import secrets, hmac as _hmac, hashlib as _hl
        wrong_secret = secrets.token_bytes(32)
        binding = self._binding()
        payload = {"v": 1, "plan_id": "ts-x", "tool": "type_text",
                   "method": "keyboard", "issued_at": time.time(),
                   "ttl": 300, "binding": binding}
        msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = _hmac.new(wrong_secret, msg, _hl.sha256).digest()
        import base64
        token = base64.urlsafe_b64encode(msg).rstrip(b"=").decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        vr = t.verify_token(token, {})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "SIGNATURE")
        self.assertIsInstance(vr.plan_id, str)
        self.assertTrue(vr.plan_id.startswith("ts-"))

    def test_expired(self):
        t = self._tok
        token = t.make_expired_token("type_text", "keyboard", self._binding(), t._SECRET)
        vr = t.verify_token(token, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "hello"})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "EXPIRED")
        self.assertIsInstance(vr.plan_id, str)
        self.assertTrue(vr.plan_id.startswith("ts-"))

    def test_consume_race(self):
        t = self._tok
        binding = self._binding()
        token = t.issue_token("type_text", "keyboard", binding)
        plan_id = t.decode_token(token)["plan_id"]
        # Simulate server A consumed it.
        t.mark_consumed(plan_id, t.CONSUMED_DIR)
        vr = t.verify_token(token, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "hello"})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "CONSUME_RACE")

    def test_binding_mismatch_tool(self):
        t = self._tok
        binding = self._binding(tool="click_control")
        token = t.issue_token("click_control", "invoke", binding)
        vr = t.verify_token(token, {"window": "Some Other Window", "automation_id": "", "name": "Edit"})  # wrong tool + window
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "BINDING_MISMATCH")

    def test_binding_mismatch_text(self):
        t = self._tok
        binding = self._binding()
        token = t.issue_token("type_text", "keyboard", binding)
        vr = t.verify_token(token, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "different text"})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "BINDING_MISMATCH")

    def test_method_mismatch(self):
        t = self._tok
        token_method = "value"
        resolved_method = "keyboard"
        self.assertTrue(t.check_method_mismatch(token_method, resolved_method))
        self.assertFalse(t.check_method_mismatch("keyboard", "keyboard"))


class TestCrossProcessSimulation(unittest.TestCase):
    """Simulate server A issues a plan, server B executes it — same secret,
    consumed-on-disk set survives the process boundary."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="tok_xproc_"))
        import server.action_token as t
        t.CONFIG_DIR = self._tmp
        t.SECRET_FILE = self._tmp / ".secret.key"
        t.CONSUMED_DIR = self._tmp / "consumed"
        t._ensure_dirs()
        t._SECRET = t._load_or_create_secret()
        self._tok = t
        self._tok._gc_consumed(0)

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self._tmp)
        except Exception:
            pass

    def test_plan_issued_by_a_validates_in_b(self):
        """Server A issues; Server B verifies with the same secret → OK."""
        t = self._tok
        binding = {
            "tool": "type_text", "process": "notepad.exe",
            "window": "Untitled - Notepad", "hwnd": 123,
            "control_name": "Text editor", "control_type": "DocumentControl",
            "automation_id": "", "args_hash": t._hash_args({"text": "hello"}),
        }
        token = t.issue_token("type_text", "keyboard", binding)
        # Server B verifies (same secret, same binding).
        vr = t.verify_token(token, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "hello"})
        self.assertTrue(vr.ok)

    def test_plan_consumed_by_a_refused_by_b(self):
        """Server A consumes the plan; Server B then tries → CONSUME_RACE."""
        t = self._tok
        binding = {
            "tool": "type_text", "process": "notepad.exe",
            "window": "Untitled - Notepad", "hwnd": 123,
            "control_name": "Text editor", "control_type": "DocumentControl",
            "automation_id": "", "args_hash": t._hash_args({"text": "hello"}),
        }
        token = t.issue_token("type_text", "keyboard", binding)
        plan_id = t.decode_token(token)["plan_id"]
        # Server A marks consumed.
        t.mark_consumed(plan_id, t.CONSUMED_DIR)
        # Server B tries — consumed flag survives the "process boundary".
        vr = t.verify_token(token, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "hello"})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "CONSUME_RACE")

    def test_plan_tampered_by_interceptor_refused(self):
        """An attacker who intercepts the token and changes the text → SIGNATURE."""
        t = self._tok
        binding = {
            "tool": "type_text", "process": "notepad.exe",
            "window": "Untitled - Notepad", "hwnd": 123,
            "control_name": "Text editor", "control_type": "DocumentControl",
            "automation_id": "", "args_hash": t._hash_args({"text": "hello"}),
        }
        token = t.issue_token("type_text", "keyboard", binding)
        tampered = t.make_tampered_token(token, t._SECRET)
        vr = t.verify_token(tampered, {"window": "Untitled - Notepad", "automation_id": "", "name": "Text editor", "text": "hello"})
        self.assertFalse(vr.ok)
        self.assertEqual(vr.reason, "SIGNATURE")


if __name__ == "__main__":
    unittest.main()
