import pytest
from execution import Executor, ProtectionFailure, verify_single_bracket, verify_core_bracket
from crosstrade import AmbiguousMutation
from models import Allocation


def alloc(engine="SILVER", qty=3):
    if engine=="CORE":
        return Allocation(account_id="A",event_id="e",engine="CORE",side="LONG",qty=qty,entry=100,stop=95,tp1=105,tp2=110,tp1_qty=(qty+1)//2,runner_qty=qty//2,base_risk=100,effective_risk_budget=100,risk_per_contract=10)
    return Allocation(account_id="A",event_id="e",engine=engine,side="LONG",qty=qty,entry=100,stop=95,tp1=105,tp1_qty=qty,runner_qty=0,base_risk=100,effective_risk_budget=100,risk_per_contract=10)

class FakeClient:
    def __init__(self, mode="normal"):
        self.mode=mode; self.place_calls=0; self.flatten_calls=0; self.change_calls=[]
        self.rows=[
          {"id":"oldT","orderType":"Limit","qty":99,"limitPrice":105},
          {"id":"oldS","orderType":"Stop","qty":99,"stopPrice":95},
        ]
        self.history=[]
    async def orders(self, account): return {"data":[dict(x) for x in self.rows]}
    async def all_orders(self): return {"data":[dict(x) for x in self.history]}
    async def place(self, account,payload):
        self.place_calls+=1; self.last_payload=payload
        if payload.get("atmTargets") is not None:
            qs=[int(x) for x in payload["atmQtys"].split(",")]
            ts=[float(x) for x in payload["atmTargets"].split(",")]
            ss=[float(x) for x in payload["atmStops"].split(",")]
            # Broker provisional prices emulate fill-relative child prices.
            for i,(q,t,s) in enumerate(zip(qs,ts,ss),1):
                self.rows.append({"id":f"t{i}","orderType":"Limit","qty":q,"limitPrice":100+t})
                self.rows.append({"id":f"s{i}","orderType":"Stop","qty":q,"stopPrice":100-s})
            self.history.append({"id":"p","clOrdId":payload["orderId"]})
            if self.mode=="ambiguous": raise AmbiguousMutation("timeout")
            # Deliberately omit child IDs to exercise before/after ownership delta for ATM.
            return {"response":{"orderId":"p"}}
        self.rows.extend([
          {"id":"t1","orderType":"Limit","qty":payload["qty"],"limitPrice":payload["takeProfit"]},
          {"id":"s1","orderType":"Stop","qty":payload["qty"],"stopPrice":payload["stopLoss"]},
        ])
        self.history.append({"id":"p","clOrdId":payload["orderId"]})
        if self.mode=="ambiguous": raise AmbiguousMutation("timeout")
        return {"response":{"orderId":"p","oso1Id":"t1","oso2Id":"s1","osoChildIds":["t1","s1"]}}
    async def change(self, account, oid, payload):
        self.change_calls.append((oid,dict(payload)))
        for o in self.rows:
            if o["id"]==oid:
                if "qty" in payload: o["qty"]=payload["qty"]
                if "limitPrice" in payload: o["limitPrice"]=payload["limitPrice"]
                if "stopPrice" in payload: o["stopPrice"]=payload["stopPrice"]
                o["orderType"]="Limit" if "limitPrice" in payload else "Stop"
                return {"ok":True}
        raise RuntimeError("unknown order")
    async def flatten(self, account,instrument="MNQ"):
        self.flatten_calls+=1; return {"ok":True}

@pytest.mark.asyncio
async def test_single_uses_exact_returned_child_ids_and_ignores_unrelated():
    c=FakeClient(); e=Executor(c,bracket_confirm_retries=1,bracket_confirm_delay=0,change_delay=0)
    r=await e.place_single("acct",alloc(),"AP_e_A")
    assert set(r.child_order_ids)=={"t1","s1"}
    assert c.place_calls==1 and c.flatten_calls==0

@pytest.mark.asyncio
async def test_ambiguous_place_is_never_resent():
    c=FakeClient("ambiguous"); e=Executor(c,bracket_confirm_retries=1,bracket_confirm_delay=0,change_delay=0)
    r=await e.place_single("acct",alloc(),"AP_e_A")
    assert c.place_calls==1
    assert set(r.child_order_ids)=={"t1","s1"}

@pytest.mark.asyncio
async def test_unreconciled_ambiguous_place_fails_without_resend():
    class F(FakeClient):
        async def place(self,a,p): self.place_calls+=1; raise AmbiguousMutation("timeout")
    c=F(); e=Executor(c,bracket_confirm_retries=1,bracket_confirm_delay=0)
    with pytest.raises(ProtectionFailure,match="no resend"):
        await e.place_single("acct",alloc(),"AP_e_A")
    assert c.place_calls==1

@pytest.mark.asyncio
async def test_core_atm_fields_are_comma_separated_strings_and_call_normalization():
    c=FakeClient(); e=Executor(c,bracket_confirm_retries=2,bracket_confirm_delay=0,change_delay=0)
    a=alloc("CORE",5); r=await e.place_core("acct",a,"AP_e_A")
    assert c.last_payload["atmTargets"]=="5,10"
    assert c.last_payload["atmStops"]=="5,5"
    assert c.last_payload["atmQtys"]=="3,2"
    assert all(isinstance(c.last_payload[k],str) for k in ("atmTargets","atmStops","atmQtys"))
    assert set(r.child_order_ids)=={"t1","s1","t2","s2"}
    assert verify_core_bracket(c.rows,a,set(r.child_order_ids))

@pytest.mark.asyncio
async def test_core_never_changes_preexisting_orders():
    c=FakeClient(); e=Executor(c,bracket_confirm_retries=2,bracket_confirm_delay=0,change_delay=0)
    await e.place_core("acct",alloc("CORE",4),"AP_e_A")
    changed={oid for oid,_ in c.change_calls}
    assert "oldT" not in changed and "oldS" not in changed
    oldt=next(x for x in c.rows if x["id"]=="oldT"); olds=next(x for x in c.rows if x["id"]=="oldS")
    assert oldt["qty"]==99 and olds["qty"]==99

@pytest.mark.asyncio
async def test_failed_owned_bracket_readback_flattens():
    class F(FakeClient):
        async def place(self,a,p):
            self.place_calls+=1
            self.rows.append({"id":"t1","orderType":"Limit","qty":p["qty"],"limitPrice":999})
            return {"response":{"oso1Id":"t1"}}
    c=F(); e=Executor(c,bracket_confirm_retries=1,bracket_confirm_delay=0)
    with pytest.raises(ProtectionFailure,match="flattened"):
        await e.place_single("acct",alloc(),"AP")
    assert c.flatten_calls==1

def test_unowned_identical_bracket_cannot_satisfy_owned_verification():
    a=alloc(); rows=[{"id":"otherT","orderType":"Limit","qty":3,"limitPrice":105},{"id":"otherS","orderType":"Stop","qty":3,"stopPrice":95}]
    assert verify_single_bracket(rows,a) is True
    assert verify_single_bracket(rows,a,{"mineT","mineS"}) is False

@pytest.mark.asyncio
async def test_execution_receipt_carries_parent_order_id_from_tradovate_response():
    c=FakeClient()
    ex=Executor(c,bracket_confirm_retries=1,bracket_confirm_delay=0,change_delay=0)
    r=await ex.place_single("acct",alloc(),"cid")
    assert r.parent_order_id=="p"

@pytest.mark.asyncio
async def test_core_short_target_tiers_normalize_by_side_absolute_price():
    class ShortFake(FakeClient):
        async def place(self, account, payload):
            self.place_calls += 1; self.last_payload = payload
            # Provisional broker children for a SHORT: nearer target is higher, runner lower.
            self.rows.extend([
                {"id":"t1","orderType":"Limit","qty":2,"limitPrice":95},
                {"id":"s1","orderType":"Stop","qty":2,"stopPrice":105},
                {"id":"t2","orderType":"Limit","qty":2,"limitPrice":90},
                {"id":"s2","orderType":"Stop","qty":2,"stopPrice":105},
            ])
            self.history.append({"id":"p","clOrdId":payload["orderId"]})
            return {"response":{"orderId":"p"}}
    a=Allocation(account_id="A",event_id="e",engine="CORE",side="SHORT",qty=4,
                 entry=100,stop=105,tp1=95,tp2=90,tp1_qty=2,runner_qty=2,
                 base_risk=100,effective_risk_budget=100,risk_per_contract=10)
    c=ShortFake(); e=Executor(c,bracket_confirm_retries=2,bracket_confirm_delay=0,change_delay=0)
    r=await e.place_core("acct",a,"AP_short")
    assert set(r.child_order_ids)=={"t1","s1","t2","s2"}
    assert verify_core_bracket(c.rows,a,set(r.child_order_ids))
