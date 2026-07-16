"""Centralized Claude model constants for the fleet.

Every service that reads a model ID from env should fall back to
``DEFAULT_CLAUDE_MODEL`` when the env var is unset, so a Helm template
rename or a missing var in a new component fails loudly on the current
model instead of silently booting on a year-old default.

Bump this on model release and the fleet moves together. See issue #146.
"""

from __future__ import annotations

# Current Sonnet — best speed / intelligence balance, used as the fallback
# for CLAUDE_MODEL / LLM_MODEL in cortex, immune, synapse, recall, and the
# maki_common.claude wrapper defaults. Loop runners (work.py, idle.py) still
# explicitly pin Opus for hard reasoning; they don't route through this.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

__all__ = ["DEFAULT_CLAUDE_MODEL"]
