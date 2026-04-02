"""Centralized NATS subject constants for all Maki services."""

# Cortex (reasoning engine)
CORTEX_TURN_REQUEST = "maki.cortex.turn.request"
CORTEX_TURN_RESPONSE = "maki.cortex.turn.response"
CORTEX_HEALTH = "maki.cortex.health"
CORTEX_STUCK = "maki.cortex.stuck"

# Ears (Discord interface)
EARS_MESSAGE_IN = "maki.ears.message.in"
EARS_MESSAGE_OUT = "maki.ears.message.out"
EARS_THOUGHT_OUT = "maki.ears.thought.out"
EARS_VITALS_OUT = "maki.ears.vitals.out"
EARS_IMMUNE_OUT = "maki.ears.immune.out"

# Immune (ops intelligence)
IMMUNE_HEALTH = "maki.immune.health"
IMMUNE_ACTION = "maki.immune.action"
IMMUNE_ALERT = "maki.immune.alert"
IMMUNE_STATE_REQUEST = "maki.immune.state"
IMMUNE_COMMAND = "maki.immune.command"

# Deploy coordination
DEPLOY_REQUEST = "maki.deploy.request"
DEPLOY_STATUS_REQUEST = "maki.deploy.status"

# Ears (cont.)
EARS_REMINDER_OUT = "maki.ears.reminder.out"

# Memory
MEMORY_STORE = "maki.memory.store"

# Conversation stream
CONVERSATION_STREAM = "maki.conversation"
