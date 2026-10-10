"""Postgres-backed NATS listeners: generic DB query + error_patterns CRUD.

- ``db_query_listener`` — request/reply endpoint cortex uses to run
  read-only SQL. Safety: only ``SELECT``/``WITH`` pass the first-word
  check, and the query runs inside a Postgres ``READ ONLY`` transaction
  so any DML smuggled inside a CTE is rejected by the server (issue
  #288). LIMIT 50 is injected if missing; 10s statement timeout.
- ``pattern_query_listener`` — serve ``error_patterns`` rows for
  immune's passive loop.
- ``pattern_update_listener`` — fire-and-forget bump of an existing
  pattern's stats (occurrence count, confidence, last_seen_at).
- ``pattern_write_listener`` — insert a new classified pattern from
  immune's Claude escalation.

Every listener is wrapped in ``subscribe_supervised`` (issue #175).
Queue-grouped so exactly one stem pod handles each request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import asyncpg
from maki_common import subscribe_supervised
from maki_common.subjects import DB_QUERY, PATTERN_QUERY, PATTERN_UPDATE, PATTERN_WRITE

log = logging.getLogger(__name__)


async def _handle_db_query(msg, db_pool: asyncpg.Pool) -> None:
    """Run one DB query and respond via NATS request/reply.

    Safety: only SELECT/WITH (CTE) queries pass the first-word check, then the
    query is executed inside a Postgres ``READ ONLY`` transaction so any DML
    smuggled inside a CTE (``WITH x AS (DELETE ... RETURNING *) SELECT ...``)
    is rejected by the server at execution time. See issue #288.
    """
    from maki_common.tools.utils import mcp_result

    try:
        data = json.loads(msg.data.decode())
        sql = data.get("sql", "").strip()

        # Validate: SELECT or WITH only (first-word check is a cheap reject,
        # the READ ONLY transaction below is the real safety boundary).
        first_word = sql.split()[0].upper() if sql.split() else ""
        if first_word not in ("SELECT", "WITH"):
            await msg.respond(
                json.dumps(
                    mcp_result(
                        "Only SELECT and WITH (CTE) queries are allowed — "
                        "executed inside a READ ONLY transaction, so any DML "
                        "(INSERT/UPDATE/DELETE) nested in a CTE will be rejected."
                    )
                ).encode()
            )
            return

        # Reject multi-statement queries
        if ";" in sql.rstrip(";"):
            await msg.respond(json.dumps(mcp_result("Multi-statement queries are not allowed.")).encode())
            return

        sql = sql.rstrip(";")

        # Inject LIMIT if missing
        if "limit" not in sql.lower():
            sql += " LIMIT 50"

        log.info("Executing DB query", extra={"sql": sql[:200]})

        async with db_pool.acquire() as conn:
            # READ ONLY transaction: Postgres rejects DML at execution time even
            # when the statement starts with WITH and hides INSERT/UPDATE/DELETE
            # inside a CTE. Without this, the first-word check is bypassable
            # (issue #288).
            async with conn.transaction(readonly=True):
                rows = await asyncio.wait_for(conn.fetch(sql), timeout=10.0)

        if not rows:
            await msg.respond(json.dumps(mcp_result("Query returned 0 rows.")).encode())
            return

        # Format as text table
        columns = list(rows[0].keys())
        lines = [" | ".join(columns)]
        lines.append("-" * len(lines[0]))
        for row in rows:
            lines.append(" | ".join(str(row[c]) for c in columns))
        lines.append(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")

        await msg.respond(json.dumps(mcp_result("\n".join(lines))).encode())

    except TimeoutError:
        log.warning("DB query timed out")
        try:
            await msg.respond(json.dumps(mcp_result("Query timed out (10s limit).")).encode())
        except Exception:
            pass
    except Exception:
        log.exception("DB query error")
        try:
            await msg.respond(json.dumps(mcp_result("DB query failed — check logs.")).encode())
        except Exception:
            pass


async def db_query_listener(nc, db_pool: asyncpg.Pool, *, queue: str) -> None:
    """Handle generic DB query requests from cortex via NATS request/reply.

    Safety: only SELECT/WITH queries pass the first-word check, the query
    runs inside a Postgres READ ONLY transaction (so DML hidden in a CTE
    is rejected by the server — issue #288), LIMIT 50 is injected if
    missing, and a 10s query timeout is enforced.

    Wrapped in ``subscribe_supervised`` so a NATS reconnect / stream drain
    re-subscribes instead of silently dropping every subsequent DB query
    from cortex (issue #175).
    """

    async def handler(msg):
        await _handle_db_query(msg, db_pool)

    await subscribe_supervised(
        nc,
        DB_QUERY,
        handler,
        queue=queue,
        # 30s is 3× the inner DB timeout (10s). If we hit this the pool
        # itself is wedged — bound catches it before it blackholes every
        # subsequent DB query for cortex on this pod (#492).
        handler_timeout=30.0,
        name="stem.db_query",
    )


async def _handle_pattern_query(msg, db_pool: asyncpg.Pool) -> None:
    """Serve one error_patterns query for immune's passive loop."""
    try:
        data = json.loads(msg.data.decode())
        component = data.get("component", "")

        if not component or not db_pool:
            await msg.respond(json.dumps({"patterns": []}).encode())
            return

        async with db_pool.acquire() as conn:
            rows = await asyncio.wait_for(
                conn.fetch(
                    "SELECT id, component, pattern, classification, confidence, "
                    "occurrence_count, notes "
                    "FROM error_patterns WHERE component = $1",
                    component,
                ),
                timeout=5.0,
            )

        patterns = [
            {
                "id": str(row["id"]),
                "component": row["component"],
                "pattern": row["pattern"],
                "classification": row["classification"],
                "confidence": row["confidence"],
                "occurrence_count": row["occurrence_count"],
                "notes": row["notes"],
            }
            for row in rows
        ]

        await msg.respond(json.dumps({"patterns": patterns}).encode())

    except Exception:
        log.exception("Pattern query error")
        try:
            await msg.respond(json.dumps({"patterns": []}).encode())
        except Exception:
            pass


async def pattern_query_listener(nc, db_pool: asyncpg.Pool, *, queue: str) -> None:
    """Serve error_patterns queries for immune's passive loop.

    Returns JSON list of patterns for a given component. Wrapped in
    ``subscribe_supervised`` so a NATS reconnect / stream drain re-subscribes
    instead of silently dropping every subsequent immune query (issue #175).
    """

    async def handler(msg):
        await _handle_pattern_query(msg, db_pool)

    await subscribe_supervised(
        nc,
        PATTERN_QUERY,
        handler,
        queue=queue,
        # Inner query has a 5s timeout; give the handler 4× that headroom
        # for pool acquisition + response encode + reply (#492).
        handler_timeout=20.0,
        name="stem.pattern_query",
    )


async def _handle_pattern_update(msg, db_pool: asyncpg.Pool) -> None:
    """Apply one fire-and-forget pattern stats bump."""
    try:
        data = json.loads(msg.data.decode())
        pattern_id = data.get("id")
        confidence_delta = data.get("confidence_delta", 0.0)
        if not pattern_id or not db_pool:
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE error_patterns "
                "SET occurrence_count = occurrence_count + 1, "
                "    confidence = LEAST(confidence + $1, 1.0), "
                "    last_seen_at = now() "
                "WHERE id = $2",
                confidence_delta,
                uuid.UUID(pattern_id),
            )

        log.debug("Pattern stats updated", extra={"pattern_id": pattern_id})

    except Exception:
        log.exception("Pattern update error")


async def pattern_update_listener(nc, db_pool: asyncpg.Pool, *, queue: str) -> None:
    """Handle fire-and-forget updates to error_patterns from immune.

    Supports bumping occurrence_count/confidence/last_seen_at for known
    patterns. Wrapped in ``subscribe_supervised`` so a NATS reconnect /
    stream drain re-subscribes instead of silently dropping every update
    (issue #175).
    """

    async def handler(msg):
        await _handle_pattern_update(msg, db_pool)

    await subscribe_supervised(
        nc,
        PATTERN_UPDATE,
        handler,
        queue=queue,
        # Fire-and-forget bump — single UPDATE. Twenty seconds catches a
        # wedged pool acquire without over-eagerly failing normal writes
        # (#492).
        handler_timeout=20.0,
        name="stem.pattern_update",
    )


async def _handle_pattern_write(msg, db_pool: asyncpg.Pool) -> None:
    """Insert one new error_patterns row from immune's Claude escalation."""
    try:
        data = json.loads(msg.data.decode())
        component = data.get("component", "")
        pattern = data.get("pattern", "")
        classification = data.get("classification", "escalate")
        confidence = float(data.get("confidence", 0.5))
        notes = data.get("notes", "")

        if not component or not pattern or not db_pool:
            return

        async with db_pool.acquire() as conn:
            await asyncio.wait_for(
                conn.execute(
                    "INSERT INTO error_patterns "
                    "(component, pattern, classification, confidence, occurrence_count, notes) "
                    "VALUES ($1, $2, $3, $4, 1, $5) "
                    "ON CONFLICT (component, pattern) DO NOTHING",
                    component,
                    pattern,
                    classification,
                    confidence,
                    notes,
                ),
                timeout=5.0,
            )

        log.info(
            "Pattern written",
            extra={"component": component, "pattern": pattern[:60], "classification": classification},
        )

    except Exception:
        log.exception("Pattern write error")


async def pattern_write_listener(nc, db_pool: asyncpg.Pool, *, queue: str) -> None:
    """Handle write requests for new error_patterns from immune's Claude escalation.

    Inserts a new classified pattern. Skips silently if the
    component+pattern already exists. Wrapped in ``subscribe_supervised``
    so a NATS reconnect / stream drain re-subscribes instead of silently
    dropping every subsequent pattern write (issue #175).
    """

    async def handler(msg):
        await _handle_pattern_write(msg, db_pool)

    await subscribe_supervised(
        nc,
        PATTERN_WRITE,
        handler,
        queue=queue,
        # Inner INSERT has a 5s timeout; give the handler 4× headroom for
        # pool acquire + conflict resolution (#492).
        handler_timeout=20.0,
        name="stem.pattern_write",
    )
