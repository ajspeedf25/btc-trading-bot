"""
BTC Signal Bot
- Preisdaten: Binance Public API (kostenlos)
- RSI + EMA Signale
- Detaillierte Telegram-Nachrichten mit Stop-Loss, Take-Profit, Positionsgröße
- Positionsgröße: immer 10% des Kontostands pro Trade
- Kerzen-Bestätigung: Nach Signal werden die nächsten 3 Kerzen einzeln bewertet
- Wöchentlicher Performance-Report (Sonntags 09:00)
- Google Sheets Logging
- Mindestabstand zwischen Signalen: 30 Minuten (SIGNAL_COOLDOWN)
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

ACCOUNT_BALANCE   = float(os.environ.get("ACCOUNT_BALANCE", 500))   # € Gesamtkapital
TRADE_ALLOCATION  = 0.10   # 10% des Kapitals als Margin einsetzen
RISK_PERCENT      = 0.01   # 1% des Gesamtkapitals als max. Verlust pro Trade
MAX_HEBEL         = 10.0   # Sicherheits-Obergrenze für den Hebel

SYMBOL            = "BTCUSDT"
RSI_PERIOD        = 14
RSI_OVERSOLD      = 40
RSI_OVERBOUGHT    = 60
EMA_FAST          = 20
EMA_SLOW          = 50
KLINE_INTERVAL    = "5m"     # 5-Minuten-Kerzen wie in der Anleitung
CHECK_INTERVAL    = 60       # Sekunden
SIGNAL_COOLDOWN   = 30       # Minuten Mindestabstand zwischen Signalen

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

SHEET_HEADER = ["Timestamp", "Signal", "Symbol", "Preis (USD)", "RSI",
                "EMA20", "EMA50", "Stop-Loss", "Take-Profit",
                "Positionsgröße (€)", "Hebel", "Hinweis", "Screenshot"]

def ensure_header() -> None:
    try:
        sheet = get_sheet()
        if sheet.row_values(1) != SHEET_HEADER:
            sheet.insert_row(SHEET_HEADER, 1)
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

def log_screenshot_to_sheet(photo_url: str) -> str:
    """
    Sucht die letzte Signal-Zeile (LONG/SHORT) ohne Screenshot-Eintrag
    und trägt den Foto-Link in die Screenshot-Spalte ein.
    """
    try:
        sheet   = get_sheet()
        rows    = sheet.get_all_values()
        header  = rows[0]
        scr_col = header.index("Screenshot") + 1   # 1-basiert für gspread
        sig_col = header.index("Signal")

        # Letzte Zeile mit LONG/SHORT ohne Screenshot (von unten suchen)
        target_row = None
        for i in range(len(rows) - 1, 0, -1):
            row     = rows[i]
            sig_val = row[sig_col]     if len(row) > sig_col    else ""
            scr_val = row[scr_col - 1] if len(row) >= scr_col   else ""
            if sig_val in ("LONG", "SHORT") and not scr_val.strip():
                target_row = i + 1    # Sheet-Zeilen sind 1-basiert
                break

        if target_row is None:
            return "⚠️ Keine offene Signal-Zeile ohne Screenshot gefunden."

        sheet.update_cell(target_row, scr_col, photo_url)
        ts  = rows[target_row - 1][header.index("Timestamp")]
        sig = rows[target_row - 1][sig_col]
        logger.info(f"Screenshot in Zeile {target_row} eingetragen ({sig} @ {ts})")
        return f"✅ Screenshot eingetragen\nSignal: <b>{sig}</b> | Zeitstempel: {ts}"

    except Exception as e:
        logger.error(f"Screenshot-Sheet-Fehler: {e}")
        return f"❌ Fehler beim Eintragen: {e}"

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
    Positionsgröße (Margin) = 10% des Kontostands.
    Max. Verlust           = 1% des Gesamtkapitals.
    Hebel                  = max_verlust / (margin * stop_distance_pct)
                             → so groß wie nötig, max. MAX_HEBEL.

    Beispiel bei 500 € Konto, 0.5% Stop:
      margin     = 50 €
      max_verlust= 5 €
      hebel      = 5 / (50 * 0.005) = 20x → auf MAX_HEBEL gedeckelt
    """
    margin      = round(ACCOUNT_BALANCE * TRADE_ALLOCATION, 2)   # 10% Einsatz
    max_verlust = round(ACCOUNT_BALANCE * RISK_PERCENT, 2)        # 1% Risiko

    # Hebel so berechnen, dass bei SL-Treffer exakt max_verlust verloren geht
    hebel_raw   = max_verlust / (margin * stop_distance_pct)
    hebel       = round(min(hebel_raw, MAX_HEBEL), 1)

    # Tatsächliche Positionsgröße (Margin × Hebel)
    pos_size    = round(margin * hebel, 2)

    # Tatsächlicher Verlust bei SL (kann < max_verlust sein wenn Hebel gedeckelt)
    real_verlust = round(margin * stop_distance_pct * hebel, 2)

    # Liquidationsdistanz ≈ 1/Hebel (ohne Funding/Fees)
    liq_distance = round((1 / hebel) * 100, 1)

    stop_loss   = round(price * (1 - stop_distance_pct), 2)
    take_profit = round(price * (1 + stop_distance_pct * 2), 2)  # RR 1:2

    return {
        "margin":        margin,
        "pos_size":      pos_size,
        "max_verlust":   max_verlust,
        "real_verlust":  real_verlust,
        "hebel":         hebel,
        "liq_distance":  liq_distance,
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
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
        f"📦 Margin (10%):         <b>{calc['margin']} €</b>\n"
        f"⚡ Hebel:                <b>{calc['hebel']}x</b>\n"
        f"📊 Positionsgröße:       <b>{calc['pos_size']} €</b>\n"
        f"💸 Max. Verlust (1%):    <b>{calc['real_verlust']} €</b>\n"
        f"⚠️ Liquidation ca.:      <b>{calc['liq_distance']}%</b> entfernt\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Indikatoren:\n"
        f"  RSI ({RSI_PERIOD}):  {rsi}\n"
        f"  EMA{EMA_FAST}:      ${ema_fast:,.2f}\n"
        f"  EMA{EMA_SLOW}:      ${ema_slow:,.2f}\n\n"
        f"🕯 Bestätigung der nächsten 3 Kerzen läuft – du erhältst separate Nachrichten.\n\n"
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
        f"📦 Margin (10%):         <b>{calc['margin']} €</b>\n"
        f"⚡ Hebel:                <b>{calc['hebel']}x</b>\n"
        f"📊 Positionsgröße:       <b>{calc['pos_size']} €</b>\n"
        f"💸 Max. Verlust (1%):    <b>{calc['real_verlust']} €</b>\n"
        f"⚠️ Liquidation ca.:      <b>{calc['liq_distance']}%</b> entfernt\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Indikatoren:\n"
        f"  RSI ({RSI_PERIOD}):  {rsi}\n"
        f"  EMA{EMA_FAST}:      ${ema_fast:,.2f}\n"
        f"  EMA{EMA_SLOW}:      ${ema_slow:,.2f}\n\n"
        f"🕯 Bestätigung der nächsten 3 Kerzen läuft – du erhältst separate Nachrichten.\n\n"
        f"⏰ Nächste 5-Min-Kerze in: <b>{minutes_to_candle} Min.</b>\n"
        f"🕐 {now}"
    )

# ─────────────────────────────────────────────
# KERZEN-BESTÄTIGUNG
# ─────────────────────────────────────────────

def _wait_for_next_closed_candle(after_time: datetime.datetime) -> dict | None:
    """
    Wartet, bis eine neue abgeschlossene 5-Min-Kerze verfügbar ist,
    deren open_time NACH after_time liegt. Gibt die Kerze als dict zurück.
    Timeout: 10 Minuten, danach None.
    """
    deadline = after_time + datetime.timedelta(minutes=10)
    while datetime.datetime.now() < deadline:
        try:
            klines = get_klines(SYMBOL, KLINE_INTERVAL, limit=5)
            # klines[-1] ist die noch laufende Kerze → [-2] ist die letzte abgeschlossene
            last_closed = klines[-2]
            open_time   = datetime.datetime.fromtimestamp(last_closed[0] / 1000)
            if open_time > after_time:
                return {
                    "open_time":  open_time,
                    "open":       float(last_closed[1]),
                    "high":       float(last_closed[2]),
                    "low":        float(last_closed[3]),
                    "close":      float(last_closed[4]),
                    "volume":     float(last_closed[5]),
                }
        except Exception as e:
            logger.error(f"Kerzen-Abruf Fehler: {e}")
        time.sleep(15)
    return None


def _check_candle_long(candle: dict, entry_price: float, candle_num: int,
                        prev_candle: dict | None) -> tuple[bool, str]:
    """
    Prüft eine Kerze auf Long-Bestätigung.
    Gibt (bestätigt: bool, nachricht: str) zurück.
    """
    c     = candle["close"]
    o     = candle["open"]
    h     = candle["high"]
    l     = candle["low"]
    vol   = candle["volume"]
    body  = abs(c - o)
    range_ = h - l if (h - l) > 0 else 0.0001

    if candle_num == 1:
        # Kerze 1: Schlusskurs muss über Einstiegspreis liegen, bullische Kerze
        if c > entry_price and c > o:
            return True, (
                f"✅ Kerze 1 bestätigt\n"
                f"Schlusskurs ${c:,.2f} liegt über Einstieg ${entry_price:,.2f} "
                f"– bullische Kerze (Body: ${body:,.2f})"
            )
        elif c <= entry_price:
            return False, (
                f"❌ Kerze 1 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs ${c:,.2f} liegt UNTER Einstieg ${entry_price:,.2f} "
                f"– kein Aufwärtsmomentum"
            )
        else:
            return False, (
                f"⚠️ Kerze 1 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs ${c:,.2f} über Einstieg, aber bärische Kerze "
                f"(Close unter Open) – Schwäche im Aufwärtsdruck"
            )

    elif candle_num == 2:
        # Kerze 2: Kein Unterschreiten des Einstiegs, Tief darf nicht zu tief fallen
        support_level = round(entry_price * 0.998, 2)  # max. 0.2% unter Einstieg
        if l >= support_level and c >= entry_price:
            return True, (
                f"✅ Kerze 2 bestätigt\n"
                f"Tief ${l:,.2f} hält Support bei ${support_level:,.2f} "
                f"– Aufwärtstrend intakt, Schlusskurs ${c:,.2f}"
            )
        elif l < support_level:
            return False, (
                f"❌ Kerze 2 nicht bestätigt – Trade abbrechen\n"
                f"Tief ${l:,.2f} hat Support ${support_level:,.2f} gebrochen "
                f"– Stop-Loss-Gefahr, Trend ungültig"
            )
        else:
            return False, (
                f"⚠️ Kerze 2 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs ${c:,.2f} unter Einstieg ${entry_price:,.2f} "
                f"– Aufwärtsdruck lässt nach"
            )

    else:  # Kerze 3
        # Kerze 3: Volumen-Bestätigung + klares Momentum
        prev_vol     = prev_candle["volume"] if prev_candle else vol
        vol_ok       = vol > prev_vol * 1.1          # mind. 10% mehr Volumen
        momentum_ok  = c > o and (body / range_) > 0.5  # bullische Körper-Dominanz
        new_high     = h > (prev_candle["high"] if prev_candle else h)

        if vol_ok and momentum_ok and new_high:
            return True, (
                f"✅ Kerze 3 bestätigt – Einstieg empfohlen ✅\n"
                f"Starkes Volumen (+{round((vol/prev_vol-1)*100)}% vs. Vorkerze), "
                f"bullischer Schlusskurs ${c:,.2f}, "
                f"neues Hoch bei ${h:,.2f} – Ausbruch bestätigt"
            )
        elif vol_ok and momentum_ok:
            return True, (
                f"✅ Kerze 3 bestätigt – Einstieg möglich ✅\n"
                f"Volumen und Momentum stimmen (Schlusskurs ${c:,.2f}), "
                f"jedoch kein neues Hoch – Einstieg mit erhöhter Vorsicht"
            )
        elif not vol_ok and momentum_ok:
            return False, (
                f"⚠️ Kerze 3 nicht bestätigt – Trade abbrechen\n"
                f"Aufwärtskerze vorhanden (${c:,.2f}), aber Volumen zu schwach "
                f"({round(vol):,} vs. {round(prev_vol):,} Vorkerze) – "
                f"Ausbruch ohne Überzeugung, kein Einstieg"
            )
        else:
            return False, (
                f"❌ Kerze 3 nicht bestätigt – Trade abbrechen\n"
                f"Kein Momentum: bärische Kerze (Close ${c:,.2f} unter Open ${o:,.2f}) "
                f"und schwaches Volumen – Signal ungültig"
            )


def _check_candle_short(candle: dict, entry_price: float, candle_num: int,
                         prev_candle: dict | None) -> tuple[bool, str]:
    """
    Prüft eine Kerze auf Short-Bestätigung.
    Gibt (bestätigt: bool, nachricht: str) zurück.
    """
    c     = candle["close"]
    o     = candle["open"]
    h     = candle["high"]
    l     = candle["low"]
    vol   = candle["volume"]
    body  = abs(c - o)
    range_ = h - l if (h - l) > 0 else 0.0001

    if candle_num == 1:
        if c < entry_price and c < o:
            return True, (
                f"✅ Kerze 1 bestätigt\n"
                f"Schlusskurs ${c:,.2f} liegt unter Einstieg ${entry_price:,.2f} "
                f"– bärische Kerze (Body: ${body:,.2f})"
            )
        elif c >= entry_price:
            return False, (
                f"❌ Kerze 1 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs ${c:,.2f} liegt ÜBER Einstieg ${entry_price:,.2f} "
                f"– kein Abwärtsmomentum"
            )
        else:
            return False, (
                f"⚠️ Kerze 1 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs unter Einstieg, aber bullische Kerze "
                f"(Close über Open) – Gegendruck vorhanden"
            )

    elif candle_num == 2:
        resistance = round(entry_price * 1.002, 2)  # max. 0.2% über Einstieg
        if h <= resistance and c <= entry_price:
            return True, (
                f"✅ Kerze 2 bestätigt\n"
                f"Hoch ${h:,.2f} hält Widerstand bei ${resistance:,.2f} "
                f"– Abwärtstrend intakt, Schlusskurs ${c:,.2f}"
            )
        elif h > resistance:
            return False, (
                f"❌ Kerze 2 nicht bestätigt – Trade abbrechen\n"
                f"Hoch ${h:,.2f} hat Widerstand ${resistance:,.2f} überschritten "
                f"– Short-Setup ungültig, Stop-Loss-Gefahr"
            )
        else:
            return False, (
                f"⚠️ Kerze 2 nicht bestätigt – Trade abbrechen\n"
                f"Schlusskurs ${c:,.2f} über Einstieg ${entry_price:,.2f} "
                f"– Abwärtsdruck lässt nach"
            )

    else:  # Kerze 3
        prev_vol    = prev_candle["volume"] if prev_candle else vol
        vol_ok      = vol > prev_vol * 1.1
        momentum_ok = c < o and (body / range_) > 0.5   # bärische Körper-Dominanz
        new_low     = l < (prev_candle["low"] if prev_candle else l)

        if vol_ok and momentum_ok and new_low:
            return True, (
                f"✅ Kerze 3 bestätigt – Einstieg empfohlen ✅\n"
                f"Starkes Volumen (+{round((vol/prev_vol-1)*100)}% vs. Vorkerze), "
                f"bärischer Schlusskurs ${c:,.2f}, "
                f"neues Tief bei ${l:,.2f} – Ausbruch nach unten bestätigt"
            )
        elif vol_ok and momentum_ok:
            return True, (
                f"✅ Kerze 3 bestätigt – Einstieg möglich ✅\n"
                f"Volumen und Momentum stimmen (Schlusskurs ${c:,.2f}), "
                f"jedoch kein neues Tief – Einstieg mit erhöhter Vorsicht"
            )
        elif not vol_ok and momentum_ok:
            return False, (
                f"⚠️ Kerze 3 nicht bestätigt – Trade abbrechen\n"
                f"Abwärtskerze vorhanden (${c:,.2f}), aber Volumen zu schwach "
                f"({round(vol):,} vs. {round(prev_vol):,} Vorkerze) – "
                f"Ausbruch ohne Überzeugung, kein Einstieg"
            )
        else:
            return False, (
                f"❌ Kerze 3 nicht bestätigt – Trade abbrechen\n"
                f"Kein Momentum: bullische Kerze (Close ${c:,.2f} über Open ${o:,.2f}) "
                f"und schwaches Volumen – Signal ungültig"
            )


def run_candle_confirmation(direction: str, entry_price: float) -> None:
    """
    Läuft in einem eigenen Thread. Wartet auf 3 abgeschlossene Kerzen nach dem Signal
    und sendet für jede eine separate Telegram-Nachricht.
    direction: "LONG" oder "SHORT"
    """
    logger.info(f"Kerzen-Bestätigung gestartet für {direction} @ ${entry_price:,.2f}")
    check_fn   = _check_candle_long if direction == "LONG" else _check_candle_short
    after_time = datetime.datetime.now()
    prev_candle = None

    for candle_num in range(1, 4):
        candle = _wait_for_next_closed_candle(after_time)
        if candle is None:
            send_telegram(
                f"⏰ Kerze {candle_num} Timeout – Bestätigung abgebrochen.\n"
                f"Keine neue Kerze nach 10 Minuten erhalten."
            )
            return

        ok, msg = check_fn(candle, entry_price, candle_num, prev_candle)
        send_telegram(msg)
        logger.info(f"Kerze {candle_num}: {'✅' if ok else '❌'} | {direction}")

        if not ok:
            return  # Abbruch bei nicht bestätigter Kerze

        after_time  = candle["open_time"]
        prev_candle = candle


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
        msg += (
            f"\n💰 Kontostand: <b>{ACCOUNT_BALANCE} €</b> | "
            f"Margin: <b>10% = {round(ACCOUNT_BALANCE * TRADE_ALLOCATION, 2)} €</b> | "
            f"Max. Risiko: <b>1% = {round(ACCOUNT_BALANCE * RISK_PERCENT, 2)} €</b>"
        )

        send_telegram(msg)
    except Exception as e:
        logger.error(f"Report-Fehler: {e}")
        send_telegram(f"⚠️ Report-Fehler: {e}")

# ─────────────────────────────────────────────
# TELEGRAM FOTO-HANDLER (Screenshot-Upload)
# ─────────────────────────────────────────────

def get_photo_url(file_id: str) -> str:
    """Gibt die direkte Download-URL eines Telegram-Fotos zurück."""
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
        params={"file_id": file_id}, timeout=10
    )
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

@tbot.message_handler(content_types=["photo"])
def handle_photo(message) -> None:
    """
    Wird ausgelöst, wenn du ein Foto an den Bot schickst.
    Nimmt die höchste Auflösung, holt die Download-URL
    und trägt sie in die letzte offene Sheet-Zeile ein.
    """
    try:
        # Höchste Auflösung = letztes Element in photo-Array
        file_id   = message.photo[-1].file_id
        photo_url = get_photo_url(file_id)
        caption   = message.caption or ""

        status = log_screenshot_to_sheet(photo_url)
        reply  = f"📸 <b>Screenshot empfangen</b>\n{status}"
        if caption:
            reply += f"\n📝 Caption: {caption}"
        tbot.reply_to(message, reply, parse_mode="HTML")
        logger.info(f"Foto verarbeitet: {photo_url}")
    except Exception as e:
        logger.error(f"Foto-Handler Fehler: {e}")
        tbot.reply_to(message, f"❌ Fehler beim Verarbeiten: {e}")

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
        self.last_signal      = None
        self.prev_ema_cross   = None                # vorheriger EMA-Kreuzungsstatus
        self.last_signal_time = None                # Zeitstempel des letzten Signals
        logger.info("BTC Signal Bot gestartet.")
        send_telegram(
            f"🤖 <b>BTC Signal Bot gestartet</b>\n\n"
            f"Symbol:       <b>{SYMBOL}</b>\n"
            f"Intervall:    {KLINE_INTERVAL}\n"
            f"RSI Long:     &lt; {RSI_OVERSOLD}\n"
            f"RSI Short:    &gt; {RSI_OVERBOUGHT}\n"
            f"EMA:          {EMA_FAST}/{EMA_SLOW}\n"
            f"Kontostand:   <b>{ACCOUNT_BALANCE} €</b>\n"
            f"Margin/Trade: <b>10% = {round(ACCOUNT_BALANCE * TRADE_ALLOCATION, 2)} €</b>\n"
            f"Max. Risiko:  <b>1% = {round(ACCOUNT_BALANCE * RISK_PERCENT, 2)} €</b>\n"
            f"Max. Hebel:   <b>{MAX_HEBEL}x</b>\n"
            f"⏳ Cooldown:   <b>{SIGNAL_COOLDOWN} Min.</b> zwischen Signalen\n"
            f"📊 Report:    Sonntags 09:00 Uhr"
        )
        ensure_header()
        threading.Thread(target=run_schedule, daemon=True).start()
        # Polling-Thread für eingehende Telegram-Nachrichten (z.B. Foto-Uploads)
        threading.Thread(target=tbot.infinity_polling, daemon=True).start()

    # ── Cooldown-Prüfung ──────────────────────────────────────────────────────
    def _cooldown_ok(self) -> bool:
        """Gibt True zurück, wenn seit dem letzten Signal mind. SIGNAL_COOLDOWN Minuten vergangen sind."""
        if self.last_signal_time is None:
            return True
        elapsed = (datetime.datetime.now() - self.last_signal_time).total_seconds() / 60
        if elapsed < SIGNAL_COOLDOWN:
            logger.info(f"Cooldown aktiv – noch {SIGNAL_COOLDOWN - elapsed:.1f} Min. warten.")
            return False
        return True

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
        klines   = get_klines(SYMBOL, KLINE_INTERVAL, limit=100)
        closes   = [float(k[4]) for k in klines]
        price    = get_current_price(SYMBOL)
        rsi      = calculate_rsi(closes, RSI_PERIOD)
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
            if self._cooldown_ok():
                self.last_signal      = "LONG"
                self.last_signal_time = datetime.datetime.now()
                msg = build_long_message(price, rsi, ema_fast, ema_slow, calc, minutes)
                send_telegram(msg)
                log_signal("LONG", price, rsi, ema_fast, ema_slow,
                           calc["stop_loss"], calc["take_profit"],
                           calc["pos_size"], 1.0,
                           f"RSI<{RSI_OVERSOLD}, EMA bullish")
                threading.Thread(
                    target=run_candle_confirmation,
                    args=("LONG", price),
                    daemon=True
                ).start()

        # ── SHORT SIGNAL ─────────────────────────────────────────
        elif rsi > RSI_OVERBOUGHT and ema_bearish and self.last_signal != "SHORT":
            if self._cooldown_ok():
                self.last_signal      = "SHORT"
                self.last_signal_time = datetime.datetime.now()
                msg = build_short_message(price, rsi, ema_fast, ema_slow, calc, minutes)
                send_telegram(msg)
                log_signal("SHORT", price, rsi, ema_fast, ema_slow,
                           calc["take_profit"], calc["stop_loss"],
                           calc["pos_size"], 1.0,
                           f"RSI>{RSI_OVERBOUGHT}, EMA bearish")
                threading.Thread(
                    target=run_candle_confirmation,
                    args=("SHORT", price),
                    daemon=True
                ).start()

        # ── RESET ────────────────────────────────────────────────
        elif RSI_OVERSOLD <= rsi <= RSI_OVERBOUGHT:
            if self.last_signal is not None:
                logger.info("RSI neutral – Signal zurückgesetzt.")
            self.last_signal = None
            # Hinweis: last_signal_time bleibt erhalten, damit der Cooldown
            # auch nach einem Reset noch greift.

# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────

if __name__ == "__main__":
    BTCSignalBot().run()
