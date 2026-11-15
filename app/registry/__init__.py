"""Session registry (PLAN §5): SQLite, the only source of session truth.

Session existence is the app's fact, not pi's (invariant 6): the registry
owns sessions and records pi's sessionFile verbatim; empty sessions are
never persisted by pi, so a registry row without a transcript is "idle,
nothing to resume", not an error.
"""
