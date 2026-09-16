import pytest

from events import parse_alert
from live import LiveRouter
from management import SILVER_STOP_CONTRACT
from models import ActiveTrade, AccountRule


def _trade(stage=0, stop=110.0):
    return ActiveTrade(
        account_id='A', event_id='e', engine='SILVER', side='SHORT', entry=100.0,
        initial_stop=110.0, current_stop=stop, tp1=70.0, total_qty=3,
        tp1_qty=3, runner_qty=0, current_position_qty=3, previous_position_qty=3,
        stop_stage=stage, stop_order_ids=['s1'], target_order_ids=['t1'],
    )


def test_parser_accepts_stage1_contract():
    e = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|'
        'STAGE=1|TRIGGER_R=2.00|LOCK_R=0.25|QTY=3|SL=97.50'
    )
    assert e.kind == 'SILVER_STOP_MOVE'
    assert e.fields['STAGE'] == '1'


def test_parser_accepts_stage2_contract():
    e = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|'
        'STAGE=2|TRIGGER_R=3.00|LOCK_R=0.50|QTY=3|SL=95.00'
    )
    assert e.kind == 'SILVER_STOP_MOVE'
    assert e.fields['STAGE'] == '2'


@pytest.mark.parametrize('raw', [
    'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|STAGE=1|TRIGGER_R=2.00|LOCK_R=0.25|QTY=3|SL=97.50',
    'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|STAGE=1|TRIGGER_R=3.00|LOCK_R=0.25|QTY=3|SL=97.50',
    'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|STAGE=2|TRIGGER_R=3.00|LOCK_R=0.25|QTY=3|SL=95.00',
])
def test_parser_rejects_invalid_staged_contract(raw):
    with pytest.raises(ValueError):
        parse_alert(raw)


class Store:
    def __init__(self, trade):
        self.trade = trade

    def all_trades(self):
        return [self.trade] if self.trade else []

    def save_trade(self, trade):
        self.trade = trade

    def delete_trade(self, account_id):
        self.trade = None


class Executor:
    def __init__(self):
        self.calls = []

    async def change_stop_orders(self, account, ids, new_stop, qty):
        self.calls.append((account, list(ids), new_stop, qty))
        return list(ids)


def _rule():
    return AccountRule(
        account_id='A', crosstrade_account='acct', enabled=True, rules_verified=True,
        account_type='personal', profile='standard', starting_balance=100000,
        max_loss=0, max_contracts=20, personal_risk_method='fixed', personal_risk_value=250,
    )


@pytest.mark.asyncio
async def test_live_stage1_uses_destination_actual_fill_geometry():
    r = LiveRouter.__new__(LiveRouter)
    r.accounts = [_rule()]
    r.store = Store(_trade())
    r.executor = Executor()

    async def position_qty(_rule):
        return -3

    r.position_qty = position_qty
    e = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|'
        'STAGE=1|TRIGGER_R=2.00|LOCK_R=0.25|QTY=3|SL=97.25'
    )
    out = await r.silver_stop(e)
    assert r.executor.calls[0][2] == 97.5  # destination actual fill/initial stop, not Pine SL
    assert r.store.trade.stop_stage == 1
    assert out['results'][0]['reason'] == 'SILVER_2R_LOCK_0.25R'


@pytest.mark.asyncio
async def test_live_stage2_advances_and_late_stage1_cannot_loosen():
    r = LiveRouter.__new__(LiveRouter)
    r.accounts = [_rule()]
    r.store = Store(_trade(stage=1, stop=97.5))
    r.executor = Executor()

    async def position_qty(_rule):
        return -3

    r.position_qty = position_qty
    e2 = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|'
        'STAGE=2|TRIGGER_R=3.00|LOCK_R=0.50|QTY=3|SL=95.00'
    )
    await r.silver_stop(e2)
    assert r.executor.calls[-1][2] == 95.0
    assert r.store.trade.stop_stage == 2

    before = len(r.executor.calls)
    e1 = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|CONTRACT=SILVER_SB35_STAGE_V1|'
        'STAGE=1|TRIGGER_R=2.00|LOCK_R=0.25|QTY=3|SL=97.50'
    )
    out = await r.silver_stop(e1)
    assert len(r.executor.calls) == before
    assert out['results'][0]['status'] == 'NO_CHANGE'


def test_contract_identity():
    assert SILVER_STOP_CONTRACT == 'SILVER_SB35_STAGE_V1'
