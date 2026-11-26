"""H2 research-only Bybit client, always P3 in the production limiter.

Use the git-versioned canonical limiter parameter contract, but NEVER its
activation flags. The H2 provider explicitly opts into the SAME cross-process
limiter state and semantic fingerprint as production. Mismatch/missing file or
file permission => fail closed, never a private or local-only fallback.
"""
from __future__ import annotations

import os

from app.config.limiter_contract import load_limiter_contract
from app.config.settings import Settings, _verify_limiter_state_path_access
from app.exchange.bybit_client import BybitClient

P3_RESEARCH_PRIORITY = "P3"


class H2SharedBybitClient(BybitClient):
    """Force P3 for every research public GET, including every kline page."""

    def _public_get(self, endpoint, *, retry_priority=None, **params):
        # Deliberately ignore retry_priority from callers: never escalate.
        return super()._public_get(
            endpoint, retry_priority=P3_RESEARCH_PRIORITY, **params
        )


def build_h2_shared_client(config):
    path = config.shared_limiter_contract_path
    if not path or not os.path.isabs(path):
        raise ValueError(
            "H2_SHARED_LIMITER_CONTRACT_REQUIRED: absolute path to the "
            "deployed production limiter contract must be configured"
        )
    values = load_limiter_contract(path)
    state_path = values["global_limiter_state_path"]
    if not os.path.isabs(state_path):
        raise ValueError(
            "H2_SHARED_LIMITER_STATE_MUST_BE_ABSOLUTE: no private limiter"
        )
    _verify_limiter_state_path_access(state_path)
    settings = Settings(
        **values,
        global_bybit_rate_limiter_enabled=True,
        global_limiter_contract_path=path,
        bybit_timeout=config.bybit_timeout_s,
        bybit_rate_limit_rps=1.0,
        bybit_max_attempts=1,
        bybit_rate_limit_max_retries=0,
    )
    # The durable limiter config_fingerprint check is enforced by the
    # actual acquire() path before any HTTP send.
    return H2SharedBybitClient(settings)
