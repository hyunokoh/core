"""Travel Rule transport package.

Houses pluggable adapters for the real Travel Rule providers (Sumsub TR,
Notabene TRP, TRISA) plus the legacy in-memory stub used when no provider
env vars are configured. The public surface is intentionally tiny: callers
hit :func:`tools.travel_rule.adapters.get_adapter` to obtain whichever
adapter the environment selects, then call ``.post_ivms(...)`` on it.

See ``tools/travel_rule/README.md`` for the full architecture, environment
variables, sample request/response shapes, and the retry/backoff schedule.
"""
