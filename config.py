from __future__ import annotations

import json
from pathlib import Path
from .models import AccountRule, VerifiedRiskState


def _decode(raw: str):
    data = json.loads(raw)
    if isinstance(data, dict):
        return data.get('accounts', data.get('risk_states', data))
    return data


def load_accounts(path: str, env_json: str = '') -> list[AccountRule]:
    if env_json.strip():
        data = _decode(env_json)
    else:
        p = Path(path)
        if not p.exists():
            return []
        data = json.loads(p.read_text())
        data = data.get('accounts', data) if isinstance(data, dict) else data
    if isinstance(data, dict):
        data = list(data.values())
    return [AccountRule.model_validate(x) for x in (data or [])]


def load_risk_states(env_json: str = '') -> list[VerifiedRiskState]:
    if not env_json.strip():
        return []
    data = _decode(env_json)
    if isinstance(data, dict):
        data = list(data.values())
    return [VerifiedRiskState.model_validate(x) for x in (data or [])]
