import importlib.util
import sys
from pathlib import Path
import pytest

import parity as p

spec = importlib.util.spec_from_file_location("golden", Path(__file__).with_name("golden_oracle_v1_0.py"))
g = importlib.util.module_from_spec(spec); sys.modules[spec.name] = g; spec.loader.exec_module(g)

@pytest.mark.parametrize("x", [0, .49, .5, 1.49, 1.5, 2.49, 2.5, 10.5, 49.5])
def test_pine_round_matches_golden(x): assert p.pine_round_positive(x) == g.pine_round_positive(x)

@pytest.mark.parametrize("dd", ["static","eod","live"])
@pytest.mark.parametrize("profile", ["standard","aggressive"])
@pytest.mark.parametrize("progress", [0,79.99,80,100])
def test_challenge_risk_matches_golden(dd, profile, progress):
    a=p.challenge_risk(cushion=2400,max_loss=3000,drawdown=dd,profile=profile,progress_pct=progress)
    b=g.challenge_risk(cushion=2400,max_loss=3000,drawdown=dd,profile=profile,progress_pct=progress)
    assert (a.pct,a.raw,a.cap,a.base,a.progress_mult,a.final) == pytest.approx((b.pct,b.raw,b.cap,b.base,b.progress_mult,b.final))

@pytest.mark.parametrize("profile", ["standard","aggressive"])
@pytest.mark.parametrize("cushion", [100,899,900,1499,1500,2249,2250,3000,6000])
def test_funded_matches_golden(profile,cushion):
    assert p.funded_prelock_risk(cushion=cushion,max_loss=3000,profile=profile) == g.funded_prelock_risk(cushion=cushion,max_loss=3000,profile=profile)
    assert p.funded_postlock_risk(cushion=cushion,max_loss=3000,profile=profile) == g.funded_postlock_risk(cushion=cushion,max_loss=3000,profile=profile)

@pytest.mark.parametrize("actual,profile,expected", [(30,"standard",.30),(30,"aggressive",.40),(40,"aggressive",.50),(50,"aggressive",.50),(55,"aggressive",.55)])
def test_planning_ceiling(actual,profile,expected):
    assert p.challenge_planning_ceiling(actual,profile,True)==pytest.approx(expected)
    assert p.challenge_planning_ceiling(actual,profile,True)==g.challenge_planning_ceiling(actual,profile,True)

@pytest.mark.parametrize("realized", [-500,0,1150,1199,1200,1250])
def test_consistency_room_negative_days_and_floor(realized):
    assert p.consistency_room(realized_today=realized,enabled=True,ceiling_fraction=.4,base_target=3000) == g.consistency_room(realized_today=realized,enabled=True,ceiling_fraction=.4,base_target=3000)

@pytest.mark.parametrize("qty", [1,2,3,4,5,6,11,25,50])
def test_core_split(qty): assert p.core_split(qty)==g.core_split(qty)

@pytest.mark.parametrize("score,cap", [(0,3),(1,3),(2,4),(3,5),(4,6),(8,6)])
def test_prop_core_caps(score,cap):
    assert p.module_contract_cap(module="CORE",score=score,account_cap=50,account_type="challenge") == cap
    assert p.module_contract_cap(module="CORE",score=score,account_cap=50,account_type="challenge") == g.module_contract_cap(module="CORE",score=score,account_cap=50,account_type="challenge")

@pytest.mark.parametrize("score", [1,2,3,4])
def test_personal_core_uses_account_cap(score):
    assert p.module_contract_cap(module="CORE",score=score,account_cap=37,account_type="personal") == 37

@pytest.mark.parametrize("source,score,side", [("PDL",4,"LONG"),("HPL",3,"LONG"),("ORL",4,"LONG"),("ORH",3,"SHORT"),("ASH",2,"SHORT"),("PDH",1,"LONG")])
def test_prop_core_multiplier_matches_original_golden(source,score,side):
    assert p.core_risk_multiplier(source=source,score=score,side=side,account_type="challenge") == g.core_risk_multiplier(source=source,score=score,side=side)

@pytest.mark.parametrize("score,expected", [(1,.75),(2,1),(3,1.25),(4,1.5)])
def test_personal_core_score_only(score,expected):
    assert p.core_risk_multiplier(source="PDL",score=score,side="LONG",account_type="personal") == expected

@pytest.mark.parametrize("cushion,rpc", [(3000,20),(3000,50),(1500,25),(600,20)])
def test_funded_12r_safe_qty(cushion,rpc):
    assert p.funded_core_safe_qty(cushion=cushion,risk_per_contract=rpc)==g.funded_core_safe_qty(cushion=cushion,risk_per_contract=rpc)

@pytest.mark.parametrize("engine,entry,stop,expected", [("ORG",20000,19990,22.9),("SILVER",20000,19990,20),("CORE",20000,19990,20),("TGIF",20000,19990,20),("DWC",20000,19990,20)])
def test_engine_specific_risk_per_contract(engine,entry,stop,expected): assert p.risk_per_contract(engine=engine,entry=entry,stop=stop)==pytest.approx(expected)

def test_consistency_forced_one_then_clip():
    q=p.consistency_qty(pre_qty=6,projected_profit_per_contract=200,realized_today=1140,enabled=True,ceiling_fraction=.4,base_target=3000)
    assert q==1
    t=p.consistency_target(entry=100,natural_target=200,qty=q,realized_today=1140,enabled=True,ceiling_fraction=.4,base_target=3000)
    assert t < 200

def test_core_consistency_matches_golden_grid():
    for pre in range(1,12):
      for realized in (-300,0,500,1100,1149,1150,1199):
        kw=dict(pre_qty=pre,tp1_profit_per_contract=50,tp2_profit_per_contract=150,min_runner_qty=2,realized_today=realized,enabled=True,ceiling_fraction=.4,base_target=3000)
        assert p.core_consistency_qty(**kw)==g.core_consistency_qty(**kw)
