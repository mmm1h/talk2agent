from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from acp.schema import ToolCallProgress, ToolCallStart


_TOOL_DETAIL_LIMIT = 4
_TOOL_PREVIEW_ITEM_LIMIT = 2
_TOOL_TEXT_LIMIT = 120


@dataclass(frozen=True, slots=True)
class ToolActivitySummary:
    tool_call_id: str
    title: str
    status: str
    kind: str | None = None
    details: tuple[str, ...] = ()
    input_summary: str | None = None
    path_refs: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    terminal_ids: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ()


def summarize_tool_update(update: Any) -> ToolActivitySummary | None:
    if not isinstance(update, (ToolCallStart, ToolCallProgress)):
        return None

    tool_call_id = str(getattr(update, "toolCallId", "") or "")
    title = _normalize_inline_text(getattr(update, "title", None)) or tool_call_id or "tool"
    status = _normalize_inline_text(getattr(update, "status", None)) or "pending"
    kind = _normalize_inline_text(getattr(update, "kind", None))

    details: list[str] = []
    raw_input_detail = _raw_input_detail(
        kind,
        getattr(update, "rawInput", getattr(update, "raw_input", None)),
    )
    if raw_input_detail is not None:
        details.append(raw_input_detail)

    path_refs = _dedupe(
        (
            *_extract_location_refs(getattr(update, "locations", None)),
            *_extract_diff_paths(getattr(update, "content", None)),
        )
    )
    if path_refs:
        details.append(_preview_list("paths", path_refs))

    paths = _dedupe(tuple(_path_ref_path(ref) for ref in path_refs))

    terminal_ids = _dedupe(_extract_terminal_ids(getattr(update, "content", None)))
    if terminal_ids:
        details.append(_preview_list("terminal", terminal_ids))

    content_types = _dedupe(_extract_content_types(getattr(update, "content", None)))
    if content_types:
        details.append(_preview_list("content", content_types))

    return ToolActivitySummary(
        tool_call_id=tool_call_id,
        title=title,
        status=status,
        kind=kind,
        details=tuple(details[:_TOOL_DETAIL_LIMIT]),
        input_summary=raw_input_detail,
        path_refs=path_refs,
        paths=paths,
        terminal_ids=terminal_ids,
        content_types=content_types,
    )


def render_tool_update_text(update: Any) -> str | None:
    summary = summarize_tool_update(update)
    if summary is None:
        return None

    if isinstance(update, ToolCallStart):
        prefix = "\n[tool]"
    elif summary.status == "completed":
        prefix = "[tool completed]"
    elif summary.status == "failed":
        prefix = "[tool failed]"
    else:
        return None

    kind_suffix = "" if not summary.kind else f" [{summary.kind}]"
    lines = [f"{prefix} {summary.title}{kind_suffix}\n"]
    for detail in summary.details:
        lines.append(f"{detail}\n")
    return "".join(lines)


def _normalize_inline_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    return normalized or None


def _truncate_text(text: str, *, limit: int = _TOOL_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _coerce_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(exclude_none=True)
        except TypeError:
            dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped

    if hasattr(value, "__dict__"):
        dumped = {
            key: raw_value
            for key, raw_value in vars(value).items()
            if not key.startswith("_") and raw_value is not None
        }
        if dumped:
            return dumped

    return None


def _coerce_scalar_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_inline_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [_normalize_inline_text(item) for item in value]
        normalized_parts = [part for part in parts if part]
        if normalized_parts:
            return ", ".join(normalized_parts)
        return None

    mapping = _coerce_mapping(value)
    if mapping is not None:
        for raw_value in mapping.values():
            text = _coerce_scalar_text(raw_value)
            if text:
                return text
        return None

    return _normalize_inline_text(value)


def _mapping_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _raw_input_detail(kind: str | None, raw_input: Any) -> str | None:
    mapping = _coerce_mapping(raw_input)
    value = raw_input
    label = "input"

    if kind == "execute":
        label = "cmd"
        candidate_keys = ("command", "cmd", "shellCommand", "input", "text")
    elif kind == "search":
        label = "query"
        candidate_keys = ("query", "pattern", "q", "input", "text")
    elif kind == "fetch":
        label = "url"
        candidate_keys = ("url", "uri", "href", "input")
    elif kind in {"read", "edit", "delete", "move"}:
        label = "target"
        candidate_keys = ("path", "paths", "file", "from", "to", "input")
    elif kind == "switch_mode":
        label = "value"
        candidate_keys = ("mode", "value", "input")
    else:
        candidate_keys = ("input", "text", "prompt", "value", "query", "path", "url", "command")

    if mapping is not None:
        value = _mapping_value(mapping, candidate_keys)
        if value is None and len(mapping) == 1:
            value = next(iter(mapping.values()))

    text = _coerce_scalar_text(value)
    if text is None:
        return None
    return f"{label}: {_truncate_text(text)}"


def _extract_location_refs(raw_locations: Any) -> tuple[str, ...]:
    refs: list[str] = []
    if raw_locations is None:
        return ()
    for raw_location in raw_locations:
        path = _normalize_inline_text(getattr(raw_location, "path", None))
        if not path:
            continue
        line = getattr(raw_location, "line", None)
        if isinstance(line, int) and line > 0:
            refs.append(f"{path}:{line}")
        else:
            refs.append(path)
    return tuple(refs)


def _extract_diff_paths(raw_content: Any) -> tuple[str, ...]:
    paths: list[str] = []
    if raw_content is None:
        return ()
    for item in raw_content:
        if getattr(item, "type", None) != "diff":
            continue
        path = _normalize_inline_text(getattr(item, "path", None))
        if path:
            paths.append(path)
    return tuple(paths)


def _extract_terminal_ids(raw_content: Any) -> tuple[str, ...]:
    terminal_ids: list[str] = []
    if raw_content is None:
        return ()
    for item in raw_content:
        if getattr(item, "type", None) != "terminal":
            continue
        terminal_id = _normalize_inline_text(getattr(item, "terminalId", None))
        if terminal_id:
            terminal_ids.append(terminal_id)
    return tuple(terminal_ids)


def _extract_content_types(raw_content: Any) -> tuple[str, ...]:
    content_types: list[str] = []
    if raw_content is None:
        return ()
    for item in raw_content:
        if getattr(item, "type", None) != "content":
            continue
        block = getattr(item, "content", None)
        block_type = _normalize_inline_text(getattr(block, "type", None))
        if block_type:
            content_types.append(block_type)
    return tuple(content_types)


def _path_ref_path(path_ref: str) -> str:
    normalized = _normalize_inline_text(path_ref) or ""
    if not normalized:
        return normalized

    last_colon = normalized.rfind(":")
    if last_colon <= 0:
        return normalized

    suffix = normalized[last_colon + 1 :]
    if suffix.isdigit():
        candidate = normalized[:last_colon]
        second_colon = candidate.rfind(":")
        if second_colon > 0 and candidate[second_colon + 1 :].isdigit():
            return candidate[:second_colon]
        return candidate
    return normalized


def _preview_list(label: str, values: tuple[str, ...]) -> str:
    visible_values = values[:_TOOL_PREVIEW_ITEM_LIMIT]
    preview = ", ".join(visible_values)
    remaining = len(values) - len(visible_values)
    if remaining > 0:
        preview = f"{preview}, +{remaining} more"
    return f"{label}: {preview}"


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)
