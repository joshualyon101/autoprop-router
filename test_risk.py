from app.models import AccountConfig, AccountRuntime
from app.risk import pine_round_positive, size_trade, challenge_budget, funded_budget, consistency_adjust


def cfg(**kw):
    base = dict(
        id="x", account_name="X", firm="Test", program="Test",
        phase="challenge", starting_balance=50000, max_loss=2000,
        profit_target=3000, max_micros=30, drawdown_type="eod_trail_to_lock",
        risk_profile="Standard", enabled=True, rules_verified=True
    )
    base.update(kw)
    return AccountConfig(**base)


def test_pine_round():
    assert pine_round_positive(0.49) == 0
    assert pine_round_positive(0.50) == 1
    assert pine_round_positive(1.50) == 2


def test_challenge_standard_eod_budget_new_50k():
    a = cfg()
    s = AccountRuntime(account_id="x", account_name="X", balance=50000, mll_floor=48000, cushion=2000)
    # 18.75% current cushion = 375, below 24.375% MaxLoss cap = 487.50
    assert challenge_budget(a, s) == 375.0


def test_challenge_near_pass_reduces_budget():
    a = cfg()
    s = AccountRuntime(account_id="x", account_name="X", balance=52400, mll_floor=50000, cushion=2400)
    # Raw 450, cap 487.5, at 80% target => 0.85
    assert round(challenge_budget(a, s), 6) == 382.5


def test_funded_prelock_standard():
    a = cfg(phase="funded", profit_target=0)
    s = AccountRuntime(account_id="x", account_name="X", balance=50000, mll_floor=48000, cushion=2000, mll_locked=False)
    budget, one, cap = funded_budget(a, s)
    assert budget == 300.0
    assert one is False
    assert cap is None


def test_funded_low_cushion_one_contract_guard():
    a = cfg(phase="funded", profit_target=0)
    s = AccountRuntime(account_id="x", account_name="X", balance=48400, mll_floor=48000, cushion=400, mll_locked=False)
    r = size_trade(a, s, contract_risk=150.0)
    # 35% of cushion = $140, so even one $150-risk contract is blocked.
    assert r.quantity == 0


def test_consistency_size_first_and_target_clip():
    a = cfg(consistency_pct=40)
    s = AccountRuntime(
        account_id="x", account_name="X", balance=51000, mll_floor=49000, cushion=2000,
        realized_today=1100, largest_winning_day=1100
    )
    # $1200 daily ceiling -> only $100 room remains.
    qty, tp1, tp2, room = consistency_adjust(
        a=a, s=s, pre_qty=5, entry=20000, tp1=20050, tp2=20100,
        point_value=2.0, tick_size=0.25, min_runner_qty=2, min_profit_room=50
    )
    assert room == 100
    assert qty == 1
    # 1-contract TP1 natural profit is $100, so it fits exactly.
    assert tp1 == 20050
