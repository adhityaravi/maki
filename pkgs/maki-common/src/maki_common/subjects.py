"""Centralized NATS subject constants for all Maki services."""

# Cortex (reasoning engine)
CORTEX_TURN_REQUEST = "maki.cortex.turn.request"
CORTEX_TURN_RESPONSE = "maki.cortex.turn.response"
CORTEX_HEALTH = "maki.cortex.health"
CORTEX_STUCK = "maki.cortex.stuck"
CORTEX_TOKEN_USAGE = "maki.cortex.token.usage"

# Ears (Discord interface)
EARS_IN = "maki.ears.in"
EARS_OUT = "maki.ears.out"
EARS_VITALS_OUT = "maki.ears.vitals.out"
EARS_IMMUNE_OUT = "maki.ears.immune.out"
EARS_SEARCH = "maki.ears.search"
EARS_INTERACTION = "maki.ears.interaction"  # base — append .{proposal_id} at runtime

# Immune (ops intelligence)
IMMUNE_HEALTH = "maki.immune.health"
IMMUNE_ACTION = "maki.immune.action"
IMMUNE_ALERT = "maki.immune.alert"
IMMUNE_STATE_REQUEST = "maki.immune.state"
IMMUNE_COMMAND = "maki.immune.command"
IMMUNE_SITE_QUERY = "maki.immune.site"  # append .{site_name} at runtime
IMMUNE_LOGS_REQUEST = "maki.immune.logs"  # pod log retrieval for reflection loop (#252)

# Deploy coordination
DEPLOY_REQUEST = "maki.deploy.request"
DEPLOY_STATUS_REQUEST = "maki.deploy.status"
DEPLOY_PROPAGATE = "maki.deploy.propagate"
RESTART_REQUEST = "maki.restart.request"
RESTART_PROPAGATE = "maki.restart.propagate"

# Config sync (cross-site propagation)
CONFIG_SYNC = "maki.config.sync"

# Memory
MEMORY_STORE = "maki.memory.store"

# Conversation stream
CONVERSATION_STREAM = "maki.conversation"

# Trading
# TRADING_SIGNAL (maki.trading.signal) was removed in #168 — the automated
# trading loop that used to publish it lived in the deleted maki_loops repo,
# so the subject had zero publishers left. The manual-trade path
# (TRADING_MANUAL_TRADE) and cortex tool-request path (TRADING_TOOL_REQUEST)
# are both still live; the broader excise-vs-revive question is #242.
TRADING_MANUAL_TRADE = "maki.trading.manual_trade"
TRADING_TOOL_REQUEST = "maki.trading.tool"

# Generic DB query
DB_QUERY = "maki.db.query"

# Error pattern matching (immune ↔ stem)
PATTERN_QUERY = "maki.immune.pattern.query"
PATTERN_UPDATE = "maki.immune.pattern.update"
PATTERN_WRITE = "maki.immune.pattern.write"  # insert new classified pattern
