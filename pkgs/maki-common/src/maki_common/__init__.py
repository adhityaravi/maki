"""maki-common: Shared utilities for Maki services."""

from maki_common.config import apply_config_updates, parse_config_tags, parse_tagged, strip_tags
from maki_common.futures import PendingFutures, PendingQueues
from maki_common.logging import configure_logging, get_logger
from maki_common.nats import connect_nats, init_kv, kv_get_float, kv_put_float, load_kv_config, try_claim_loop

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "apply_config_updates",
    "configure_logging",
    "connect_nats",
    "get_logger",
    "init_kv",
    "kv_get_float",
    "kv_put_float",
    "load_kv_config",
    "try_claim_loop",
    "parse_config_tags",
    "parse_tagged",
    "PendingFutures",
    "PendingQueues",
    "strip_tags",
]
