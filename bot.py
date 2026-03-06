"""
BTC Signal Bot
- Preisdaten: Binance Public API (kostenlos, kein Account nötig)
- RSI-Signale via Telegram
- Wöchentlicher Performance-Report (jeden Sonntag 09:00)
- Logging via Google Sheets
- Google Credentials aus Railway-Umgebungsvariable (kein credentials.json nötig)
"""

import os
import json
import time
import logging
import datetime
import threading
import tempfile
import requests
import schedule
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import telebot

# ─────────────────────────────────────────────
# KONFIGURATION – aus Railway-Variablen
# ─────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "8623042691:AAGHfryZ13YeqvMOfbCy-mxMtJ6SEK2ZLt4")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_ECHTE_CHAT_ID_HIER")
SHEET_NAME       = os.environ.get("GOOGLE_SHEET_NAME","BTC Trade Log")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "")  # kompletter JSON-String

SYMBOL         = "BTCUSDT"
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
KLINE_INTERVAL = "1h"
CHECK_INTERVAL = 60

BINANCE_BASE_URL = "https://api.binance.com"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("btc_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

tbot = telebot.TeleBot(TELEGRAM_TOKEN)

def send_telegram(message: str) -> None:
    try:
        tbot.send_message(TELEGRAM_CHAT_ID, message, parse_mode="HTML")
        logger.info(f"Telegram ✓ | {message[:80].strip()}")
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")

# ─────────────────────────────────────────────
# GOOGLE SHEETS – Credentials aus Env-Variable
# ─────────────────────────────────────────────

def get_sheet():
    """
    Liest Google-Credentials aus der Umgebungsvariable GOOGLE_CREDENTIALS.
    Schreibt sie temporär als Datei, damit oauth2client sie lesen kann.
    """
    if not GOOGLE_CREDS_JSON:
        raise ValueError("GOOGLE_CREDENTIALS Umgebungsvariable fehlt in Railway!")

    creds_dict = json.loads(GOOGLE_CREDS_JSON)

    # Temporäre Datei anlegen (Railway hat kein persistentes Dateisystem)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(creds_dict, tmp)
        tmp_path = tmp.name

    scope  = ["https://spreadsheets.google.com/feeds",
               "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(tmp_path, scope)
    client = gspread.authorize(creds)
    os.unlink(tmp_path)  # Temp-Datei wieder löschen
    return client.open(SHEET_NAME).sheet1

def ensure_header() -> None:
    try:
        sheet  = get_sheet()
        header = ["Timestamp", "Signal", "Symbol", "Preis (USD)", "RSI", "Hinweis"]
        if sheet.row_values(1) != header:
            sheet.insert_row(header, 1)
    except Exception as e:
        logger.error(f"Header-Fehler: {e}")

def log_signal(signal: str, price: float, rsi: float, note: str = "") -> None:
    try:
        sheet = get_sheet()
        ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([ts, signal, SYMBOL, price, rsi, note])
        logger.info(f"Sheet ✓ {signal} | ${price:,.2f} | RSI {rsi}")
    except Exception as e:
        logger.error(f"Sheet-Fehler: {e}")

def get_all_signals() -> list:
    try:
        return get_sheet().get_all_records()
    except Exception as e:
        logger.error(f"Sheet-Lesefehler: {e}")
        return []

# ─────────────────────────────────────────────
# BINANCE PUBLIC API
# ─────────────────────────────────────────────

def get_klines(symbol: str, interval: str, limit: int = 100) -> list:
    url  = f"{BINANCE_BASE_URL}/api/v3/klines"
    resp = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_current_price(symbol: str) -> float:
    url  = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    resp = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])

# ─────────────────────────────────────────────
# RSI
# ─────────────────────────────────────────────

def calculate_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        raise ValueError("Zu wenige Datenpunkte für RSI.")
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ─────────────────────────────────────────────
# WÖCHENTLICHER PERFORMANCE-REPORT
# ─────────────────────────────────────────────

def self_optimize() -> None:
    logger.info("Wöchentlicher Report wird erstellt...")
    try:
        records = get_all_signals()
        if not records:
            send_telegram("📊 <b>Wöchentlicher Report</b>\nNoch keine Signale vorhanden.")
            return

        since = datetime.datetime.now() - datetime.timedelta(days=7)
        week  = [r for r in records
                 if r.get("Signal") in ("BUY SIGNAL", "SELL SIGNAL")
                 and datetime.datetime.strptime(r["Timestamp"], "%Y-%m-%d %H:%M:%S") >= since]

        buy_count  = sum(1 for r in week if r["Signal"] == "BUY SIGNAL")
        sell_count = sum(1 for r in week if r["Signal"] == "SELL SIGNAL")
        total      = len(week)
        rsi_values = [float(r["RSI"]) for r in week if r.get("RSI")]
        avg_rsi    = round(sum(rsi_values) / len(rsi_values), 1) if rsi_values else 0
        prices     = [float(r["Preis (USD)"]) for r in week if r.get("Preis (USD)")]
        avg_price  = round(sum(prices) / len(prices), 2) if prices else 0
        kw         = datetime.datetime.now().strftime("KW %W")

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>WÖCHENTLICHER REPORT</b>\n"
            f"Zeitraum: {kw} | Signale: {total}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🟢 Kaufsignale:    <b>{buy_count}</b>\n"
            f"🔴 Verkaufsignale: <b>{sell_count}</b>\n"
            f"📉 Ø RSI:          <b>{avg_rsi}</b>\n"
            f"💵 Ø BTC-Preis:    <b>${avg_price:,.2f}</b>\n\n"
            f"🤖 <b>KI-HINWEIS:</b>\n"
        )
        if buy_count > sell_count * 2:
            msg += "⚠️ Viele Kaufsignale – Markt könnte überverkauft sein.\n"
        elif sell_count > buy_count * 2:
            msg += "⚠️ Viele Verkaufsignale – Abwärtsdruck, vorsichtig sein!\n"
        else:
            msg += "✅ Ausgeglichene Signal-Verteilung diese Woche.\n"
        if avg_rsi < 35:
            msg += "📐 RSI niedrig → historisch guter Einstiegsbereich.\n"
        elif avg_rsi > 65:
            msg += "📐 RSI hoch → Markt überhitzt, Risiko beachten.\n"

        send_telegram(msg)
    except Exception as e:
        logger.error(f"Report-Fehler: {e}")
        send_telegram(f"⚠️ Report-Fehler: {e}")

# ─────────────────────────────────────────────
# SCHEDULE THREAD
# ─────────────────────────────────────────────

def run_schedule() -> None:
    schedule.every().sunday.at("09:00").do(self_optimize)
    logger.info("Schedule aktiv – Report jeden Sonntag um 09:00 Uhr.")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────
# SIGNAL BOT
# ─────────────────────────────────────────────

class BTCSignalBot:
    def __init__(self):
        self.last_signal = None
        logger.info("BTC Signal Bot gestartet.")
        send_telegram(
            "🤖 <b>BTC Signal Bot gestartet</b>\n"
            f"Symbol:      <b>{SYMBOL}</b>\n"
            f"Intervall:   {KLINE_INTERVAL}\n"
            f"RSI Kauf:    &lt; {RSI_OVERSOLD}\n"
            f"RSI Verkauf: &gt; {RSI_OVERBOUGHT}\n"
            f"Prüfung alle {CHECK_INTERVAL}s\n"
            f"📊 Report: Sonntags 09:00 Uhr"
        )
        ensure_header()
        threading.Thread(target=run_schedule, daemon=True).start()

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Bot gestoppt.")
                send_telegram("⛔ <b>Bot manuell gestoppt.</b>")
                break
            except Exception as e:
                logger.error(f"Fehler: {e}")
                send_telegram(f"⚠️ <b>Fehler:</b> {e}")
            time.sleep(CHECK_INTERVAL)

    def _tick(self) -> None:
        klines = get_klines(SYMBOL, KLINE_INTERVAL, limit=RSI_PERIOD + 5)
        closes = [float(k[4]) for k in klines]
        rsi    = calculate_rsi(closes, RSI_PERIOD)
        price  = get_current_price(SYMBOL)
        logger.info(f"BTC ${price:,.2f} | RSI {rsi} | Letztes Signal: {self.last_signal}")

        if rsi < RSI_OVERSOLD and self.last_signal != "BUY":
            self.last_signal = "BUY"
            send_telegram(
                f"🟢 <b>KAUFSIGNAL – BTC überverkauft!</b>\n\n"
                f"💵 Preis:  <b>${price:,.2f}</b>\n"
                f"📉 RSI:    <b>{rsi}</b> (unter {RSI_OVERSOLD})\n"
                f"⏰ Zeit:   {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                f"⚡ <i>Möglicher Einstiegspunkt!</i>"
            )
            log_signal("BUY SIGNAL", price, rsi, f"RSI unter {RSI_OVERSOLD}")

        elif rsi > RSI_OVERBOUGHT and self.last_signal != "SELL":
            self.last_signal = "SELL"
            send_telegram(
                f"🔴 <b>VERKAUFSIGNAL – BTC überkauft!</b>\n\n"
                f"💵 Preis:  <b>${price:,.2f}</b>\n"
                f"📈 RSI:    <b>{rsi}</b> (über {RSI_OVERBOUGHT})\n"
                f"⏰ Zeit:   {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                f"⚡ <i>Möglicher Ausstiegspunkt!</i>"
            )
            log_signal("SELL SIGNAL", price, rsi, f"RSI über {RSI_OVERBOUGHT}")

        elif RSI_OVERSOLD <= rsi <= RSI_OVERBOUGHT:
            if self.last_signal is not None:
                logger.info("RSI neutral – Signal zurückgesetzt.")
            self.last_signal = None

# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────

if __name__ == "__main__":
    BTCSignalBot().run()
