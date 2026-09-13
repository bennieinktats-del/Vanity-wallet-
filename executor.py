def cmd_get_key(parts):
    if len(parts) < 2:
        send_telegram("❌ Usage: <code>key [pair_id]</code>")
        return
    
    try:
        pair_id = int(parts[1])
    except ValueError:
        send_telegram("❌ Pair ID must be a number.")
        return
    
    row = db.execute("SELECT * FROM execution_queue WHERE id = ?", (pair_id,)).fetchone()
    if not row:
        send_telegram(f"❌ Pair #{pair_id} not found.")
        return
    
    if not row['wallet_c_private_key']:
        send_telegram(f"❌ Pair #{pair_id} has no generated wallet yet.")
        return
    
    send_telegram(
        f"🔑 <b>Private Key for Pair #{pair_id}</b>\n\n"
        f"<b>Wallet C Address:</b>\n<code>{row['wallet_c_address']}</code>\n\n"
        f"⚠️ <b>PRIVATE KEY:</b>\n<code>{row['wallet_c_private_key']}</code>\n\n"
        f"<b>Wallet A (Target):</b>\n<code>{row['wallet_a']}</code>\n\n"
        f"<i>Save this key securely!</i>"
    )
