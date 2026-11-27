"""Issue #324 / H1-E — Phase C infrastructure adapters (RESEARCH_ONLY).

PostgreSQL-backed implementations of the FROZEN Phase A contracts plus the
isolated worker.  Nothing here runs at import time; every entry point is
explicit and gated (``NO_DEPLOY`` / ``NO_DDL_APPLY`` / ``H1_OOS_HOLD``
remain in force — see ``worker.main`` gates).
"""
