# /main.py
import uvicorn
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from services.tx_queue import get_tx_queue
from routers import monero_bridge
from scheduled_tasks.monero_bridge import init_monero_bridge, poll_deposits

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Monero Bridge API",
    description="XMR-SNIP20 bridge backend for Erth Network.",
    version="1.0.0"
)

# --- Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# --- Event Handlers & Scheduler ---
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup_event():
    """Initializes the bridge and starts the scheduler."""
    print("\n[Startup] Monero Bridge starting...", flush=True)

    # Initialize transaction queue
    tx_queue = get_tx_queue()
    await tx_queue.initialize()
    print(f"[Startup] TX Queue ready: {tx_queue.wallet_address}", flush=True)

    # Initialize Monero bridge
    await init_monero_bridge()
    if config.MONERO_BRIDGE_ENABLED:
        scheduler.add_job(poll_deposits, 'interval', seconds=30, id='monero_deposit_poll')

    scheduler.start()
    print("[Startup] Ready\n", flush=True)

@app.on_event("shutdown")
async def shutdown_event():
    """Shuts down the scheduler and transaction queue."""
    scheduler.shutdown()
    await get_tx_queue().close()
    print("Application shutdown.")

# --- API Routers ---
app.include_router(monero_bridge.router, tags=["Monero Bridge"])

@app.get("/", tags=["Health Check"])
async def read_root():
    return {"message": "Monero Bridge API"}

# --- Run Server ---
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.WEBHOOK_PORT,
        reload=True,
        access_log=False
    )
