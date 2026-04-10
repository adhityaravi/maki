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
