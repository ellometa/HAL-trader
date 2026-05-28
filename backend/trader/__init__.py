"""HAL autonomous paper trader (Fork B from plans/ideation/07).

The architecture, in one breath: the LLM never sees the broker. It sees
detector features + notes + portfolio state and emits a single structured
``TradePlan``. A deterministic ``risk`` layer validates that plan against
hard caps and against the *actual* detector output (no phantom setups),
then a dumb ``PaperBroker`` simulates the fill. Every decision — accepted
or rejected — is appended to a journal.

There is exactly one broker and it is simulated. Going live is not a flag;
it is a new module that does not exist yet. That is deliberate.
"""
