"""Loop discovery and manual trigger dispatch.

Builtin loops (idle, work) plus anything registered via ``maki.loops``
entry points — the private ``maki-loops`` package uses this to register
care and daytrading without touching stem's source. ``trigger_loop`` is
the ``!loop <name>`` Discord command's entry point.
"""

from __future__ import annotations

import json
import logging
from importlib.metadata import entry_points

from maki_common import load_kv_config, spawn_background
from maki_common.subjects import EARS_OUT

from maki_stem.loops import IDLE_LOOP_SPEC, WORK_LOOP_SPEC, LoopSpec, StemContext

log = logging.getLogger(__name__)


def discover_loops() -> list[LoopSpec]:
    """Discover loop specs from entry points and builtin loops.

    Looks for 'maki.loops' entry points — allows private maki-loops package
    to register additional loops (e.g., care, daytrading) without modifying this code.

    Returns builtin loops (idle, work) + any discovered from entry points.
    Duplicate names are logged and skipped (first one wins).
    """
    loops: list[LoopSpec] = [IDLE_LOOP_SPEC, WORK_LOOP_SPEC]
    seen_names = {spec.name for spec in loops}

    try:
        eps = entry_points(group="maki.loops")
        for ep in eps:
            try:
                spec = ep.load()
                if not isinstance(spec, LoopSpec):
                    log.warning(
                        "Entry point returned non-LoopSpec, skipping",
                        extra={"entry_point": ep.name, "type": type(spec).__name__},
                    )
                    continue
                if spec.name in seen_names:
                    log.warning("Duplicate loop name, skipping", extra={"loop_name": spec.name, "entry_point": ep.name})
                    continue
                loops.append(spec)
                seen_names.add(spec.name)
                log.info("Discovered external loop", extra={"loop_name": spec.name, "entry_point": ep.name})
            except Exception:
                log.exception("Failed to load loop entry point", extra={"entry_point": ep.name})
    except Exception:
        log.exception("Failed to discover loop entry points")

    log.info("Loop discovery complete", extra={"total_loops": len(loops), "loop_names": list(seen_names)})
    return loops


async def _run_manual_loop(spec: LoopSpec, ctx: StemContext, loop_name: str) -> None:
    """Run a loop body in the background (fire-and-forget from !loop command)."""
    try:
        config = await load_kv_config(ctx.config_kv, ctx.default_config)
        await spec.body(spec, config, ctx)
    except Exception:
        log.exception("Manual loop trigger failed", extra={"loop": loop_name})


async def trigger_loop(
    ctx: StemContext,
    loop_specs: list[LoopSpec],
    loop_name: str,
    forward_to: dict,
) -> None:
    """Manually trigger a named loop, bypassing cron/guards."""
    spec = next((s for s in loop_specs if s.name == loop_name), None)
    if spec is None:
        names = ", ".join(s.name for s in loop_specs)
        reply = {"response": f"Unknown loop '{loop_name}'. Available: {names}", "done": True, **forward_to}
        await ctx.nc.publish(EARS_OUT, json.dumps(reply).encode())
        return
    reply = {"response": f"Triggering loop: {loop_name}", "done": True, **forward_to}
    await ctx.nc.publish(EARS_OUT, json.dumps(reply).encode())
    spawn_background(_run_manual_loop(spec, ctx, loop_name), name=f"stem.manual_loop.{loop_name}")
