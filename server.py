# server.py — Upgraded: Mini App file selector API + WebApp serving
import asyncio
import sys
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import app as tg_app, tasks, GLOBAL_SEMAPHORE, is_video_path, build_caption, choose_thumbnail, _get_video_duration
from utils.cleanup import cleanup_worker
from database import count_users, get_unzip_task

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


@fastapi_app.get("/", response_class=PlainTextResponse)
async def root():
    return "Serena Unzip Bot v3 is running ✅"


@fastapi_app.get("/health", response_class=PlainTextResponse)
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
