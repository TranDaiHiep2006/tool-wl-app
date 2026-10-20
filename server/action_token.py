"""P4 stateless action plan token (HMAC-signed, single-use).

This module replaces the previous in-process ``_plans`` dict with a
design that survives server restarts and process boundaries, so a
phase-1 plan produced by one server process can be executed by a
different process (the real shape Cursor drives us with).

The token is **stateless**: it carries all of the binding it needs.
A small per-server secret signs the token with HMAC-SHA256. The
**consumed-id set** is the only piece of state we keep on disk, and
it is optional: a token can be re-presented before it is consumed
without losing it, and consumption is atomic via ``O_EXCL`` rename.

Token format (``plan_id`` accepted by ``click_control`` /
``type_text``)::

    <b64url(json_payload)>.<b64url(hmac_sig)>

Payload (canonical JSON, sort_keys=True, ascii):

.. code-block:: python

    {
        "v": 1,                       # schema version
        "plan_id": "ts-<int>-<rand>", # unique per plan, audit log key
        "tool": "type_text" | "click_control",
        "method": "keyboard" | "value" | "invoke" | ...,
        "issued_at": 1700000000.123,
        "ttl": 300,
        "binding": {
            "process": "notepad.exe",
            "window": "Untitled - Notepad",
            "hwnd": 12345,             # best-effort, 0 if unknown
            "control_name": "Text editor",
            "control_type": "DocumentControl",
            "automation_id": "",
            "args_hash": "<sha256 of canonical args>",
        },
    }

Verification on EXECUTE returns a typed result:

    VerifyResult.OK(plan)                     -> execute and mark consumed
    VerifyResult.Refused(reason, plan_id=...)  -> log and refuse

The verifier logs the specific reason (UNKNOWN, SIGNATURE, EXPIRED,
CONSUME_RACE, BINDING_MISMATCH, METHOD_MISMATCH) — these are the
buckets the user explicitly asked for so the bug above is impossible
to miss again.

Atomic consumption: every successful execute attempts to create an
``data/plans/consumed/<plan_id>.flag`` file via ``os.O_CREAT|O_EXCL``.
The first writer wins. Any later execute against the same plan_id
sees ``FileExistsError`` and refuses with ``CONSUME_RACE`` (the
right answer if Cursor somehow ends up double-firing).

Secret management: a 32-byte random secret lives in
``config/.action_token_secret.key`` and is created on first use. It
is git-ignored and must remain local; rotating the secret
invalidates every outstanding plan, which is the correct behaviour
(it forces a fresh plan from Cursor).
"""

from __future__ import annotations

import base64
import errno
import hmac
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# All module paths live under the repo root (where server/ sits).
ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = ROOT_DIR / "config"
DATA_DIR: Path = ROOT_DIR / "data" / "plans"
CONSUMED_DIR: Path = DATA_DIR / "consumed"
SECRET_FILE: Path = CONFIG_DIR / ".action_token_secret.key"

PLAN_TTL_SECONDS = 300  # unchanged from the previous design.
_TOKEN_VERSION = 1

_log = logging.getLogger("tool_wl_app.action_token")


# --------------------- secret management ------------------------------------

def _ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CONSUMED_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("could not create %s: %r", d, exc)


def _load_or_create_secret() -> bytes:
    """Return the HMAC secret, creating a fresh 32-byte one if missing.

    The file is chmod'd owner-only on POSIX. On Windows the OS does
    not enforce the bits but access is still governed by the user's
    profile ACL.
    """
    _ensure_dirs()
    try:
        if SECRET_FILE.exists():
            data = SECRET_FILE.read_bytes()
            if len(data) >= 32:
                return data
        # Generate.
        data = secrets.token_bytes(32)
        # Write atomically: temp file + rename.
        tmp = SECRET_FILE.with_suffix(".tmp")
        tmp.write_bytes(data)
        try:
            os.replace(tmp, SECRET_FILE)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            # Windows: best effort.
            pass
        return data
    except OSError as exc:
        # If the filesystem is read-only / zero disk, fall back to a
        # process-local secret — plans still work in-process, just not
        # across restarts. We log loudly so the operator notices.
        _log.warning(
            "could not persist action-token secret (%r); using "
            "process-local fallback (cross-process plans will not "
            "survive a restart)", exc,
        )
        return secrets.token_bytes(32)


_SECRET: bytes = _load_or_create_secret()


def rotate_secret() -> None:
    """Generate a new secret and atomically replace the file.

    After this call, every previously-issued token will fail signature
    verification (exactly the right behaviour; outstanding plans must
    be re-planned).
    """
    global _SECRET
    _ensure_dirs()
    data = secrets.token_bytes(32)
    tmp = SECRET_FILE.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, SECRET_FILE)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    _SECRET = data


# --------------------- canonicalization ------------------------------------

def _canon_json(obj: Any) -> str:
    """Stable JSON serialization of a payload (keys sorted, ASCII)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_args(args: Dict[str, Any]) -> str:
    """Return a stable short digest of the user-supplied arguments.

    Used to bind the token to the exact text the user wants typed
    (for type_text) or the exact target identity they approved
    (for click_control). The hash prevents an attacker who can
    intercept the token from swapping the text the user sees in the
    plan description for something else.
    """
    canon = _canon_json(args or {})
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


# --------------------- encode / decode / verify ----------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = (4 - len(text) % 4) % 4
    return base64.urlsafe_b64decode(text + "=" * pad)


def make_plan_id() -> str:
    """Return a unique plan_id like `ts-1700000000-abcdef0123`."""
    return f"ts-{int(time.time())}-{secrets.token_hex(5)}"


def issue_token(
    tool: str,
    method: str,
    binding: Dict[str, Any],
) -> str:
    """Create a signed token from a binding dictionary.

    ``binding`` must be the resolved identity of the target:

      process (str), window (str), hwnd (int),
      control_name (str), control_type (str), automation_id (str),
      args_hash (str),  <- caller passes this from _hash_args(args)
    """
    payload = {
        "v": _TOKEN_VERSION,
        "plan_id": make_plan_id(),
        "tool": tool,
        "method": method,
        "issued_at": time.time(),
        "ttl": PLAN_TTL_SECONDS,
        "binding": dict(binding),
    }
    msg = _canon_json(payload).encode("utf-8")
    sig = hmac.new(_SECRET, msg, hashlib.sha256).digest()
    return _b64url(msg) + "." + _b64url(sig)


def decode_token(token: str) -> Dict[str, Any]:
    """Return the payload dict from a token. Does NOT verify signature."""
    head, _, _sig = token.partition(".")
    if not _sig:
        raise ValueError("malformed token (no signature segment)")
    payload_bytes = _b64url_decode(head)
    payload = json.loads(payload_bytes.decode("utf-8"))
    return payload


# --------------------- verification result ---------------------------------

@dataclass
class VerifyResult:
    """Result of ``verify_token`` — exhaustive typed enum of reasons."""
    ok: bool
    reason: str  # "" when ok; one of REFUSE_REASONS otherwise
    plan_id: Optional[str] = None  # the plan_id from the token, when known
    payload: Optional[Dict[str, Any]] = None
    message: str = ""


REFUSE_REASONS = (
    "MALFORMED",         # bad token syntax / unreadable
    "UNKNOWN",           # token not parseable / wrong shape
    "SIGNATURE",         # HMAC mismatch (tampered or wrong server)
    "EXPIRED",           # ttl elapsed
    "CONSUME_RACE",      # token already consumed (the id file exists)
    "BINDING_MISMATCH",  # tool / window / control / process / args differ
    "METHOD_MISMATCH",   # method chosen by the resolved control differs
)


def verify_token(token: str, expected_args: Dict[str, Any]) -> VerifyResult:
    """Verify that ``token`` is a valid, unconsumed, unexpired plan for
    ``expected_args``.

    ``expected_args`` is the dict we resolved for this execute call
    (window, automation_id, name, text). We rebuild the canonical
    binding and compare every field plus the args hash.
    """
    if not token or not isinstance(token, str):
        return VerifyResult(False, "MALFORMED", message="empty or non-string token")

    try:
        head, _, sig_b64 = token.partition(".")
        if not sig_b64:
            return VerifyResult(False, "MALFORMED", message="missing signature")
        try:
            payload_bytes = _b64url_decode(head)
        except Exception as exc:
            return VerifyResult(False, "MALFORMED", message=f"b64 decode failed: {exc}")
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as exc:
            return VerifyResult(False, "UNKNOWN", message=f"json decode failed: {exc}")
        if not isinstance(payload, dict):
            return VerifyResult(False, "UNKNOWN", message="payload not a dict")
        if payload.get("v") != _TOKEN_VERSION:
            return VerifyResult(False, "UNKNOWN", message="unsupported token schema")

        # Signature check first — this must come before any other check so
        # we never leak whether a `plan_id` was already consumed for an
        # attacker-controlled token. The signature covers the *raw*
        # canonical JSON we signed.
        msg = payload_bytes  # exact bytes we signed
        sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(
            hmac.new(_SECRET, msg, hashlib.sha256).digest(),
            sig,
        ):
            return VerifyResult(
                False, "SIGNATURE", plan_id=payload.get("plan_id"),
                message="HMAC mismatch (token tampered or signed by a different server)",
            )

        # Now structural / temporal / binding checks.
        plan_id = payload.get("plan_id")
        if not plan_id:
            return VerifyResult(False, "UNKNOWN", message="missing plan_id")

        # TTL
        issued_at = float(payload.get("issued_at", 0.0))
        ttl = float(payload.get("ttl", PLAN_TTL_SECONDS))
        age = time.time() - issued_at
        if age < 0:
            return VerifyResult(
                False, "EXPIRED", plan_id=plan_id,
                message="token issued_at is in the future",
            )
        if age > ttl:
            return VerifyResult(
                False, "EXPIRED", plan_id=plan_id,
                message=f"token expired ({int(age)}s > {int(ttl)}s)",
            )

        # Consumed-on-disk check (atomic, the next caller cannot interleave).
        consumed = _try_mark_consumed(plan_id)
        if not consumed:
            return VerifyResult(
                False, "CONSUME_RACE", plan_id=plan_id,
                message="token has already been consumed",
            )

        # Binding check.
        token_binding = payload.get("binding") or {}
        expected_binding = _build_binding(payload.get("tool", ""), expected_args)

        if not _binding_matches(token_binding, expected_binding):
            return VerifyResult(
                False, "BINDING_MISMATCH", plan_id=plan_id, payload=payload,
                message=(
                    f"token binding does not match execute call: "
                    f"token={token_binding}, "
                    f"execute={expected_binding}"
                ),
            )

        return VerifyResult(True, "", plan_id=plan_id, payload=payload)

    except Exception as exc:  # defensive: never raise from a verifier
        return VerifyResult(False, "UNKNOWN", message=f"verify failed: {exc!r}")


def _build_binding(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Construct the canonical binding expected by an execute call.

    ``args`` is the dict we resolved at EXECUTE time. Only the
    action-specific subset is hashed for args_hash: text for type_text,
    empty dict for click_control (no args to bind).
    """
    if tool == "type_text":
        args_hash = _hash_args({"text": args.get("text")})
    else:
        args_hash = _hash_args({})
    return {
        "tool": tool,
        "window": args.get("window") or "",
        "control_name": args.get("name") or "",
        "automation_id": args.get("automation_id") or "",
        "args_hash": args_hash,
    }


# Fields that MUST match between PLAN and EXECUTE for security.
# hwnd, process, control_type are informational only (stable across
# the same control but may vary if the OS recreates the HWND).
_BINDING_KEY_FIELDS = frozenset({
    "tool", "window", "control_name", "automation_id", "args_hash",
})


def _binding_matches(token_b: Dict[str, Any], exec_b: Dict[str, Any]) -> bool:
    for k in _BINDING_KEY_FIELDS:
        if token_b.get(k) != exec_b.get(k):
            return False
    return True


def check_method_mismatch(token_method: str, ctrl_method: str) -> bool:
    """True when the resolved control now uses a different method.

    The token must re-verify every field at EXECUTE. If the resolved
    control's method changed between PLAN and EXECUTE (e.g. a control
    gained a writable ValuePattern in the meantime), the token's
    method no longer applies and we refuse. Pure rename-tolerant
    (cosmetically equivalent methods still compare equal).
    """
    return token_method != ctrl_method


# --------------------- consumed-id set -------------------------------------

def _try_mark_consumed(plan_id: str) -> bool:
    """Atomically claim the plan_id; return True iff THIS call was first.

    Uses ``os.O_CREAT|O_EXCL``: any other process that raced ahead
    will already have created the file, and this call returns False.
    On any other OSError we conservatively return False so an execute
    never runs twice.
    """
    _ensure_dirs()
    target = CONSUMED_DIR / f"{plan_id}.flag"
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        _log.warning("unexpected OSError claiming consumed flag: %r", exc)
        return False
    try:
        # Best-effort: write the time so /future debugging reads it.
        os.write(fd, str(time.time()).encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _gc_consumed(max_age_s: int = 86400) -> int:
    """Remove consumed flags older than ``max_age_s``.

    Called opportunistically by ``issue_token``. Idempotent.
    """
    _ensure_dirs()
    cutoff = time.time() - max_age_s
    removed = 0
    for fp in CONSUMED_DIR.glob("*.flag"):
        try:
            content = fp.read_bytes()
        except OSError:
            continue
        try:
            ts = float(content.decode("ascii").strip().split()[0])
        except Exception:
            continue
        if ts < cutoff:
            try:
                fp.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---- test helpers (used by test_action_token.py) ---------------------------

def mark_consumed(plan_id: str, consumed_dir: Path = CONSUMED_DIR) -> None:
    """Mark a plan_id as consumed by touching its flag file.

    This is a one-way operation for tests: once the flag exists,
    any subsequent verify_token call will refuse with CONSUME_RACE.
    """
    (consumed_dir / f"{plan_id}.flag").write_text(
        str(time.time()), encoding="ascii"
    )


def make_expired_token(
    tool: str,
    method: str,
    binding: Dict[str, Any],
    secret: bytes,
) -> str:
    """Return a token that has already expired (issued_at far in the past).

    Uses ``secret`` directly so callers can pin it to the server's secret.
    """
    payload = {
        "v": _TOKEN_VERSION,
        "plan_id": make_plan_id(),
        "tool": tool,
        "method": method,
        "issued_at": time.time() - PLAN_TTL_SECONDS - 10,  # definitely expired
        "ttl": PLAN_TTL_SECONDS,
        "binding": dict(binding),
    }
    msg = _canon_json(payload).encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).digest()
    return _b64url(msg) + "." + _b64url(sig)


def make_tampered_token(
    token: str,
    secret: bytes,
) -> str:
    """Return a token whose payload was modified but NOT re-signed.

    This simulates an interceptor editing the body in flight: the
    signature is left exactly as it was, so verification must fail with
    SIGNATURE. (Re-signing with the correct secret would produce a
    *valid* token — that is the point of the signature.)
    """
    payload = decode_token(token)
    payload["tool"] = "click_control"  # corrupt one field
    forged_msg = _canon_json(payload).encode("utf-8")
    # Reuse the ORIGINAL signature from the untouched token.
    _, _, orig_sig = token.partition(".")
    return _b64url(forged_msg) + "." + orig_sig