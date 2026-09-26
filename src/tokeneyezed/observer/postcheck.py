"""Deterministic, attempt-scoped checks for completed tool calls."""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .core import Decision, validate_event

INTENT_HEADER = "X-Tokeneyezed-Intent"
MAX_INTENT_CHARS = 2048
_MAX_TRACKED_ATTEMPTS = 256
_MAX_FAILURES_PER_ATTEMPT = 64
_FILE_SUFFIXES = {
    "c",
    "cc",
    "cpp",
    "css",
    "go",
    "h",
    "html",
    "java",
    "js",
    "jsx",
    "json",
    "md",
    "py",
    "ps1",
    "rs",
    "sh",
    "toml",
    "ts",
    "tsx",
    "txt",
    "yaml",
    "yml",
}
_PATH_TOKEN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/])?(?:[\w.-]+[\\/])+[\w.-]+"
    r"|(?<![\w./\\])[\w.-]+\.(?:c|cc|cpp|css|go|h|html|java|js|jsx|json|md|py|ps1|"
    r"rs|sh|toml|ts|tsx|txt|yaml|yml)\b",
    re.IGNORECASE,
)
_EXCLUDED_SCOPE = re.compile(
    r"\b(?:without|don't|do not|avoid|never)\s+"
    r"(?:touch(?:ing)?|edit(?:ing)?|modify(?:ing)?|chang(?:e|ing))\s+"
    r"(.+?)(?=\s+(?:and|but|while)\b|[;,]|$)",
    re.IGNORECASE,
)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete|Move to) File: (.+)$", re.MULTILINE)
_FAILED_TEXT = re.compile(
    r"(?im)(?:^\s*(?:error|fatal|failed|failure|exception|traceback|permission denied|"
    r"command not found|no such file or directory)\s*[:.]?|"
    r"\b(?:command|tool|process)\s+(?:has\s+)?failed\b|"
    r"\bexit(?:ed)?(?:\s+with)?\s+(?:code\s+)?[1-9]\d*\b|"
    r"\b[1-9]\d*\s+(?:tests?\s+)?failed\b|"
    r"\b(?:assertionerror|attributeerror|importerror|keyerror|modulenotfounderror|"
    r"nameerror|oserror|runtimeerror|syntaxerror|timeout(?:error)?|typeerror|valueerror)\b)"
)
_TEST_COMMAND = re.compile(
    r"(?:^|[;&|\s])(?:uv\s+run\s+)?(?:python(?:3)?\s+-m\s+)?"
    r"(?:pytest|unittest|tox|nox|cargo\s+test|go\s+test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+(?:run\s+)?test|make\s+test)\b",
    re.IGNORECASE,
)


def encode_intent_header(intent: str) -> str:
    """Encode bounded intent as an ASCII-safe HTTP header value."""
    if not isinstance(intent, str) or not intent:
        return ""
    return base64.urlsafe_b64encode(intent[:MAX_INTENT_CHARS].encode("utf-8")).decode("ascii")


def decode_intent_header(value: str) -> str:
    if not value or len(value) > 4096:
        return ""
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        if len(decoded) > MAX_INTENT_CHARS:
            return ""
        return decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


@dataclass
class _AttemptState:
    no_progress_calls: int = 0
    last_no_progress_note: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    repeated_failure_notes: set[str] = field(default_factory=set)
    off_scope_notes: set[str] = field(default_factory=set)


def _normalized_path(value: str) -> str:
    value = value.strip("`'\".,;:()[]{}<> ").replace("\\", "/")
    value = posixpath.normpath(value)
    value = re.sub(r"^[a-z]:/?", "", value, flags=re.IGNORECASE)
    return value.lstrip("/").casefold()


def _intent_paths(intent: str) -> tuple[set[str], set[str]]:
    excluded: set[str] = set()
    for match in _EXCLUDED_SCOPE.finditer(intent):
        excluded.update(_normalized_path(path) for path in _PATH_TOKEN.findall(match.group(1)))
    remaining = _EXCLUDED_SCOPE.sub("", intent)
    scoped = {
        _normalized_path(path)
        for path in _PATH_TOKEN.findall(remaining)
        if _normalized_path(path) not in excluded
    }
    return scoped, excluded


def _edit_targets(event: dict) -> set[str]:
    if event["tool"] not in ("edit", "write"):
        return set()
    payload = event["input"]
    targets: set[str] = set()
    if isinstance(payload, dict):
        for key in ("file_path", "path", "filename"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                targets.add(_normalized_path(value))
        patch = payload.get("patch", payload.get("command", ""))
    else:
        patch = payload
    if isinstance(patch, str):
        targets.update(_normalized_path(path) for path in _PATCH_FILE.findall(patch))
    return targets


def _is_file_path(path: str) -> bool:
    suffix = path.rsplit("/", 1)[-1].rsplit(".", 1)
    return len(suffix) == 2 and suffix[-1] in _FILE_SUFFIXES


def _matches_scope(target: str, scope: str) -> bool:
    if target == scope or target.endswith("/" + scope):
        return True
    if not _is_file_path(scope):
        return target.startswith(scope.rstrip("/") + "/") or (
            "/" + scope.rstrip("/") + "/"
        ) in target
    return False


def _out_of_scope_targets(event: dict, intent: str) -> list[tuple[str, str]]:
    targets = _edit_targets(event)
    if not targets or not intent:
        return []
    scopes, excluded = _intent_paths(intent)
    violations = []
    for target in sorted(targets):
        if any(_matches_scope(target, path) for path in excluded):
            violations.append((target, "explicitly excluded"))
        elif scopes and not any(_matches_scope(target, path) for path in scopes):
            violations.append((target, "outside the explicit file scope"))
    return violations


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _strings(child)


def _structured_failure(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("is_error", "isError", "failed", "failure"):
            if value.get(key) is True:
                return True
        for key in ("exit_code", "exitCode", "return_code", "returncode"):
            code = value.get(key)
            if isinstance(code, int) and not isinstance(code, bool) and code != 0:
                return True
        if str(value.get("status", "")).casefold() in {"error", "failed", "failure"}:
            return True
        if isinstance(value.get("error"), str) and value["error"].strip():
            return True
        return any(_structured_failure(child) for child in value.values())
    if isinstance(value, list | tuple):
        return any(_structured_failure(child) for child in value)
    return False


def _tool_failed(event: dict) -> bool:
    summary = event.get("output_summary")
    if not isinstance(summary, str) or not summary:
        return False
    try:
        result = json.loads(summary)
    except json.JSONDecodeError:
        result = summary
    if _structured_failure(result):
        return True
    return any(_FAILED_TEXT.search(text) for text in _strings(result))


def _failure_signature(event: dict) -> str:
    encoded = json.dumps(
        [event["tool"], event["input"]], sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _command(event: dict) -> str:
    payload = event["input"]
    if isinstance(payload, str):
        return payload
    command = payload.get("command") if isinstance(payload, dict) else None
    return command if isinstance(command, str) else ""


def _made_progress(event: dict, failed: bool) -> bool:
    if failed:
        return False
    if event["tool"] in ("edit", "write"):
        return True
    return event["tool"] == "bash" and bool(_TEST_COMMAND.search(_command(event)))


class PostChecker:
    """Issue corrective notes for clear scope drift, repeated errors, or stalled work.

    State is bounded and lives for one observer-service process. Checks intentionally use explicit
    paths and identical failed inputs rather than guessing semantic similarity.
    """

    def __init__(self, no_progress_limit: int = 8, max_attempts: int = _MAX_TRACKED_ATTEMPTS):
        if isinstance(no_progress_limit, bool) or not isinstance(no_progress_limit, int):
            raise ValueError("no_progress_limit must be a positive integer")
        if no_progress_limit < 1:
            raise ValueError("no_progress_limit must be a positive integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be a positive integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.no_progress_limit = no_progress_limit
        self.max_attempts = max_attempts
        self._attempts: OrderedDict[tuple[str, str], _AttemptState] = OrderedDict()

    def _state_for(self, event: dict) -> tuple[tuple[str, str], _AttemptState]:
        key = (event["session_id"], event["attempt_id"])
        state = self._attempts.get(key)
        if state is None:
            state = _AttemptState()
            self._attempts[key] = state
        self._attempts.move_to_end(key)
        while len(self._attempts) > self.max_attempts:
            self._attempts.popitem(last=False)
        return key, state

    def check(self, event: dict, intent: str = "") -> Decision:
        validate_event(event)
        if event["phase"] != "post":
            return Decision("allow")
        _, state = self._state_for(event)
        messages = []

        for target, reason in _out_of_scope_targets(event, intent):
            if target not in state.off_scope_notes:
                state.off_scope_notes.add(target)
                messages.append(
                    f"Edit target `{target}` is {reason} in the declared intent for attempt "
                    f"`{event['attempt_id']}`. Keep edits within the named scope or request a "
                    "revised intent."
                )

        failed = _tool_failed(event)
        if failed:
            signature = _failure_signature(event)
            if signature not in state.failures and len(state.failures) >= _MAX_FAILURES_PER_ATTEMPT:
                oldest = next(iter(state.failures))
                del state.failures[oldest]
                state.repeated_failure_notes.discard(oldest)
            count = state.failures.get(signature, 0) + 1
            state.failures[signature] = count
            if count == 2 and signature not in state.repeated_failure_notes:
                state.repeated_failure_notes.add(signature)
                messages.append(
                    f"The same tool input has failed twice in attempt `{event['attempt_id']}`. "
                    "Use the previous error to change the input instead of retrying it unchanged."
                )

        if _made_progress(event, failed):
            state.no_progress_calls = 0
            state.last_no_progress_note = 0
        else:
            state.no_progress_calls += 1
            if (
                state.no_progress_calls >= self.no_progress_limit
                and state.no_progress_calls - state.last_no_progress_note >= self.no_progress_limit
            ):
                state.last_no_progress_note = state.no_progress_calls
                messages.append(
                    f"There has been no successful edit or test run in the last "
                    f"{self.no_progress_limit} post-tool calls for attempt "
                    f"`{event['attempt_id']}`. "
                    "Make a scoped change or run the relevant tests."
                )

        return Decision("note", " ".join(messages)) if messages else Decision("allow")

    def finish_attempt(self, event: dict) -> None:
        self._attempts.pop((event["session_id"], event["attempt_id"]), None)
