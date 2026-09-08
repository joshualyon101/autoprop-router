from datetime import datetime, timezone
import pytest
from state import extract_closed_cash
from state_refresh import refresh_account_state
from models import AccountRule, VerifiedRiskState


def test_cash_timestamp_falls_back_to_live_observation_time():
    observed = datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc)
    cash, ts = extract_closed_cash({'amount': 49999.25}, observed_at=observed)
    assert cash == 49999.25
    assert ts == observed


class FakeClient:
    async def get_account(self, account):
        return {'data': {'balance': {'amount': 49900.0}}}
    async def fills_history(self, **kwargs):
        return {'data': []}


@pytest.mark.asyncio
async def test_runtime_durable_fill_read_proves_daily_ledger_even_if_staged_flag_false():
    rule = AccountRule(account_id='CT_X', crosstrade_account='ACC', enabled=True,
                       rules_verified=True, account_type='challenge', profile='standard',
                       starting_balance=50000, max_loss=2000, max_contracts=30,
                       drawdown='eod', challenge_target=3000,
                       challenge_consistency_enabled=True, challenge_consistency_pct=40)
    risk = VerifiedRiskState(account_id='CT_X', mll_floor=48000, mll_verified=True,
                             funded_locked=False, largest_winning_day=0,
                             ledger_verified=False,
                             verified_at=datetime.now(timezone.utc), source='migration')
    state = await refresh_account_state(FakeClient(), rule, risk)
    assert state.closed_cash_balance == 49900.0
    assert state.mll_floor == 48000
    assert state.daily_ledger_verified is True
    assert state.state_timestamp.tzinfo is not None
