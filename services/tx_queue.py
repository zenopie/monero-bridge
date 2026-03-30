# /services/tx_queue.py
"""
Unified transaction queue for Secret Network.
Ensures all transactions from this backend are serialized through a single
queue to prevent account sequence mismatch errors.
"""
import asyncio
import logging
from typing import Any, List, Optional
from dataclasses import dataclass

from secret_sdk.client.lcd import AsyncLCDClient
from secret_sdk.key.mnemonic import MnemonicKey
from secret_sdk.exceptions import LCDResponseError

import config

logger = logging.getLogger(__name__)


@dataclass
class TxResult:
    """Result of a transaction submission."""
    success: bool
    tx_hash: Optional[str] = None
    code: int = 0
    raw_log: Optional[str] = None
    error: Optional[str] = None
    logs: Optional[list] = None


class TransactionQueue:
    """
    Singleton transaction queue that serializes all blockchain transactions.

    Usage:
        result = await get_tx_queue().submit(
            msg_list=[msg],
            gas=500000,
            memo="my transaction"
        )

        if result.success:
            print(f"TX Hash: {result.tx_hash}")
        else:
            print(f"Error: {result.error}")
    """

    _instance: Optional['TransactionQueue'] = None

    def __init__(self):
        """Initialize the transaction queue. Use get_tx_queue() instead."""
        self._queue_lock = asyncio.Lock()
        self._client: Optional[AsyncLCDClient] = None
        self._wallet = None
        self._initialized = False

        # Retry configuration
        self.max_retries = 2
        self.sequence_error_patterns = [
            "account sequence mismatch",
            "incorrect account sequence",
        ]

    @classmethod
    def get_instance(cls) -> 'TransactionQueue':
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = TransactionQueue()
        return cls._instance

    async def initialize(self) -> None:
        """Initialize the client connection. Call during app startup."""
        if self._client is None:
            self._client = AsyncLCDClient(
                chain_id=config.SECRET_CHAIN_ID,
                url=config.SECRET_LCD_URL
            )
            await self._client.__aenter__()
            self._wallet = self._client.wallet(MnemonicKey(config.WALLET_KEY))
            self._initialized = True
            logger.info("TransactionQueue: Client initialized")

    async def close(self) -> None:
        """Close the client connection. Call during app shutdown."""
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            self._wallet = None
            self._initialized = False
            logger.info("TransactionQueue: Client closed")

    def _is_sequence_error(self, error_msg: str) -> bool:
        """Check if error is a sequence mismatch."""
        error_lower = error_msg.lower()
        return any(pattern in error_lower for pattern in self.sequence_error_patterns)

    async def submit(
        self,
        msg_list: List[Any],
        gas: int = 500000,
        memo: str = "",
        wait_for_confirmation: bool = True,
        confirmation_timeout: int = 30,
    ) -> TxResult:
        """
        Submit a transaction through the queue.

        Args:
            msg_list: List of messages to include in the transaction
            gas: Gas limit for the transaction
            memo: Transaction memo
            wait_for_confirmation: Whether to wait for on-chain confirmation
            confirmation_timeout: Seconds to wait for confirmation

        Returns:
            TxResult with success status, tx_hash, and any error details
        """
        if not self._initialized:
            await self.initialize()

        async with self._queue_lock:
            return await self._submit_with_retry(
                msg_list=msg_list,
                gas=gas,
                memo=memo,
                wait_for_confirmation=wait_for_confirmation,
                confirmation_timeout=confirmation_timeout,
            )

    async def _submit_with_retry(
        self,
        msg_list: List[Any],
        gas: int,
        memo: str,
        wait_for_confirmation: bool,
        confirmation_timeout: int,
    ) -> TxResult:
        """Internal method with retry logic for sequence errors."""
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                # Broadcast the transaction
                tx = await self._wallet.create_and_broadcast_tx(
                    msg_list=msg_list,
                    gas=gas,
                    memo=memo
                )

                # Check broadcast result
                if tx.code != 0:
                    error_msg = tx.raw_log or f"Broadcast failed with code {tx.code}"

                    # Check if it's a sequence error and we can retry
                    if self._is_sequence_error(error_msg) and attempt < self.max_retries:
                        logger.warning(
                            f"TransactionQueue: Sequence error on attempt {attempt + 1}, "
                            f"retrying after short delay..."
                        )
                        await asyncio.sleep(1)
                        continue

                    logger.error(f"TransactionQueue: Broadcast failed: {error_msg}")
                    return TxResult(
                        success=False,
                        code=tx.code,
                        raw_log=tx.raw_log,
                        error=error_msg
                    )

                logger.info(f"TransactionQueue: Broadcast success, txhash={tx.txhash}")

                # If not waiting for confirmation, return now
                if not wait_for_confirmation:
                    return TxResult(
                        success=True,
                        tx_hash=tx.txhash,
                        code=0
                    )

                # Poll for confirmation
                tx_info = await self._wait_for_confirmation(
                    tx.txhash,
                    confirmation_timeout
                )

                if tx_info is None:
                    return TxResult(
                        success=False,
                        tx_hash=tx.txhash,
                        error="Transaction confirmation timed out"
                    )

                if tx_info.code != 0:
                    error_msg = getattr(tx_info, 'rawlog', None) or str(tx_info.logs)
                    return TxResult(
                        success=False,
                        tx_hash=tx.txhash,
                        code=tx_info.code,
                        raw_log=error_msg,
                        error=f"Transaction failed on-chain: {error_msg}",
                        logs=tx_info.logs
                    )

                return TxResult(
                    success=True,
                    tx_hash=tx_info.txhash,
                    code=0,
                    logs=tx_info.logs
                )

            except Exception as e:
                last_error = str(e)

                # Check if it's a sequence error we can retry
                if self._is_sequence_error(last_error) and attempt < self.max_retries:
                    logger.warning(
                        f"TransactionQueue: Sequence error (exception) on attempt {attempt + 1}, "
                        f"retrying..."
                    )
                    await asyncio.sleep(1)
                    continue

                logger.exception(f"TransactionQueue: Exception during submit: {e}")
                return TxResult(
                    success=False,
                    error=last_error
                )

        # Should not reach here, but just in case
        return TxResult(
            success=False,
            error=last_error or "Max retries exceeded"
        )

    async def _wait_for_confirmation(
        self,
        tx_hash: str,
        timeout: int
    ):
        """Poll for transaction confirmation."""
        for _ in range(timeout):
            try:
                tx_info = await self._client.tx.tx_info(tx_hash)
                if tx_info:
                    return tx_info
            except LCDResponseError as e:
                if "tx not found" in str(e).lower():
                    await asyncio.sleep(1)
                    continue
                raise
            except Exception:
                await asyncio.sleep(1)
                continue

        return None

    @property
    def wallet_address(self) -> Optional[str]:
        """Get the wallet address (requires initialization)."""
        if self._wallet:
            return self._wallet.key.acc_address
        return None

    @property
    def client(self) -> Optional[AsyncLCDClient]:
        """Get the underlying client for queries."""
        return self._client

    @property
    def encryption_utils(self):
        """Get encryption utils from the client."""
        if self._client:
            return self._client.encrypt_utils
        return None


def get_tx_queue() -> TransactionQueue:
    """Get the global transaction queue instance."""
    return TransactionQueue.get_instance()
