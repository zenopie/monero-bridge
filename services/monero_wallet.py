# /services/monero_wallet.py
"""
Monero wallet service for the XMR-SNIP20 bridge.
Uses monero-wallet-rpc JSON-RPC interface.

Requires a running monero-wallet-rpc instance.
"""
import logging
import requests
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Transfer:
    """Represents a Monero transfer."""
    txid: str
    amount: int  # In atomic units (piconero)
    confirmations: int
    subaddr_index: Dict[str, int]  # {"major": 0, "minor": N}
    height: int
    timestamp: int
    address: str
    payment_id: str = ""


class MoneroWallet:
    """
    Monero wallet using monero-wallet-rpc JSON-RPC interface.

    Usage:
        wallet = MoneroWallet(
            rpc_url="http://localhost:18083/json_rpc",
            mnemonic="word1 word2 ... word25",
            wallet_password="bridge123"
        )
        wallet.connect()  # Creates/opens wallet from mnemonic
        address = wallet.create_subaddress()
    """

    def __init__(
        self,
        rpc_url: str,
        mnemonic: str,
        wallet_password: str = "bridge123",
        wallet_name: str = "bridge",
        restore_height: int = 0,
        **kwargs,  # Ignore extra params for compatibility
    ):
        self.rpc_url = rpc_url.rstrip("/")
        if not self.rpc_url.endswith("/json_rpc"):
            self.rpc_url = f"{self.rpc_url}/json_rpc"
        self.mnemonic = mnemonic
        self.wallet_password = wallet_password
        self.wallet_name = wallet_name
        self.restore_height = restore_height
        self._connected = False
        self._primary_address: Optional[str] = None

    def _rpc_call(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make a JSON-RPC call to wallet-rpc."""
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
        }
        if params:
            payload["params"] = params

        try:
            response = requests.post(
                self.rpc_url,
                json=payload,
                timeout=300,  # 5 minutes - wallet-rpc can be slow when syncing
            )
            response.raise_for_status()
            result = response.json()

            if "error" in result:
                error = result["error"]
                raise MoneroWalletError(f"RPC error: {error.get('message', error)}")

            return result.get("result", {})
        except requests.RequestException as e:
            raise MoneroWalletError(f"RPC connection failed: {e}")

    def connect(self) -> None:
        """
        Initialize wallet - try to open existing wallet or restore from mnemonic.
        """
        # First try to open existing wallet
        try:
            self._rpc_call("open_wallet", {
                "filename": self.wallet_name,
                "password": self.wallet_password,
            })
            logger.info(f"Opened existing wallet: {self.wallet_name}")
            self._connected = True
            self._primary_address = self.get_address()
            logger.info(f"Monero wallet connected. Address: {self._primary_address}")
            return
        except MoneroWalletError as e:
            if "Failed to open wallet" not in str(e) and "wallet" not in str(e).lower():
                raise
            logger.info(f"Wallet doesn't exist, restoring from mnemonic...")

        # Restore wallet from mnemonic seed
        try:
            print(f"[MoneroWallet] Restoring wallet from mnemonic at height {self.restore_height}...", flush=True)
            result = self._rpc_call("restore_deterministic_wallet", {
                "filename": self.wallet_name,
                "password": self.wallet_password,
                "seed": self.mnemonic,
                "restore_height": self.restore_height,
                "autosave_current": True,
            })
            self._primary_address = result.get("address")
            print(f"[MoneroWallet] Restored wallet from mnemonic. Address: {self._primary_address}", flush=True)
            self._connected = True
        except MoneroWalletError as e:
            if "already exists" in str(e):
                self._rpc_call("open_wallet", {
                    "filename": self.wallet_name,
                    "password": self.wallet_password,
                })
                self._connected = True
                self._primary_address = self.get_address()
                logger.info(f"Monero wallet connected. Address: {self._primary_address}")
            else:
                raise

    def get_address(self, account_index: int = 0, address_index: int = 0) -> str:
        """Get wallet address."""
        result = self._rpc_call("get_address", {
            "account_index": account_index,
            "address_index": [address_index],
        })
        addresses = result.get("addresses", [])
        if addresses:
            return addresses[0].get("address", "")
        return result.get("address", "")

    def create_subaddress(self, label: str = "", account_index: int = 0) -> Dict[str, Any]:
        """Create a new subaddress for receiving deposits."""
        result = self._rpc_call("create_address", {
            "account_index": account_index,
            "label": label,
        })
        return {
            "address": result.get("address", ""),
            "address_index": result.get("address_index", 0),
        }

    def get_subaddress(self, index: int, account_index: int = 0) -> str:
        """Get subaddress by index."""
        result = self._rpc_call("get_address", {
            "account_index": account_index,
            "address_index": [index],
        })
        addresses = result.get("addresses", [])
        if addresses:
            return addresses[0].get("address", "")
        raise ValueError(f"Subaddress index {index} not found")

    def get_all_subaddresses(self, account_index: int = 0) -> List[Dict[str, Any]]:
        """Get all subaddresses for an account."""
        result = self._rpc_call("get_address", {
            "account_index": account_index,
        })
        return result.get("addresses", [])

    def get_incoming_transfers(self, min_confirmations: int = 0) -> List[Transfer]:
        """Get all incoming transfers to the wallet."""
        transfers = []

        result = self._rpc_call("get_transfers", {
            "in": True,
            "pending": True,
            "pool": True,
            "account_index": 0,
            "subaddr_indices": [],
        })

        for tx in result.get("in", []):
            confirmations = tx.get("confirmations", 0)
            if confirmations < min_confirmations:
                continue
            transfers.append(Transfer(
                txid=tx.get("txid", ""),
                amount=tx.get("amount", 0),
                confirmations=confirmations,
                subaddr_index=tx.get("subaddr_index", {"major": 0, "minor": 0}),
                height=tx.get("height", 0),
                timestamp=tx.get("timestamp", 0),
                address=tx.get("address", ""),
                payment_id=tx.get("payment_id", ""),
            ))

        for tx in result.get("pool", []):
            if min_confirmations > 0:
                continue
            transfers.append(Transfer(
                txid=tx.get("txid", ""),
                amount=tx.get("amount", 0),
                confirmations=0,
                subaddr_index=tx.get("subaddr_index", {"major": 0, "minor": 0}),
                height=0,
                timestamp=tx.get("timestamp", 0),
                address=tx.get("address", ""),
                payment_id=tx.get("payment_id", ""),
            ))

        return transfers

    def get_height(self) -> int:
        """Get current wallet height."""
        result = self._rpc_call("get_height")
        return result.get("height", 0)

    def get_balance(self, account_index: int = 0) -> Dict[str, int]:
        """Get wallet balance in atomic units."""
        result = self._rpc_call("get_balance", {
            "account_index": account_index,
        })
        return {
            "balance": result.get("balance", 0),
            "unlocked_balance": result.get("unlocked_balance", 0),
        }

    def transfer(self, address: str, amount_atomic: int, priority: int = 1) -> Dict[str, Any]:
        """Send XMR to an address."""
        result = self._rpc_call("transfer", {
            "destinations": [{"amount": amount_atomic, "address": address}],
            "priority": priority,
            "get_tx_key": True,
        })
        return {
            "tx_hash": result.get("tx_hash", ""),
            "fee": result.get("fee", 0),
        }

    def refresh(self) -> None:
        """Refresh wallet."""
        self._rpc_call("refresh")

    def save(self) -> None:
        """Save wallet to disk."""
        self._rpc_call("store")


class MoneroWalletError(Exception):
    """Exception for Monero wallet errors."""
    pass
