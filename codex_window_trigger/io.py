"""Bounded, fail-closed local file helpers for the sentinel CLI."""
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
from typing import Any

from .evaluator import parse_utc


_MAX_JSON_BYTES = 1_048_576
_TITLE = re.compile(r"^\[codex-5h-touch\] ([0-9a-f]{64}|canary-000000000000)$")
_BRANCH = re.compile(r"^trigger/([0-9a-f]{12}|000000000000)$")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON numbers must be finite")
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("JSON numbers must be finite")


def read_json(path: Path) -> Any:
    """Read a bounded UTF-8 JSON value, rejecting non-finite numbers."""
    data = path.read_bytes()
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds size limit")
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds limit") from exc


def read_existing_prs(path: Path) -> dict[str, datetime]:
    """Return reconciliable PR keys with their real aware creation timestamps."""
    value = read_json(path)
    if not isinstance(value, list):
        raise ValueError("PR list must be an array")
    result: dict[str, datetime] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("PR item must be an object")
        title, branch = item.get("title"), _branch(item)
        matching_title = isinstance(title, str) and _TITLE.fullmatch(title)
        matching_branch = isinstance(branch, str) and _BRANCH.fullmatch(branch)
        if not matching_title and not matching_branch:
            continue
        if not matching_title or not matching_branch:
            raise ValueError("matching trigger PR is malformed")
        key = _TITLE.fullmatch(title).group(1)  # type: ignore[union-attr]
        suffix = _BRANCH.fullmatch(branch).group(1)  # type: ignore[union-attr]
        if (key == "canary-000000000000" and suffix != "000000000000") or (key != "canary-000000000000" and suffix != key[:12]):
            raise ValueError("matching trigger PR key does not match branch")
        raw_created = item.get("created_at", item.get("createdAt"))
        try:
            created = parse_utc(raw_created)
        except ValueError as exc:
            raise ValueError("matching trigger PR timestamp is invalid") from exc
        prior = result.get(key)
        if prior is not None and prior != created:
            raise ValueError("matching trigger PR is duplicated")
        result[key] = created.astimezone(UTC)
    return result


def _branch(item: dict[str, Any]) -> object:
    head = item.get("head")
    if isinstance(head, dict) and "ref" in head:
        return head["ref"]
    return item.get("headRefName")
