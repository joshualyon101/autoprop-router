import pytest

from execution import Executor, ProtectionFailure, verify_single_bracket, verify_core_bracket
from models import Allocation


def single_alloc(qty=3):
    return Allocation(account_id='A', event_id='e', engine='SILVER', side='SHORT', qty=qty,
                      entry=29467.0, stop=29483.0, tp1=29208.75, tp2=None,
                      tp1_qty=qty, runner_qty=0, base_risk=100,
                      effective_risk_budget=100, risk_per_contract=32.0)


def core_alloc(qty=4):
    return Allocation(account_id='A', event_id='c', engine='CORE', side='LONG', qty=qty,
                      entry=100.0, stop=95.0, tp1=105.0, tp2=110.0,
                      tp1_qty=2, runner_qty=2, base_risk=100,
                      effective_risk_budget=100, risk_per_contract=10.0)


class RealisticTradovateClient:
    """Mocks current CrossTrade Tradovate REST semantics.

    GET /accounts/{account}/orders exposes raw Order rows only (id/status), while
    lifecycle exposes OrderVersion quantity/type/prices.
    """
    def __init__(self):
        self.rows = {}
        self.versions = {}
        self.commands = {}
        self.reports = {}
        self.next_command_id = 1000
        self.flatten_calls = 0
        self.change_calls = []
        self.last_payload = None

    async def orders(self, account):
        data=[]
        for oid,row in self.rows.items():
            if str(row.get('ordStatus','')).lower() in {'working','suspended'}:
                data.append(dict(row))
        return {'success':True,'data':data}

    async def all_orders(self):
        # Identity-envelope shape documented by CrossTrade.
        return {'success':True,'data':[{'environment':'live','userId':'1','name':'id','data':[dict(x) for x in self.rows.values()]}]}

    async def order_lifecycle(self, account, oid):
        oid=str(oid)
        if oid not in self.rows:
            raise RuntimeError('unknown order')
        return {'success':True,'data':{
            'order':dict(self.rows[oid]),
            'version':dict(self.versions[oid]),
            'commands':list(self.commands.get(oid,[])),
            'reports':list(self.reports.get(oid,[])),
        }}

    async def place(self, account, payload):
        self.last_payload=dict(payload)
        if payload.get('atmTargets') is not None:
            # Provisional Core children, no enriched fields in /orders.
            self.rows.update({
                't1':{'id':'t1','ordStatus':'Working'},
                's1':{'id':'s1','ordStatus':'Working'},
                't2':{'id':'t2','ordStatus':'Working'},
                's2':{'id':'s2','ordStatus':'Working'},
                'p':{'id':'p','ordStatus':'Filled'},
            })
            self.versions.update({
                't1':{'orderId':'t1','orderQty':2,'orderType':'Limit','price':105.0},
                's1':{'orderId':'s1','orderQty':2,'orderType':'Stop','stopPrice':95.0},
                't2':{'orderId':'t2','orderQty':2,'orderType':'Limit','price':110.0},
                's2':{'orderId':'s2','orderQty':2,'orderType':'Stop','stopPrice':95.0},
                'p':{'orderId':'p','orderQty':4,'orderType':'Market'},
            })
            self.commands['p']=[{'commandType':'New','clOrdId':payload['orderId']}]
            return {'response':{'orderId':'p','osoChildIds':['t1','s1','t2','s2']}}
        self.rows.update({
            't1':{'id':'t1','ordStatus':'Working'},
            's1':{'id':'s1','ordStatus':'Working'},
            'p':{'id':'p','ordStatus':'Filled'},
        })
        self.versions.update({
            't1':{'orderId':'t1','orderQty':payload['qty'],'orderType':'Limit','price':payload['takeProfit']},
            's1':{'orderId':'s1','orderQty':payload['qty'],'orderType':'Stop','stopPrice':payload['stopLoss']},
            'p':{'orderId':'p','orderQty':payload['qty'],'orderType':'Market'},
        })
        self.commands['p']=[{'commandType':'New','clOrdId':payload['orderId']}]
        return {'response':{'orderId':'p','oso1Id':'t1','oso2Id':'s1','osoChildIds':['t1','s1']}}

    async def change(self, account, oid, payload):
        oid=str(oid); self.change_calls.append((oid,dict(payload)))
        v=self.versions[oid]
        if 'qty' in payload: v['orderQty']=int(payload['qty'])
        if 'orderType' in payload: v['orderType']=str(payload['orderType']).capitalize()
        if 'limitPrice' in payload: v['price']=float(payload['limitPrice'])
        if 'stopPrice' in payload: v['stopPrice']=float(payload['stopPrice'])
        self.next_command_id += 1
        command_id = self.next_command_id
        self.commands.setdefault(oid, []).append({
            'id':command_id, 'orderId':oid, 'commandType':'Modify',
            'commandStatus':'Replaced',
        })
        self.reports.setdefault(oid, []).append({
            'commandId':command_id, 'commandStatus':'Replaced',
            'rejectReason':'Success', 'ordStatus':self.rows[oid]['ordStatus'],
        })
        return {'ok':True}

    async def flatten(self, account, instrument='MNQ1!'):
        self.flatten_calls += 1
        return {'ok':True}


@pytest.mark.asyncio
async def test_single_bracket_verifies_from_lifecycle_order_version_not_raw_order_rows():
    c=RealisticTradovateClient()
    e=Executor(c, execution_symbol='MNQ', bracket_confirm_retries=1, bracket_confirm_delay=0)
    r=await e.place_single('acct', single_alloc(), 'CID-SILVER')
    assert set(r.child_order_ids)=={'t1','s1'}
    assert c.flatten_calls==0
    rows=await e.working_orders('acct')
    assert verify_single_bracket(rows, single_alloc(), {'t1','s1'})


@pytest.mark.asyncio
async def test_owned_roles_and_stop_change_use_lifecycle_enrichment():
    c=RealisticTradovateClient()
    e=Executor(c, execution_symbol='MNQ1!', bracket_confirm_retries=1, bracket_confirm_delay=0,
               change_retries=1, change_delay=0)
    r=await e.place_single('acct', single_alloc(), 'CID-SILVER')
    targets,stops=await e.owned_roles('acct', r.child_order_ids)
    assert targets==['t1'] and stops==['s1']
    changed=await e.change_stop_orders('acct', stops, 29459.0, 3)
    assert changed==['s1']
    assert c.versions['s1']['stopPrice']==29459.0


@pytest.mark.asyncio
async def test_core_normalization_works_when_working_orders_are_raw_tradovate_rows():
    c=RealisticTradovateClient()
    e=Executor(c, execution_symbol='MNQ1!', bracket_confirm_retries=1, bracket_confirm_delay=0,
               change_retries=1, change_delay=0)
    r=await e.place_core('acct', core_alloc(), 'CID-CORE')
    assert set(r.child_order_ids)=={'t1','s1','t2','s2'}
    rows=await e.working_orders('acct')
    assert verify_core_bracket(rows, core_alloc(), set(r.child_order_ids))
    assert c.flatten_calls==0


@pytest.mark.asyncio
async def test_all_orders_identity_envelope_is_flattened_for_reconciliation_candidates():
    c=RealisticTradovateClient()
    # Seed a filled parent carrying the custom id on its lifecycle New command.
    c.rows['p']={'id':'p','ordStatus':'Filled'}
    c.versions['p']={'orderId':'p','orderQty':1,'orderType':'Market'}
    c.commands['p']=[{'commandType':'New','clOrdId':'CID-AMB'}]
    e=Executor(c)
    assert await e._reconcile_custom_order('acct','CID-AMB')=='p'


class ChangeLifecycleClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.change_calls = 0
        self.lifecycle_calls = 0
        self.version = {
            'orderId':'s1', 'orderQty':2, 'orderType':'Stop', 'stopPrice':95.0,
        }
        self.commands = [{
            'id':10, 'orderId':'s1', 'commandType':'Modify',
            'commandStatus':'Replaced',
        }]
        self.reports = [{
            'commandId':10, 'commandStatus':'Replaced',
            'rejectReason':'Success', 'ordStatus':'Working',
        }]

    async def order_lifecycle(self, account, oid):
        self.lifecycle_calls += 1
        payload = {'success':True, 'data':{
            'order':{'id':'s1', 'ordStatus':'Working'},
            'version':dict(self.version),
            'commands':[dict(x) for x in self.commands],
            'reports':[dict(x) for x in self.reports],
        }}
        if self.outcome == 'unavailable' and self.change_calls:
            payload['partial'] = True
            payload['unavailable'] = ['reports:11']
        return payload

    async def change(self, account, oid, payload):
        self.change_calls += 1
        self.version['orderQty'] = int(payload['qty'])
        self.version['orderType'] = 'Stop'
        self.version['stopPrice'] = float(payload['stopPrice'])
        if self.outcome == 'accepted':
            command_status = 'Replaced'
            reports = [{
                'commandId':11, 'commandStatus':'Replaced',
                'rejectReason':'Success', 'ordStatus':'Working',
            }]
        elif self.outcome == 'rejected':
            # The version already contains the requested price, but the command report is
            # authoritative and says the broker rejected that modification.
            command_status = 'RiskPassed'
            reports = [{
                'commandId':11, 'commandStatus':'ExecutionRejected',
                'rejectReason':'InvalidPrice', 'ordStatus':'Working',
            }]
        elif self.outcome == 'pending_success':
            command_status = 'Replaced'
            reports = [{
                'commandId':11, 'commandStatus':'PendingExecution',
                'rejectReason':'Success', 'ordStatus':'Working',
            }]
        elif self.outcome == 'replaced_blank':
            command_status = 'Replaced'
            reports = [{
                'commandId':11, 'commandStatus':'Replaced',
                'rejectReason':'', 'ordStatus':'Working',
            }]
        else:
            command_status = 'PendingExecution' if self.outcome == 'pending' else 'Replaced'
            reports = []
        self.commands.append({
            'id':11, 'orderId':'s1', 'commandType':'Modify',
            'commandStatus':command_status,
        })
        self.reports.extend(reports)
        return {'success':True, 'response':{}}


@pytest.mark.asyncio
async def test_change_requires_new_modify_and_correlated_success_report():
    client = ChangeLifecycleClient('accepted')
    executor = Executor(client, change_retries=2, change_delay=0)

    await executor._change_exact(
        'acct', 's1', {'qty':2, 'orderType':'stop', 'stopPrice':96.0}
    )

    assert client.change_calls == 1


@pytest.mark.asyncio
async def test_rejected_modify_report_overrides_matching_order_version():
    client = ChangeLifecycleClient('rejected')
    executor = Executor(client, change_retries=2, change_delay=0)

    with pytest.raises(ProtectionFailure, match='InvalidPrice|invalidprice'):
        await executor._change_exact(
            'acct', 's1', {'qty':2, 'orderType':'stop', 'stopPrice':96.0}
        )

    assert client.version['stopPrice'] == 96.0
    assert client.change_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'outcome',
    ['pending', 'pending_success', 'replaced_blank', 'missing', 'unavailable'],
)
async def test_unconfirmed_modify_is_polled_without_resend(outcome):
    client = ChangeLifecycleClient(outcome)
    executor = Executor(client, change_retries=3, change_delay=0)

    with pytest.raises(ProtectionFailure, match='outcome unconfirmed; no resend'):
        await executor._change_exact(
            'acct', 's1', {'qty':2, 'orderType':'stop', 'stopPrice':96.0}
        )

    assert client.change_calls == 1
    assert client.lifecycle_calls == 4  # one baseline read plus three confirmation polls


class ResolvingChangeLifecycleClient(ChangeLifecycleClient):
    async def order_lifecycle(self, account, oid):
        # Baseline is call 1 and the first post-CHANGE observation is call 2. Resolve only
        # before the second post-CHANGE poll so the test proves that polling—not resend—wins.
        if self.change_calls and self.lifecycle_calls >= 2:
            self.commands[-1]['commandStatus'] = 'Replaced'
            if not any(str(r.get('commandId')) == '11' for r in self.reports):
                self.reports.append({
                    'commandId':11, 'commandStatus':'Replaced',
                    'rejectReason':'Success', 'ordStatus':'Working',
                })
        return await super().order_lifecycle(account, oid)


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', ['pending', 'missing'])
async def test_change_waits_for_final_report_without_resending(initial):
    client = ResolvingChangeLifecycleClient(initial)
    executor = Executor(client, change_retries=3, change_delay=0)

    await executor._change_exact(
        'acct', 's1', {'qty':2, 'orderType':'stop', 'stopPrice':96.0}
    )

    assert client.change_calls == 1
    assert client.lifecycle_calls == 3  # baseline, pending/missing, final Replaced+Success
