def self_optimize():
    # Sheet auslesen
    sheet = client.open("BTC Trade Log").sheet1
    rows = sheet.get_all_records()
    df = pd.DataFrame(rows)
    
    # Nur abgeschlossene Trades
    df = df[df["Ergebnis €"] != ""]
    df["Ergebnis €"] = df["Ergebnis €"].astype(float)
    
    wins  = df[df["Ergebnis €"] > 0]
    losses= df[df["Ergebnis €"] < 0]
    
    win_rate = len(wins) / len(df) * 100
    avg_win  = wins["Ergebnis €"].mean()
    avg_loss = losses["Ergebnis €"].mean()
    rr_ratio = abs(avg_win / avg_loss)
    
    msg = (
        f"📈 <b>Selbstanalyse ({len(df)} Trades)</b>\n"
        f"✅ Win-Rate: {win_rate:.1f}%\n"
        f"💚 Ø Gewinn: {avg_win:.2f} €\n"
        f"🔴 Ø Verlust: {avg_loss:.2f} €\n"
        f"⚖️ RR-Ratio: 1:{rr_ratio:.2f}\n"
    )
    
    # Empfehlung
    if win_rate < 45:
        msg += "\n⚠️ Empfehlung: Einstiegsbedingungen verschärfen"
    if rr_ratio < 1.5:
        msg += "\n⚠️ Empfehlung: TP weiter setzen oder Stop enger"
    
    send_telegram(msg)

# Jeden Sonntag Analyse
schedule.every().sunday.at("09:00").do(self_optimize)
