# server.py
import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import app as tg_app
from utils.cleanup import cleanup_worker
from database import count_users

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
    return "Serena Unzip Bot v2 is running ✅"


@fastapi_app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"


# ── /stats — for Mini App live counters ──────────────────────────────────────
@fastapi_app.get("/stats")
async def stats():
    try:
        from database import count_users
        total_users = await count_users()
    except Exception:
        total_users = 0
    return {
        "users": total_users,
        "tasks": 0,   # extend with real DB counters if needed
        "files": 0,
    }


# ── Mini App — serve webapp/index.html ───────────────────────────────────────
# Accessible at: https://yourbot.onrender.com/app
@fastapi_app.get("/app", response_class=HTMLResponse)
@fastapi_app.get("/app/", response_class=HTMLResponse)
async def mini_app():
    """Telegram Mini App entry point."""
    webapp_path = BASE_DIR / "webapp" / "index.html"
    if webapp_path.exists():
        return HTMLResponse(content=webapp_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Mini App not found — upload webapp/index.html</h1>", status_code=404)


# ── Mount /webapp as static folder (CSS, JS, images if any) ──────────────────
webapp_dir = BASE_DIR / "webapp"
webapp_dir.mkdir(exist_ok=True)
fastapi_app.mount("/webapp", StaticFiles(directory=str(webapp_dir)), name="webapp")
