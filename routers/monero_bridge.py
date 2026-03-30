# /routers/monero_bridge.py
"""
FastAPI endpoints for the XMR-SNIP20 bridge.
- Generate deposit addresses
- Check deposit status
- Request withdrawals
"""
import asyncio
import logging
import re
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException

from pydantic import BaseModel, Field

from secret_sdk.client.lcd import LCDClient, AsyncLCDClient

import config
from scheduled_tasks.monero_bridge import (
    get_deposit_address_by_secret,
    save_deposit_address,
    get_deposits_by_secret_address,
    get_deposit_by_txid,
    reset_deposit_to_ready,
    get_balance_sync,
    get_height_sync,
    get_subaddress_at_index_sync,
    get_deposit_index_from_contract,
    withdrawal_exists_by_contract_id,
    create_withdrawal_from_contract,
    CONFIRMATION_THRESHOLD,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Thread pool for sync operations
_executor = ThreadPoolExecutor(max_workers=2)


# --- Request/Response Models ---

class DepositAddressRequest(BaseModel):
    secret_address: str = Field(..., description="Secret Network address to receive minted tokens")


class DepositAddressResponse(BaseModel):
    monero_address: str = Field(..., description="Monero subaddress for deposits")
    secret_address: str = Field(..., description="Secret Network address that will receive tokens")
    confirmations_required: int = Field(default=CONFIRMATION_THRESHOLD)
    message: str = Field(default="Send XMR to this address. Tokens will be minted after confirmations.")


class DepositStatus(BaseModel):
    txid: str
    amount_xmr: float
    confirmations: int
    status: str  # pending, confirming, ready, minted, failed
    mint_tx_hash: Optional[str] = None
    detected_at: str


class DepositStatusResponse(BaseModel):
    secret_address: str
    deposit_address: Optional[str] = None
    deposits: List[DepositStatus]
    total_deposited_xmr: float
    total_minted_xmr: float


class WithdrawRequest(BaseModel):
    secret_address: str = Field(..., description="Secret Network address to process pending withdrawals for")


class PendingWithdrawal(BaseModel):
    withdrawal_id: int
    amount_xmr: float
    monero_address: str
    status: str


class WithdrawResponse(BaseModel):
    secret_address: str
    withdrawals_queued: int
    pending_withdrawals: List[PendingWithdrawal]
    message: str


class BridgeInfoResponse(BaseModel):
    confirmations_required: int
    token_contract: str
    deposit_enabled: bool
    withdrawal_enabled: bool
    min_deposit_xmr: float
    min_withdrawal_xmr: float
    wallet_height: Optional[int] = None


class RetryMintRequest(BaseModel):
    address: str = Field(..., description="Secret Network address that owns the deposit")
    txid: str = Field(..., description="Monero transaction hash of the failed deposit")


class RetryMintResponse(BaseModel):
    success: bool
    txid: str
    message: str


# --- Validation ---

def is_valid_secret_address(address: str) -> bool:
    """Validate Secret Network address format."""
    if not address or not isinstance(address, str):
        return False
    if not address.startswith("secret1"):
        return False
    if len(address) < 39 or len(address) > 90:
        return False
    bech32_pattern = r'^secret1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]+$'
    return bool(re.match(bech32_pattern, address))


def is_valid_monero_address(address: str) -> bool:
    """
    Validate Monero address format.
    - Standard addresses start with '4' and are 95 characters
    - Subaddresses start with '8' and are 95 characters
    - Integrated addresses start with '4' and are 106 characters
    """
    if not address or not isinstance(address, str):
        return False
    if len(address) == 95:
        return address[0] in ('4', '8')
    if len(address) == 106:
        return address[0] == '4'
    return False


# --- Endpoints ---

@router.get("/info", response_model=BridgeInfoResponse, summary="Get bridge information")
async def get_bridge_info():
    """Get current bridge configuration and status."""
    xmr_contract = config.XMR_TOKEN_CONTRACT

    # Get wallet height if bridge is enabled
    wallet_height = None
    if config.MONERO_BRIDGE_ENABLED:
        try:
            loop = asyncio.get_event_loop()
            wallet_height = await loop.run_in_executor(_executor, get_height_sync)
        except Exception:
            pass  # Wallet not available

    return BridgeInfoResponse(
        confirmations_required=CONFIRMATION_THRESHOLD,
        token_contract=xmr_contract or "",
        deposit_enabled=config.MONERO_BRIDGE_ENABLED and bool(xmr_contract),
        withdrawal_enabled=config.MONERO_BRIDGE_ENABLED and bool(xmr_contract),
        min_deposit_xmr=0.001,
        min_withdrawal_xmr=0.001,
        wallet_height=wallet_height,
    )


@router.post("/deposit-address", response_model=DepositAddressResponse, summary="Get deposit address")
async def generate_deposit_address(req: DepositAddressRequest):
    """
    Get the Monero deposit address for a Secret Network address.

    Flow:
    1. User must first call RegisterDepositAddress on the minter contract
    2. Then call this endpoint to get their Monero deposit address
    3. Send XMR to this address, tokens will be minted after confirmations
    """
    # Validate Secret address
    if not is_valid_secret_address(req.secret_address):
        raise HTTPException(status_code=400, detail=f"Invalid Secret Network address: {req.secret_address}")

    # Check if user already has a deposit address in local cache
    existing = get_deposit_address_by_secret(req.secret_address)
    if existing:
        return DepositAddressResponse(
            monero_address=existing["subaddress"],
            secret_address=req.secret_address,
            confirmations_required=CONFIRMATION_THRESHOLD,
            message="Send XMR to this address."
        )

    # Check bridge is configured
    if not config.MONERO_BRIDGE_ENABLED:
        raise HTTPException(status_code=503, detail="Bridge not configured")

    # Query the minter contract for the user's deposit index
    try:
        index = await get_deposit_index_from_contract(req.secret_address)

        if index is None:
            raise HTTPException(
                status_code=404,
                detail="No deposit address registered. Please call RegisterDepositAddress on the minter contract first."
            )

        # Get the subaddress at this index (deterministic from wallet seed)
        loop = asyncio.get_event_loop()
        subaddress = await loop.run_in_executor(
            _executor,
            get_subaddress_at_index_sync,
            index
        )

        # Cache the mapping in local database
        save_deposit_address(
            subaddress=subaddress,
            subaddress_index=index,
            secret_address=req.secret_address,
            label=f"contract:{req.secret_address}"
        )

        print(f"[MoneroBridge] Synced deposit address index {index} for {req.secret_address}", flush=True)

        return DepositAddressResponse(
            monero_address=subaddress,
            secret_address=req.secret_address,
            confirmations_required=CONFIRMATION_THRESHOLD,
            message="Send XMR to this address."
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Wallet not initialized: {e}")
        raise HTTPException(status_code=503, detail="Monero wallet not available")
    except Exception as e:
        logger.exception("Error getting deposit address")
        raise HTTPException(status_code=500, detail="Failed to get deposit address")


@router.get("/deposit-status/{secret_address}", response_model=DepositStatusResponse, summary="Check deposit status")
async def check_deposit_status(secret_address: str):
    """
    Check the status of all deposits for a Secret Network address.
    Returns all deposits and their confirmation status.
    """
    if not is_valid_secret_address(secret_address):
        raise HTTPException(status_code=400, detail=f"Invalid Secret Network address: {secret_address}")

    # Get deposit address
    deposit_addr = get_deposit_address_by_secret(secret_address)

    # Get all deposits
    deposits = get_deposits_by_secret_address(secret_address)

    deposit_statuses = []
    total_deposited = 0.0
    total_minted = 0.0

    for d in deposits:
        amount_xmr = d["amount_atomic"] / 1e12
        total_deposited += amount_xmr
        if d["status"] == "minted":
            total_minted += amount_xmr

        deposit_statuses.append(DepositStatus(
            txid=d["txid"],
            amount_xmr=amount_xmr,
            confirmations=d["confirmations"],
            status=d["status"],
            mint_tx_hash=d["mint_tx_hash"],
            detected_at=d["detected_at"]
        ))

    return DepositStatusResponse(
        secret_address=secret_address,
        deposit_address=deposit_addr["subaddress"] if deposit_addr else None,
        deposits=deposit_statuses,
        total_deposited_xmr=total_deposited,
        total_minted_xmr=total_minted
    )


@router.post("/withdraw", response_model=WithdrawResponse, summary="Process pending withdrawals")
async def request_withdrawal(req: WithdrawRequest):
    """
    Process all pending withdrawals for a Secret Network address.

    Flow:
    1. User calls minter contract's withdraw() function (burns tokens, records withdrawal)
    2. User/frontend calls this endpoint with their secret_address
    3. Backend queries contract for all pending withdrawals for that address
    4. Backend queues all pending XMR withdrawals
    5. XMR is sent and CompleteWithdrawal is called on the contract
    """
    # Validate Secret address
    if not is_valid_secret_address(req.secret_address):
        raise HTTPException(status_code=400, detail=f"Invalid Secret Network address: {req.secret_address}")

    # Check if minter contract is configured
    if not config.XMR_MINTER_CONTRACT:
        raise HTTPException(status_code=503, detail="Minter contract not configured")

    # Query the minter contract for pending withdrawals
    try:
        async with AsyncLCDClient(chain_id=config.SECRET_CHAIN_ID, url=config.SECRET_LCD_URL) as secret_client:
            query_msg = {"get_pending_withdrawals": {"address": req.secret_address}}

            response = await secret_client.wasm.contract_query(
                config.XMR_MINTER_CONTRACT,
                query_msg,
                config.XMR_MINTER_HASH,
            )

            # Response: { withdrawals: Vec<Withdrawal> }
            pending_withdrawals = response.get("withdrawals", []) if response else []

    except Exception as e:
        logger.exception(f"Error querying minter contract for pending withdrawals")
        raise HTTPException(status_code=500, detail=f"Failed to query contract: {str(e)}")

    if not pending_withdrawals:
        return WithdrawResponse(
            secret_address=req.secret_address,
            withdrawals_queued=0,
            pending_withdrawals=[],
            message="No pending withdrawals found"
        )

    # Process each pending withdrawal
    queued_withdrawals = []
    for withdrawal in pending_withdrawals:
        withdrawal_id = int(withdrawal.get("id", 0))
        monero_address = withdrawal.get("monero_address", "")
        amount = int(withdrawal.get("amount", 0))
        fee = int(withdrawal.get("fee", 0))

        # Skip if already processed
        if withdrawal_exists_by_contract_id(withdrawal_id):
            continue

        # Validate Monero address
        if not monero_address or not is_valid_monero_address(monero_address):
            logger.warning(f"Withdrawal #{withdrawal_id} has invalid Monero address: {monero_address}")
            continue

        # Create the withdrawal record
        create_withdrawal_from_contract(
            contract_withdrawal_id=withdrawal_id,
            secret_address=req.secret_address,
            monero_address=monero_address,
            amount_atomic=amount,
            fee_atomic=fee,
        )

        queued_withdrawals.append(PendingWithdrawal(
            withdrawal_id=withdrawal_id,
            amount_xmr=amount / 1e12,
            monero_address=monero_address,
            status="pending"
        ))

        print(f"[MoneroBridge] Withdrawal #{withdrawal_id} queued: {amount/1e12:.6f} XMR to {monero_address[:20]}...", flush=True)

    return WithdrawResponse(
        secret_address=req.secret_address,
        withdrawals_queued=len(queued_withdrawals),
        pending_withdrawals=queued_withdrawals,
        message=f"{len(queued_withdrawals)} withdrawal(s) queued for processing"
    )


@router.get("/balance", summary="Get bridge wallet balance")
async def get_bridge_balance():
    """Get the current XMR balance of the bridge wallet (for transparency)."""
    if not config.MONERO_BRIDGE_ENABLED:
        raise HTTPException(status_code=503, detail="Bridge not configured")

    try:
        loop = asyncio.get_event_loop()
        balance = await loop.run_in_executor(_executor, get_balance_sync)
        return {
            "balance_xmr": balance["balance"] / 1e12,
            "unlocked_balance_xmr": balance["unlocked_balance"] / 1e12,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail="Monero wallet not available")
    except Exception as e:
        logger.exception("Error getting balance")
        raise HTTPException(status_code=500, detail="Failed to get balance")


@router.post("/retry-mint", response_model=RetryMintResponse, summary="Retry a failed mint")
async def retry_mint(req: RetryMintRequest):
    """
    Retry minting tokens for a failed deposit.

    This endpoint allows retrying a mint operation that previously failed.
    The deposit must exist, belong to the provided address, and have 'failed' status.
    Once reset, the background task will automatically pick it up and retry minting.
    """
    # Validate Secret address
    if not is_valid_secret_address(req.address):
        raise HTTPException(status_code=400, detail=f"Invalid Secret Network address: {req.address}")

    # Get the deposit by txid
    deposit = get_deposit_by_txid(req.txid)
    if not deposit:
        raise HTTPException(status_code=404, detail=f"Deposit not found for txid: {req.txid}")

    # Get the deposit address to verify ownership
    deposit_addr = get_deposit_address_by_secret(req.address)
    if not deposit_addr:
        raise HTTPException(status_code=404, detail=f"No deposit address found for: {req.address}")

    # Verify the deposit belongs to this address
    if deposit["subaddress_index"] != deposit_addr["subaddress_index"]:
        raise HTTPException(status_code=403, detail="Deposit does not belong to this address")

    # Check deposit status
    if deposit["status"] != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Deposit status is '{deposit['status']}', only 'failed' deposits can be retried"
        )

    # Reset the deposit to 'ready' status
    success = reset_deposit_to_ready(req.txid)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to reset deposit status")

    print(f"[MoneroBridge] Retry-mint requested for {req.txid[:16]}... by {req.address}", flush=True)

    return RetryMintResponse(
        success=True,
        txid=req.txid,
        message="Deposit reset to ready. Minting will be retried automatically."
    )
