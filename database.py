# database.py
import datetime
from typing import Dict, Any, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from config import Config

MONGO_URI = Config.MONGO_URI
USE_DB = bool(MONGO_URI) and ("localhost" not in MONGO_URI and "127.0.0.1" not in MONGO_URI)

if USE_DB:
    _client = AsyncIOMotorClient(MONGO_URI)
    db       = _client[Config.DB_NAME]
    users_col    = db["users"]
    files_col    = db["temp_files"]
    settings_col = db["user_settings"]
    referral_col = db["referrals"]
else:
    _client = users_col = files_col = settings_col = referral_col = None

# ── in-memory fallbacks ──────────────────────────────────────────────────────
_mem_users:    Dict[int, Dict[str, Any]] = {}
_mem_files:    Dict[str, Dict[str, Any]] = {}
_mem_settings: Dict[int, Dict[str, Any]] = {}
_mem_referrals: Dict[int, int] = {}   # user_id -> referrer_id


def _default_user(user_id: int) -> Dict[str, Any]:
    today = datetime.date.today().isoformat()
    return {
        "_id": user_id,
        "is_premium": False,
        "premium_until": None,
        "is_banned": False,
        "referrer_id": None,
        "referral_count": 0,
        "settings": {
            "auto_delete_min": Config.AUTO_DELETE_DEFAULT_MIN,
            "lang": "en",
            "default_extract_mode": "full",
            "preferred_output": "file",
        },
        "stats": {
            "last_reset": today,
            "daily_tasks": 0,
            "daily_size_mb": 0.0,
            "last_task_ts": None,
            "total_tasks": 0,
        },
    }


async def _safe_db(coro, default=None):
    if not USE_DB:
        return default
    try:
        return await coro
    except (PyMongoError, Exception):
        return default


# ── user helpers ─────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int) -> Dict[str, Any]:
    today = datetime.date.today().isoformat()
    user = _mem_users.get(user_id)

    if USE_DB and user is None:
        doc = await _safe_db(users_col.find_one({"_id": user_id}))
        if doc:
            user = doc
            _mem_users[user_id] = user

    if user is None:
        user = _default_user(user_id)
        _mem_users[user_id] = user
        if USE_DB:
            await _safe_db(users_col.insert_one(dict(user)))
    else:
        stats = user.get("stats", {})
        if stats.get("last_reset") != today:
            stats["last_reset"]   = today
            stats["daily_tasks"]  = 0
            stats["daily_size_mb"] = 0.0
            user["stats"] = stats
            _mem_users[user_id] = user
            if USE_DB:
                await _safe_db(users_col.update_one(
                    {"_id": user_id},
                    {"$set": {"stats.last_reset": today,
                              "stats.daily_tasks": 0,
                              "stats.daily_size_mb": 0.0}},
                ))
    return user


async def update_user_stats(user_id: int, size_mb: float):
    user = _mem_users.get(user_id) or await get_or_create_user(user_id)
    stats = user.setdefault("stats", {})
    stats["daily_tasks"]  = stats.get("daily_tasks", 0)  + 1
    stats["total_tasks"]  = stats.get("total_tasks", 0)  + 1
    stats["daily_size_mb"] = float(stats.get("daily_size_mb", 0.0)) + float(size_mb)
    stats["last_task_ts"] = datetime.datetime.utcnow()
    _mem_users[user_id] = user
    if USE_DB:
        await _safe_db(users_col.update_one(
            {"_id": user_id},
            {"$inc": {"stats.daily_tasks": 1, "stats.daily_size_mb": float(size_mb), "stats.total_tasks": 1},
             "$set": {"stats.last_task_ts": datetime.datetime.utcnow()}},
            upsert=True,
        ))


async def set_premium(user_id: int, until_ts: Optional[float]):
    user = _mem_users.get(user_id) or _default_user(user_id)
    user["is_premium"]   = until_ts is not None
    user["premium_until"] = until_ts
    _mem_users[user_id] = user
    if USE_DB:
        await _safe_db(users_col.update_one(
            {"_id": user_id},
            {"$set": {"is_premium": user["is_premium"], "premium_until": until_ts}},
            upsert=True,
        ))


async def get_premium_until(user_id: int) -> Optional[float]:
    user = _mem_users.get(user_id)
    if user:
        return user.get("premium_until")
    if USE_DB:
        doc = await _safe_db(users_col.find_one({"_id": user_id}, {"premium_until": 1}))
        if doc:
            _mem_users[user_id] = doc
            return doc.get("premium_until")
    return None


async def set_ban(user_id: int, value: bool = True):
    user = _mem_users.get(user_id) or _default_user(user_id)
    user["is_banned"] = value
    _mem_users[user_id] = user
    if USE_DB:
        await _safe_db(users_col.update_one(
            {"_id": user_id}, {"$set": {"is_banned": value}}, upsert=True
        ))


async def is_banned(user_id: int) -> bool:
    user = _mem_users.get(user_id)
    if user is not None:
        return bool(user.get("is_banned", False))
    if USE_DB:
        doc = await _safe_db(users_col.find_one({"_id": user_id}, {"is_banned": 1}))
        if doc:
            _mem_users[user_id] = doc
            return bool(doc.get("is_banned", False))
    return False


async def get_all_users() -> List[int]:
    if USE_DB:
        users = []
        cursor = await _safe_db(users_col.find({}, {"_id": 1}))
        if cursor:
            async for doc in cursor:
                users.append(doc["_id"])
                _mem_users.setdefault(doc["_id"], _default_user(doc["_id"]))
            return users
    return list(_mem_users.keys())


async def count_users():
    if USE_DB:
        total   = await _safe_db(users_col.count_documents({}), 0) or 0
        premium = await _safe_db(users_col.count_documents({"is_premium": True}), 0) or 0
        banned  = await _safe_db(users_col.count_documents({"is_banned": True}), 0) or 0
        return total, premium, banned
    total   = len(_mem_users)
    premium = sum(1 for u in _mem_users.values() if u.get("is_premium"))
    banned  = sum(1 for u in _mem_users.values() if u.get("is_banned"))
    return total, premium, banned


# ── user settings (caption/thumb/etc.) ──────────────────────────────────────

async def save_user_settings(user_id: int, data: Dict[str, Any]):
    _mem_settings[user_id] = data
    if USE_DB:
        await _safe_db(settings_col.update_one(
            {"_id": user_id}, {"$set": data}, upsert=True
        ))


async def get_user_settings(user_id: int) -> Optional[Dict[str, Any]]:
    cached = _mem_settings.get(user_id)
    if cached:
        return cached
    if USE_DB:
        doc = await _safe_db(settings_col.find_one({"_id": user_id}))
        if doc:
            doc.pop("_id", None)
            _mem_settings[user_id] = doc
            return doc
    return None


# ── referral system ──────────────────────────────────────────────────────────

async def register_referral(new_user_id: int, referrer_id: int):
    if new_user_id == referrer_id:
        return
    _mem_referrals[new_user_id] = referrer_id
    # Increment referrer count
    user = _mem_users.get(referrer_id) or await get_or_create_user(referrer_id)
    user["referral_count"] = user.get("referral_count", 0) + 1
    user["referrer_id"]    = None   # this user was not referred (they referred someone)
    _mem_users[referrer_id] = user
    if USE_DB:
        await _safe_db(referral_col.update_one(
            {"_id": new_user_id}, {"$set": {"referrer": referrer_id}}, upsert=True
        ))
        await _safe_db(users_col.update_one(
            {"_id": referrer_id}, {"$inc": {"referral_count": 1}}, upsert=True
        ))


async def get_referral_count(user_id: int) -> int:
    user = _mem_users.get(user_id)
    if user:
        return user.get("referral_count", 0)
    if USE_DB:
        doc = await _safe_db(users_col.find_one({"_id": user_id}, {"referral_count": 1}))
        if doc:
            return doc.get("referral_count", 0)
    return 0


async def has_been_referred(user_id: int) -> bool:
    if user_id in _mem_referrals:
        return True
    if USE_DB:
        doc = await _safe_db(referral_col.find_one({"_id": user_id}))
        return doc is not None
    return False


# ── temp files helpers (for cleanup_worker) ──────────────────────────────────

async def register_temp_path(user_id: int, path: str, ttl_min: int):
    now = datetime.datetime.utcnow()
    _mem_files[path] = {"user_id": user_id, "path": path, "created_at": now, "ttl_min": ttl_min}
    if USE_DB:
        await _safe_db(files_col.insert_one(
            {"user_id": user_id, "path": path, "created_at": now, "ttl_min": ttl_min}
        ))


async def get_expired_temp_paths(now: Optional[datetime.datetime] = None):
    if now is None:
        now = datetime.datetime.utcnow()
    expired = []
    for p, info in list(_mem_files.items()):
        if info["created_at"] + datetime.timedelta(minutes=info["ttl_min"]) <= now:
            expired.append(p)
            _mem_files.pop(p, None)
    if USE_DB:
        cursor = await _safe_db(files_col.find({}))
        remove_ids = []
        if cursor:
            async for doc in cursor:
                created = doc.get("created_at")
                ttl_min = doc.get("ttl_min", Config.AUTO_DELETE_DEFAULT_MIN)
                if created and created + datetime.timedelta(minutes=ttl_min) <= now:
                    expired.append(doc.get("path"))
                    remove_ids.append(doc["_id"])
        if remove_ids:
            await _safe_db(files_col.delete_many({"_id": {"$in": remove_ids}}))
    return list({p for p in expired if p})
