#!/usr/bin/env python3
"""
Sector Cap vs Equal-Weight Spread Alerter  v2
==============================================
Alerts: threshold crossings + spread reversals
Delivery: HTML email (always) + SMS + desktop (alerts only)
Extras: factor dashboard, regime signal, RS charts, AI interpretation
"""

import base64
import io
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Definitions ───────────────────────────────────────────────────────────────
SECTORS = [
    {"name": "Technology",     "cw": "XLK",  "ew": "RSPT"},
    {"name": "Financials",     "cw": "XLF",  "ew": "RSPF"},
    {"name": "Energy",         "cw": "XLE",  "ew": "RSPE"},
    {"name": "Health Care",    "cw": "XLV",  "ew": "RYH"},
    {"name": "Industrials",    "cw": "XLI",  "ew": "RGI"},
    {"name": "Cons. Disc.",    "cw": "XLY",  "ew": "RSPD"},
    {"name": "Cons. Staples",  "cw": "XLP",  "ew": "RSPS"},
    {"name": "Utilities",      "cw": "XLU",  "ew": "RSPU"},
    {"name": "Real Estate",    "cw": "XLRE", "ew": "RSPR"},
    {"name": "Materials",      "cw": "XLB",  "ew": "RSPM"},
    {"name": "Comm. Services", "cw": "XLC",  "ew": "RSPG"},
]

FACTOR_PAIRS = [
    {"name": "Market Breadth",    "cw": "SPY",  "ew": "RSP",  "label": "SPY vs RSP",      "pos": "Eq-wt leading",     "neg": "Large caps leading"},
    {"name": "Momentum vs Value", "cw": "VLUE", "ew": "MTUM", "label": "MTUM vs VLUE",    "pos": "Momentum leading",  "neg": "Value leading"},
    {"name": "Small vs Large",    "cw": "SPY",  "ew": "IWM",  "label": "IWM vs SPY",      "pos": "Small caps leading","neg": "Large caps leading"},
    {"name": "Growth vs Value",   "cw": "IVE",  "ew": "IVW",  "label": "IVW vs IVE",      "pos": "Growth leading",    "neg": "Value leading"},
]

TOP_HOLDINGS = {
    "Technology":    ["NVDA", "MSFT", "AAPL", "AVGO", "META"],
    "Financials":    ["BRK.B", "JPM", "V", "MA", "BAC"],
    "Energy":        ["XOM", "CVX", "COP", "EOG", "SLB"],
    "Health Care":   ["LLY", "UNH", "JNJ", "ABBV", "MRK"],
    "Industrials":   ["GE", "RTX", "CAT", "UPS", "HON"],
    "Cons. Disc.":   ["AMZN", "TSLA", "HD", "MCD", "NKE"],
    "Cons. Staples": ["WMT", "PG", "COST", "KO", "PEP"],
    "Utilities":     ["NEE", "SO", "DUK", "AEP", "SRE"],
    "Real Estate":   ["PLD", "AMT", "EQIX", "WELL", "SPG"],
    "Materials":     ["LIN", "APD", "SHW", "FCX", "NEM"],
    "Comm. Services":["META", "GOOGL", "NFLX", "DIS", "CMCSA"],
}

# ── Config ────────────────────────────────────────────────────────────────────
PERIOD       = os.getenv("ALERT_PERIOD",    "1mo")
INTERVAL     = os.getenv("ALERT_INTERVAL",  "1d")
THRESHOLD    = float(os.getenv("ALERT_THRESHOLD", "2.0"))
REVERSAL_MIN = float(os.getenv("REVERSAL_MIN",    "1.0"))
COOLDOWN_HRS = float(os.getenv("COOLDOWN_HRS",    "20.0"))
MAX_HISTORY  = int(os.getenv("MAX_HISTORY",       "30"))
STATE_FILE   = Path(os.getenv("STATE_FILE", "sector_state.json"))

SMTP_HOST    = os.getenv("SMTP_HOST",    "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER",    "")
SMTP_PASS    = os.getenv("SMTP_PASS",    "")
EMAIL_TO     = os.getenv("ALERT_EMAIL_TO", "")

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID",  "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN",   "")
TWILIO_FROM  = os.getenv("TWILIO_FROM_NUMBER",  "")
TWILIO_TO_N  = os.getenv("TWILIO_TO_NUMBER",    "")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DESKTOP_ON    = os.getenv("DESKTOP_ALERTS", "true").lower() == "true"
YF_HEADERS    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FETCH_DELAY   = 0.35

# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_series(symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={INTERVAL}&range={PERIOD}")
    try:
        r = requests.get(url, timeout=12, headers=YF_HEADERS)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        return res["timestamp"], res["indicators"]["quote"][0]["close"]
    except Exception as e:
        log.warning(f"    Fetch failed {symbol}: {e}")
        return None, None

def calc_return(closes):
    if not closes:
        return None
    valid = [c for c in closes if c is not None]
    return (valid[-1] / valid[0] - 1) * 100 if len(valid) >= 2 else None

def calc_rs(ew_c, cw_c):
    """EW/CW ratio normalized to 0% at start"""
    ratios, base = [], None
    for e, c in zip(ew_c, cw_c):
        if e is not None and c is not None and c != 0:
            r = e / c
            if base is None:
                base = r
            ratios.append(round((r / base - 1) * 100, 3))
        else:
            ratios.append(None)
    return ratios

def fetch_all(pairs):
    results = []
    for p in pairs:
        log.info(f"  {p['name']:<22} {p['cw']} / {p['ew']}")
        ts_cw, c_cw = fetch_series(p["cw"])
        time.sleep(FETCH_DELAY)
        ts_ew, c_ew = fetch_series(p["ew"])
        time.sleep(FETCH_DELAY)
        cw_ret = calc_return(c_cw)
        ew_ret = calc_return(c_ew)
        spread = (ew_ret - cw_ret) if (cw_ret is not None and ew_ret is not None) else None
        rs, dates = None, None
        if ts_cw and c_cw and c_ew:
            n = min(len(ts_cw), len(c_ew))
            rs = calc_rs(c_ew[:n], c_cw[:n])
            dates = [datetime.fromtimestamp(ts).strftime("%m/%d") for ts in ts_cw[:n]]
        results.append({**p, "cw_ret": cw_ret, "ew_ret": ew_ret,
                        "spread": spread, "rs": rs, "dates": dates})
    return results

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(sector_data, factor_data, threshold_fired, reversal_signs):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state()
    history = state.get("spread_history", {})
    for d in sector_data + factor_data:
        name = d["name"]
        if name not in history:
            history[name] = []
        if d["spread"] is not None:
            history[name] = [x for x in history[name] if x["date"] != today]
            history[name].append({"date": today, "spread": round(d["spread"], 3)})
            history[name] = sorted(history[name], key=lambda x: x["date"])[-MAX_HISTORY:]
    STATE_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": PERIOD,
        "spreads": {d["name"]: d["spread"] for d in sector_data + factor_data},
        "threshold_fired": threshold_fired,
        "reversal_signs": reversal_signs,
        "spread_history": history,
    }, indent=2))
    log.info(f"State saved -> {STATE_FILE}")

def calc_ma(hist, n):
    vals = [x["spread"] for x in hist[-n:] if x["spread"] is not None]
    return round(sum(vals) / len(vals), 2) if len(vals) >= 2 else None

# ── Alerts ────────────────────────────────────────────────────────────────────
def detect_alerts(sector_data, prev_spreads, threshold_fired, reversal_signs):
    alerts, now = [], datetime.now(timezone.utc)
    for d in sector_data:
        sp, name = d["spread"], d["name"]
        if sp is None:
            continue
        prev_sp = prev_spreads.get(name)
        # Threshold
        if abs(sp) >= THRESHOLD:
            last = threshold_fired.get(name)
            ok = (not last) or (now - datetime.fromisoformat(last) > timedelta(hours=COOLDOWN_HRS))
            if ok:
                alerts.append({**d, "type": "THRESHOLD",
                                "direction": "BROAD" if sp > 0 else "NARROW",
                                "prev_spread": prev_sp})
                threshold_fired[name] = now.isoformat()
        # Reversal
        if prev_sp is not None and abs(prev_sp) >= REVERSAL_MIN:
            cur_sign, old_sign = (1 if sp >= 0 else -1), (1 if prev_sp >= 0 else -1)
            if old_sign != cur_sign and reversal_signs.get(name) != cur_sign:
                alerts.append({**d, "type": "REVERSAL",
                                "direction": "BROAD" if sp > 0 else "NARROW",
                                "from_direction": "BROAD" if prev_sp > 0 else "NARROW",
                                "prev_spread": prev_sp})
                reversal_signs[name] = cur_sign
    return alerts, threshold_fired, reversal_signs

# ── AI ────────────────────────────────────────────────────────────────────────
def call_ai(sector_data, factor_data, alerts, prev_spreads, history):
    if not ANTHROPIC_KEY:
        log.info("  No ANTHROPIC_API_KEY — skipping AI")
        return ""
    broad = sum(1 for d in sector_data if d["spread"] and d["spread"] > 0)
    narrow = sum(1 for d in sector_data if d["spread"] and d["spread"] < 0)
    sector_lines = []
    for d in sector_data:
        sp = d["spread"]
        prev = prev_spreads.get(d["name"])
        delta = f"{sp-prev:+.2f}%" if sp and prev else "n/a"
        ma5 = calc_ma(history.get(d["name"], []), 5)
        sector_lines.append(
            f"{d['name']}: {sp:+.2f}% (delta={delta}, 5d-MA={f'{ma5:+.2f}%' if ma5 is not None else 'n/a'})"
            if sp else f"{d['name']}: n/a"
        )
    factor_lines = [f"{d['label']}: {d['spread']:+.2f}%" if d["spread"] else f"{d['label']}: n/a"
                    for d in factor_data]
    alert_lines = [f"[{a['type']}] {a['sector']} {a['spread']:+.2f}% -> {a['direction']}"
                   for a in alerts]
    prompt = f"""You are a quantitative market analyst. Analyze sector breadth data for a systematic ES/NQ futures trader.

Period: {PERIOD} | Threshold: +/-{THRESHOLD}%
Regime: {broad} broad / {narrow} narrow of {len(sector_data)} sectors

Factor Dashboard:
{chr(10).join(factor_lines)}

Sector Spreads (EW - CW):
{chr(10).join(sector_lines)}

Alerts fired: {len(alerts)}
{chr(10).join(alert_lines) if alerts else 'None'}

Provide exactly these sections:

REGIME SUMMARY
2-3 sentences on overall breadth quality and concentration theme.

NOTABLE DIVERGENCES
Top 3 sectors. For each: what the spread and trend implies about internal dynamics.

FACTOR ALIGNMENT
How momentum/value, small/large, growth/value corroborates or contradicts the sector picture.

TRADING BIAS
Specific factor tilts and strategy implications. What regime are we in and what does it favor.

WATCH LIST
1-2 developing situations to monitor in coming days.

Be direct and quantitative. No filler."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json",
                     "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        r.raise_for_status()
        log.info("  AI analysis complete")
        return r.json()["content"][0]["text"]
    except Exception as e:
        log.warning(f"  AI call failed: {e}")
        return ""

# ── Charts ────────────────────────────────────────────────────────────────────
DARK_BG = "#0f172a"
CARD_BG = "#1e293b"

def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return b64

def make_bar_chart(sector_data):
    data = [(d["name"], d["spread"]) for d in sector_data if d["spread"] is not None]
    data.sort(key=lambda x: x[1])
    names, spreads = [x[0] for x in data], [x[1] for x in data]
    colors = ["#22c55e" if s > 0 else "#ef4444" for s in spreads]
    fig, ax = plt.subplots(figsize=(7.5, 5))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(DARK_BG)
    bars = ax.barh(names, spreads, color=colors, height=0.6)
    ax.axvline(0, color="#475569", linewidth=0.8, linestyle="--")
    for bar, val in zip(bars, spreads):
        ax.text(val + (0.08 if val >= 0 else -0.08),
                bar.get_y() + bar.get_height() / 2,
                f"{val:+.2f}%", va="center",
                ha="left" if val >= 0 else "right",
                fontsize=8.5, color="white", fontfamily="monospace")
    ax.set_xlabel("Spread  (EW - CW)", color="#94a3b8", fontsize=9)
    ax.set_title(f"Sector Spreads — {PERIOD}", color="#e2e8f0", fontsize=10, pad=8)
    ax.tick_params(colors="#94a3b8", labelsize=8.5)
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e293b")
    plt.tight_layout()
    return fig_to_b64(fig)

def make_rs_chart(sector_data, factor_data):
    mkt = next((d for d in factor_data if d["name"] == "Market Breadth"), None)
    extremes = sorted([d for d in sector_data if d["spread"] is not None],
                      key=lambda x: abs(x["spread"]), reverse=True)[:3]
    to_plot = []
    if mkt and mkt["rs"] and mkt["dates"]:
        to_plot.append({"label": "SPY/RSP", "rs": mkt["rs"],
                        "dates": mkt["dates"], "color": "#60a5fa", "lw": 2.0})
    for d in extremes:
        if d["rs"] and d["dates"]:
            to_plot.append({"label": d["name"], "rs": d["rs"], "dates": d["dates"],
                            "color": "#22c55e" if d["spread"] > 0 else "#ef4444",
                            "lw": 1.5})
    if not to_plot:
        return None
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(DARK_BG)
    for item in to_plot:
        pts = [(d, v) for d, v in zip(item["dates"], item["rs"]) if v is not None]
        if pts:
            dd, vv = zip(*pts)
            ax.plot(dd, vv, label=item["label"], color=item["color"],
                    linewidth=item["lw"])
    ax.axhline(0, color="#475569", linewidth=0.8, linestyle="--")
    ax.set_title("Relative Strength Trend  (EW vs CW, normalized to 0%)",
                 color="#e2e8f0", fontsize=10, pad=8)
    ax.set_ylabel("RS %", color="#94a3b8", fontsize=9)
    ax.tick_params(colors="#94a3b8", labelsize=8, rotation=35)
    ax.legend(fontsize=8, facecolor="#1e293b", edgecolor="#334155", labelcolor="#e2e8f0")
    for sp in ax.spines.values():
        sp.set_edgecolor("#1e293b")
    n = len(ax.get_xticklabels())
    for i, lbl in enumerate(ax.get_xticklabels()):
        if i % max(1, n // 6) != 0:
            lbl.set_visible(False)
    plt.tight_layout()
    return fig_to_b64(fig)

# ── HTML email ────────────────────────────────────────────────────────────────
CSS = """
body{font-family:'Courier New',monospace;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:14px;color:#f1f5f9;letter-spacing:.08em;margin:0 0 3px}
.sub{font-size:10px;color:#475569;margin:0 0 16px}
.card{background:#1e293b;border:1px solid #334155;border-radius:6px;padding:12px 14px;margin-bottom:12px}
.ct{font-size:9px;color:#475569;letter-spacing:.12em;text-transform:uppercase;margin:0 0 8px}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:#475569;font-size:9px;text-transform:uppercase;letter-spacing:.07em;
   padding:3px 6px;border-bottom:1px solid #334155;text-align:right}
th:first-child{text-align:left}
td{padding:3px 6px;border-bottom:1px solid #1e293b}
td:first-child{color:#94a3b8;text-align:left}
.ab{background:#1a0a0a;border-left:3px solid #ef4444;padding:8px 10px;
    margin-bottom:6px;border-radius:0 4px 4px 0}
.at{font-size:9px;color:#ef4444;letter-spacing:.1em}
.am{font-size:11px;color:#e2e8f0;margin:3px 0 0}
.pill{display:inline-block;padding:2px 9px;border-radius:10px;font-size:9px;font-weight:700}
.ai{font-size:11px;line-height:1.85;white-space:pre-wrap;color:#cbd5e1}
.ait{font-size:9px;color:#3b82f6;letter-spacing:.1em;margin-bottom:6px}
img{max-width:100%;border-radius:4px;display:block}
"""

def _td(v, bold=False):
    if v is None:
        return '<td style="color:#64748b;text-align:right">—</td>'
    c = "#22c55e" if v >= 0 else "#ef4444"
    w = "font-weight:700;" if bold else ""
    return f'<td style="color:{c};text-align:right;{w}">{"+" if v>=0 else ""}{v:.2f}%</td>'

def _badge(sp):
    if sp is None:
        return '<td style="color:#64748b;text-align:center">—</td>'
    if sp > 1.5:
        return '<td style="color:#22c55e;text-align:center">▲ Broad</td>'
    if sp < -1.5:
        return '<td style="color:#ef4444;text-align:center">▼ Narrow</td>'
    return '<td style="color:#f59e0b;text-align:center">◆ Neutral</td>'

def build_html(alerts, sector_data, factor_data, prev_spreads, history, ai_text, bar_b64, rs_b64):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    broad = sum(1 for d in sector_data if d["spread"] and d["spread"] > 0)
    narrow = sum(1 for d in sector_data if d["spread"] and d["spread"] < 0)
    rc = "#22c55e" if broad > narrow else "#ef4444" if narrow > broad else "#f59e0b"
    rl = "BROAD-LEANING" if broad > narrow else "NARROW-LEANING" if narrow > broad else "MIXED"
    mkt = next((d for d in factor_data if d["name"] == "Market Breadth"), None)
    ms = mkt["spread"] if mkt else None
    mc = "#22c55e" if ms and ms > 0 else "#ef4444" if ms and ms < 0 else "#64748b"

    # Alerts
    if alerts:
        ahtml = ""
        for a in alerts:
            h = ", ".join(TOP_HOLDINGS.get(a["sector"], [])[:3])
            prev = f'Prior {a["prev_spread"]:+.2f}% -> Now {a["spread"]:+.2f}%' \
                   if a.get("prev_spread") is not None else f'Spread {a["spread"]:+.2f}%'
            ahtml += f"""<div class="ab">
<div class="at">[{a['type']}] {a['direction']}</div>
<div class="am"><b>{a['sector']}</b> — {prev}</div>
<div style="font-size:10px;color:#64748b;margin-top:3px">{a['cw']}: {a['cw_ret']:+.2f}% | {a['ew']}: {a['ew_ret']:+.2f}% | Top: {h}</div>
</div>"""
    else:
        ahtml = '<div style="color:#475569;font-size:11px">No threshold or reversal alerts this run.</div>'

    # Factor rows
    frows = ""
    for d in factor_data:
        sp = d["spread"]
        ss = f'{"+" if sp and sp>=0 else ""}{sp:.2f}%' if sp else "—"
        sc = "#22c55e" if sp and sp > 0 else "#ef4444" if sp and sp < 0 else "#64748b"
        sig = d["pos"] if sp and sp > 0 else (d["neg"] if sp and sp < 0 else "—")
        frows += (f'<tr><td>{d["label"]}</td>'
                  f'<td style="color:{sc};text-align:right;font-weight:700">{ss}</td>'
                  f'<td style="color:#94a3b8;text-align:right;font-size:10px">{sig}</td></tr>')

    # Sector rows (sorted by spread desc)
    srows = ""
    for d in sorted(sector_data, key=lambda x: x["spread"] if x["spread"] else 0, reverse=True):
        sp = d["spread"]
        prev = prev_spreads.get(d["name"])
        delta = (sp - prev) if (sp is not None and prev is not None) else None
        h = history.get(d["name"], [])
        ma5 = calc_ma(h, 5)
        dc = "#22c55e" if delta and delta > 0 else "#ef4444" if delta and delta < 0 else "#64748b"
        ds = f'{"+" if delta and delta>=0 else ""}{delta:.2f}%' if delta is not None else "—"
        m5s = f'{"+" if ma5 and ma5>=0 else ""}{ma5:.2f}%' if ma5 is not None else "—"
        srows += (f'<tr><td>{d["name"]} <span style="color:#334155;font-size:9px">{d["cw"]}/{d["ew"]}</span></td>'
                  f'{_td(d["cw_ret"])}{_td(d["ew_ret"])}{_td(sp, bold=True)}'
                  f'<td style="color:{dc};text-align:right;font-size:10px">{ds}</td>'
                  f'<td style="color:#94a3b8;text-align:right;font-size:10px">{m5s}</td>'
                  f'{_badge(sp)}</tr>')

    bar_img = f'<img src="data:image/png;base64,{bar_b64}" alt="spread chart">' if bar_b64 else ""
    rs_img  = f'<img src="data:image/png;base64,{rs_b64}" alt="RS trend">' if rs_b64 else ""
    ai_html = f'<div class="ait">AI ANALYSIS</div><div class="ai">{ai_text}</div>' if ai_text else ""
    ms_str  = f'{"+" if ms and ms>=0 else ""}{ms:.2f}%' if ms else "—"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{CSS}</style></head><body><div class="wrap">
<h1>SECTOR BREADTH REPORT</h1>
<div class="sub">{ts} ET &nbsp;|&nbsp; Period: {PERIOD} &nbsp;|&nbsp; Threshold: +/-{THRESHOLD}%</div>

<div class="card">
  <div class="ct">Market Breadth Headline</div>
  <table><tr>
    <td style="font-size:13px;color:#e2e8f0">SPY vs RSP</td>
    <td style="font-size:18px;font-weight:700;color:{mc};text-align:right">{ms_str}</td>
    <td style="color:#94a3b8;text-align:right;font-size:10px">{"Eq-wt leading — broad" if ms and ms>0 else "Large caps leading — concentrated" if ms else "—"}</td>
  </tr></table>
  <div style="margin-top:8px">
    <span class="pill" style="background:{rc}22;color:{rc};border:1px solid {rc}44">
      {broad} Broad / {narrow} Narrow — {rl}
    </span>
  </div>
</div>

<div class="card"><div class="ct">Alerts</div>{ahtml}</div>

<div class="card">
  <div class="ct">Factor Dashboard</div>
  <table><thead><tr><th style="text-align:left">Pair</th><th>Spread</th><th>Signal</th></tr></thead>
  <tbody>{frows}</tbody></table>
</div>

<div class="card">
  <div class="ct">Sector Table</div>
  <table><thead><tr>
    <th style="text-align:left">Sector</th>
    <th>Cap Wt</th><th>Eq Wt</th><th>Spread</th><th>Delta</th><th>5d MA</th><th>Breadth</th>
  </tr></thead><tbody>{srows}</tbody></table>
</div>

{"<div class='card'><div class='ct'>Spread Snapshot</div>" + bar_img + "</div>" if bar_img else ""}
{"<div class='card'><div class='ct'>Relative Strength Trend</div>" + rs_img + "</div>" if rs_img else ""}
{"<div class='card'>" + ai_html + "</div>" if ai_html else ""}

</div></body></html>"""

# ── Delivery ──────────────────────────────────────────────────────────────────
def send_email(subject, html):
    if not all([SMTP_USER, SMTP_PASS, EMAIL_TO]):
        log.warning("Email not configured — skipping")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")

def send_sms(alerts):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO_N]):
        log.warning("Twilio not configured — skipping SMS")
        return
    try:
        from twilio.rest import Client
        body = f"[SectorAlert] {len(alerts)} signal(s):\n"
        body += "\n".join(f"- {a['sector']} {a['spread']:+.2f}% [{a['type']}]" for a in alerts[:5])
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body.strip(), from_=TWILIO_FROM, to=TWILIO_TO_N)
        log.info("SMS sent")
    except Exception as e:
        log.error(f"SMS failed: {e}")

def send_desktop(alerts):
    if not DESKTOP_ON:
        return
    try:
        from plyer import notification
        notification.notify(
            title=f"SectorAlert — {len(alerts)} signal(s)",
            message="\n".join(f"{a['sector']}: {a['spread']:+.2f}% [{a['type']}]"
                              for a in alerts[:3]),
            app_name="SectorAlert", timeout=12)
        log.info("Desktop notification sent")
    except Exception as e:
        log.error(f"Desktop notification failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Sector Alert v2 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Period: {PERIOD}  Threshold: +/-{THRESHOLD}%  Cooldown: {COOLDOWN_HRS}h")
    log.info("=" * 60)

    prev          = load_state()
    prev_spreads  = prev.get("spreads", {})
    thr_fired     = prev.get("threshold_fired", {})
    rev_signs     = prev.get("reversal_signs", {})
    history       = prev.get("spread_history", {})

    log.info("Fetching sector data...")
    sector_data = fetch_all(SECTORS)

    log.info("Fetching factor data...")
    factor_data = fetch_all(FACTOR_PAIRS)

    log.info("Detecting alerts...")
    alerts, thr_fired, rev_signs = detect_alerts(sector_data, prev_spreads, thr_fired, rev_signs)
    log.info(f"Alerts fired: {len(alerts)}")

    log.info("Generating charts...")
    bar_b64 = make_bar_chart(sector_data)
    rs_b64  = make_rs_chart(sector_data, factor_data)

    log.info("Calling AI analysis...")
    ai_text = call_ai(sector_data, factor_data, alerts, prev_spreads, history)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = (f"[SectorAlert] {len(alerts)} signal(s) — {ts}" if alerts
               else f"[SectorReport] Daily breadth — {ts}")

    html = build_html(alerts, sector_data, factor_data, prev_spreads, history,
                      ai_text, bar_b64, rs_b64)
    send_email(subject, html)

    if alerts:
        send_sms(alerts)
        send_desktop(alerts)

    save_state(sector_data, factor_data, thr_fired, rev_signs)
    log.info("Done.")

if __name__ == "__main__":
    main()
