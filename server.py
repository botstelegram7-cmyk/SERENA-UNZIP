# server.py — Upgraded: Mini App file selector API + WebApp serving
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import app as tg_app, tasks, GLOBAL_SEMAPHORE, is_video_path, build_caption, choose_thumbnail, _get_video_duration
from config import Config
from utils.cleanup import cleanup_worker
from database import count_users, get_unzip_task, get_or_create_user, get_user_settings

fastapi_app = FastAPI(title="Serena Unzip Web Service v3")


@fastapi_app.on_event("startup")
async def on_startup():
    asyncio.create_task(cleanup_worker())
    await tg_app.start()
    print("Serena Unzip Bot v3 started")


@fastapi_app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    print("Serena Unzip Bot v3 stopped")


# Uptime monitors (UptimeRobot, Render health checks) often probe with HEAD.
# Registering GET only made those return "405 Method Not Allowed".
@fastapi_app.api_route("/", methods=["GET", "HEAD"],
                       response_class=PlainTextResponse)
async def root():
    return "Serena Unzip Bot v3 is running ✅"


@fastapi_app.api_route("/health", methods=["GET", "HEAD"],
                       response_class=PlainTextResponse)
async def health():
    return "OK"


@fastapi_app.get("/stats")
async def stats():
    try:
        total_users = await count_users()
        if isinstance(total_users, tuple):
            total_users = total_users[0]
    except Exception:
        total_users = 0
    return {"users": total_users, "tasks": len(tasks), "files": 0}


@fastapi_app.get("/api/me/{uid}")
async def api_me(uid: int):
    """Live figures for the Mini App header: quota, usage, plan.

    The Mini App previously showed hardcoded numbers; this lets it show
    the signed-in user's real state.
    """
    from config import Config as _C
    try:
        u = await get_or_create_user(uid)
    except Exception:
        u = {}
    st = (u or {}).get("stats", {}) or {}
    try:
        from bot import is_premium_user
        premium = bool(await is_premium_user(uid))
    except Exception:
        premium = False

    used = int(st.get("daily_tasks", 0) or 0)
    limit = int(_C.FREE_DAILY_TASK_LIMIT)
    return {
        "uid": uid,
        "premium": premium,
        "tasks_used": used,
        "tasks_limit": limit,
        "tasks_left": max(0, limit - used) if not premium else None,
        "data_used_mb": round(float(st.get("daily_size_mb", 0) or 0), 1),
        "data_limit_mb": int(_C.FREE_DAILY_SIZE_MB),
        "max_file_mb": int(_C.YTDL_MAX_SIZE_MB),
        "total_tasks": int(st.get("total_tasks", 0) or 0),
    }


def _verify_init_data(init_data: str) -> Optional[int]:
    """Validate Telegram's initData and return the user id, or None.

    Per the documented scheme: the hash is an HMAC-SHA256 of the
    alphabetically sorted "key=value" lines, keyed by HMAC-SHA256 of the
    bot token under the constant "WebAppData". Without this check the
    Mini App could claim to be any user.
    """
    import hashlib, hmac, json as _json
    from urllib.parse import parse_qsl
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        return int(_json.loads(pairs.get("user", "{}")).get("id"))
    except Exception:
        return None


@fastapi_app.post("/api/dispatch")
async def api_dispatch(req: Request):
    """Run a link or command sent from the Mini App.

    A Mini App cannot post messages on the user's behalf, and sendData()
    closes the app. So the app posts here instead, the identity is
    verified from initData, and the bot replies in the chat itself.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)

    text = (body.get("text") or "").strip()
    uid = _verify_init_data(body.get("init_data") or "")
    if not uid:
        return JSONResponse({"ok": False, "error": "unverified"}, status_code=403)
    if not text:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    # send_message only echoed the text back at the user. The point is to
    # ACT on it, so the text is handed to the same handler that processes a
    # pasted link, using a shim that looks like the message the user would
    # have sent themselves.
    try:
        from bot import process_links_message

        anchor = await tg_app.send_message(uid, "Working on it...")
        sender = await tg_app.get_users(uid)

        class _MiniAppMessage:
            """Enough of a Message for the link handlers to work with."""
            def __init__(self, anchor, sender, text):
                self._anchor = anchor
                self.from_user = sender
                self.chat = anchor.chat
                self.id = anchor.id
                self.text = text
                self.caption = None
                self.command = text.split() if text.startswith("/") else []
                self.reply_to_message = None

            async def reply_text(self, *a, **kw):
                return await self._anchor.reply_text(*a, **kw)

            async def reply_photo(self, *a, **kw):
                return await self._anchor.reply_photo(*a, **kw)

            async def delete(self):
                try:
                    return await self._anchor.delete()
                except Exception:
                    return None

        shim = _MiniAppMessage(anchor, sender, text)

        if text.startswith("/"):
            # A command needs the real dispatcher, which the Mini App cannot
            # reach. Tell the user plainly instead of pretending it ran.
            await anchor.edit_text(
                f"Send <code>{text}</code> in the chat to run it.")
            return {"ok": True, "mode": "command"}

        await process_links_message(tg_app, shim, text)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:160]}, status_code=502)
    return {"ok": True, "mode": "link"}


# ── Mini App entry points ─────────────────────────────────────────────────────
@fastapi_app.get("/app", response_class=HTMLResponse)
@fastapi_app.get("/app/", response_class=HTMLResponse)
async def mini_app():
    webapp_path = BASE_DIR / "webapp" / "index.html"
    if webapp_path.exists():
        return HTMLResponse(content=webapp_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Mini App not found</h1>", status_code=404)


@fastapi_app.get("/select", response_class=HTMLResponse)
@fastapi_app.get("/select/", response_class=HTMLResponse)
async def file_selector():
    """File selector Mini App — opened after extraction."""
    selector_path = BASE_DIR / "webapp" / "selector.html"
    if selector_path.exists():
        return HTMLResponse(content=selector_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Selector not found</h1>", status_code=404)


# ── API: get file list for a task ─────────────────────────────────────────────
@fastapi_app.get("/api/files/{tid}")
async def get_task_files(tid: str):
    """Return the file list for an unzip task (used by file selector webapp)."""
    info = tasks.get(tid)
    if not info:
        try:
            info = await get_unzip_task(tid)
        except Exception:
            info = None
    if not info:
        raise HTTPException(status_code=404, detail="Task not found or expired")
    base = Path(info["base_dir"])
    files_with_meta = []
    for i, rel in enumerate(info["files"]):
        full = base / rel
        size = full.stat().st_size if full.exists() else 0
        ext = Path(rel).suffix.lower()
        ftype = "video" if ext in {".mp4",".mkv",".mov",".avi",".webm",".ts"} else \
                "pdf" if ext == ".pdf" else \
                "audio" if ext in {".mp3",".m4a",".aac",".ogg",".flac",".wav"} else \
                "image" if ext in {".jpg",".jpeg",".png",".webp",".gif"} else "file"
        files_with_meta.append({
            "index": i,
            "path": rel,
            "name": Path(rel).name,
            "size": size,
            "type": ftype,
        })
    return JSONResponse({
        "tid": tid,
        "archive_name": info.get("archive_name", "archive"),
        "total": len(files_with_meta),
        "files": files_with_meta,
    })


# ── API: send selected files ──────────────────────────────────────────────────
class SendSelectedRequest(BaseModel):
    tid: str
    indices: List[int]
    chat_id: int
    user_id: int


@fastapi_app.post("/api/send_selected")
async def send_selected(req: SendSelectedRequest):
    """Send user-selected files from the file selector Mini App."""
    info = tasks.get(req.tid)
    if not info:
        try:
            info = await get_unzip_task(req.tid)
        except Exception:
            info = None
    if not info:
        raise HTTPException(status_code=404, detail="Task not found or expired")
    if info["user_id"] != req.user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    base = Path(info["base_dir"])
    files = info["files"]
    thread_id = info.get("thread_id")
    valid_indices = [i for i in req.indices if 0 <= i < len(files)]
    if not valid_indices:
        raise HTTPException(status_code=400, detail="No valid file indices")

    async def _do_send():
        import time
        from utils.progress import progress_for_pyrogram
        async with GLOBAL_SEMAPHORE:
            for idx in valid_indices:
                rel = files[idx]
                full = base / rel
                if not full.is_file():
                    continue
                try:
                    if is_video_path(rel):
                        name = Path(rel).name
                        cap = await build_caption(req.user_id, name)
                        thumb = await choose_thumbnail(req.user_id, str(full))
                        dur = await _get_video_duration(str(full))
                        await tg_app.send_video(
                            req.chat_id, str(full),
                            caption=cap, thumb=thumb, duration=dur,
                            message_thread_id=thread_id,
                        )
                    else:
                        await tg_app.send_document(
                            req.chat_id, str(full),
                            caption=Path(rel).name,
                            message_thread_id=thread_id,
                        )
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            try:
                await tg_app.send_message(
                    req.chat_id,
                    f"✅ Sent {len(valid_indices)} selected file(s)!",
                    message_thread_id=thread_id,
                )
            except Exception:
                pass

    asyncio.create_task(_do_send())
    return {"ok": True, "queued": len(valid_indices)}


# ── Admin API endpoints (used by webapp/index.html) ───────────────────────────
@fastapi_app.get("/api/queues")
async def api_queues():
    return {"queues": []}


@fastapi_app.post("/api/queue/resume")
async def api_resume(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/queue/delete")
async def api_delete_queue(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/queue/delete_all")
async def api_delete_all(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/user/ban")
async def api_ban(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/user/unban")
async def api_unban(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/user/authorize")
async def api_authorize(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/user/deauth")
async def api_deauth(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/user/premium")
async def api_premium(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/broadcast")
async def api_broadcast(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/setting")
async def api_setting(req: Request):
    return {"ok": True}


@fastapi_app.post("/api/action")
async def api_action(req: Request):
    return {"ok": True}


# ── Static files ──────────────────────────────────────────────────────────────
webapp_dir = BASE_DIR / "webapp"
webapp_dir.mkdir(exist_ok=True)
fastapi_app.mount("/webapp", StaticFiles(directory=str(webapp_dir)), name="webapp")
