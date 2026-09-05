
from __future__ import annotations

from html import escape
from urllib.parse import quote

from fastapi.responses import HTMLResponse

PHASES = ["challenge", "funded", "personal"]
DRAWDOWNS = [
    "static",
    "eod_trail_to_lock",
    "live_trail_to_lock",
    "eod_trail_no_prepass_lock",
    "live_trail_no_prepass_lock",
]
RISK_PROFILES = ["Standard", "Aggressive"]


def _sel(value, current):
    return " selected" if str(value) == str(current) else ""


def render_account_manager(*, token: str, registry: list[dict], states: dict, overrides: dict, mode: str):
    rows = []
    for r in registry:
        aid = r["account_id"]
        st = states.get(aid)
        ov = overrides.get(aid, {})
        balance = getattr(st, "balance", None) if st else None
        netliq = getattr(st, "net_liq", None) if st else None
        mll = getattr(st, "mll_floor", None) if st else None
        cushion = getattr(st, "cushion", None) if st else None
        rows.append(f"""
        <tr>
          <td><b>{escape(r['account_name'])}</b><br><small>{escape(aid)}</small></td>
          <td>{escape(r['firm'])}<br>{escape(r['program'])}</td>
          <td>{escape(r['phase'])}</td>
          <td>{r['starting_balance']:,.0f}</td>
          <td>{(balance if balance is not None else netliq) if (balance is not None or netliq is not None) else '—'}</td>
          <td>{mll if mll is not None else '—'}</td>
          <td>{cushion if cushion is not None else '—'}</td>
          <td>{"YES" if r['risk_ready'] else "NO"}</td>
          <td>{escape(r['profile_source'])}</td>
          <td><a href="#edit-{escape(aid)}">Edit</a></td>
        </tr>
        """)

    forms = []
    for r in registry:
        aid = r["account_id"]
        st = states.get(aid)
        current_mll = getattr(st, "mll_floor", None) if st else None
        forms.append(f"""
        <section id="edit-{escape(aid)}">
          <h3>{escape(r['account_name'])}</h3>
          <p><code>{escape(aid)}</code> · Current MLL: <b>{current_mll if current_mll is not None else 'unknown'}</b></p>
          <form method="post" action="/admin/account/{quote(token)}/{quote(aid)}/override">
            <div class="grid">
              <label>Firm<input name="firm" value="{escape(r['firm'])}"></label>
              <label>Program<input name="program" value="{escape(r['program'])}"></label>
              <label>Phase<select name="phase">{''.join(f'<option value="{x}"{_sel(x,r["phase"])}>{x}</option>' for x in PHASES)}</select></label>
              <label>Starting balance<input name="starting_balance" type="number" step="0.01" value="{r['starting_balance']}"></label>
              <label>Max loss<input name="max_loss" type="number" step="0.01" value="{r['max_loss']}"></label>
              <label>Profit target<input name="profit_target" type="number" step="0.01" value="{r['profit_target']}"></label>
              <label>Max micros<input name="max_micros" type="number" value="{r['max_micros']}"></label>
              <label>Consistency %<input name="consistency_pct" type="number" step="0.01" value="{r['consistency_pct'] if r['consistency_pct'] is not None else ''}"></label>
              <label>Drawdown<select name="drawdown_type">{''.join(f'<option value="{x}"{_sel(x,r["drawdown_type"])}>{x}</option>' for x in DRAWDOWNS)}</select></label>
              <label>Risk profile<select name="risk_profile">{''.join(f'<option value="{x}"{_sel(x,ov.get("risk_profile","Standard"))}>{x}</option>' for x in RISK_PROFILES)}</select></label>
              <label>Current MLL bootstrap<input name="mll_floor" type="number" step="0.01" placeholder="Only needed once for existing accounts"></label>
              <label>MLL locked?<select name="mll_locked"><option value="false">No</option><option value="true">Yes</option></select></label>
            </div>
            <label><input type="checkbox" name="rules_verified" value="true" {"checked" if r["rules_verified"] else ""}> Rules verified</label>
            <label><input type="checkbox" name="risk_ready" value="true" {"checked" if r["risk_ready"] else ""}> Risk ready</label>
            <p><button type="submit">Save override</button></p>
          </form>
          <form method="post" action="/admin/account/{quote(token)}/{quote(aid)}/override/reset">
            <button class="secondary" type="submit">Remove manual override</button>
          </form>
        </section>
        """)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoProp Account Manager</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;max-width:1400px}} table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}}
label{{display:block;margin:8px 0}} input,select{{width:100%;box-sizing:border-box;padding:7px}} section{{margin-top:32px;padding-top:12px;border-top:2px solid #ddd}}
button{{padding:9px 14px}} .secondary{{background:#eee}} .warning{{padding:12px;background:#fff3cd;border:1px solid #ffe69c}}
code{{word-break:break-all}}
</style></head><body>
<h1>AutoProp Router Account Manager</h1>
<p>Execution mode: <b>{escape(mode)}</b></p>
<div class="warning"><b>Trading ON/OFF remains in CrossTrade.</b> Use Tradovate Account Manager → Block Signals / Closing Only. This page changes AutoProp classification and risk rules only.</div>
<h2>Discovered accounts</h2>
<table><thead><tr><th>Account</th><th>Profile</th><th>Phase</th><th>Start</th><th>Balance</th><th>MLL</th><th>Cushion</th><th>Risk ready</th><th>Source</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Manual overrides</h2>
{''.join(forms)}
</body></html>"""
    return HTMLResponse(html)
