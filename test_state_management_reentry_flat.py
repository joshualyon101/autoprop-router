from datetime import datetime, timezone
import pytest
from state import extract_closed_cash, StateUnverified, ny_date, realized_by_ny_day
from management import core_on_market_pulse, silver_lock_stop
from models import ActiveTrade, MarketPulse
from org_reentry import prove_prior_org_stop


def trade(**kw):
    d=dict(account_id="A",event_id="e",engine="CORE",side="LONG",entry=100,initial_stop=90,current_stop=90,tp1=120,tp2=140,total_qty=5,tp1_qty=3,runner_qty=2,current_position_qty=5,previous_position_qty=5,entry_native_bar_index=10)
    d.update(kw); return ActiveTrade(**d)

def pulse(**kw):
    d=dict(native_time_ms=1,native_close_time_ms=2,native_bar_index=11,open=100,high=100,low=100,close=100,atr=5)
    d.update(kw); return MarketPulse(**d)

def test_closed_cash_never_substitutes_net_liq():
    with pytest.raises(StateUnverified,match="net liquidation"):
        extract_closed_cash({"netLiquidation":123456,"timestamp":"2026-09-07T14:00:00Z"})

def test_closed_cash_amount_and_timestamp_required():
    v,ts=extract_closed_cash({"amount":100123.5,"timestamp":"2026-09-07T14:00:00Z"})
    assert v==100123.5 and ts.tzinfo is not None
    with pytest.raises(StateUnverified,match="timestamp"):
        extract_closed_cash({"amount":100000})

def test_ny_calendar_day_not_futures_trade_date_boundary():
    # 2026-09-08 00:30 UTC is still Sep 7 in New York.
    assert ny_date(datetime(2026,9,8,0,30,tzinfo=timezone.utc))=="2026-09-07"

def test_realized_ledger_uses_fill_timestamp_ny_day():
    fills=[
      {"executionId":"1","instrument":"MNQZ6","timestamp":"2026-09-07T23:00:00Z","action":"buy","qty":1,"price":100},
      {"executionId":"2","instrument":"MNQZ6","timestamp":"2026-09-08T00:30:00Z","action":"sell","qty":1,"price":110},
    ]
    out=realized_by_ny_day(fills)
    assert out["2026-09-07"]==20

def test_duplicate_fill_ids_do_not_double_count():
    f1={"executionId":"1","instrument":"MNQZ6","timestamp":"2026-09-07T14:00:00Z","action":"buy","qty":1,"price":100}
    f2={"executionId":"2","instrument":"MNQZ6","timestamp":"2026-09-07T14:05:00Z","action":"sell","qty":1,"price":110}
    assert realized_by_ny_day([f1,f2,f2])["2026-09-07"]==20

def test_core_75_checked_before_50():
    d=core_on_market_pulse(trade(),pulse(high=116))
    assert d.reason=="PRE_TP1_75_TO_BE" and d.new_stop==100

def test_core_50_moves_halfway_to_initial_stop():
    d=core_on_market_pulse(trade(),pulse(high=111))
    assert d.reason=="PRE_TP1_50_HALF_STOP" and d.new_stop==95

def test_core_no_pre_tp1_move_on_entry_native_bar():
    d=core_on_market_pulse(trade(),pulse(native_bar_index=10,high=120))
    assert d.new_stop is None

def test_core_tp1_transition_destination_position_reduction():
    t=trade(previous_position_qty=5,current_position_qty=2)
    d=core_on_market_pulse(t,pulse())
    assert d.tp1_transition and d.new_stop==100.25

def test_core_runner_trail_never_loosens_long():
    t=trade(current_stop=105,tp1_filled=True,current_position_qty=2,previous_position_qty=2)
    assert core_on_market_pulse(t,pulse(runner_trail_low=104,atr=5)).new_stop is None
    d=core_on_market_pulse(t,pulse(runner_trail_low=110,atr=5))
    assert d.new_stop==109.5

def test_core_runner_trail_short_formula():
    t=trade(side="SHORT",entry=100,initial_stop=110,current_stop=110,tp1=80,tp2=60,tp1_filled=True,current_position_qty=2,previous_position_qty=2)
    d=core_on_market_pulse(t,pulse(runner_trail_high=90,atr=5))
    assert d.new_stop==90.5

def test_silver_stage1_2r_locks_quarter_r():
    t=trade(engine="SILVER",side="SHORT",entry=100,initial_stop=110,current_stop=110,stop_stage=0)
    d=silver_lock_stop(t,1)
    assert d.new_stop==97.5 and d.reason=="SILVER_2R_LOCK_0.25R"

def test_silver_stage2_3r_locks_half_r():
    t=trade(engine="SILVER",side="SHORT",entry=100,initial_stop=110,current_stop=97.5,stop_stage=1)
    d=silver_lock_stop(t,2)
    assert d.new_stop==95 and d.reason=="SILVER_3R_LOCK_0.50R"

def test_silver_stage_management_is_monotonic_and_idempotent():
    t=trade(engine="SILVER",side="SHORT",entry=100,initial_stop=110,current_stop=94,stop_stage=2)
    assert silver_lock_stop(t,1).new_stop is None
    assert silver_lock_stop(t,2).new_stop is None

def test_org_reentry_requires_destination_stop_child_fill():
    fills=[{"orderId":"target1","qty":3},{"orderId":"stopABC","qty":0}]
    assert not prove_prior_org_stop(fills=fills,stop_order_ids={"stopABC"})
    fills.append({"orderId":"stopABC","qty":3})
    assert prove_prior_org_stop(fills=fills,stop_order_ids={"stopABC"})
    assert not prove_prior_org_stop(fills=fills,stop_order_ids={"other"})
