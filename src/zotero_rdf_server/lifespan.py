from fastapi import FastAPI
from contextlib import asynccontextmanager
import time, threading

from .config import log_level, DELAY
from .logging_config import logger
from .store import initialize_store, refresh_store

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()
    if log_level != "DEBUG":
        logger.info(f"Delay loading for {DELAY} seconds")
        time.sleep(DELAY)
    threading.Thread(target=refresh_store, daemon=True).start()
    yield