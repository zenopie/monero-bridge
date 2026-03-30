# /scheduled_tasks/monero_bridge.py
"""
Scheduled task for Monero bridge deposit detection and confirmation tracking.
- Polls monerod for incoming transfers using wallet keys from ENV
- Tracks confirmations until threshold (20)
- Mints SNIP-20 tokens on Secret Network when confirmed
"""
import os
import asyncio
import sqlite3
import logging
import traceback
from typing import Optional, Dict, Any, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from secret_sdk.core.wasm import MsgExecuteContract

import config
from services.monero_wallet import MoneroWallet, MoneroWalletError
from services.tx_queue import get_tx_queue

logger = logging.getLogger(__name__)

# --- Database Configuration ---
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "monero_bridge.db")

# Confirmation threshold for minting
CONFIRMATION_THRESHOLD = config.XMR_CONFIRMATION_THRESHOLD

# Retry settings for failed mints
MAX_MINT_RETRIES = 5
RETRY_DELAY_MINUTES = 10

# Thread pool for running synchronous monero operations
_executor = ThreadPoolExecutor(max_workers=2)

# Global wallet instance (initialized once at startup)
_monero_wallet: Optional[MoneroWallet] = None


def _get_conn() -> sqlite3.Connection:
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_bridge_db() -> None:
    """Initialize the Monero bridge database schema."""
    conn = _get_conn()
    try:
        cur = conn.cursor()

        # Deposit addresses table - maps subaddresses to Secret addresses
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deposit_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subaddress TEXT UNIQUE NOT NULL,
                subaddress_index INTEGER UNIQUE NOT NULL,
                secret_address TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                label TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposit_secret_addr ON deposit_addresses(secret_address)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposit_subaddr_index ON deposit_addresses(subaddress_index)")

        # Deposits table - tracks incoming XMR deposits
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                txid TEXT UNIQUE NOT NULL,
                subaddress_index INTEGER NOT NULL,
                secret_address TEXT NOT NULL,
                amount_atomic INTEGER NOT NULL,
                confirmations INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                block_height INTEGER,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TEXT,
                mint_tx_hash TEXT,
                mint_error TEXT,
                FOREIGN KEY (subaddress_index) REFERENCES deposit_addresses(subaddress_index)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_secret_addr ON deposits(secret_address)")

        # Withdrawals table - tracks outgoing XMR withdrawals (burn SNIP-20 -> send XMR)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_withdrawal_id INTEGER UNIQUE,
                secret_address TEXT NOT NULL,
                monero_address TEXT NOT NULL,
                amount_atomic INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                burn_tx_hash TEXT,
                monero_txid TEXT,
                monero_tx_key TEXT,
                fee_atomic INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                error TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_contract_id ON withdrawals(contract_withdrawal_id)")

        # Metadata table for tracking sync state
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bridge_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
    finally:
        conn.close()


# --- Deposit Address Management ---

def get_deposit_address_by_secret(secret_address: str) -> Optional[Dict[str, Any]]:
    """Get existing deposit address for a Secret Network address."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM deposit_addresses WHERE secret_address = ? ORDER BY created_at DESC LIMIT 1",
            (secret_address,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_deposit_address(subaddress: str, subaddress_index: int, secret_address: str, label: str = "") -> None:
    """Save a new deposit address mapping."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO deposit_addresses (subaddress, subaddress_index, secret_address, label)
               VALUES (?, ?, ?, ?)""",
            (subaddress, subaddress_index, secret_address, label)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_active_subaddress_indices() -> List[int]:
    """Get all subaddress indices we're watching for deposits."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT subaddress_index FROM deposit_addresses")
        return [row["subaddress_index"] for row in cur.fetchall()]
    finally:
        conn.close()


def get_secret_address_for_subaddress(subaddress_index: int) -> Optional[str]:
    """Look up Secret Network address for a given subaddress index."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT secret_address FROM deposit_addresses WHERE subaddress_index = ?",
            (subaddress_index,)
        )
        row = cur.fetchone()
        return row["secret_address"] if row else None
    finally:
        conn.close()


# --- Deposit Tracking ---

def get_pending_deposits() -> List[Dict[str, Any]]:
    """Get all deposits that haven't been minted yet."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM deposits WHERE status IN ('pending', 'confirming')")
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def deposit_exists(txid: str) -> bool:
    """Check if a deposit with this txid already exists."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM deposits WHERE txid = ?", (txid,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def save_deposit(
    txid: str,
    subaddress_index: int,
    secret_address: str,
    amount_atomic: int,
    confirmations: int,
    block_height: int
) -> None:
    """Save a new deposit."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        status = "confirming" if confirmations > 0 else "pending"
        cur.execute(
            """INSERT INTO deposits (txid, subaddress_index, secret_address, amount_atomic,
                                     confirmations, status, block_height)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (txid, subaddress_index, secret_address, amount_atomic, confirmations, status, block_height)
        )
        conn.commit()
        print(f"[MoneroBridge] New deposit detected: {txid[:16]}... {amount_atomic/1e12:.6f} XMR -> {secret_address}", flush=True)
    finally:
        conn.close()


def update_deposit_confirmations(txid: str, confirmations: int) -> None:
    """Update confirmation count for a deposit."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        status = "confirming" if confirmations < CONFIRMATION_THRESHOLD else "ready"
        cur.execute(
            "UPDATE deposits SET confirmations = ?, status = ? WHERE txid = ?",
            (confirmations, status, txid)
        )
        conn.commit()
    finally:
        conn.close()


def mark_deposit_minted(txid: str, mint_tx_hash: str) -> None:
    """Mark a deposit as successfully minted."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE deposits SET status = 'minted', mint_tx_hash = ?, confirmed_at = ?
               WHERE txid = ?""",
            (mint_tx_hash, datetime.utcnow().isoformat(), txid)
        )
        conn.commit()
        print(f"[MoneroBridge] Deposit {txid[:16]}... minted successfully: {mint_tx_hash}", flush=True)
    finally:
        conn.close()


def mark_deposit_failed(txid: str, error: str) -> None:
    """Mark a deposit mint as failed."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE deposits SET status = 'failed', mint_error = ? WHERE txid = ?",
            (error, txid)
        )
        conn.commit()
        print(f"[MoneroBridge] Deposit {txid[:16]}... mint failed: {error}", flush=True)
    finally:
        conn.close()


def get_deposits_by_secret_address(secret_address: str) -> List[Dict[str, Any]]:
    """Get all deposits for a Secret Network address."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM deposits WHERE secret_address = ? ORDER BY detected_at DESC",
            (secret_address,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_deposit_by_txid(txid: str) -> Optional[Dict[str, Any]]:
    """Get a deposit by its Monero transaction ID."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM deposits WHERE txid = ?", (txid,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reset_deposit_to_ready(txid: str) -> bool:
    """Reset a failed deposit's status to 'ready' so it can be reprocessed."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE deposits SET status = 'ready', mint_error = NULL WHERE txid = ? AND status = 'failed'",
            (txid,)
        )
        conn.commit()
        updated = cur.rowcount > 0
        if updated:
            print(f"[MoneroBridge] Deposit {txid[:16]}... reset to ready for retry", flush=True)
        return updated
    finally:
        conn.close()


# --- Withdrawal Management ---

def create_withdrawal(
    secret_address: str,
    monero_address: str,
    amount_atomic: int,
    burn_tx_hash: str
) -> int:
    """Create a new withdrawal request. Returns the withdrawal ID."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO withdrawals (secret_address, monero_address, amount_atomic, burn_tx_hash)
               VALUES (?, ?, ?, ?)""",
            (secret_address, monero_address, amount_atomic, burn_tx_hash)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_pending_withdrawals() -> List[Dict[str, Any]]:
    """Get all pending withdrawals that need to be processed."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM withdrawals WHERE status = 'pending'")
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def mark_withdrawal_sent(
    withdrawal_id: int,
    monero_txid: str,
    monero_tx_key: str,
    fee_atomic: int
) -> None:
    """Mark a withdrawal as sent."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE withdrawals SET status = 'sent', monero_txid = ?, monero_tx_key = ?,
               fee_atomic = ?, processed_at = ? WHERE id = ?""",
            (monero_txid, monero_tx_key, fee_atomic, datetime.utcnow().isoformat(), withdrawal_id)
        )
        conn.commit()
    finally:
        conn.close()


def mark_withdrawal_failed(withdrawal_id: int, error: str) -> None:
    """Mark a withdrawal as failed."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE withdrawals SET status = 'failed', error = ? WHERE id = ?",
            (error, withdrawal_id)
        )
        conn.commit()
    finally:
        conn.close()


# --- Withdrawal Event Detection ---

def withdrawal_exists_by_contract_id(contract_withdrawal_id: int) -> bool:
    """Check if we already processed this withdrawal from the contract."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM withdrawals WHERE contract_withdrawal_id = ?", (contract_withdrawal_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def create_withdrawal_from_contract(
    contract_withdrawal_id: int,
    secret_address: str,
    monero_address: str,
    amount_atomic: int,
    fee_atomic: int,
) -> int:
    """Create a withdrawal record from contract event data."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO withdrawals
               (contract_withdrawal_id, secret_address, monero_address, amount_atomic, fee_atomic, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (contract_withdrawal_id, secret_address, monero_address, amount_atomic, fee_atomic)
        )
        conn.commit()
        print(f"[MoneroBridge] New withdrawal detected: #{contract_withdrawal_id} {amount_atomic/1e12:.6f} XMR -> {monero_address[:20]}...", flush=True)
        return cur.lastrowid
    finally:
        conn.close()


# --- Bridge Metadata ---

def get_metadata(key: str) -> Optional[str]:
    """Get a metadata value."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bridge_metadata WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_metadata(key: str, value: str) -> None:
    """Set a metadata value."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO bridge_metadata (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
            (key, value, datetime.utcnow().isoformat(), value, datetime.utcnow().isoformat())
        )
        conn.commit()
    finally:
        conn.close()


# --- Wallet Access ---

def get_wallet() -> MoneroWallet:
    """Get the global wallet instance."""
    global _monero_wallet
    if not _monero_wallet:
        raise RuntimeError("Monero wallet not initialized")
    return _monero_wallet


def create_subaddress_sync(label: str = "") -> Dict[str, Any]:
    """Create a new subaddress (synchronous, for use with executor)."""
    return get_wallet().create_subaddress(label=label)


def get_incoming_transfers_sync() -> List:
    """Get incoming transfers (synchronous, for use with executor)."""
    return get_wallet().get_incoming_transfers()


def get_balance_sync() -> Dict[str, int]:
    """Get wallet balance (synchronous, for use with executor)."""
    return get_wallet().get_balance()


def send_transfer_sync(address: str, amount_atomic: int) -> Dict[str, Any]:
    """Send XMR (synchronous, for use with executor)."""
    return get_wallet().transfer(address, amount_atomic)


# --- Main Scheduled Task ---

def get_height_sync() -> int:
    """Get current wallet height (non-blocking)."""
    return get_wallet().get_height()


async def poll_deposits():
    """
    Main scheduled task: Poll for new deposits and update confirmations.
    Called every ~30 seconds by the scheduler.
    """
    if not config.MONERO_BRIDGE_ENABLED:
        return

    # Skip if wallet not initialized (e.g., startup race condition)
    if _monero_wallet is None:
        return

    try:
        loop = asyncio.get_event_loop()

        # Get all incoming transfers (run in thread to not block)
        transfers = await loop.run_in_executor(_executor, get_incoming_transfers_sync)

        # Process each transfer
        for transfer in transfers:
            _process_transfer(transfer)

        # Update confirmations for pending deposits
        _update_pending_confirmations(transfers)

        # Process ready deposits (mint tokens)
        await _process_ready_deposits()

        # Process pending withdrawals
        await _process_pending_withdrawals()

    except Exception as e:
        print(f"[MoneroBridge] Error polling deposits: {e}", flush=True)
        traceback.print_exc()


def _process_transfer(transfer):
    """Process a single transfer from the wallet."""
    txid = transfer.txid
    subaddr_index = transfer.subaddr_index.get("minor", 0)

    # Skip if we already know about this deposit
    if deposit_exists(txid):
        return

    # Skip if this is to an address we're not tracking (e.g., main address)
    secret_address = get_secret_address_for_subaddress(subaddr_index)
    if not secret_address:
        # This deposit is to an address we didn't create for bridging
        return

    # Save new deposit
    save_deposit(
        txid=txid,
        subaddress_index=subaddr_index,
        secret_address=secret_address,
        amount_atomic=transfer.amount,
        confirmations=transfer.confirmations,
        block_height=transfer.height
    )


def _update_pending_confirmations(transfers: List):
    """Update confirmation counts for all pending deposits."""
    pending = get_pending_deposits()
    if not pending:
        return

    # Create lookup map
    transfer_map = {t.txid: t for t in transfers}

    for deposit in pending:
        txid = deposit["txid"]
        if txid in transfer_map:
            new_confirmations = transfer_map[txid].confirmations
            if new_confirmations != deposit["confirmations"]:
                update_deposit_confirmations(txid, new_confirmations)
                print(f"[MoneroBridge] Deposit {txid[:16]}... now has {new_confirmations} confirmations", flush=True)


async def _process_ready_deposits():
    """Mint tokens for deposits that have reached confirmation threshold."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM deposits WHERE status = 'ready'")
        ready_deposits = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    if not ready_deposits:
        return

    print(f"[MoneroBridge] Processing {len(ready_deposits)} ready deposits for minting", flush=True)

    for deposit in ready_deposits:
        await _mint_tokens(deposit)


async def _mint_tokens(deposit: Dict[str, Any]):
    """Mint SNIP-20 tokens for a confirmed deposit via the minter contract."""
    txid = deposit["txid"]
    secret_address = deposit["secret_address"]
    amount_atomic = deposit["amount_atomic"]

    if not config.XMR_MINTER_CONTRACT:
        print(f"[MoneroBridge] ERROR: XMR_MINTER_CONTRACT not configured, cannot mint", flush=True)
        mark_deposit_failed(txid, "Minter contract not configured")
        return

    print(f"[MoneroBridge] Processing deposit {txid[:16]}... for {secret_address}", flush=True)

    try:
        tx_queue = get_tx_queue()

        # First check if this deposit was already processed on-chain (prevents double-mint on restart)
        try:
            is_processed_query = {"is_processed": {"monero_tx_id": txid}}
            is_processed = await tx_queue.client.wasm.contract_query(
                config.XMR_MINTER_CONTRACT,
                is_processed_query,
                config.XMR_MINTER_HASH,
            )

            if is_processed.get("is_processed", False):
                print(f"[MoneroBridge] Deposit {txid[:16]}... already processed on-chain, updating local DB", flush=True)
                deposit_query = {"get_deposit": {"monero_tx_id": txid}}
                deposit_response = await tx_queue.client.wasm.contract_query(
                    config.XMR_MINTER_CONTRACT,
                    deposit_query,
                    config.XMR_MINTER_HASH,
                )
                deposit_info = deposit_response.get("deposit") if deposit_response else None
                mint_tx = deposit_info.get("mint_tx_hash", "") if deposit_info else ""
                mark_deposit_minted(txid, mint_tx)
                return
        except Exception as e:
            print(f"[MoneroBridge] Warning: Could not check IsProcessed: {e}", flush=True)

        process_deposit_msg = {
            "process_deposit": {
                "recipient": secret_address,
                "amount": str(amount_atomic),
                "monero_tx_id": txid
            }
        }

        print(f"[MoneroBridge] Calling ProcessDeposit: {amount_atomic} atomic XMR to {secret_address}", flush=True)

        msg = MsgExecuteContract(
            sender=tx_queue.wallet_address,
            contract=config.XMR_MINTER_CONTRACT,
            msg=process_deposit_msg,
            code_hash=config.XMR_MINTER_HASH,
            encryption_utils=tx_queue.encryption_utils
        )

        tx_result = await tx_queue.submit(msg_list=[msg], gas=400000, memo="XMR bridge deposit")

        if not tx_result.success:
            mark_deposit_failed(txid, tx_result.error or "Transaction failed")
            return

        mark_deposit_minted(txid, tx_result.tx_hash)
        print(f"[MoneroBridge] Successfully minted for deposit {txid[:16]}..., tx: {tx_result.tx_hash}", flush=True)

    except Exception as e:
        mark_deposit_failed(txid, str(e))
        traceback.print_exc()


async def _process_pending_withdrawals():
    """Process pending withdrawals by sending XMR and calling CompleteWithdrawal on contract."""
    pending = get_pending_withdrawals()
    if not pending:
        return

    print(f"[MoneroBridge] Processing {len(pending)} pending withdrawals", flush=True)
    loop = asyncio.get_event_loop()

    for withdrawal in pending:
        try:
            # Check wallet has sufficient balance
            balance = await loop.run_in_executor(_executor, get_balance_sync)
            if balance["unlocked_balance"] < withdrawal["amount_atomic"]:
                print(f"[MoneroBridge] Insufficient balance for withdrawal {withdrawal['id']}", flush=True)
                continue

            # Send XMR (run in thread)
            result = await loop.run_in_executor(
                _executor,
                send_transfer_sync,
                withdrawal["monero_address"],
                withdrawal["amount_atomic"]
            )

            print(f"[MoneroBridge] Withdrawal {withdrawal['id']} XMR sent: {result['tx_hash']}", flush=True)

            # Call CompleteWithdrawal on the minter contract
            contract_withdrawal_id = withdrawal.get("contract_withdrawal_id")
            if contract_withdrawal_id is not None and config.XMR_MINTER_CONTRACT:
                try:
                    await _complete_withdrawal_on_contract(
                        contract_withdrawal_id,
                        result["tx_hash"]
                    )
                except Exception as e:
                    # Log but don't fail - XMR was already sent
                    print(f"[MoneroBridge] Warning: Failed to call CompleteWithdrawal: {e}", flush=True)

            mark_withdrawal_sent(
                withdrawal["id"],
                monero_txid=result["tx_hash"],
                monero_tx_key="",
                fee_atomic=result.get("fee", 0)
            )

            print(f"[MoneroBridge] Withdrawal {withdrawal['id']} completed", flush=True)

        except Exception as e:
            mark_withdrawal_failed(withdrawal["id"], str(e))
            traceback.print_exc()


async def _complete_withdrawal_on_contract(withdrawal_id: int, monero_txid: str):
    """Call CompleteWithdrawal on the minter contract."""
    tx_queue = get_tx_queue()

    complete_msg = {
        "complete_withdrawal": {
            "withdrawal_id": withdrawal_id
        }
    }

    print(f"[MoneroBridge] Calling CompleteWithdrawal for #{withdrawal_id}", flush=True)

    msg = MsgExecuteContract(
        sender=tx_queue.wallet_address,
        contract=config.XMR_MINTER_CONTRACT,
        msg=complete_msg,
        code_hash=config.XMR_MINTER_HASH,
        encryption_utils=tx_queue.encryption_utils
    )

    tx_result = await tx_queue.submit(
        msg_list=[msg],
        gas=200000,
        memo=f"XMR withdrawal complete: {monero_txid[:16]}"
    )

    if not tx_result.success:
        raise Exception(f"CompleteWithdrawal failed: {tx_result.error}")

    print(f"[MoneroBridge] CompleteWithdrawal confirmed: {tx_result.tx_hash}", flush=True)


# --- Contract Sync Functions ---

def get_subaddress_at_index_sync(index: int) -> str:
    """Get the subaddress at a specific index (deterministic from wallet seed)."""
    wallet = get_wallet()
    return wallet.get_subaddress(index)


async def sync_deposit_indices_from_contract() -> int:
    """
    Sync all deposit indices from the minter contract to local database.
    Returns the number of indices synced.
    """
    if not config.XMR_MINTER_CONTRACT:
        return 0

    all_indices = []
    start_after = None
    tx_queue = get_tx_queue()

    while True:
        query_msg = {
            "get_all_deposit_indices": {
                "start_after": start_after,
                "limit": 100
            }
        }
        result = await tx_queue.client.wasm.contract_query(
            config.XMR_MINTER_CONTRACT,
            query_msg,
            config.XMR_MINTER_HASH
        )

        indices = result.get("indices", [])
        all_indices.extend(indices)

        if len(indices) < 100:
            break
        start_after = indices[-1][0]  # Last address for pagination

    # Sync each index to local database
    loop = asyncio.get_event_loop()
    synced = 0

    for secret_address, index in all_indices:
        # Check if already in local DB
        existing = get_deposit_address_by_secret(secret_address)
        if existing and existing["subaddress_index"] == index:
            continue

        # Get the subaddress at this index
        try:
            subaddress = await loop.run_in_executor(
                _executor,
                get_subaddress_at_index_sync,
                index
            )

            # Save or update in local DB
            if not existing:
                save_deposit_address(
                    subaddress=subaddress,
                    subaddress_index=index,
                    secret_address=secret_address,
                    label=f"contract:{secret_address}"
                )
                synced += 1

        except Exception as e:
            print(f"[MoneroBridge] Error syncing index {index} for {secret_address}: {e}", flush=True)

    return synced


async def get_deposit_index_from_contract(secret_address: str) -> Optional[int]:
    """Query the minter contract for a user's deposit index."""
    if not config.XMR_MINTER_CONTRACT:
        return None

    tx_queue = get_tx_queue()
    query_msg = {"get_deposit_index": {"address": secret_address}}
    result = await tx_queue.client.wasm.contract_query(
        config.XMR_MINTER_CONTRACT,
        query_msg,
        config.XMR_MINTER_HASH
    )
    return result.get("index")


# --- Initialization ---

async def init_monero_bridge():
    """Initialize the Monero bridge on application startup."""
    global _monero_wallet

    init_bridge_db()

    if config.MONERO_BRIDGE_ENABLED:
        # Retry logic to wait for wallet-rpc to be ready (supervisord race condition)
        max_retries = 10
        retry_delay = 2  # seconds

        for attempt in range(max_retries):
            try:
                _monero_wallet = MoneroWallet(
                    rpc_url=config.MONERO_WALLET_RPC_URL,
                    mnemonic=config.MONERO_MNEMONIC,
                    wallet_password=config.MONERO_WALLET_PASSWORD,
                    restore_height=config.MONERO_RESTORE_HEIGHT,
                )

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(_executor, _monero_wallet.connect)

                balance = await loop.run_in_executor(_executor, get_balance_sync)

                # Sync deposit indices from contract
                synced = await sync_deposit_indices_from_contract()
                if synced > 0:
                    print(f"[Startup] XMR Bridge: {balance['balance']/1e12:.6f} XMR, synced {synced} new addresses", flush=True)
                else:
                    print(f"[Startup] XMR Bridge: {balance['balance']/1e12:.6f} XMR", flush=True)
                return  # Success

            except Exception as e:
                _monero_wallet = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                else:
                    print(f"[Startup] XMR Bridge: Failed after {max_retries} attempts ({e})", flush=True)
    else:
        print("[Startup] XMR Bridge: Not configured", flush=True)
