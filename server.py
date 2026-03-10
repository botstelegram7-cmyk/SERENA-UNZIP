# server.py
import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import app as tg_app
from utils.cleanup import cleanup_worker

fastapi_app = FastAPI(title="Serena Unzip Web Service v2")

@fastapi_app.on_event("startup")
async def on_startup():
    asyncio.create_task(cleanup_worker())
    await tg_app.start()
    print("Serena Unzip Bot v2 started")

@fastapi_app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    print("Serena Unzip Bot v2 stopped")

@fastapi_app.get("/", response_class=PlainTextResponse)
async def root():
    return "Serena Unzip Bot v2 is running"

@fastapi_app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"
