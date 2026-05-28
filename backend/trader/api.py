"""FastAPI router for the paper trader, mounted under /trader.

Endpoints are intentionally small and verb-shaped. The dashboard is a single
self-contained HTML page (no build step, no framework) that polls status and
journal — enough to watch the account, read the last few decisions, and hit
the kill switch. The extension panel from doc 07 would consume these same
endpoints; it just doesn't exist yet.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from backend.trader.service import get_service

router = APIRouter(prefix="/trader", tags=["trader"])


@router.get("/status")
async def status() -> dict:
    return get_service().status()


@router.post("/start")
async def start() -> dict:
    return get_service().start()


@router.post("/stop")
async def stop() -> dict:
    return await get_service().stop()


@router.post("/kill")
async def kill() -> dict:
    return await get_service().kill()


@router.post("/rearm")
async def rearm() -> dict:
    return get_service().rearm()


@router.post("/decide")
async def decide(symbol: str = Query(...)) -> dict:
    return await get_service().decide_once(symbol)


@router.get("/journal")
async def journal(n: int = Query(20, ge=1, le=500)) -> dict:
    svc = get_service()
    return {"records": svc.journal.tail(n)}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>HAL paper trader</title>
<style>
  body{font:14px/1.5 ui-monospace,Menlo,monospace;margin:24px;background:#0b0e14;color:#cdd6f4}
  h1{font-size:18px} h2{font-size:14px;color:#89b4fa;margin:18px 0 6px}
  button{font:inherit;padding:6px 12px;margin-right:8px;border:1px solid #45475a;
    background:#181825;color:#cdd6f4;border-radius:6px;cursor:pointer}
  button:hover{background:#313244} .kill{border-color:#f38ba8;color:#f38ba8}
  table{border-collapse:collapse;width:100%} td,th{text-align:left;padding:3px 10px 3px 0}
  .pill{padding:1px 8px;border-radius:10px;font-size:12px}
  .ok{background:#1e3a2a;color:#a6e3a1} .bad{background:#3a1e26;color:#f38ba8}
  pre{background:#11111b;padding:10px;border-radius:6px;overflow:auto;max-height:340px}
  .num{font-variant-numeric:tabular-nums}
</style></head><body>
<h1>HAL — autonomous paper trader</h1>
<div>
  <button onclick="act('start')">start</button>
  <button onclick="act('stop')">stop</button>
  <button class="kill" onclick="act('kill')">KILL</button>
  <button onclick="act('rearm')">re-arm</button>
</div>
<h2>account</h2><div id="acct"></div>
<h2>open positions</h2><div id="pos"></div>
<h2>risk</h2><div id="risk"></div>
<h2>recent decisions</h2><pre id="journal"></pre>
<script>
async function act(verb){ await fetch('/trader/'+verb,{method:'POST'}); refresh(); }
function pill(b,t){ return '<span class="pill '+(b?'ok':'bad')+'">'+t+'</span>'; }
async function refresh(){
  const s = await (await fetch('/trader/status')).json();
  const a = s.account||{};
  document.getElementById('acct').innerHTML =
    'running '+pill(s.running,s.running?'yes':'no')+' &nbsp; policy: '+s.policy+
    (s.model?(' ('+s.model+')'):'')+'<br>equity <b class=num>'+fmt(a.equity)+
    '</b> &nbsp; realized '+fmt(a.realized_equity)+' &nbsp; HWM '+fmt(a.high_water_mark)+
    ' &nbsp; closed trades '+(a.closed_trades||0);
  const ps = (a.open_positions||[]);
  document.getElementById('pos').innerHTML = ps.length ? table(ps) : '<i>flat</i>';
  const r = s.risk||{};
  document.getElementById('risk').innerHTML =
    'entries '+pill(r.entries_allowed,r.entries_allowed?'allowed':'blocked')+
    ' &nbsp; killed '+pill(!r.killed,r.killed?'YES':'no')+
    ' &nbsp; perm-halt '+pill(!r.permanent_halt,r.permanent_halt?'YES':'no')+
    ' &nbsp; daily-halt '+pill(!r.halted_today,r.halted_today?'YES':'no')+
    ' &nbsp; realized today '+fmt(r.realized_today)+
    (r.block_reason?('<br><span class=bad>'+r.block_reason+'</span>'):'');
  const j = await (await fetch('/trader/journal?n=8')).json();
  document.getElementById('journal').textContent =
    (j.records||[]).slice().reverse().map(rec =>
      rec.ts+'  '+rec.symbol+'  '+rec.plan.action+
      '  px='+fmt(rec.current_price)+'  eq='+fmt(rec.equity)+
      (rec.execution&&rec.execution.executed?'  [EXECUTED]':'')+
      (rec.exits&&rec.exits.length?('  [EXIT '+rec.exits.map(e=>e.reason+' '+fmt(e.net_pnl)).join(', ')+']'):'')+
      '\\n    '+(rec.plan.rationale||'')
    ).join('\\n');
}
function fmt(x){ return (x==null)?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:2}); }
function table(rows){
  const cols=['symbol','side','qty','entry_price','stop','take_profit','mark','unrealized'];
  return '<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+
    rows.map(r=>'<tr>'+cols.map(c=>'<td class=num>'+fmt2(r[c])+'</td>').join('')+'</tr>').join('')+'</table>';
}
function fmt2(x){ return (typeof x==='number')?fmt(x):x; }
refresh(); setInterval(refresh, 5000);
</script></body></html>"""
