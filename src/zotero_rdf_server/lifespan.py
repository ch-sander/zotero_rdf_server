from fastapi import FastAPI
from contextlib import asynccontextmanager
import time, threading, asyncio

from .config import log_level, DELAY, STORE_MODE
from .logging_config import logger
from .store import initialize_store, refresh_store

@asynccontextmanager
async def app_lifespan_legacy(app: FastAPI):
    initialize_store()
    if log_level != "DEBUG":
        logger.info(f"Delay loading for {DELAY} seconds")
        time.sleep(DELAY)
    threading.Thread(target=refresh_store, daemon=True).start()
    yield


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()

    if log_level != "DEBUG" and DELAY and DELAY > 0:
        logger.info(f"Delay loading for {DELAY} seconds")
        await asyncio.sleep(DELAY)

    if STORE_MODE in {"memory", "directory_rw"}:
        threading.Thread(target=refresh_store, daemon=True).start()
    else:
        logger.info("STORE_MODE is read-only.")
        threading.Thread(target=refresh_store, daemon=True).start()

    yield

    try:
        if STORE_MODE in {"directory_rw"}:
            from .store import store
            store.flush()
    except Exception as e:
        logger.warning(f"Flush on shutdown failed: {e}")