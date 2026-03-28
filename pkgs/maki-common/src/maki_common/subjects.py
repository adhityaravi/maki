"""Centralized NATS subject constants for all Maki services."""

# Cortex (reasoning engine)
CORTEX_TURN_REQUEST = "maki.cortex.turn.request"
CORTEX_TURN_RESPONSE = "maki.cortex.turn.response"
CORTEX_HEALTH = "maki.cortex.health"

# Ears (Discord interface)
EARS_MESSAGE_IN = "maki.ears.message.in"
EARS_MESSAGE_OUT = "maki.ears.message.out"
EARS_THOUGHT_OUT = "maki.ears.thought.out"
EARS_VITALS_OUT = "maki.ears.vitals.out"

# Immune (ops intelligence)
IMMUNE_HEALTH = "maki.immune.health"
IMMUNE_ACTION = "maki.immune.action"
IMMUNE_ALERT = "maki.immune.alert"

# Conversation stream
CONVERSATION_STREAM = "maki.conversation"
