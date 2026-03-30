# /config.py
import os
import logging

# --- Environment & Ports ---
WEBHOOK_PORT = 8000
ALLOWED_ORIGINS = [
    "https://erth.network",
    "http://localhost:3000",
]

# --- Secret Network ---
SECRET_LCD_URL = os.getenv("SECRET_LCD_URL", "https://lcd.erth.network")
SECRET_CHAIN_ID = os.getenv("SECRET_CHAIN_ID", "secret-4")

# --- Wallet Key ---
def get_wallet_key() -> str:
    """Loads the wallet mnemonic from the 'WALLET_KEY' environment variable."""
    key = os.getenv("WALLET_KEY")
    if not key:
        raise ValueError("FATAL: WALLET_KEY environment variable not set or is empty.")
    return key

WALLET_KEY = get_wallet_key()
logging.info("Wallet key loaded from environment variable.")

# --- XMR Token Contract ---
XMR_TOKEN_CONTRACT = os.getenv(
    "XMR_TOKEN_CONTRACT",
    "secret1uahwhw3rk2x8vx4963yxqw6nl8h9drk79kasfx"
)
XMR_TOKEN_HASH = os.getenv(
    "XMR_TOKEN_HASH",
    "83d3a35e92363d3ef743dec1e9f2e8b3a18f2c761fe44169f11b436648a21078"
)

# --- XMR Bridge Minter Contract ---
XMR_MINTER_CONTRACT = os.getenv(
    "XMR_MINTER_CONTRACT",
    "secret130nl6yfwuzjmnuknwfccq7jl8qekl4kcz0ppap"
)
XMR_MINTER_HASH = os.getenv(
    "XMR_MINTER_HASH",
    "91bed2b7eac8ba8e29c89efc32fcf0d0d0352d75f945da9035bc2ac4fa1ac14d"
)

# --- Monero Bridge Configuration ---
# Monero wallet-rpc URL (local, managed by supervisord)
MONERO_WALLET_RPC_URL = os.getenv("MONERO_WALLET_RPC_URL", "http://127.0.0.1:18083")

# Wallet mnemonic (25 words)
MONERO_MNEMONIC = os.getenv("MONERO_MNEMONIC", "")

# Wallet password (for wallet file encryption)
MONERO_WALLET_PASSWORD = os.getenv("MONERO_WALLET_PASSWORD", "bridge123")

# Bridge settings
XMR_CONFIRMATION_THRESHOLD = int(os.getenv("XMR_CONFIRMATION_THRESHOLD", "20"))
# Restore height for wallet sync (set to recent block height for faster sync)
MONERO_RESTORE_HEIGHT = int(os.getenv("MONERO_RESTORE_HEIGHT", "3300000"))

# Check if bridge is configured
MONERO_BRIDGE_ENABLED = bool(MONERO_MNEMONIC)

if MONERO_BRIDGE_ENABLED:
    logging.info(f"Monero bridge configured: wallet-rpc at {MONERO_WALLET_RPC_URL}")
else:
    logging.info("Monero bridge not configured (missing MONERO_MNEMONIC)")
