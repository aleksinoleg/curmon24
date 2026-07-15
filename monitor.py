#!/usr/bin/env python3
"""
UAH/USD rate monitor — Phase 1.

Polls:
  * PrivatBank non-cash (Privat24) USD buy/sell rate
  * Interbank USD bid/ask (scraped from minfin)

Logs everything to data/rates.csv, keeps state in data/state.json,
and sends Telegram notifications when:
  * Privat changes its rate
  * interbank moves >= ALERT_THRESHOLD (UAH) since the last alert baseline

Run:  python monitor.py            # normal run (needs TELEGRAM_* env vars)
      python monitor.py --debug    # fetch and print, no Telegram, no state write
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- config

KYIV = ZoneInfo("Europe/Kyiv")
DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "rates.csv"
DATA_DIR.mkdir(exist_ok=True)

ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "0.15"))  # UAH per USD

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}

DEBUG = "--debug" in sys.argv

# --------------------------------------------------------------------------- fetchers


def fetch_privat():
    """Return (buy, sell, source) for USD at PrivatBank.

    Tries the live pubinfo endpoint first (coursid=11 — non-cash / Privat24,
    coursid=5 — cash as fallback), then falls back to the daily archive API.
    """
    for coursid, kind in ((11, "cashless"), (5, "cash")):
        try:
            r = requests.get(
                "https://api.privatbank.ua/p24api/pubinfo",
                params={"json": "", "exchange": "", "coursid": coursid},
                headers=HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            for row in r.json():
                if row.get("ccy") == "USD":
                    return float(row["buy"]), float(row["sale"]), f"pubinfo:{kind}"
        except Exception as e:  # noqa: BLE001 — any failure -> next source
            if DEBUG:
                print(f"[privat] pubinfo coursid={coursid} failed: {e}")

    # Fallback: daily archive (verified working; daily granularity only)
    today = datetime.now(KYIV).strftime("%d.%m.%Y")
    r = requests.get(
        "https://api.privatbank.ua/p24api/exchange_rates",
        params={"json": "", "date": today},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    for row in r.json().get("exchangeRate", []):
        if row.get("currency") == "USD" and "purchaseRate" in row:
            return float(row["purchaseRate"]), float(row["saleRate"]), "archive"

    raise RuntimeError("all PrivatBank sources failed")


NUM_RE = re.compile(r"\d{2}[.,]\d{2,4}")


def _nums(text):
    return [float(m.replace(",", ".")) for m in NUM_RE.findall(text)]


def fetch_interbank():
    """Return (bid, ask, source) for USD on the interbank market.

    Scrapes minfin. Page layout may change — if parsing breaks, run with
    --debug and adjust the row-matching logic below.
    """
    r = requests.get(
        "https://index.minfin.com.ua/ua/exchange/mb/",
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Strategy 1: a table row mentioning USD/Долар with two plausible numbers
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if re.search(r"(?i)\b(usd|долар)\b", text):
            nums = [n for n in _nums(text) if 20.0 < n < 100.0]
            if len(nums) >= 2:
                bid, ask = sorted(nums[:2])
                return bid, ask, "minfin:table"

    # Strategy 2: regex over the whole page near a USD marker
    m = re.search(
        r"(?is)(?:\busd\b|долар).{0,400}?(\d{2}[.,]\d{2,4})\s*/?\s*(\d{2}[.,]\d{2,4})",
        soup.get_text(" ", strip=True),
    )
    if m:
        a, b = (float(x.replace(",", ".")) for x in m.groups())
        if 20.0 < a < 100.0 and 20.0 < b < 100.0:
            return min(a, b), max(a, b), "minfin:regex"

    raise RuntimeError("could not parse interbank rate from minfin")


def interbank_session_open(now):
    """Interbank trades Mon–Thu ~9:00–17:00, Fri to 16:00 Kyiv time."""
    if now.weekday() >= 5:
        return False
    close_h = 16 if now.weekday() == 4 else 17
    return 9 <= now.hour < close_h + 1  # +1h grace to catch closing quotes


# --------------------------------------------------------------------------- telegram


def send_telegram(text):
    if DEBUG:
        print("--- would send ---\n" + text + "\n------------------")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()


# --------------------------------------------------------------------------- state & log


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def append_log(ts, source, buy, sell):
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "source", "buy_bid", "sell_ask"])
        w.writerow([ts, source, f"{buy:.4f}", f"{sell:.4f}"])


# --------------------------------------------------------------------------- main


def main():
    now = datetime.now(KYIV)
    ts = now.strftime("%Y-%m-%d %H:%M")
    state = load_state()
    messages = []

    # ---- PrivatBank ----
    pb_buy = pb_sell = None
    try:
        pb_buy, pb_sell, pb_src = fetch_privat()
        append_log(ts, f"privat:{pb_src}", pb_buy, pb_sell)
        prev_buy, prev_sell = state.get("pb_buy"), state.get("pb_sell")
        if prev_buy is not None and (pb_buy != prev_buy or pb_sell != prev_sell):
            arrow = "⬆️" if pb_buy > prev_buy else "⬇️"
            messages.append(
                f"🏦 <b>Приват24 змінив курс</b> {arrow}\n"
                f"Купівля: {prev_buy:.2f} → <b>{pb_buy:.2f}</b>\n"
                f"Продаж: {prev_sell:.2f} → {pb_sell:.2f}"
            )
        state["pb_buy"], state["pb_sell"] = pb_buy, pb_sell
    except Exception as e:  # noqa: BLE001
        print(f"[warn] privat fetch failed: {e}")

    # ---- Interbank ----
    if interbank_session_open(now):
        try:
            ib_bid, ib_ask, ib_src = fetch_interbank()
            ib_mid = round((ib_bid + ib_ask) / 2, 4)
            append_log(ts, f"interbank:{ib_src}", ib_bid, ib_ask)

            today = now.date().isoformat()
            if state.get("ib_session_date") != today:
                state["ib_session_date"] = today
                state["ib_open"] = ib_mid
                state["ib_alert_base"] = ib_mid

            delta = ib_mid - state.get("ib_alert_base", ib_mid)
            if abs(delta) >= ALERT_THRESHOLD:
                since_open = ib_mid - state.get("ib_open", ib_mid)
                if delta > 0:
                    trend, hint = (
                        "росте 📈",
                        "Приват, ймовірно, підніме курс купівлі — можна почекати з продажем.",
                    )
                else:
                    trend, hint = (
                        "падає 📉",
                        "Приват може знизити курс купівлі — якщо плануєш продавати, краще не зволікати.",
                    )
                pb_line = (
                    f"\nПриват24 купівля зараз: <b>{pb_buy:.2f}</b>"
                    if pb_buy is not None
                    else ""
                )
                messages.append(
                    f"📊 <b>Міжбанк {trend}</b>\n"
                    f"Зараз: {ib_bid:.2f} / {ib_ask:.2f}"
                    f" ({delta:+.2f} від останнього сигналу, {since_open:+.2f} від відкриття)"
                    f"{pb_line}\n\n💡 {hint}"
                )
                state["ib_alert_base"] = ib_mid

            state["ib_bid"], state["ib_ask"] = ib_bid, ib_ask
        except Exception as e:  # noqa: BLE001
            print(f"[warn] interbank fetch failed: {e}")

    # ---- notify & persist ----
    if messages:
        send_telegram("\n\n".join(messages))

    if not DEBUG:
        save_state(state)
    else:
        print("state:", json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
