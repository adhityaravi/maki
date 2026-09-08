"""Config tag parsing and self-tuning utilities."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

from nats.js.kv import KeyValue

log = logging.getLogger(__name__)

# Canonical [TAG:content] matcher. Historically a single regex —
# ``\[(\w+):(.*?)\]`` with ``re.DOTALL`` — but the lazy ``.*?`` stops at the
# FIRST ``]`` after the tag prefix. There's no way to escape ``]`` inside the
# content, so any payload containing one gets silently truncated. Real cases
# Claude routinely emits into DIGEST/ALERT/RESPONSE tags:
#
#   * type annotations like ``list[str]``
#   * markdown links: ``see [runbook](url)``
#   * issue refs: ``regression from [#123]``
#   * code fragments with array indexing, regex classes, Python slices
#
# All would be mangled — ``parse_tagged`` returned truncated content,
# ``strip_tags`` left the trailing ``] ...`` garbage in the summary — and
# nobody noticed because the failure is silent. See issue #505.
#
# Fix: replace the regex with a balanced-bracket scanner. ``_TAG_START`` finds
# where a candidate tag begins; ``_iter_tags`` walks the string keeping a
# ``[``/``]`` depth counter and closes only when depth returns to zero, so
# internal brackets survive round-trip. All three helpers (parse_config_tags,
# parse_tagged, strip_tags) share this one iterator so they cannot silently
# disagree about what "content" means.
_TAG_START = re.compile(r"\[(\w+):")


def _iter_tags(text: str) -> Iterator[tuple[str, str, int, int]]:
    """Yield ``(tag, content, start, end)`` for every balanced ``[TAG:...]`` block.

    ``start`` is the offset of the opening ``[``; ``end`` is one past the
    closing ``]`` (so ``text[start:end]`` is the full tag literal). Content is
    returned verbatim — no stripping — because config-tag parsing depends on
    the exact ``key=value`` bytes.

    Unbalanced openers (``[DIGEST:foo`` with no closing bracket) are silently
    skipped, matching the pre-existing regex behaviour: a partial tag simply
    isn't a tag.
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        m = _TAG_START.match(text, i)
        if not m:
            i += 1
            continue
        tag_name = m.group(1)
        content_start = m.end()
        # Walk forward tracking bracket depth. depth==1 as we enter (the
        # opening ``[`` of the tag itself). Close only when depth drops to 0.
        depth = 1
        j = content_start
        while j < n:
            c = text[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth == 0:
            # j points at the closing ']'. Content is [content_start, j).
            yield tag_name, text[content_start:j], i, j + 1
            i = j + 1
        else:
            # Unbalanced — no closer. Skip this candidate opener and keep
            # scanning so a *later* well-formed tag still gets found.
            i += 1


def parse_config_tags(text: str) -> list[tuple[str, str]]:
    """Parse [CONFIG:key=value] tags from text.

    Returns list of (key, raw_value_string) tuples. Values containing ``]``
    (e.g. JSON arrays, objects, or bracketed literals) survive intact thanks
    to the balanced-bracket scanner in :func:`_iter_tags`.
    """
    out: list[tuple[str, str]] = []
    for tag, content, _start, _end in _iter_tags(text):
        if tag != "CONFIG":
            continue
        key, sep, raw_value = content.partition("=")
        if not sep:
            continue
        out.append((key, raw_value))
    return out


def parse_tagged(text: str, tag: str) -> list[str]:
    """Parse [TAG:content] sections from text.

    Args:
        text: The text to parse.
        tag: The tag name (e.g. "DIGEST", "ALERT").

    Returns list of content strings found. Content is ``.strip()``'d for
    readability; internal ``]`` characters are preserved (see :func:`_iter_tags`).
    """
    return [content.strip() for found_tag, content, _s, _e in _iter_tags(text) if found_tag == tag]


def strip_tags(text: str) -> str:
    """Remove all [TAG:...] sections from text.

    Uses the same balanced-bracket scanner as :func:`parse_tagged` so the two
    remain exact inverses — anything ``parse_tagged`` would extract is exactly
    what ``strip_tags`` removes, including payloads with internal ``]``.
    """
    spans = [(start, end) for _tag, _content, start, end in _iter_tags(text)]
    if not spans:
        return text.strip()
    out: list[str] = []
    prev = 0
    for start, end in spans:
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    return "".join(out).strip()


async def apply_config_updates(
    kv: KeyValue,
    updates: list[tuple[str, str]],
    allowed_keys: set[str] | None = None,
    validators: dict[str, list] | None = None,
) -> None:
    """Apply parsed config updates to a KV bucket.

    Args:
        kv: NATS KV bucket.
        updates: List of (key, raw_value) from parse_config_tags().
        allowed_keys: If provided, only these keys are accepted.
        validators: Optional dict of key → list of allowed values.
            If the parsed value is not in the list, the update is rejected.
    """
    for key, raw_value in updates:
        if allowed_keys and key not in allowed_keys:
            log.warning("Rejected config update for unknown key", extra={"key": key})
            continue
        try:
            parsed = json.loads(raw_value)
            if validators and key in validators:
                if parsed not in validators[key]:
                    log.warning(
                        "Rejected config update: value not allowed",
                        extra={"key": key, "value": parsed, "allowed": validators[key]},
                    )
                    continue
            await kv.put(key, json.dumps(parsed).encode())
            log.info("Config self-tuned", extra={"key": key, "value": parsed})
        except Exception:
            log.warning("Failed to parse config update", extra={"key": key, "raw_value": raw_value})
