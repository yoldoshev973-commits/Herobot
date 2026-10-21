"""Admin amallari jurnali: kim, qachon, nimani o'zgartirgani (bloklash, import, hazil o'chirish, chetlatish...).
Barcha /api/admin/* POST so'rovlari (va shaxsiy ma'lumot ko'rish: transcript) middleware orqali avtomatik yoziladi;
Telegram bot admin buyruqlari log() orqali yoziladi. Parollar, tokenlar va uzun matnlar jurnalga tushmaydi."""
import json
import logging
from datetime import timedelta

from aiohttp import web

import database as db

logger = logging.getLogger(__name__)

SENSITIVE_GET = {"/api/admin/transcript", "/api/admin/transcript_file", "/api/admin/schedule", "/api/admin/acct_detail"}
HIDE_KEYS = {"password", "pass", "token", "text", "code", "data", "raw", "hero_password", "message", "new_pass", "new_password", "pwd", "bearer", "authorization", "secret"}
LABELS = {
    "/api/admin/extend": "Obuna muddati uzaytirildi", "/api/admin/toggle_block": "Foydalanuvchi bloklandi/blokdan chiqarildi",
    "/api/admin/backup": "Zaxira olindi", "/api/admin/import": "Zaxiradan import qilindi", "/api/admin/sync_usernames": "Username'lar yangilandi",
    "/api/admin/lessons_toggle": "Avto-yuborish yoqildi/o'chirildi", "/api/admin/schedule_refresh": "Jadval yangilandi", "/api/admin/transcript_refresh": "Transcript yangilandi",
    "/api/admin/transcript_send": "Transcript fayli yuborildi", "/api/admin/transcript_file": "Transcript fayli ko'rildi", "/api/admin/transcript": "Transcript ko'rildi",
    "/api/admin/schedule": "Dars jadvali ko'rildi", "/api/admin/acct_detail": "Akkaunt tarixi ko'rildi", "/api/admin/report_send": "Hisobot Excel yuborildi", "/api/admin/jokes_import": "Hazillar qo'shildi/almashtirildi",
    "/api/admin/jokes_update": "Hazil tahrirlandi", "/api/admin/jokes_delete": "Hazil o'chirildi", "/api/admin/jokes_send": "Hazillar fayli yuborildi",
    "/api/admin/health_fix": "Akkauntlar tuzatildi (qayta login)", "/api/admin/health_delete": "Akkaunt o'chirildi (admin, arxivga)", "/api/admin/chat_ban": "Suhbatdan chetlatildi", "/api/admin/chat_unban": "Suhbatdan chetlatish olib tashlandi",
    "/api/admin/chat_review": "Shikoyat ko'rib chiqildi",
}


async def init_table():
    async with db.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY, at TIMESTAMP DEFAULT NOW(), actor_uid INTEGER, actor TEXT, action TEXT,
                target TEXT, detail TEXT, result TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);""")
        await conn.execute("DELETE FROM audit_log WHERE at < NOW() - INTERVAL '180 days'")


async def log(actor_uid, actor, action, target="", detail="", result=""):
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("INSERT INTO audit_log (actor_uid, actor, action, target, detail, result) VALUES ($1,$2,$3,$4,$5,$6)",
                               actor_uid, str(actor or "")[:80], str(action)[:120], str(target or "")[:120], str(detail or "")[:300], str(result or "")[:200])
    except Exception as e:
        logger.warning(f"Jurnalga yozilmadi: {e}")


async def _actor(uid):
    try:
        async with db.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT telegram_id, username, full_name, login FROM app_users WHERE id=$1", int(uid))
        if r:
            return ("@" + r["username"]) if r["username"] else (r["full_name"] or r["login"] or str(r["telegram_id"]))
    except Exception:
        pass
    return f"id {uid}"


def _detail(body):
    if not isinstance(body, dict):
        return ""
    parts = []
    for k, v in body.items():
        if k in HIDE_KEYS or isinstance(v, (dict, list)) or v is None:
            continue
        s = str(v)
        if len(s) <= 60:
            parts.append(f"{k}={s}")
    return ", ".join(parts)[:300]


def make_middleware(verify_token):
    @web.middleware
    async def audit_mw(request, handler):
        path = request.path
        track = path.startswith("/api/admin/") and (request.method in ("POST", "PUT", "PATCH", "DELETE") or path in SENSITIVE_GET)
        if not track:
            return await handler(request)
        body = {}
        if request.method != "GET" and (request.content_length or 0) < 200_000:
            try:
                raw = await request.read()
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
        if not isinstance(body, dict): body = {}
        try:
            resp = await handler(request)
        except web.HTTPException as ex:   # rad etilgan/xato so'rovlar ham jurnalga tushsin
            resp = ex
        except Exception:
            resp = web.json_response({"status": "error", "message": "Server xatosi"}, status=500)
            logger.exception("Admin so'rovida kutilmagan xato")
        try:
            u_id, role = verify_token(request)
            if u_id and role == "super_admin":
                res = ""
                try:
                    j = json.loads(resp.body) if isinstance(resp, web.Response) and isinstance(resp.body, (bytes, bytearray)) else {}
                    res = (f"{j.get('status', '')}: {j.get('message', '')}".strip(": "))[:200]
                except Exception:
                    res = str(getattr(resp, "status", ""))
                q = request.query.get("email") or body.get("email") or body.get("target_id") or body.get("id") or ""
                await log(u_id, await _actor(u_id), LABELS.get(path, path), q, _detail(body) or _detail(dict(request.query)), res)
        except Exception as e:
            logger.warning(f"Audit middleware xatosi: {e}")
        return resp
    return audit_mw


async def recent(limit=200, q=""):
    async with db.pool.acquire() as conn:
        if q:
            rows = await conn.fetch("SELECT * FROM audit_log WHERE (actor ILIKE $1 OR action ILIKE $1 OR target ILIKE $1 OR detail ILIKE $1) ORDER BY id DESC LIMIT $2", f"%{q}%", int(limit))
        else:
            rows = await conn.fetch("SELECT * FROM audit_log ORDER BY id DESC LIMIT $1", int(limit))
    return [{"id": r["id"], "time": (r["at"] + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M:%S"), "actor": r["actor"], "action": r["action"],
             "target": r["target"], "detail": r["detail"], "result": r["result"]} for r in rows]
