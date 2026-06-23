#!/usr/bin/env python3
"""
Sector Cap vs Equal-Weight Spread Alerter
==========================================
Monitors SPDR (cap-wt) vs Invesco RSP (equal-wt) sector spreads.

Alert triggers:
  THRESHOLD  — |spread| >= ALERT_THRESHOLD (default 2.0%)
  REVERSAL   — spread sign flips vs prior run, provided prior |spread| >= REVERSAL_MIN

Delivery: email (SMTP), SMS (Twilio), desktop notification (plyer/Windows)

State file tracks last-seen spreads, last-fired timestamps, and last-seen signs
to prevent duplicate alerts within the cooldown window.
"""

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

import requests

from pathlib import Path
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

# ── Sector definitions ────────────────────────────────────────────────────────
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

# ── Configuration (env-driven, all overridable) ───────────────────────────────
PERIOD         = os.getenv("ALERT_PERIOD",    "1mo")    # Yahoo Finance range
INTERVAL       = os.getenv("ALERT_INTERVAL",  "1d")     # Yahoo Finance interval
THRESHOLD      = float(os.getenv("ALERT_THRESHOLD", "2.0"))   # % spread to trigger
REVERSAL_MIN   = float(os.getenv("REVERSAL_MIN",    "1.0"))   # min prior |spread| to count reversal
COOLDOWN_HRS   = float(os.getenv("COOLDOWN_HRS",    "20.0"))  # hours before re-firing same threshold alert
STATE_FILE     = Path(os.getenv("STATE_FILE", "sector_state.json"))

# Email
SMTP_HOST      = os.getenv("SMTP_HOST",    "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER",    "")
SMTP_PASS      = os.getenv("SMTP_PASS",    "")
EMAIL_TO       = os.getenv("ALERT_EMAIL_TO", "")

# Twilio
TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID",  "")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN",   "")
TWILIO_FROM    = os.getenv("TWILIO_FROM_NUMBER",  "")
TWILIO_TO      = os.getenv("TWILIO_TO_NUMBER",    "")

# Desktop (disable on headless servers)
DESKTOP_ON     = os.getenv("DESKTOP_ALERTS", "true").lower() == "true"

YF_HEADERS     = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FETCH_DELAY    = 0.35   # seconds between Yahoo Finance calls

# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_return(symbol: str) -> float | None:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={INTERVAL}&range={PERIOD}"
    )
    try:
        r = requests.get(url, timeout=12, headers=YF_HEADERS)
        r.raise_for_status()
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        valid = [c for c in closes if c is not None]
        if len(valid) < 2:
            log.warning(f"    Insufficient closes for {symbol}")
            return None
        return (valid[-1] / valid[0] - 1) * 100
    except Exception as e:
        log.warning(f"    Fetch failed {symbol}: {e}")
        return None


def fetch_all_spreads() -> dict:
    results = {}
    for s in SECTORS:
        log.info(f"  {s['name']:<20} {s['cw']} / {s['ew']}")
        cw_ret = fetch_return(s["cw"])
        time.sleep(FETCH_DELAY)
        ew_ret = fetch_return(s["ew"])
        time.sleep(FETCH_DELAY)
        spread = (ew_ret - cw_ret) if (cw_ret is not None and ew_ret is not None) else None
        results[s["name"]] = {
            "cw": cw_ret, "ew": ew_ret, "spread": spread,
            "cw_ticker": s["cw"], "ew_ticker": s["ew"],
        }
    return results

# ── State management ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not load state file: {e}")
    return {}


def save_state(spreads: dict, threshold_fired: dict, reversal_signs: dict):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": PERIOD,
        "spreads": {k: v["spread"] for k, v in spreads.items()},
        "threshold_fired": threshold_fired,
        "reversal_signs": reversal_signs,
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2))
    log.info(f"State saved → {STATE_FILE}")

# ── Alert detection ───────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def detect_alerts(
    current: dict,
    prev_spreads: dict,
    threshold_fired: dict,
    reversal_signs: dict,
) -> tuple[list[dict], dict, dict]:
    """
    Returns (alerts, updated_threshold_fired, updated_reversal_signs).
    Modifies threshold_fired and reversal_signs in-place as alerts fire.
    """
    alerts = []
    now = _now_utc()

    for name, data in current.items():
        sp = data["spread"]
        if sp is None:
            continue

        prev_sp = prev_spreads.get(name)

        # ── Threshold alert ────────────────────────────────────────────────
        if abs(sp) >= THRESHOLD:
            last_fired_str = threshold_fired.get(name)
            cooldown_ok = True
            if last_fired_str:
                elapsed = now - _parse_ts(last_fired_str)
                cooldown_ok = elapsed > timedelta(hours=COOLDOWN_HRS)

            if cooldown_ok:
                direction = "BROAD" if sp > 0 else "NARROW"
                alerts.append({
                    "type": "THRESHOLD",
                    "sector": name,
                    "spread": sp,
                    "prev_spread": prev_sp,
                    "direction": direction,
                    "cw_ticker": data["cw_ticker"],
                    "ew_ticker": data["ew_ticker"],
                    "cw_ret": data["cw"],
                    "ew_ret": data["ew"],
                    "detail": (
                        f"{name} spread {sp:+.2f}% breached ±{THRESHOLD}% threshold — "
                        f"{direction} breadth  [{data['ew_ticker']} vs {data['cw_ticker']}]"
                    ),
                })
                threshold_fired[name] = now.isoformat()

        # ── Reversal alert ─────────────────────────────────────────────────
        if prev_sp is not None and abs(prev_sp) >= REVERSAL_MIN:
            prev_sign = reversal_signs.get(name)
            cur_sign = 1 if sp >= 0 else -1
            old_sign = 1 if prev_sp >= 0 else -1

            sign_changed = old_sign != cur_sign
            not_already_fired_this_sign = prev_sign != cur_sign

            if sign_changed and not_already_fired_this_sign:
                from_label = "BROAD" if prev_sp > 0 else "NARROW"
                to_label   = "BROAD" if sp > 0 else "NARROW"
                alerts.append({
                    "type": "REVERSAL",
                    "sector": name,
                    "spread": sp,
                    "prev_spread": prev_sp,
                    "direction": to_label,
                    "cw_ticker": data["cw_ticker"],
                    "ew_ticker": data["ew_ticker"],
                    "cw_ret": data["cw"],
                    "ew_ret": data["ew"],
                    "detail": (
                        f"{name} breadth reversed {from_label} → {to_label}: "
                        f"{prev_sp:+.2f}% → {sp:+.2f}%  [{data['ew_ticker']} vs {data['cw_ticker']}]"
                    ),
                })
                reversal_signs[name] = cur_sign

    return alerts, threshold_fired, reversal_signs

# ── Formatting helpers ────────────────────────────────────────────────────────
def _breadth_label(sp: float | None) -> str:
    if sp is None:
        return "—"
    if sp > 1.5:
        return "▲ Broad"
    if sp < -1.5:
        return "▼ Narrow"
    return "◆ Neutral"


def build_snapshot_table(all_spreads: dict) -> str:
    lines = [f"{'Sector':<20} {'CW':>8} {'EW':>8} {'Spread':>8}  Breadth"]
    lines.append("─" * 58)
    for name, d in all_spreads.items():
        cw_s = f"{d['cw']:+.2f}%" if d["cw"] is not None else "n/a"
        ew_s = f"{d['ew']:+.2f}%" if d["ew"] is not None else "n/a"
        sp_s = f"{d['spread']:+.2f}%" if d["spread"] is not None else "n/a"
        lines.append(
            f"{name:<20} {cw_s:>8} {ew_s:>8} {sp_s:>8}  {_breadth_label(d['spread'])}"
        )
    return "\n".join(lines)

# ── Delivery ──────────────────────────────────────────────────────────────────
def send_email(alerts: list[dict], all_spreads: dict):
    if not all([SMTP_USER, SMTP_PASS, EMAIL_TO]):
        log.warning("Email credentials not set — skipping")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"[SectorAlert] {len(alerts)} signal(s) — {ts}"

    lines = [
        f"Sector Cap vs Equal-Weight Alert",
        f"Period: {PERIOD}  |  Threshold: ±{THRESHOLD}%  |  Run: {ts}",
        "",
        "── ALERTS " + "─" * 48,
    ]
    for i, a in enumerate(alerts, 1):
        lines.append(f"{i}. [{a['type']}] {a['detail']}")
        if a["prev_spread"] is not None:
            lines.append(f"   Prior spread: {a['prev_spread']:+.2f}%  →  Now: {a['spread']:+.2f}%")
        lines.append(f"   {a['cw_ticker']}: {a['cw_ret']:+.2f}%   {a['ew_ticker']}: {a['ew_ret']:+.2f}%")
        lines.append("")

    lines += [
        "── FULL SNAPSHOT " + "─" * 41,
        build_snapshot_table(all_spreads),
        "",
        f"State file: {STATE_FILE}",
    ]

    body = "\n".join(lines)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("✓ Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


def send_sms(alerts: list[dict]):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.warning("Twilio credentials not set — skipping SMS")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        body = f"[SectorAlert] {len(alerts)} signal(s):\n"
        for a in alerts[:5]:
            body += f"• {a['sector']} {a['spread']:+.2f}% [{a['type']}]\n"
        if len(alerts) > 5:
            body += f"(+{len(alerts)-5} more — check email)"
        client.messages.create(body=body.strip(), from_=TWILIO_FROM, to=TWILIO_TO)
        log.info("✓ SMS sent")
    except Exception as e:
        log.error(f"SMS failed: {e}")


def send_desktop(alerts: list[dict]):
    if not DESKTOP_ON:
        return
    try:
        from plyer import notification
        title = f"SectorAlert — {len(alerts)} signal(s)"
        lines = [f"{a['sector']}: {a['spread']:+.2f}% [{a['type']}]" for a in alerts[:3]]
        if len(alerts) > 3:
            lines.append(f"(+{len(alerts)-3} more)")
        notification.notify(
            title=title,
            message="\n".join(lines),
            app_name="SectorAlert",
            timeout=12,
        )
        log.info("✓ Desktop notification sent")
    except Exception as e:
        log.error(f"Desktop notification failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"{'='*60}")
    log.info(f"Sector Alert Run — {ts}")
    log.info(f"Period: {PERIOD}  Threshold: ±{THRESHOLD}%  Reversal min: {REVERSAL_MIN}%  Cooldown: {COOLDOWN_HRS}h")
    log.info(f"{'='*60}")

    prev = load_state()
    prev_spreads      = prev.get("spreads", {})
    threshold_fired   = prev.get("threshold_fired", {})
    reversal_signs    = prev.get("reversal_signs", {})

    log.info("Fetching sector data…")
    current = fetch_all_spreads()

    log.info("Detecting alerts…")
    alerts, threshold_fired, reversal_signs = detect_alerts(
        current, prev_spreads, threshold_fired, reversal_signs
    )

    log.info(f"{'─'*40}")
    log.info(f"Alerts fired: {len(alerts)}")
    for a in alerts:
        log.info(f"  [{a['type']}] {a['detail']}")

    log.info("Snapshot:")
    for line in build_snapshot_table(current).splitlines():
        log.info(f"  {line}")

    if alerts:
        send_email(alerts, current)
        send_sms(alerts)
        send_desktop(alerts)
    else:
        log.info("No alerts — no notifications sent")

    save_state(current, threshold_fired, reversal_signs)
    log.info("Done.")


if __name__ == "__main__":
    main()
