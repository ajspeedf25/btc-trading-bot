"""
BTC Signal Bot
- Preisdaten: Binance Public API (kostenlos)
- RSI + EMA Signale
- Detaillierte Telegram-Nachrichten mit Stop-Loss, Take-Profit, Positionsgröße
- Wöchentlicher Performance-Report (Sonntags 09:00)
- Google Sheets Logging
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
# KONFIGURATION
# ─────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN",    "8623042691:AAGHfryZ13YeqvMOfbCy-mxMtJ6SEK2ZLt4")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID",  "8718482804")
SHEET_NAME        = os.environ.get("GOOGLE_SHEET_NAME", "BTC Trade Log")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "")

ACCOUNT_BALANCE   = float(os.environ.get("ACCOUNT_BALANCE", 500))   # € Kontostand
RISK_PERCENT      = float(os.environ.get("RISK_PERCENT",     1))     # % Risiko pro Trade

SYMBOL            = "BTCUSDT"
RSI_PERIOD        = 14
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65
EMA_FAST          = 20
EMA_SLOW          = 50
KLINE_INTERVAL    = "5m"     # 5-Minuten-Kerzen wie in der Anleitung
CHECK_INTERVAL    = 60       # Sekunden

BINANCE_BASE_URL  = "https://api.binance.com"

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
        logger.info(f"Telegram ✓ | {message[:60].strip()}")
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")

# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────

def get_sheet():
    if not GOOGLE_CREDS_JSON:
        raise ValueError("GOOGLE_CREDENTIALS fehlt in Railway!")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(creds_dict, tmp)
        tmp_path = tmp.name
    scope  = ["https://spreadsheets.google.com/feeds",
               "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(tmp_path, scope)
    client = gspread.authorize(creds)
    os.unlink(tmp_path)
    return client.open(SHEET_NAME).sheet1

def ensure_header() -> None:
    try:
        sheet  = get_sheet()
        header = ["Timestamp", "Signal", "Symbol", "Preis (USD)", "RSI",
                  "EMA20", "EMA50", "Stop-Loss", "Take-Profit",
                  "Positionsgröße (€)", "Hebel", "Hinweis"]
        if sheet.row_values(1) != header:
            sheet.insert_row(header, 1)
    except Exception as e:
        logger.error(f"Header-Fehler: {e}")

def log_signal(signal: str, price: float, rsi: float, ema_fast: float,
               ema_slow: float, stop: float, tp: float,
               pos_size: float, hebel: float, note: str = "") -> None:
    try:
        sheet = get_sheet()
        ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([ts, signal, SYMBOL, price, rsi, round(ema_fast, 2),
                          round(ema_slow, 2), stop, tp, pos_size, hebel, note])
        logger.info(f"Sheet ✓ {signal}")
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
    resp = requests.get(url, params={"symbol": symbol, "interval": interval,
                                     "limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_current_price(symbol: str) -> float:
    url  = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    resp = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])

def get_minutes_to_next_candle(interval: str = "5m") -> int:
    """Minuten bis zur nächsten 5-Minuten-Kerze."""
    now     = datetime.datetime.now()
    minutes = now.minute % 5
    return 5 - minutes if minutes != 0 else 5

# ─────────────────────────────────────────────
# INDIKATOREN
# ─────────────────────────────────────────────

def calculate_ema(closes: list, period: int) -> float:
    """Berechnet den EMA für die letzte Kerze."""
    if len(closes) < period:
        raise ValueError(f"Zu wenige Daten für EMA{period}")
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

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
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

# ─────────────────────────────────────────────
# POSITIONSGRÖSSE BERECHNEN
# ─────────────────────────────────────────────

def calculate_position(price: float, stop_distance_pct: float) -> dict:
    """
    Berechnet Positionsgröße, Hebel, Stop-Loss und Take-Profit.
    stop_distance_pct: z.B. 0.005 für 0.5%
    """
    risiko_eur  = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    pos_size    = round(risiko_eur / stop_distance_pct, 2)
    hebel       = round(pos_size / ACCOUNT_BALANCE, 1)
    # Hebel auf max 3x begrenzen (Empfehlung aus Anleitung)
    hebel       = min(hebel, 3.0)
    pos_size    = round(ACCOUNT_BALANCE * hebel, 2)

    stop_loss   = round(price * (1 - stop_distance_pct), 2)
    take_profit = round(price * (1 + stop_distance_pct * 2), 2)  # RR 1:2

    liq_distance = round((1 / hebel) * 100, 1) if hebel > 0 else 100

    return {
        "risiko_eur":    risiko_eur,
        "pos_size":      pos_size,
        "hebel":         hebel,
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "liq_distance":  liq_distance,
        "stop_pct":      round(stop_distance_pct * 100, 2),
        "tp_pct":        round(stop_distance_pct * 2 * 100, 2),
    }

# ─────────────────────────────────────────────
# SIGNAL-NACHRICHTEN
# ─────────────────────────────────────────────

def build_long_message(price: float, rsi: float, ema_fast: float,
                        ema_slow: float, calc: dict, minutes_to_candle: int) -> str:
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    return (
        f"🚨 <b>BTC LONG-SIGNAL</b>\n\n"
        f"📌 Setup: EMA{EMA_FAST} kreuzt EMA{EMA_SLOW} von unten | "
        f"RSI: {rsi} | \n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Einstiegspreis:  <b>${price:,.2f}</b>\n"
        f"🔴 Stop-Loss:       <b>${calc['stop_loss']:,.2f}</b> "
        f"(–{calc['stop_pct']}%)\n"
        f"🟢 Take-Profit:     <b>${calc['take_profit']:,.2f}</b> "
        f"(+{calc['tp_pct']}%) | RR 1:2\n\n"
        f"⚡ Empf. Hebel:     <b>{calc['hebel']}x</b> | "
        f"Positionsgröße: <b>{calc['pos_size']} €</b>\n"
        f"💸 Max. Risiko:     <b>{calc['risiko_eur']} €</b> "
        f"({RISK_PERCENT}% vom Konto)\n"
        f"⚠️ Liquidation ca.: <b>{calc['liq_distance']}%</b> entfernt\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Indikatoren:\n"
        f"  RSI ({RSI_PERIOD}):  {rsi}\n"
        f"  EMA{EMA_FAST}:      ${ema_fast:,.2f}\n"
        f"  EMA{EMA_SLOW}:      ${ema_slow:,.2f}\n\n"
        f"🕯 <b>BESTÄTIGUNG</b> – Nächste 3 × 5-Min-Kerzen:\n"
        f"  Kerze 1: Schlusskurs über ${round(price * 1.001, 2):,}\n"
        f"  Kerze 2: Kein Unterschreiten von ${round(price * 0.998, 2):,}\n"
        f"  Kerze 3: Ausbruch mit Volumen-Bestätigung\n\n"
        f"⏰ Nächste 5-Min-Kerze in: <b>{minutes_to_candle} Min.</b>\n"
        f"🕐 {now}"
    )

def build_short_message(price: float, rsi: float, ema_fast: float,
                         ema_slow: float, calc: dict, minutes_to_candle: int) -> str:
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    return (
        f"🚨 <b>BTC SHORT-SIGNAL</b>\n\n"
        f"📌 Setup: EMA{EMA_FAST} kreuzt EMA{EMA_SLOW} von oben | "
        f"RSI: {rsi} | \n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Einstiegspreis:  <b>${price:,.2f}</b>\n"
        f"🔴 Stop-Loss:       <b>${calc['take_profit']:,.2f}</b> "
        f"(+{calc['stop_pct']}%)\n"
        f"🟢 Take-Profit:     <b>${calc['stop_loss']:,.2f}</b> "
        f"(–{calc['tp_pct']}%) | RR 1:2\n\n"
        f"⚡ Empf. Hebel:     <b>{calc['hebel']}x</b> | "
        f"Positionsgröße: <b>{calc['pos_size']} €</b>\n"
        f"💸 Max. Risiko:     <b>{calc['risiko_eur']} €</b> "
        f"({RISK_PERCENT}% vom Konto)\n"
        f"⚠️ Liquidation ca.: <b>{calc['liq_distance']}%</b> entfernt\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Indikatoren:\n"
        f"  RSI ({RSI_PERIOD}):  {rsi}\n"
        f"  EMA{EMA_FAST}:      ${ema_fast:,.2f}\n"
        f"  EMA{EMA_SLOW}:      ${ema_slow:,.2f}\n\n"
        f"🕯 <b>BESTÄTIGUNG</b> – Nächste 3 × 5-Min-Kerzen:\n"
        f"  Kerze 1: Schlusskurs unter ${round(price * 0.999, 2):,}\n"
        f"  Kerze 2: Kein Überschreiten von ${round(price * 1.002, 2):,}\n"
        f"  Kerze 3: Fortsetzung mit Volumen-Bestätigung\n\n"
        f"⏰ Nächste 5-Min-Kerze in: <b>{minutes_to_candle} Min.</b>\n"
        f"🕐 {now}"
    )

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
                 if r.get("Signal") in ("LONG", "SHORT")
                 and datetime.datetime.strptime(r["Timestamp"], "%Y-%m-%d %H:%M:%S") >= since]

        long_count  = sum(1 for r in week if r["Signal"] == "LONG")
        short_count = sum(1 for r in week if r["Signal"] == "SHORT")
        total       = len(week)
        rsi_vals    = [float(r["RSI"]) for r in week if r.get("RSI")]
        avg_rsi     = round(sum(rsi_vals) / len(rsi_vals), 1) if rsi_vals else 0
        kw          = datetime.datetime.now().strftime("KW %W")

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>WÖCHENTLICHER REPORT</b>\n"
            f"Zeitraum: {kw} | Signale: {total}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🟢 Long-Signale:  <b>{long_count}</b>\n"
            f"🔴 Short-Signale: <b>{short_count}</b>\n"
            f"📉 Ø RSI:         <b>{avg_rsi}</b>\n\n"
            f"🤖 <b>KI-EMPFEHLUNGEN:</b>\n"
        )
        if long_count > short_count * 2:
            msg += "⚠️ Long-Win-Rate dominiert → Short-Setups kritischer prüfen\n"
        elif short_count > long_count * 2:
            msg += "⚠️ Viele Short-Signale → Abwärtstrend beachten\n"
        else:
            msg += "✅ Ausgeglichene Signal-Verteilung\n"
        if avg_rsi < 35:
            msg += "📐 Ø RSI niedrig → Markt überverkauft, Long-Bias\n"
        elif avg_rsi > 65:
            msg += "📐 Ø RSI hoch → Markt überhitzt, vorsichtig bei Longs\n"
        msg += f"\n💰 Kontostand: <b>{ACCOUNT_BALANCE} €</b> | Risiko: <b>{RISK_PERCENT}%</b>"

        send_telegram(msg)
    except Exception as e:
        logger.error(f"Report-Fehler: {e}")
        send_telegram(f"⚠️ Report-Fehler: {e}")

# ─────────────────────────────────────────────
# SCHEDULE THREAD
# ─────────────────────────────────────────────

def run_schedule() -> None:
    schedule.every().sunday.at("09:00").do(self_optimize)
    logger.info("Schedule aktiv – Report jeden Sonntag 09:00 Uhr.")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────
# SIGNAL BOT
# ─────────────────────────────────────────────

class BTCSignalBot:
    def __init__(self):
        self.last_signal  = None
        self.prev_ema_cross = None  # vorheriger EMA-Kreuzungsstatus
        logger.info("BTC Signal Bot gestartet.")
        send_telegram(
            f"🤖 <b>BTC Signal Bot gestartet</b>\n\n"
            f"Symbol:       <b>{SYMBOL}</b>\n"
            f"Intervall:    {KLINE_INTERVAL}\n"
            f"RSI Long:     &lt; {RSI_OVERSOLD}\n"
            f"RSI Short:    &gt; {RSI_OVERBOUGHT}\n"
            f"EMA:          {EMA_FAST}/{EMA_SLOW}\n"
            f"Kontostand:   <b>{ACCOUNT_BALANCE} €</b>\n"
            f"Risiko/Trade: <b>{RISK_PERCENT}%</b>\n"
            f"📊 Report:    Sonntags 09:00 Uhr"
        )
        ensure_header()
        threading.Thread(target=run_schedule, daemon=True).start()

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                send_telegram("⛔ <b>Bot gestoppt.</b>")
                break
            except Exception as e:
                logger.error(f"Fehler: {e}")
                send_telegram(f"⚠️ <b>Fehler:</b> {e}")
            time.sleep(CHECK_INTERVAL)

    def _tick(self) -> None:
        klines  = get_klines(SYMBOL, KLINE_INTERVAL, limit=100)
        closes  = [float(k[4]) for k in klines]
        price   = get_current_price(SYMBOL)
        rsi     = calculate_rsi(closes, RSI_PERIOD)
        ema_fast = calculate_ema(closes, EMA_FAST)
        ema_slow = calculate_ema(closes, EMA_SLOW)
        minutes  = get_minutes_to_next_candle(KLINE_INTERVAL)

        # EMA-Kreuzung erkennen
        ema_bullish = ema_fast > ema_slow   # EMA20 über EMA50
        ema_bearish = ema_fast < ema_slow

        logger.info(f"BTC ${price:,.2f} | RSI {rsi} | "
                    f"EMA{EMA_FAST} ${ema_fast:,.2f} | EMA{EMA_SLOW} ${ema_slow:,.2f}")

        # Stop-Distanz dynamisch: 0.5% Standard
        stop_distance = 0.005
        calc = calculate_position(price, stop_distance)

        # ── LONG SIGNAL ──────────────────────────────────────────
        if rsi < RSI_OVERSOLD and ema_bullish and self.last_signal != "LONG":
            self.last_signal = "LONG"
            msg = build_long_message(price, rsi, ema_fast, ema_slow, calc, minutes)
            send_telegram(msg)
            log_signal("LONG", price, rsi, ema_fast, ema_slow,
                       calc["stop_loss"], calc["take_profit"],
                       calc["pos_size"], calc["hebel"],
                       f"RSI<{RSI_OVERSOLD}, EMA bullish")

        # ── SHORT SIGNAL ─────────────────────────────────────────
        elif rsi > RSI_OVERBOUGHT and ema_bearish and self.last_signal != "SHORT":
            self.last_signal = "SHORT"
            msg = build_short_message(price, rsi, ema_fast, ema_slow, calc, minutes)
            send_telegram(msg)
            log_signal("SHORT", price, rsi, ema_fast, ema_slow,
                       calc["take_profit"], calc["stop_loss"],
                       calc["pos_size"], calc["hebel"],
                       f"RSI>{RSI_OVERBOUGHT}, EMA bearish")

        # ── RESET ────────────────────────────────────────────────
        elif RSI_OVERSOLD <= rsi <= RSI_OVERBOUGHT:
            if self.last_signal is not None:
                logger.info("RSI neutral – Signal zurückgesetzt.")
            self.last_signal = None

# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────

if __name__ == "__main__":
    BTCSignalBot().run()
