from __future__ import annotations
from html import escape
from urllib.parse import quote
from fastapi.responses import HTMLResponse

PHASES=["challenge","funded","personal"]
DRAWDOWNS=["static","eod_trail_to_lock","live_trail_to_lock","eod_trail_no_prepass_lock","live_trail_no_prepass_lock"]
RISK_PROFILES=["Standard","Aggressive"]
def _sel(v,c): return " selected" if str(v)==str(c) else ""
def _money(v): return "—" if v is None else f"{float(v):,.2f}"

def render_account_manager(*,token,registry,states,overrides,mode,management_mode,armed):
    rows=[]; forms=[]
    for r in registry:
        aid=r['account_id']; st=states.get(aid); ov=overrides.get(aid,{})
        bal=getattr(st,'balance',None) if st else None; net=getattr(st,'net_liq',None) if st else None
        mll=getattr(st,'mll_floor',None) if st else None; cushion=getattr(st,'cushion',None) if st else None
        rows.append(f"<tr><td><b>{escape(r['account_name'])}</b><br><small>{escape(aid)}</small></td><td>{escape(r['firm'])}<br>{escape(r['program'])}</td><td>{escape(r['phase'])}</td><td>{r['starting_balance']:,.0f}</td><td>{_money(bal if bal is not None else net)}</td><td>{_money(mll)}</td><td>{_money(cushion)}</td><td>{'YES' if r['risk_ready'] else 'NO'}</td><td>{escape(r['profile_source'])}</td><td><a href='#edit-{escape(aid)}'>Edit</a></td></tr>")
        forms.append(f"""<section id="edit-{escape(aid)}"><h3>{escape(r['account_name'])}</h3><p><code>{escape(aid)}</code> · Current MLL: <b>{_money(mll)}</b></p>
        <form method="post" action="/admin/account/{quote(token)}/{quote(aid)}/override"><div class="grid">
        <label>Firm<input name="firm" value="{escape(r['firm'])}"></label><label>Program<input name="program" value="{escape(r['program'])}"></label>
        <label>Phase<select name="phase">{''.join(f'<option value="{x}"{_sel(x,r["phase"])}>{x}</option>' for x in PHASES)}</select></label>
        <label>Starting balance<input name="starting_balance" type="number" step="0.01" value="{r['starting_balance']}"></label>
        <label>Max loss<input name="max_loss" type="number" step="0.01" value="{r['max_loss']}"></label>
        <label>Profit target<input name="profit_target" type="number" step="0.01" value="{r['profit_target']}"></label>
        <label>Max micros<input name="max_micros" type="number" value="{r['max_micros']}"></label>
        <label>Consistency %<input name="consistency_pct" type="number" step="0.01" value="{r['consistency_pct'] if r['consistency_pct'] is not None else ''}"></label>
        <label>Drawdown<select name="drawdown_type">{''.join(f'<option value="{x}"{_sel(x,r["drawdown_type"])}>{x}</option>' for x in DRAWDOWNS)}</select></label>
        <label>Lock offset ($)<input name="lock_offset" type="number" step="1" value="{ov.get('lock_offset',0)}"></label>
        <label>Risk profile<select name="risk_profile">{''.join(f'<option value="{x}"{_sel(x,ov.get("risk_profile","Standard"))}>{x}</option>' for x in RISK_PROFILES)}</select></label>
        <label>Current MLL bootstrap<input name="mll_floor" type="number" step="0.01" placeholder="Required once for existing trailing accounts"></label>
        <label>MLL locked?<select name="mll_locked"><option value="false">No</option><option value="true">Yes</option></select></label></div>
        <label><input type="checkbox" name="rules_verified" value="true" {'checked' if r['rules_verified'] else ''}> Rules verified</label>
        <label><input type="checkbox" name="risk_ready" value="true" {'checked' if r['risk_ready'] else ''}> Risk ready</label>
        <p><button type="submit">Save override</button></p></form>
        <form method="post" action="/admin/account/{quote(token)}/{quote(aid)}/override/reset"><button class="secondary" type="submit">Remove manual override</button></form></section>""")
    html=f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AutoProp Account Manager</title><style>
    body{{font-family:system-ui,sans-serif;margin:24px;max-width:1450px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}label{{display:block;margin:8px 0}}input,select{{width:100%;box-sizing:border-box;padding:7px}}section{{margin-top:32px;padding-top:12px;border-top:2px solid #ddd}}button{{padding:9px 14px}}.secondary{{background:#eee}}.warning{{padding:12px;background:#fff3cd;border:1px solid #ffe69c}}.ok{{padding:12px;background:#e7f6ec;border:1px solid #b7dfc2}}code{{word-break:break-all}}</style></head><body>
    <h1>AutoProp Router Account Manager</h1><p>Execution: <b>{escape(mode)}</b> · Management: <b>{escape(management_mode)}</b> · Broker mutation armed: <b>{'YES' if armed else 'NO'}</b></p>
    <div class="warning"><b>Account ON/OFF stays in CrossTrade.</b> Use <b>Closing Only</b> when an account has an open AutoProp position but should take no new entries. Use <b>Block Signals</b> only when that account is flat.</div>
    <h2>Discovered accounts</h2><table><thead><tr><th>Account</th><th>Profile</th><th>Phase</th><th>Start</th><th>Balance</th><th>MLL</th><th>Cushion</th><th>Risk ready</th><th>Source</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <h2>Manual overrides / MLL bootstrap</h2>{''.join(forms)}</body></html>"""
    return HTMLResponse(html)
