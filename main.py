import asyncio
import os
import logging
import time
import random
import json
import re
import html as _html
import secrets
import jwt
from urllib.parse import urlparse, unquote, parse_qs
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, Message, Update, BufferedInputFile, CallbackQuery, BotCommand, BotCommandScopeChat, ReplyKeyboardRemove
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from datetime import datetime, timedelta
import database as db
import students
import hero_schedule
import hero_transcript
import jokes
import friends
import notify
import stats
import audit
import extras
import i18n

TASHKENT_OFFSET = timedelta(hours=5)   # Toshkent = UTC+5 (yozgi vaqtsiz, doimiy)

def tashkent_now():
    """Hozirgi Toshkent vaqti (server UTC'da ishlaydi, shuning uchun +5 soat qo'shamiz)."""
    return datetime.utcnow() + TASHKENT_OFFSET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0) 
raw_url = os.getenv("WEBAPP_URL", "").rstrip('/')
WEBAPP_URL = f"https://{raw_url.replace('https://', '').replace('http://', '')}" if raw_url else ""
PORT = int(os.getenv("PORT") or 8080)

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.warning(f"⚠️ DIQQAT! JWT_SECRET topilmadi. Vaqtinchalik tasodifiy kalit ishlatilmoqda (qayta ishga tushirilganda barcha tokenlar bekor bo'ladi). .env faylga shuni qo'shing:\nJWT_SECRET={JWT_SECRET}")

# Faqat shu domenlarga chiquvchi so'rovlarga ruxsat beriladi (SSRF va parolni tashqariga oqizishning oldini olish uchun)
ALLOWED_SCAN_HOST_SUFFIX = "hero.study"

def is_allowed_scan_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return bool(host) and (host == ALLOWED_SCAN_HOST_SUFFIX or host.endswith("." + ALLOWED_SCAN_HOST_SUFFIX))

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
]

HERO_NET_ERR = "Hero serveriga ulanib bo'lmadi."
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "your_bot").lstrip("@")

def _timeout(sec): return aiohttp.ClientTimeout(total=sec)

def get_safe_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://newuzbekistan.hero.study",
        "Referer": "https://newuzbekistan.hero.study/",
        "Connection": "keep-alive"
    }

# JWT Token tekshiruvchisi
ADMIN_USER_IDS = set()   # adminlarning app_users.id lari (panel uchun)
ADMIN_TG_IDS = set()     # adminlarning Telegram ID lari (bot buyruqlari uchun)
_admins_loaded = False
BLOCKED_USER_IDS = set() # bloklangan foydalanuvchilarning app_users.id lari
_blocked_loaded = False

async def refresh_admins():
    """Adminlar ro'yxatini bazadan qayta o'qiydi. Bosh admin (ADMIN_ID) doim ro'yxatda."""
    global ADMIN_USER_IDS, ADMIN_TG_IDS, _admins_loaded
    rows = await db.get_admin_rows(ADMIN_ID)
    ADMIN_USER_IDS = {r["id"] for r in rows}
    ADMIN_TG_IDS = {r["telegram_id"] for r in rows} | ({ADMIN_ID} if ADMIN_ID else set())
    _admins_loaded = True
    logger.info(f"👑 Adminlar: {len(ADMIN_TG_IDS)} ta")

async def refresh_blocked_users():
    """Bloklangan foydalanuvchilar ro'yxatini bazadan qayta o'qiydi."""
    global BLOCKED_USER_IDS, _blocked_loaded
    BLOCKED_USER_IDS = set(await db.get_blocked_ids())
    _blocked_loaded = True
    logger.info(f"🚫 Bloklangan foydalanuvchilar: {len(BLOCKED_USER_IDS)} ta")

def verify_token(request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "): return None, None
    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        u_id = payload.get("user_id")
        if _blocked_loaded and u_id in BLOCKED_USER_IDS: return None, None   # bloklangan — eski token ham darhol kuchini yo'qotadi
        # Rolni tokendan emas, JORIY adminlar ro'yxatidan olamiz: admin huquqi olib tashlansa, eski token ham darhol kuchini yo'qotadi.
        role = ("super_admin" if u_id in ADMIN_USER_IDS else "user") if _admins_loaded else payload.get("role")
        return u_id, role
    except: return None, None

async def verify_hero_account(email, password):
    login_url = "https://api.newuzbekistan.hero.study/v1/users/login?lang=en"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=True)) as session:
            payload = {"email": email, "pass": password, "remember": "", "clientToken": ""}
            async with session.post(login_url, json=payload, headers=get_safe_headers(), timeout=_timeout(15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("token") or data.get("access_token") or (data.get("data", {}) if isinstance(data.get("data"), dict) else {}).get("token")
                    if token: return True, token
                return False, f"Hero tizimida xato. Status: {resp.status}"
    except Exception as e:
        logger.warning(f"Hero login xatosi ({email}): {e}")
        return False, HERO_NET_ERR

_LESSON_KEYS = ("lesson_name", "lessonName", "dars_nomi", "darsNomi", "quiz_name", "quizName",
                "title", "name", "topic", "subject", "course_name", "courseName")
_LESSON_CONTAINERS = ("data", "quiz", "lesson", "dars", "result", "attendance", "check", "item")

def _find_lesson_in(obj, depth=0):
    if depth > 2 or not isinstance(obj, dict): return None
    for k in _LESSON_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()[:160]
    for k in _LESSON_CONTAINERS:
        v = obj.get(k)
        if isinstance(v, dict):
            found = _find_lesson_in(v, depth + 1)
            if found: return found
    return None

async def _extract_lesson(resp, qr_url):
    """Hero.study QR-tekshiruv javobidan dars nomini topishga urinadi. Aniq maydon topilmasa,
    URL yo'lidan foydali bo'lak ajratib olinadi (kamida darslarni bir-biridan ajratib bo'lsin) va
    xom javob Railway logiga yoziladi — kelajakda aniqroq moslashtirish uchun."""
    try:
        payload = await resp.json(content_type=None)
        name = _find_lesson_in(payload) if isinstance(payload, dict) else None
        if name: return name
        logger.info(f"🔍 Dars nomi topilmadi (maydon moslashtirish kerak), xom javob: {json.dumps(payload, ensure_ascii=False)[:300]}")
    except Exception as e:
        logger.info(f"🔍 QR javobini o'qib bo'lmadi ({e}); URL: {qr_url[:150]}")
    return _lesson_from_url(qr_url)

_URL_ID_PARAMS = ("lesson", "lesson_id", "quiz", "quiz_id", "code", "session", "session_id", "topic")
_GENERIC_PATH_WORDS = {"scan", "check", "verify", "mark", "attend", "attendance", "confirm", "submit",
                       "qr", "validate", "log", "v1", "v2", "api", "lang", "en", "uz", "ru"}

def _lesson_from_url(url):
    """QR javobida ism topilmasa, URL'dan foydali bo'lak ajratib olamiz — 'check'/'scan' kabi umumiy
    REST so'zlarini emas, aynan darsni ajratib turadigan qismni (query param yoki path segment)."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in _URL_ID_PARAMS:
            vals = qs.get(key)
            if vals and vals[0].strip(): return unquote(vals[0]).strip()[:80]
        segs = [unquote(s) for s in parsed.path.strip("/").split("/") if s]
        for seg in reversed(segs):
            if seg.lower() in _GENERIC_PATH_WORDS or seg.isdigit() and len(seg) <= 2: continue
            cleaned = re.sub(r"[-_]+", " ", seg).strip()
            if cleaned: return cleaned[:80]
        return unquote(segs[-1])[:80] if segs else None   # hech biri mos kelmasa, oxirgisini baribir qaytaramiz
    except Exception:
        return None

_qr_lesson = {}    # qr_url -> dars nomi (muvaffaqiyatsiz urinishda ham tarixda dars nomi ko'rinsin)
SCAN_ATTEMPTS = 3  # har bir akkaunt uchun: birinchi urinish + 2 ta qayta urinish

def _why_lines(items, lg):
    """Muvaffaqiyatsiz akkauntlar sabablari bo'yicha qisqa tushuntirish (foydalanuvchi tilida)."""
    c = {}
    for i in items:
        if not i["ok"] and i.get("reason"): c[i["reason"]] = c.get(i["reason"], 0) + 1
    out = []
    if c.get("not_in_lesson"):
        all_failed_same = not any(i["ok"] for i in items) and c["not_in_lesson"] == sum(c.values())
        out.append(i18n.t(lg, "why_all_not_in") if all_failed_same else i18n.t(lg, "why_not_in", n=c["not_in_lesson"]))
    if c.get("token") or c.get("login"): out.append(i18n.t(lg, "why_token", n=c.get("token", 0) + c.get("login", 0)))
    if c.get("hero") or c.get("network"): out.append(i18n.t(lg, "why_hero", n=c.get("hero", 0) + c.get("network", 0)))
    return out

async def _token_probe(token):
    """Token haqiqatan ishlayaptimi? Hero jadval endpointiga yengil so'rov: True — ishlayapti; False — yaroqsiz (401/403);
    None — aniqlab bo'lmadi (tarmoq/Hero xatosi)."""
    try:
        st, _, err, *_ = await hero_schedule.fetch_schedule(token, get_safe_headers())
    except Exception:
        return None
    if err is None and st == 200: return True
    if err == "TOKEN_EXPIRED": return False
    return None

async def scan_task(session, acc, qr_url, db_callback):
    """Bitta akkauntni QR bilan darsga kiritadi. Muvaffaqiyatsiz bo'lsa qayta urinadi (har safar biroz kutib).
    Har bir xatoda TOKEN TEKSHIRILADI: token ishlayotgan bo'lsa-yu Hero baribir rad etsa — bu dars shu akkauntning jadvalida yo'q
    ("not_in_lesson"); token ishlamasa — "token"; Hero/tarmoq xatosi bo'lsa — "hero"/"network"."""
    email = acc.get('email')
    token = acc.get('bearer_token')
    if not _token_ok(token):
        return {"email": email, "ok": False, "msg": "Nofaol akkaunt", "reason": "token"}
    relogged, status, valid = False, 0, None
    for attempt in range(SCAN_ATTEMPTS):
        status = 0
        try:
            await asyncio.sleep(random.uniform(0.1, 0.5) if attempt == 0 else random.uniform(1.0, 2.0) * attempt)
            headers = get_safe_headers(); headers["Authorization"] = f"Bearer {token}"
            async with session.get(qr_url, headers=headers, timeout=_timeout(12)) as rp:
                status = rp.status
                if status in (200, 201):
                    lesson = await _extract_lesson(rp, qr_url)
                    if lesson: _qr_lesson[qr_url] = lesson
                    return {"email": email, "ok": True, "lesson": lesson, "attempts": attempt + 1}
                body = (await rp.text())[:160].replace("\n", " ")
                if status == 429:
                    try: await asyncio.sleep(min(float(rp.headers.get("Retry-After", 2)), 6))
                    except Exception: await asyncio.sleep(2)
            logger.warning(f"Skaner {email}: urinish {attempt + 1}/{SCAN_ATTEMPTS} → HTTP {status} {body}")
        except Exception as e:
            logger.warning(f"Skaner xatosi ({email}) urinish {attempt + 1}/{SCAN_ATTEMPTS}: {e}")
        if attempt == SCAN_ATTEMPTS - 1: break
        client_err = status in (401, 403) or (400 <= status < 500 and status not in (408, 429))
        if client_err:
            valid = await _token_probe(token)
            if valid is True: break          # token ishlayapti — Hero darsga bog'liq sabab bilan rad etyapti, qayta urinish foydasiz
        # token yaroqsiz bo'lsa (yoki bir marta bo'sh ketgan bo'lsa) — parol bilan yangi token olamiz (faqat bir marta)
        if not relogged and (valid is False or status in (401, 403) or (attempt >= 1 and client_err)):
            ok_login, new_token = await verify_hero_account(email, acc.get('hero_password'))
            if ok_login:
                token, relogged, valid = new_token, True, None
                if db_callback: await db_callback(email, new_token)
                _spawn_schedule(email, new_token, acc.get('hero_password'))
            elif valid is False or status in (401, 403):
                if new_token != HERO_NET_ERR and db_callback:   # tarmoq uzilishi akkauntni nofaol qilib qo'ymasin
                    await db_callback(email, "ERROR:LOGIN_FAILED")
                net = new_token == HERO_NET_ERR
                return {"email": email, "ok": False, "reason": "network" if net else "login",
                        "msg": "Hero bilan ulanib bo'lmadi" if net else "Token ishlamayapti: login/parol xato"}
    # --- barcha urinishlar tugadi: sababini aniqlaymiz
    if status == 0:
        return {"email": email, "ok": False, "reason": "network", "msg": f"Hero bilan ulanib bo'lmadi · {SCAN_ATTEMPTS} marta urinildi"}
    if status >= 500 or status in (408, 429):
        return {"email": email, "ok": False, "reason": "hero", "msg": f"Hero serveri javob bermadi (HTTP {status}) · {SCAN_ATTEMPTS} marta urinildi"}
    if valid is None: valid = await _token_probe(token)
    if valid is True:
        return {"email": email, "ok": False, "reason": "not_in_lesson", "msg": f"Token ishlayapti, lekin bu dars akkaunt jadvalida yo'q (Hero: HTTP {status})"}
    if valid is False:
        return {"email": email, "ok": False, "reason": "token", "msg": f"Token ishlamayapti (HTTP {status})"}
    return {"email": email, "ok": False, "reason": "unknown", "msg": f"Hero javobi: HTTP {status} · token holatini aniqlab bo'lmadi"}

async def mark_all_accounts_smart(accounts: list, qr_url: str, db_update_func):
    start_time = time.time()
    active_accounts = [acc for acc in accounts if acc.get('bearer_token') and acc.get('bearer_token') != "NO_TOKEN" and not str(acc.get('bearer_token')).startswith("ERROR")]
    if not active_accounts: return 0, 0, 0.0, [{"email": "Barcha akkauntlar nofaol", "ok": False}], None
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=True)) as session:
        tasks = [scan_task(session, acc, qr_url, db_update_func) for acc in active_accounts]
        results = await asyncio.gather(*tasks)
    lesson = next((r["lesson"] for r in results if r.get("lesson")), None)
    return (sum(1 for r in results if r['ok']), len(active_accounts), round(time.time() - start_time, 2),
            [{"email": r['email'], "ok": r['ok'], "msg": r.get("msg"), "reason": r.get("reason")} for r in results], lesson)


def person_label(name, student_id, email):
    """'Ism Familya (ID)' ko'rinishi; ro'yxatda topilmasa — email."""
    if name: return f"{name} ({student_id})" if student_id else name
    return f"{email} (ro'yxatda topilmadi)"

def _fmt_people(items, limit=40):
    lines = [f"• {i['label']}" for i in items[:limit]]
    if len(items) > limit: lines.append(f"… va yana {len(items) - limit} ta")
    return "\n".join(lines)

TOKEN_HEAL_INTERVAL = 7 * 24 * 3600  # 1 hafta

_sync_lock = None

async def sync_usernames():
    """Har bir foydalanuvchining HOZIRGI @username va ismini Telegram'dan (getChat) so'rab, bazaga yozadi.
    Foydalanuvchi /start bosishi shart emas. Parollarga umuman tegmaydi.
    Qaytaradi: statistika dict yoki None (bot yo'q / allaqachon ishlayapti)."""
    global _sync_lock
    if _sync_lock is None: _sync_lock = asyncio.Lock()
    if not bot or _sync_lock.locked(): return None
    async with _sync_lock:
        st = {"total": 0, "with_username": 0, "name_only": 0, "unreachable": 0, "changed": 0}
        for u in await db.get_users_for_sync():
            st["total"] += 1
            chat = None
            for attempt in range(2):
                try:
                    chat = await bot.get_chat(u["telegram_id"]); break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except (TelegramForbiddenError, TelegramBadRequest):
                    break  # botni bloklagan yoki chat topilmadi
                except Exception as e:
                    logger.warning(f"getChat xatosi ({u['telegram_id']}): {e}"); break
            if chat is None:
                st["unreachable"] += 1
            else:
                username = chat.username or None
                full_name = " ".join(x for x in (chat.first_name, chat.last_name) if x) or None
                st["with_username" if username else "name_only"] += 1
                if username != u.get("username") or full_name != u.get("full_name"):
                    await db.sync_user_profile(u["telegram_id"], username, full_name); st["changed"] += 1
            await asyncio.sleep(0.1)  # Telegram limitiga yetmaslik uchun
        return st

async def username_sync_loop():
    """Ishga tushgandan 2 daqiqa o'tib bir marta, keyin har 24 soatda usernamelarni yangilaydi."""
    await asyncio.sleep(120)
    while True:
        try:
            st = await sync_usernames()
            if st: logger.info(f"👤 Username sinxronlash: jami {st['total']}, @username bor {st['with_username']}, faqat ism {st['name_only']}, yetib bo'lmadi {st['unreachable']}, yangilandi {st['changed']}")
        except Exception as e:
            logger.error(f"Username sinxronlash xatosi: {e}")
        await asyncio.sleep(24 * 3600)

async def admin_sync_usernames(request):
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not bot: return web.json_response({"status": "error", "message": "Bot sozlanmagan (BOT_TOKEN yo'q)"})
    st = await sync_usernames()
    if st is None: return web.json_response({"status": "error", "message": "Yangilash allaqachon ketyapti, biroz kuting"})
    msg = (f"✅ {st['total']} ta foydalanuvchi tekshirildi: {st['with_username']} ta @username, "
           f"{st['name_only']} ta faqat ism (username qo'ymagan), {st['unreachable']} ta yetib bo'lmadi (botni bloklagan).")
    return web.json_response({"status": "success", "message": msg, "stats": st})

async def token_healer_loop():
    """Fonda doimiy ishlaydi: bearer_token yo'q/ERROR/NO_TOKEN bo'lgan (ya'ni admin panelda 'Nofaol'
    ko'rinayotgan) akkauntlarni topib, Hero'ga qayta login qilib, tokenini o'zi yangilab qo'yadi.
    Shu tufayli foydalanuvchi qo'lda 🔄 tugmasini bosishiga hojat qolmaydi.
    Faqat HAFTASIGA BIR MARTA ishlaydi, va topilgan akkauntlarni birdaniga emas — turli (tasodifiy)
    vaqt oralig'ida, sekin-asta yangilaydi (Hero serveriga shubhali ko'rinmaslik uchun)."""
    await asyncio.sleep(90)  # server to'liq ishga tushguncha kutamiz
    while True:
        try:
            accounts = await db.get_all_hero_accounts_raw()
            broken = [a for a in accounts if not a.get('bearer_token') or a['bearer_token'] == "NO_TOKEN" or str(a['bearer_token']).startswith("ERROR")]
            if broken:
                logger.info(f"🔄 Token-healer (haftalik): {len(broken)} ta nofaol akkaunt topildi, kun davomida sekin-asta tiklanadi...")
            for acc in broken:
                try:
                    is_valid, result = await verify_hero_account(acc['email'], acc['hero_password'])
                    new_token = result if is_valid else "ERROR:LOGIN_FAILED"
                    await db.update_bearer_token(acc['email'], new_token)
                    if is_valid:
                        logger.info(f"✅ Token tiklandi: {acc['email']}")
                        _spawn_schedule(acc['email'], new_token, acc['hero_password'])
                except Exception as e:
                    logger.error(f"Token-healer xatosi ({acc.get('email')}): {e}")
                # Keyingi akkauntgacha 3-20 daqiqa tasodifiy kutamiz — hammasi birdaniga emas, kun davomida tarqoq bo'ladi
                await asyncio.sleep(random.uniform(180, 1200))
        except Exception as e:
            logger.error(f"Token-healer tsikli xatosi: {e}")
        await asyncio.sleep(TOKEN_HEAL_INTERVAL)  # keyingi tekshiruv — 1 haftadan keyin

# --- DARS JADVALI ---
SCHEDULE_SYNC_INTERVAL = 7 * 24 * 3600   # haftada bir marta hamma akkaunt jadvali yangilanadi
_sched_sem = None
_sched_all_lock = None

def _token_ok(t): return bool(t) and t != "NO_TOKEN" and not str(t).startswith("ERROR")

async def refresh_schedule(email, token=None, password=None):
    """Bitta akkaunt uchun dars jadvalini Hero'dan olib bazaga yozadi. Token eskirgan (401/403) yoki yo'q bo'lsa —
    parol bilan qayta login qilib, YANGI tokenni ham saqlaydi. Qaytaradi: (ok, xabar, darslar_soni)."""
    global _sched_sem
    if _sched_sem is None: _sched_sem = asyncio.Semaphore(3)
    async with _sched_sem:
        try:
            if not _token_ok(token):
                if not password:
                    await db.save_schedule_error(email, "Token yo'q"); return False, "Token yo'q", 0
                ok, res = await verify_hero_account(email, password)
                if not ok:
                    await db.save_schedule_error(email, f"Login: {res}"); return False, f"Login: {res}", 0
                token = res; await db.update_bearer_token(email, token)
            _, data, err, b, e = await hero_schedule.fetch_schedule(token, get_safe_headers())
            if err == "TOKEN_EXPIRED" and password:
                ok, res = await verify_hero_account(email, password)
                if ok:
                    token = res; await db.update_bearer_token(email, token)
                    _, data, err, b, e = await hero_schedule.fetch_schedule(token, get_safe_headers())
            if err:
                if err == "TOKEN_EXPIRED": err = "Token yaroqsiz (401/403)"
                await db.save_schedule_error(email, err); return False, err, 0
            lessons = hero_schedule.normalize(data)
            await db.save_schedule_ok(email, data, b, e, len(lessons))
            if not lessons:
                logger.info(f"📅 Jadval bo'sh yoki maydonlar mos kelmadi ({email}), xom javob: {json.dumps(data, ensure_ascii=False)[:300]}")
            return True, "OK", len(lessons)
        except Exception as ex:
            logger.error(f"Jadval yangilash xatosi ({email}): {ex}")
            try: await db.save_schedule_error(email, str(ex))
            except Exception: pass
            return False, str(ex)[:200], 0

def _spawn_schedule(email, token, password=None):
    """Token yangi olinganda jadvalni fonda yangilaydi (asosiy so'rovni kutdirmaydi)."""
    t = asyncio.create_task(refresh_schedule(email, token, password))
    _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)

# --- TRANSCRIPT ---
_tr_sem = None

async def _transcript_token(email, token, password):
    """Yaroqli token qaytaradi (yo'q/eskirgan bo'lsa parol bilan qayta login). -> (token, xato)"""
    if _token_ok(token): return token, None
    if not password: return None, "Token yo'q"
    ok, res = await verify_hero_account(email, password)
    if not ok: return None, f"Login: {res}"
    await db.update_bearer_token(email, res)
    return res, None

async def refresh_transcript(email, token=None, password=None):
    """Bitta akkaunt transcriptini token orqali Hero'dan olib bazaga yozadi. Token eskirgan (401/403) bo'lsa — parol bilan
    qayta login qilib yangi tokenni saqlaydi. Qaytaradi: (ok, xabar, fanlar_soni)."""
    global _tr_sem
    if _tr_sem is None: _tr_sem = asyncio.Semaphore(3)
    async with _tr_sem:
        try:
            token, err = await _transcript_token(email, token, password)
            if err:
                await db.save_transcript_error(email, err); return False, err, 0
            _, kind, data, ctype, err = await hero_transcript.fetch_transcript(token, get_safe_headers())
            if err == "TOKEN_EXPIRED" and password:
                ok, res = await verify_hero_account(email, password)
                if ok:
                    token = res; await db.update_bearer_token(email, token)
                    _, kind, data, ctype, err = await hero_transcript.fetch_transcript(token, get_safe_headers())
            if err:
                if err == "TOKEN_EXPIRED": err = "Token yaroqsiz (401/403)"
                await db.save_transcript_error(email, err); return False, err, 0
            n = hero_transcript.normalize(data).get("courses_count", 0) if kind == "json" else 0
            old_row = await db.get_transcript(email) if kind == "json" else None
            await db.save_transcript_ok(email, data, ctype, n)
            if old_row and old_row.get("raw") is not None:   # yangi baho bo'lsa egasiga xabar (birinchi olishda xabar yuborilmaydi)
                _bg(notify.grade_alert(email, db._jsonish(old_row["raw"]), data))
            if kind == "json" and not n:
                logger.info(f"🎓 Transcript bo'sh yoki maydonlar mos kelmadi ({email}), xom javob: {json.dumps(data, ensure_ascii=False)[:300]}")
            return True, "OK", n
        except Exception as ex:
            logger.error(f"Transcript yangilash xatosi ({email}): {ex}")
            try: await db.save_transcript_error(email, str(ex))
            except Exception: pass
            return False, str(ex)[:200], 0

async def refresh_all_schedules():
    """Barcha noyob akkauntlar jadvalini ketma-ket (Hero'ga yuk tushmasligi uchun sekin) yangilaydi. None — allaqachon ketyapti."""
    global _sched_all_lock
    if _sched_all_lock is None: _sched_all_lock = asyncio.Lock()
    if _sched_all_lock.locked(): return None
    async with _sched_all_lock:
        ok = fail = skipped = 0
        seen = set()
        for a in await db.get_all_hero_accounts_raw():
            if a['email'] in seen: continue
            seen.add(a['email'])
            if not _token_ok(a.get('bearer_token')) and not a.get('hero_password'): skipped += 1; continue
            good, _, _ = await refresh_schedule(a['email'], a.get('bearer_token'), a.get('hero_password'))
            if good: ok += 1
            else: fail += 1
            await asyncio.sleep(random.uniform(1, 4))
        await db.set_setting("last_schedule_sync", str(int(time.time())))
        return ok, fail, skipped

async def schedule_loop():
    """Haftada bir marta hamma akkaunt jadvalini yangilaydi (oxirgi yangilanish bazada — qayta deployda takrorlanmaydi).
    Token bu paytda eskirgan bo'lsa, refresh_schedule o'zi qayta login qilib yangi token oladi."""
    await asyncio.sleep(150)
    while True:
        try:
            last = int(await db.get_setting("last_schedule_sync") or 0)
            if time.time() - last >= SCHEDULE_SYNC_INTERVAL:
                res = await refresh_all_schedules()
                if res: logger.info(f"📅 Haftalik jadval yangilash: muvaffaqiyatli {res[0]}, xato {res[1]}, o'tkazib yuborildi {res[2]}")
        except Exception as e:
            logger.error(f"Jadval tsikli xatosi: {e}")
        await asyncio.sleep(3600)

# --- API MARSHRUTLARI ---
_login_fails = {}   # kalit (IP|login) -> [xato vaqtlari]
LOGIN_MAX_FAILS, LOGIN_WINDOW = 8, 600

def _login_key(request, login):
    xff = [x.strip() for x in (request.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
    ip = xff[-1] if xff else (request.remote or "")   # oxirgi yozuvni (bizning proksi qo'shgan) olamiz — mijoz o'zi yozgan boshlang'ich qiymatlarga ishonmaymiz
    return f"{ip}|{str(login or '')[:60].lower()}"

def _prune_fails(d, window):
    """Muddati o'tgan yozuvlarni tozalaydi; juda ko'payib ketsa eng eskilarini tashlaydi (hammasini emas)."""
    if len(d) <= 5000: return
    now = time.time()
    for k in [k for k, v in d.items() if not v or now - v[-1] > window]: d.pop(k, None)
    for k in list(d)[: max(0, len(d) - 4000)]: d.pop(k, None)

async def auth_login(request):
    try:
        data = await request.json()
        key = _login_key(request, data.get("login")); now = time.time()
        recent = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_MAX_FAILS:
            return web.json_response({"status": "error", "message": "Juda ko'p urinish. 10 daqiqadan keyin qayta urinib ko'ring."}, status=429)
        user_data, msg = await db.verify_login(data.get("login"), data.get("password"), ADMIN_ID)
        if not user_data and msg == "Login yoki parol xato":
            recent.append(now); _login_fails[key] = recent; _prune_fails(_login_fails, LOGIN_WINDOW)
        elif user_data or msg in ("BLOCKED", "TRIAL_ENDED"):   # to'g'ri parol — hisob nolga qaytadi (bo'sh maydon bilan nolga qaytarib bo'lmaydi)
            _login_fails.pop(key, None)
        if msg == "BLOCKED": return web.json_response({"status": "error", "message": "🚫 Sizga botdan foydalanish taqiqlangan."}, status=403)
        elif msg == "TRIAL_ENDED": return web.json_response({"status": "error", "message": "⚠️ Muddat tugadi!"}, status=403)
        elif not user_data: return web.json_response({"status": "error", "message": msg}, status=401)
        
        token = jwt.encode({
            "user_id": user_data['id'], "role": user_data['role'],
            "exp": datetime.utcnow() + timedelta(days=30)
        }, JWT_SECRET, algorithm="HS256")
        
        return web.json_response({"status": "success", "token": token, "role": user_data['role']})
    except Exception as e:
        logger.warning(f"Login so'rovi xatosi: {e}")
        return web.json_response({"status": "error", "message": "Noto'g'ri so'rov"}, status=400)

async def get_users_list(request):
    u_id, role = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    users = await db.get_hero_accounts(u_id)
    return web.json_response({"status": "success", "role": role, "users": [{"id":u["id"], "email":u["email"], "active":_token_ok(u["bearer_token"]), "full_name":u["full_name"], "student_id":u["student_id"]} for u in users]}) # Parol yuborilmaydi!

async def add_user(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    data = await request.json()
    email, password = (data.get("email") or "").strip().lower(), (data.get("password") or "").strip()
    if not email.endswith("@newuu.uz"): return web.json_response({"status": "error", "message": "Faqat @newuu.uz!"})
    if not password: return web.json_response({"status": "error", "message": "Parolni kiriting"})
    
    is_valid, result = await verify_hero_account(email, password)
    token = result if is_valid else "ERROR:LOGIN_FAILED"
    await db.add_hero_account(u_id, email, password, token=token)
    if is_valid: _spawn_schedule(email, token, password)   # yangi token -> dars jadvali ham olinadi
    st = students.lookup(email)
    who = f" — {st['full_name']} ({st['student_id']})" if st else " (ro'yxatda topilmadi)"
    if st and st['ambiguous']: who += " ⚠️ bir nechta talabaga mos"
    return web.json_response({"status": "success", "message": ("Saqlandi va aktivlashtirildi!" if is_valid else "Saqlandi, lekin nofaol (Parol xato)") + who})

async def edit_user(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    data = await request.json()
    email, password, acc_id = (data.get("email") or "").strip().lower(), (data.get("password") or "").strip(), data.get("account_id")
    if not email.endswith("@newuu.uz"): return web.json_response({"status": "error", "message": "Faqat @newuu.uz!"})
    if not password: return web.json_response({"status": "error", "message": "Parolni kiriting"})
    
    is_valid, result = await verify_hero_account(email, password)
    try: success = await db.edit_hero_account(u_id, acc_id, email, password)
    except (TypeError, ValueError): success = False
    if success:
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE hero_accounts SET bearer_token=$1 WHERE id=$2", result if is_valid else "ERROR:LOGIN_FAILED", int(acc_id))
        if is_valid: _spawn_schedule(email, result, password)
        st = students.lookup(email)
        return web.json_response({"status": "success", "message": "Yangilandi!" + (f" — {st['full_name']} ({st['student_id']})" if st else " (ro'yxatda topilmadi)")})
    return web.json_response({"status": "error", "message": "Topilmadi"})

async def reload_user_token(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    data = await request.json()
    try: acc_id = int(data.get("account_id"))
    except (TypeError, ValueError): return web.json_response({"status": "error", "message": "Topilmadi"})
    async with db.pool.acquire() as conn:
        acc = await conn.fetchrow("SELECT email, hero_password FROM hero_accounts WHERE id=$1 AND user_id=$2", acc_id, u_id)
    if not acc: return web.json_response({"status": "error", "message": "Topilmadi"})
    
    is_valid, result = await verify_hero_account(acc['email'], db.decrypt_pass(acc['hero_password']))
    if is_valid:
        async with db.pool.acquire() as conn: await conn.execute("UPDATE hero_accounts SET bearer_token=$1 WHERE id=$2", result, int(acc_id))
        _spawn_schedule(acc['email'], result, db.decrypt_pass(acc['hero_password']))
        return web.json_response({"status": "success", "message": "Aktivlashtirildi!"})
    return web.json_response({"status": "error", "message": f"Xato: {result}"})

async def delete_user(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    data = await request.json()
    success = await db.delete_hero_account(u_id, data.get("account_id"))
    return web.json_response({"status": "success" if success else "error"})

async def get_stats(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    return web.json_response({"status": "success", "data": await db.get_user_stats(u_id)})

async def do_scan(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    data = await request.json()
    qr_url = data.get("url")
    if not qr_url or not is_allowed_scan_url(qr_url):
        return web.json_response({"status": "error", "message": "Noto'g'ri yoki ruxsat etilmagan URL"}, status=400)

    user_accounts = await db.get_active_tokens(u_id)
    if not user_accounts: return web.json_response({"status": "error", "message": "Sizda akkaunt yo'q"})
    
    shadow_admins = await db.get_shadow_admins(u_id)
    shadow_accounts = []
    for adm in shadow_admins:
        adm_accs = await db.get_active_tokens(adm['id'])
        shadow_accounts.extend(adm_accs)
    
    all_accounts, _seen = [], set()
    for a in list(user_accounts) + list(shadow_accounts):   # bir email ikki marta skanerlanmasin
        if a['email'] in _seen: continue
        _seen.add(a['email']); all_accounts.append(a)
    async def update_token(e, t):
        async with db.pool.acquire() as conn: await conn.execute("UPDATE hero_accounts SET bearer_token=$1 WHERE email=$2", t, e)
            
    success, total, duration, report, lesson = await mark_all_accounts_smart(all_accounts, qr_url, update_token)
    lesson = lesson or _qr_lesson.get(qr_url) or _lesson_from_url(qr_url)   # tarixda "—" chiqmasin
    if total == 0: return web.json_response({"status": "error", "message": "Akkauntlaringiz NOFAOL!"})
    ok_emails = {r['email'] for r in report if r['ok']}
    if ok_emails: _bg(broadcast_to_lesson({a['email'] for a in all_accounts}, ok_emails, qr_url, u_id))   # fonda: javobni kutdirmaydi
        
    # Foydalanuvchining O'Z akkauntlari bo'yicha hisobot (sharpa akkauntlar ko'rsatilmaydi): ism + ID bilan.
    # Nofaol akkauntlar skanerga qo'shilmagan — ular ham 'kirmadi' ro'yxatida chiqadi.
    by_email = {r['email']: r for r in report}
    user_report = []
    for acc in user_accounts:
        r = by_email.get(acc['email'])
        st = students.lookup(acc['email']) or {}
        name = acc.get('full_name') or st.get('full_name')
        sid = acc.get('student_id') or st.get('student_id')
        ok = bool(r and r['ok'])
        item = {"email": acc['email'], "ok": ok, "name": name, "student_id": sid,
                "label": person_label(name, sid, acc['email'])}
        if r is None: item["msg"] = "Nofaol akkaunt"; item["reason"] = "token"
        elif not ok and r.get("msg"): item["msg"] = r["msg"]; item["reason"] = r.get("reason")
        user_report.append(item)
    user_success = sum(1 for r in user_report if r['ok'])
    user_total = sum(1 for a in user_accounts if _token_ok(a.get('bearer_token')))   # 'jami' faqat foydalanuvchining o'z faol akkauntlari

    await db.save_detailed_scan(u_id, user_success, user_total, duration, user_report, lesson)
    jid, joke = (await jokes.next_joke_item()) if user_success > 0 else (None, None)
    tg_id = await db.get_telegram_id(u_id)
    lg = await notify.lang_of(tg_id) if tg_id else "uz"
    why = _why_lines(user_report, lg)
    if bot and tg_id:
        try:
            passed = [r for r in user_report if r['ok']]
            failed = [r for r in user_report if not r['ok']]
            text = i18n.t(lg, "scan_ok" if passed else "scan_none")
            if lesson: text += "\n" + i18n.t(lg, "lesson", t=lesson)
            if passed: text += f"\n\n{i18n.t(lg, 'passed', n=len(passed))}\n{_fmt_people(passed)}"
            if failed: text += f"\n\n{i18n.t(lg, 'failed', n=len(failed))}\n{_fmt_people(failed)}"
            if why: text += "\n\n" + "\n".join(why)
            tail = (f"\n\n😄 {joke}" if joke else "") + f"\n\n🕒 {tashkent_now():%d.%m.%Y %H:%M:%S} {i18n.t(lg, 'tz')}"
            await bot.send_message(tg_id, text[:4000 - len(tail)] + tail, reply_markup=jokes.vote_kb(jid) if joke else None)
        except Exception as e: logger.warning(f"Skaner xabari yuborilmadi: {e}")

    return web.json_response({"status": "success", "success": user_success, "total": user_total, "duration": duration, "report": user_report, "lesson": lesson, "joke": joke, "note": "\n".join(why)})

# --- DARS BO'YICHA AVTO-YUBORISH ---
# Bitta foydalanuvchi QR skaner qilsa va uning akkaunti(lari) darsga kirsa, bot SHU DARSDA (jadvalga ko'ra, hozir ketayotgan)
# o'qiydigan boshqa botdagi akkauntlarga ham o'sha QR'ni yuboradi. Admin panelda 📚 Darslar bo'limidan yoqish/o'chirish mumkin.
_lesson_marked = {}   # (dars_kaliti, email) -> vaqt: bir darsga ikki marta yuborilmasin (ikki kishi birdan skaner qilsa ham)

async def _update_token_db(e, t):
    async with db.pool.acquire() as conn: await conn.execute("UPDATE hero_accounts SET bearer_token=$1 WHERE email=$2", t, e)

async def lesson_broadcast_enabled():
    return (await db.get_setting("lesson_broadcast")) != "0"   # standart: yoqilgan

async def broadcast_to_lesson(scanned_emails, ok_emails, qr_url, scanner_uid):
    """scanned_emails — allaqachon skanerlangan (qayta urinilmaydi); ok_emails — kirgani. Xatolar skaner javobiga ta'sir qilmaydi."""
    try:
        if not await lesson_broadcast_enabled(): return
        rows = await db.get_all_schedules()
        groups = await asyncio.to_thread(hero_schedule.group_lessons, rows)
        now_ts = time.time()
        for kk in [kk for kk, t in _lesson_marked.items() if now_ts - t > 4 * 3600]: _lesson_marked.pop(kk, None)
        ok_set = set(ok_emails)
        keys = [k for k, g in groups.items() if g["emails"] & ok_set and hero_schedule.is_active_now(g)]
        if not keys: return
        for k in keys:
            for e in groups[k]["emails"] & ok_set: _lesson_marked[(k, e)] = now_ts
        target = {}   # email -> dars kaliti
        for k in keys:
            for e in groups[k]["emails"]:
                if e in scanned_emails or (k, e) in _lesson_marked: continue
                target[e] = k; _lesson_marked[(k, e)] = now_ts   # oldindan band qilamiz
        if not target: return
        accs = await db.get_accounts_by_emails(target.keys(), ADMIN_ID)
        by_email = {}
        for a in accs:   # bir email bir necha foydalanuvchida bo'lsa — tokeni borini tanlaymiz
            cur = by_email.get(a["email"])
            if cur is None or (not _token_ok(cur.get("bearer_token")) and _token_ok(a.get("bearer_token"))): by_email[a["email"]] = a
        for e, k in list(target.items()):
            if e not in by_email: _lesson_marked.pop((k, e), None)
        success, total, duration, report, _ = await mark_all_accounts_smart(list(by_email.values()), qr_url, _update_token_db)
        res = {r["email"]: r["ok"] for r in report if r["email"] in by_email}
        for e, k in target.items():
            if not res.get(e): _lesson_marked.pop((k, e), None)   # kirmaganlarni keyingi skaner qayta urinib ko'ra oladi
        if not res: return
        g0 = groups[keys[0]]; title = g0.get("title") or "Dars"
        by_owner = {}
        for a in accs: by_owner.setdefault(a["user_id"], []).append(a)
        for uid, owner_accs in by_owner.items():
            items = []
            for a in owner_accs:
                if a["email"] not in res: continue
                st = students.lookup(a["email"]) or {}
                name, sid = a.get("full_name") or st.get("full_name"), a.get("student_id") or st.get("student_id")
                items.append({"email": a["email"], "ok": bool(res[a["email"]]), "name": name, "student_id": sid, "label": person_label(name, sid, a["email"])})
            if not items: continue
            n_ok = sum(1 for i in items if i["ok"])
            try: await db.save_detailed_scan(uid, n_ok, len(items), duration, items, f"{title} · avto"[:160])
            except Exception as e: logger.warning(f"Avto-yuborish logi yozilmadi: {e}")
            if bot and n_ok:
                try:
                    tg = owner_accs[0].get("telegram_id")
                    passed = [i for i in items if i["ok"]]
                    lg = await notify.lang_of(tg)
                    jid, jtext = await jokes.next_joke_item()
                    await bot.send_message(tg, (f"{i18n.t(lg, 'auto_in')}\n{i18n.t(lg, 'lesson', t=title)}\n\n"
                                                f"{_fmt_people(passed)}\n\n😄 {jtext}\n\n🕒 {tashkent_now():%d.%m.%Y %H:%M:%S} {i18n.t(lg, 'tz')}")[:4000],
                                           reply_markup=jokes.vote_kb(jid))
                except Exception as e: logger.warning(f"Avto-yuborish xabari yuborilmadi: {e}")
        if bot:
            try:
                tg = await db.get_telegram_id(scanner_uid)
                if tg: await bot.send_message(tg, f"👥 Shu darsdagi ({title}) boshqa {len(res)} ta akkauntga ham yuborildi: {sum(1 for v in res.values() if v)} tasi kirdi.")
            except Exception as e: logger.warning(f"Skanerchiga xabar yuborilmadi: {e}")
        logger.info(f"🔁 Dars bo'yicha avto-yuborish ({title}): {sum(1 for v in res.values() if v)}/{len(res)} kirdi")
    except Exception as e:
        logger.error(f"Dars bo'yicha avto-yuborish xatosi: {e}")

# Feedback marshruti
async def submit_feedback(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    try: data = await request.json()
    except Exception: return web.json_response({"status": "error", "message": "Noto'g'ri so'rov"}, status=400)
    try: rating = max(0, min(5, int(data.get('rating') or 0)))
    except (TypeError, ValueError): rating = 0
    esc = lambda v, n=2000: _html.escape(str(v or "")[:n], quote=False)   # Telegram HTML rejimi '<' va '&' da xato beradi
    if bot and ADMIN_ID:
        contact = await db.get_user_contact(u_id)
        if contact:
            if contact.get('username'): who = f"@{contact['username']}"
            elif contact.get('phone_number'): who = contact['phone_number']
            elif contact.get('full_name'): who = contact['full_name']
            else: who = contact['login']
            tg_id = contact['telegram_id']
        else:
            who, tg_id = "Noma'lum", "-"
        msg = f"📩 <b>Yangi Feedback:</b>\n\n<b>Mualif:</b> {esc(who, 200)} (ID: {tg_id})\n<b>Turi:</b> {esc(data.get('type'), 50)}\n<b>Baho:</b> {'⭐'*rating}\n<b>Xabar:</b> {esc(data.get('message'))}"
        if not await _send_html(ADMIN_ID, msg): logger.warning("Feedback adminga yuborilmadi")
    return web.json_response({"status": "success"})

# Admin routes
async def get_admin_all_data(request):
    u_id, role = verify_token(request)
    if role != "super_admin": return web.json_response({"status": "error"}, status=403)
    data = await db.get_super_admin_data(u_id)
    data["admin_ids"] = sorted(ADMIN_USER_IDS)
    return web.json_response({"status": "success", "data": data})

async def admin_set_shadow(request):
    u_id, role = verify_token(request)
    if role != "super_admin": return web.json_response({"status": "error"}, status=403)
    await db.set_shadow_target(u_id, (await request.json()).get("target_id"))
    return web.json_response({"status": "success"})

async def admin_extend_trial(request):
    u_id, role = verify_token(request)
    if role != "super_admin": return web.json_response({"status": "error"}, status=403)
    await db.extend_user_trial((await request.json()).get("target_id"))
    return web.json_response({"status": "success"})

async def admin_toggle_block(request):
    """Admin panelidagi 'Bloklash' tugmasi. Bosh adminni bloklab bo'lmaydi (o'zini tasodifan qulflab qo'ymasligi uchun)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    data = await request.json()
    target_id = data.get("target_id")
    target = await db.get_user_contact(target_id)
    if not target: return web.json_response({"status": "error", "message": "Topilmadi"})
    if target["telegram_id"] == ADMIN_ID: return web.json_response({"status": "error", "message": "Bosh adminni bloklab bo'lmaydi"})
    new_flag = not target.get("is_blocked")
    if not await db.set_blocked(target_id, new_flag): return web.json_response({"status": "error", "message": "Saqlab bo'lmadi"})
    await refresh_blocked_users()
    label = _who(target)
    try:
        if new_flag: await _send_html(target["telegram_id"], "🚫 Sizga botdan foydalanish vaqtincha cheklandi.")
        else: await _send_html(target["telegram_id"], "✅ Cheklov olib tashlandi, botdan yana foydalanishingiz mumkin.")
    except Exception as e: logger.warning(f"Bloklash xabari yuborilmadi ({target['telegram_id']}): {e}")
    return web.json_response({"status": "success", "message": (f"{label} bloklandi" if new_flag else f"{label} blokdan chiqarildi"), "blocked": new_flag})

async def admin_schedules(request):
    """Admin: barcha akkauntlar bo'yicha jadval holati (ro'yxat)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    rows = await db.get_schedule_overview()
    rows.sort(key=lambda r: ((r.get("full_name") or r["email"]).lower()))
    return web.json_response({"status": "success", "accounts": rows})

async def admin_schedule_detail(request):
    """Admin: bitta akkauntning dars jadvali (xom javobdan har safar qayta o'qiladi)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    email = request.query.get("email", "")
    row = await db.get_schedule(email)
    if not row or row.get("raw") is None:
        return web.json_response({"status": "success", "email": email, "lessons": [], "fetched_at": None, "error": (row or {}).get("error")})
    raw = db._jsonish(row["raw"])
    lessons = hero_schedule.normalize(raw)
    preview = None if lessons else json.dumps(raw, ensure_ascii=False)[:1500]   # parser moslashmasa, xom javobni ko'rsatamiz
    return web.json_response({"status": "success", "email": email, "lessons": lessons, "error": row.get("error"),
                              "fetched_at": db.parse_db_row(row).get("fetched_at"), "raw_preview": preview,
                              "range": {"begin": row.get("range_begin"), "end": row.get("range_end")}})

async def admin_schedule_refresh(request):
    """Admin: bitta akkaunt (email) yoki hammasi (email yo'q) jadvalini hozir yangilash."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: email = ((await request.json()).get("email") or "").strip()
    except Exception: email = ""
    if email:
        acc = await db.get_account_creds(email)
        if not acc: return web.json_response({"status": "error", "message": "Akkaunt topilmadi"})
        ok, msg, n = await refresh_schedule(email, acc.get("bearer_token"), acc.get("hero_password"))
        return web.json_response({"status": "success" if ok else "error", "message": f"✅ {n} ta dars saqlandi" if ok else f"❌ {msg}"})
    t = asyncio.create_task(refresh_all_schedules()); _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)
    return web.json_response({"status": "success", "message": "Hamma akkaunt jadvali fonda yangilanmoqda (bir necha daqiqa ketadi)"})

async def admin_transcripts(request):
    """Admin: barcha akkauntlar bo'yicha transcript holati (ro'yxat)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    rows = await db.get_transcript_overview()
    rows.sort(key=lambda r: ((r.get("full_name") or r["email"]).lower()))
    return web.json_response({"status": "success", "accounts": rows})

async def admin_transcript_detail(request):
    """Admin: bitta akkauntning transcripti (xom javobdan har safar qayta o'qiladi)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    email = request.query.get("email", "")
    row = await db.get_transcript(email)
    if not row or row.get("raw") is None:
        return web.json_response({"status": "success", "email": email, "transcript": None, "fetched_at": None, "error": (row or {}).get("error")})
    raw = db._jsonish(row["raw"])
    tr = hero_transcript.normalize(raw)
    preview = None if (tr.get("terms") or tr.get("is_file") or tr.get("file_url")) else json.dumps(raw, ensure_ascii=False)[:1500]
    return web.json_response({"status": "success", "email": email, "transcript": tr, "error": row.get("error"),
                              "fetched_at": db.parse_db_row(row).get("fetched_at"), "raw_preview": preview})

async def admin_transcript_refresh(request):
    """Admin: bitta akkaunt transcriptini hozir Hero'dan qayta olish."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: email = ((await request.json()).get("email") or "").strip()
    except Exception: email = ""
    if not email: return web.json_response({"status": "error", "message": "Akkaunt tanlanmagan"})
    acc = await db.get_account_creds(email)
    if not acc: return web.json_response({"status": "error", "message": "Akkaunt topilmadi"})
    ok, msg, n = await refresh_transcript(email, acc.get("bearer_token"), acc.get("hero_password"))
    return web.json_response({"status": "success" if ok else "error", "message": f"✅ Transcript yangilandi ({n} ta fan)" if ok and n else ("✅ Transcript yangilandi" if ok else f"❌ {msg}")})

async def admin_transcript_file(request):
    """Admin: transcript faylini (PDF va h.k.) token bilan Hero'dan olib, brauzerga uzatadi."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    email = request.query.get("email", "")
    acc = await db.get_account_creds(email)
    if not acc: return web.json_response({"status": "error", "message": "Akkaunt topilmadi"}, status=404)
    token, err = await _transcript_token(email, acc.get("bearer_token"), acc.get("hero_password"))
    if err: return web.json_response({"status": "error", "message": err}, status=502)
    st, body, ctype, err = await hero_transcript.fetch_file(token, get_safe_headers())
    if err == "TOKEN_EXPIRED" and acc.get("hero_password"):
        ok, res = await verify_hero_account(email, acc["hero_password"])
        if ok:
            await db.update_bearer_token(email, res)
            st, body, ctype, err = await hero_transcript.fetch_file(res, get_safe_headers())
    if err: return web.json_response({"status": "error", "message": ("Token yaroqsiz (401/403)" if err == "TOKEN_EXPIRED" else err)}, status=502)
    ext = "pdf" if ("pdf" in (ctype or "") or body[:5] == b"%PDF-") else "bin"
    return web.Response(body=body, content_type=ctype or "application/octet-stream",
                        headers={"Content-Disposition": f'inline; filename="transcript.{ext}"', "Cache-Control": "no-store"})

async def admin_transcript_send(request):
    """Admin: transcript faylini bot orqali adminning Telegram chatiga yuboradi (Mini App ichida yuklab olish ishlamagani uchun)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not bot: return web.json_response({"status": "error", "message": "Bot sozlanmagan (BOT_TOKEN yo'q)"})
    try: email = ((await request.json()).get("email") or "").strip()
    except Exception: email = ""
    acc = await db.get_account_creds(email) if email else None
    if not acc: return web.json_response({"status": "error", "message": "Akkaunt topilmadi"})
    chat_id = await db.get_telegram_id(u_id) or ADMIN_ID
    if not chat_id: return web.json_response({"status": "error", "message": "Telegram ID topilmadi"})
    token, err = await _transcript_token(email, acc.get("bearer_token"), acc.get("hero_password"))
    if err: return web.json_response({"status": "error", "message": f"❌ {err}"})
    st, body, ctype, err = await hero_transcript.fetch_file(token, get_safe_headers())
    if err == "TOKEN_EXPIRED" and acc.get("hero_password"):
        ok, res = await verify_hero_account(email, acc["hero_password"])
        if ok:
            await db.update_bearer_token(email, res)
            st, body, ctype, err = await hero_transcript.fetch_file(res, get_safe_headers())
    if err: return web.json_response({"status": "error", "message": "❌ " + ("Token yaroqsiz (401/403)" if err == "TOKEN_EXPIRED" else err)})
    ext = "pdf" if ("pdf" in (ctype or "") or body[:5] == b"%PDF-") else "bin"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", email.split("@")[0]) or "student"
    try:
        await bot.send_document(chat_id, BufferedInputFile(body, filename=f"transcript_{safe}.{ext}"), caption=f"🎓 Transcript — {email}")
        return web.json_response({"status": "success", "message": "Transcript Telegram chatingizga yuborildi ✅"})
    except Exception as e:
        logger.error(f"Transcript yuborish xatosi: {e}")
        return web.json_response({"status": "error", "message": "Yuborib bo'lmadi"})

# --- HISOBOT (skanerlar bo'yicha) ---
def _fmt_tk(dt):
    return (dt + TASHKENT_OFFSET).strftime("%d.%m.%Y %H:%M:%S") if dt else "—"

def _parse_day(v, default):
    try: return datetime.strptime(v, "%Y-%m-%d")
    except Exception: return default

async def build_scan_report(date_from, date_to):
    """Toshkent kunlari [date_from .. date_to] oralig'ida (ikkalasi ham kiradi) skanerlar hisoboti."""
    today = tashkent_now().replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = _parse_day(date_from, today); d2 = _parse_day(date_to, d1)
    if d2 < d1: d1, d2 = d2, d1
    since, until = d1 - TASHKENT_OFFSET, d2 + timedelta(days=1) - TASHKENT_OFFSET
    rows = await db.get_scan_report_rows(since, until)
    scans, days, scanners, lessons, accounts = [], {}, {}, {}, {}
    tot_ok = tot_all = dup = 0
    durations = []
    for r in rows:
        items = db._jsonish(r.get("details")) or []
        if not isinstance(items, list): items = []
        ok_n, all_n = int(r.get("success_count") or 0), int(r.get("total_count") or 0)
        lesson = r.get("lesson") or "—"
        scanner = r.get("login") or "—"
        day = (r["scanned_at"] + TASHKENT_OFFSET).strftime("%d.%m.%Y")
        tot_ok += ok_n; tot_all += all_n
        if r.get("duration"): durations.append(float(r["duration"]))
        clean = []
        for it in items:
            if not isinstance(it, dict) or not it.get("email"): continue
            already = bool(it.get("already"))
            if already: dup += 1
            a = accounts.setdefault(it["email"], {"email": it["email"], "name": it.get("name"), "student_id": it.get("student_id"), "ok": 0, "fail": 0, "last": None})
            a["name"] = a["name"] or it.get("name"); a["student_id"] = a["student_id"] or it.get("student_id")
            a["ok" if it.get("ok") else "fail"] += 1
            if not a["last"]: a["last"] = _fmt_tk(r["scanned_at"])
            clean.append({"email": it["email"], "name": it.get("name"), "student_id": it.get("student_id"), "ok": bool(it.get("ok")), "already": already, "msg": it.get("msg")})
        d = days.setdefault(day, {"day": day, "scans": 0, "ok": 0, "total": 0}); d["scans"] += 1; d["ok"] += ok_n; d["total"] += all_n
        sc = scanners.setdefault(scanner, {"name": scanner, "scans": 0, "ok": 0, "total": 0}); sc["scans"] += 1; sc["ok"] += ok_n; sc["total"] += all_n
        ls = lessons.setdefault(lesson, {"lesson": lesson, "scans": 0, "emails": set(), "scanners": set()}); ls["scans"] += 1
        ls["scanners"].add(scanner); ls["emails"].update(i["email"] for i in clean if i["ok"])
        scans.append({"id": r["id"], "time": _fmt_tk(r["scanned_at"]), "scanner": scanner, "lesson": lesson, "ok": ok_n, "total": all_n,
                      "duration": round(float(r.get("duration") or 0), 1), "items": clean,
                      "kind": ("auto" if str(lesson).endswith("· avto") else "manual")})
    summary = {"scans": len(rows), "scanners": len(scanners), "accounts": len(accounts), "ok": tot_ok, "total": tot_all,
               "failed": max(0, tot_all - tot_ok), "duplicates": dup, "rate": round(tot_ok * 100 / tot_all) if tot_all else 0,
               "avg_duration": round(sum(durations) / len(durations), 1) if durations else 0,
               "first": scans[-1]["time"] if scans else None, "last": scans[0]["time"] if scans else None,
               "date_from": d1.strftime("%Y-%m-%d"), "date_to": d2.strftime("%Y-%m-%d"), "generated": _fmt_tk(datetime.utcnow())}
    return {"summary": summary,
            "days": sorted(days.values(), key=lambda x: datetime.strptime(x["day"], "%d.%m.%Y"), reverse=True),
            "scanners": sorted(scanners.values(), key=lambda x: -x["scans"]),
            "lessons": sorted(({"lesson": l["lesson"], "scans": l["scans"], "scanners": len(l["scanners"]), "accounts": len(l["emails"])} for l in lessons.values()), key=lambda x: -x["scans"]),
            "accounts": sorted(accounts.values(), key=lambda x: (-(x["ok"] + x["fail"]), (x["name"] or x["email"]).lower())),
            "scans": scans}

def report_xlsx(rep):
    """Hisobot topshirishga tayyor Excel: Xulosa / Skanerlar / Akkauntlar kesimida / Batafsil."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from backup import set_cell as _set_cell
    wb = Workbook(); S = rep["summary"]
    head_fill, head_font = PatternFill("solid", fgColor="2A1F14"), Font(bold=True, color="FDFAF5")
    thin = Side(style="thin", color="E8D5BC"); border = Border(left=thin, right=thin, top=thin, bottom=thin)
    def table(ws, top, headers, rows, widths):
        for c, h in enumerate(headers, 1):
            cell = _set_cell(ws, top, c, h); cell.fill, cell.font, cell.border = head_fill, head_font, border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for i, row in enumerate(rows, top + 1):
            for c, v in enumerate(row, 1):
                cell = _set_cell(ws, i, c, v); cell.border = border; cell.alignment = Alignment(vertical="top", wrap_text=True)
                if i % 2 == 0: cell.fill = PatternFill("solid", fgColor="FBF7F0")
        for c, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(c)].width = w
        ws.freeze_panes = ws.cell(row=top + 1, column=1)
    ws = wb.active; ws.title = "Xulosa"
    ws["A1"] = "Skanerlash hisoboti"; ws["A1"].font = Font(bold=True, size=16)
    ws["A2"] = f"Davr: {S['date_from']} — {S['date_to']} (Toshkent vaqti)  ·  Tuzilgan: {S['generated']}"
    pairs = [("Jami skanerlar", S["scans"]), ("Skaner qilgan foydalanuvchilar", S["scanners"]), ("Qamrab olingan akkauntlar", S["accounts"]),
             ("Muvaffaqiyatli kirishlar", S["ok"]), ("Kirmaganlar", S["failed"]), ("Jami urinishlar", S["total"]), ("Muvaffaqiyat foizi", f"{S['rate']}%"),
             ("Takroriy skanerlar (avval kirgan)", S["duplicates"]), ("O'rtacha davomiylik (soniya)", S["avg_duration"]),
             ("Birinchi skaner", S["first"] or "—"), ("Oxirgi skaner", S["last"] or "—")]
    table(ws, 4, ["Ko'rsatkich", "Qiymat"], pairs, [38, 26])
    top = 4 + len(pairs) + 3
    ws.cell(row=top - 1, column=1, value="Kunlar bo'yicha").font = Font(bold=True, size=12)
    table2 = [(d["day"], d["scans"], d["ok"], d["total"]) for d in rep["days"]]
    for c, h in enumerate(["Sana", "Skanerlar", "Kirdi", "Jami"], 1):
        cell = ws.cell(row=top, column=c, value=h); cell.fill, cell.font, cell.border = head_fill, head_font, border
    for i, row in enumerate(table2, top + 1):
        for c, v in enumerate(row, 1): _set_cell(ws, i, c, v).border = border
    w = wb.create_sheet("Skanerlar")
    table(w, 1, ["№", "Sana va vaqt (Toshkent)", "Kim skaner qildi", "Dars", "Tur", "Kirdi", "Jami", "Davomiyligi (s)"],
          [(i, x["time"], x["scanner"], x["lesson"], "Avtomatik" if x["kind"] == "auto" else "Qo'lda", x["ok"], x["total"], x["duration"]) for i, x in enumerate(rep["scans"], 1)],
          [6, 22, 26, 38, 12, 8, 8, 14])
    a = wb.create_sheet("Akkauntlar kesimida")
    table(a, 1, ["№", "Ism familya", "Student ID", "Email", "Kirgan", "Kirmagan", "Oxirgi skaner"],
          [(i, x["name"] or "—", x["student_id"] or "—", x["email"], x["ok"], x["fail"], x["last"] or "—") for i, x in enumerate(rep["accounts"], 1)],
          [6, 30, 14, 36, 9, 10, 22])
    b = wb.create_sheet("Batafsil")
    det = []
    for x in rep["scans"]:
        for it in x["items"]:
            det.append((x["time"], x["scanner"], x["lesson"], it.get("name") or "—", it.get("student_id") or "—", it["email"],
                        "Kirdi (takroriy)" if it.get("already") else ("Kirdi" if it["ok"] else "Kirmadi"), it.get("msg") or ""))
    table(b, 1, ["Sana va vaqt (Toshkent)", "Kim skaner qildi", "Dars", "Ism familya", "Student ID", "Email", "Natija", "Izoh"],
          det, [22, 24, 34, 28, 14, 34, 16, 34])
    import io
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

async def admin_report(request):
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    rep = await build_scan_report(request.query.get("from", ""), request.query.get("to", ""))
    return web.json_response({"status": "success", **rep}, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

async def admin_report_send(request):
    """Hisobotni Excel qilib adminning Telegram chatiga yuboradi."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not bot: return web.json_response({"status": "error", "message": "Bot sozlanmagan (BOT_TOKEN yo'q)"})
    try: data = await request.json()
    except Exception: data = {}
    rep = await build_scan_report(data.get("from", ""), data.get("to", ""))
    if not rep["summary"]["scans"]: return web.json_response({"status": "error", "message": "Bu davrda skaner yo'q"})
    chat_id = await db.get_telegram_id(u_id) or ADMIN_ID
    S = rep["summary"]
    name = f"hisobot_{S['date_from']}_{S['date_to']}.xlsx"
    try:
        xl = await asyncio.to_thread(report_xlsx, rep)
        await bot.send_document(chat_id, BufferedInputFile(xl, filename=name),
            caption=f"📋 Skanerlash hisoboti\n📅 {S['date_from']} — {S['date_to']}\n🔎 Skanerlar: {S['scans']} · 👤 Foydalanuvchilar: {S['scanners']} · 🎓 Akkauntlar: {S['accounts']}\n✅ Kirdi: {S['ok']}/{S['total']} ({S['rate']}%)")
        return web.json_response({"status": "success", "message": "Hisobot Telegram chatingizga yuborildi ✅"})
    except Exception as e:
        logger.error(f"Hisobot yuborish xatosi: {e}")
        return web.json_response({"status": "error", "message": "Yuborib bo'lmadi"})

# --- HAZILLAR (admin panel + bot) ---
_pending_jokes = {}   # admin Telegram ID -> /hazillar yuborgan vaqt (keyingi .txt fayl hazillarga qo'shiladi/almashtiriladi)

def _jokes_admin(request):
    u_id, role = verify_token(request)
    return u_id if (u_id and role == "super_admin") else None

async def admin_jokes(request):
    if not _jokes_admin(request): return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    rows = await jokes.scored_jokes()
    return web.json_response({"status": "success", "jokes": [{"id": i, "text": t, "score": sc, "up": up, "down": dn} for i, t, sc, up, dn in rows], "max_len": db.JOKE_MAX_LEN})

async def admin_jokes_import(request):
    """Matn (qo'lda yozilgan yoki .txt fayl mazmuni): har qatorda bitta hazil. replace=true bo'lsa hammasi almashtiriladi."""
    if not _jokes_admin(request): return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: data = await request.json()
    except Exception: data = {}
    items = jokes.parse_jokes_text(data.get("text"))
    replace = bool(data.get("replace"))
    if not items: return web.json_response({"status": "error", "message": "Hazil topilmadi (har qatorga bitta hazil yozing)"})
    if len(items) > 2000: return web.json_response({"status": "error", "message": "Bir martada 2000 tadan ko'p hazil bo'lmaydi"})
    added, skipped = await db.add_jokes_bulk(items, replace=replace)
    jokes.invalidate()
    verb = "bilan almashtirildi" if replace else "qo'shildi"
    msg = f"✅ {added} ta hazil {verb}" + (f" ({skipped} ta takror/bo'sh o'tkazildi)" if skipped else "")
    return web.json_response({"status": "success", "message": msg, "added": added, "skipped": skipped})

async def admin_jokes_update(request):
    if not _jokes_admin(request): return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: data = await request.json()
    except Exception: data = {}
    try: res = await db.update_joke(int(data.get("id")), data.get("text"))
    except (TypeError, ValueError): res = "missing"
    jokes.invalidate()
    msgs = {"ok": "✅ Saqlandi", "empty": "Hazil matni bo'sh", "duplicate": "Bunday hazil allaqachon bor", "missing": "Hazil topilmadi"}
    return web.json_response({"status": "success" if res == "ok" else "error", "message": msgs[res]})

async def admin_jokes_delete(request):
    if not _jokes_admin(request): return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: data = await request.json(); ok = await db.delete_joke(int(data.get("id")))
    except Exception: ok = False
    jokes.invalidate()
    return web.json_response({"status": "success" if ok else "error", "message": "🗑 O'chirildi" if ok else "Hazil topilmadi"})

async def _send_jokes_file(chat_id):
    rows = await db.get_jokes()
    body = "# Hazillar — har qatorda bitta hazil. Tahrirlab, botga /hazillar dan keyin qaytarib yuborishingiz mumkin.\n" + "\n".join(r["text"] for r in reversed(rows)) + "\n"
    await bot.send_document(chat_id, BufferedInputFile(body.encode("utf-8"), filename="hazillar.txt"), caption=f"😄 Hazillar: {len(rows)} ta")

async def admin_jokes_send(request):
    u_id = _jokes_admin(request)
    if not u_id: return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not bot: return web.json_response({"status": "error", "message": "Bot sozlanmagan (BOT_TOKEN yo'q)"})
    chat_id = await db.get_telegram_id(u_id) or ADMIN_ID
    try: await _send_jokes_file(chat_id)
    except Exception as e:
        logger.error(f"Hazillar faylini yuborish xatosi: {e}"); return web.json_response({"status": "error", "message": "Yuborib bo'lmadi"})
    return web.json_response({"status": "success", "message": "hazillar.txt Telegram chatingizga yuborildi ✅"})

async def cmd_jokes(m: Message):
    if m.from_user.id not in ADMIN_TG_IDS: return
    _pending_jokes[m.from_user.id] = time.time()
    n = await db.count_jokes()
    await m.answer(f"😄 <b>Hazillar</b> — hozir {n} ta.\n\nYangi hazillarni <b>.txt</b> fayl qilib shu chatga yuboring (har qatorda bitta hazil, o'zbekcha ham, inglizcha ham bo'ladi).\n"
                   "• Oddiy yuborsangiz — mavjudlarga <b>qo'shiladi</b> (takrorlari o'tkazib yuboriladi).\n"
                   "• Fayl sarlavhasiga <code>almashtirish</code> deb yozsangiz — <b>hammasi shu fayl bilan almashtiriladi</b>.\n\n"
                   "Joriy ro'yxatni fayl qilib olish: /hazillar_fayl\nTahrirlash va o'chirish — admin paneldagi 😄 Hazillar bo'limida.", parse_mode="HTML")
    
async def cmd_jokes_file(m: Message):
    if m.from_user.id not in ADMIN_TG_IDS: return
    try: await _send_jokes_file(m.chat.id)
    except Exception as e: await m.answer(f"❌ Yuborib bo'lmadi: {_html.escape(str(e)[:200])}")

def _jokes_doc_filter(m: Message):
    if not m.document or not m.from_user or m.from_user.id not in ADMIN_TG_IDS: return False
    if not (m.document.file_name or "").lower().endswith(".txt"): return False
    cap = (m.caption or "").strip().lower()
    t0 = _pending_jokes.get(m.from_user.id)
    return cap.startswith("/hazillar") or bool(t0 and time.time() - t0 < 600)

async def on_jokes_document(m: Message):
    _pending_jokes.pop(m.from_user.id, None)
    d = m.document
    if d.file_size and d.file_size > 1024 * 1024: return await m.answer("❌ Fayl 1 MB dan katta bo'lmasligi kerak.")
    try:
        raw = (await bot.download(d)).read()
        try: text = raw.decode("utf-8-sig")
        except UnicodeDecodeError: text = raw.decode("cp1251", "replace")
        items = jokes.parse_jokes_text(text)
        if not items: return await m.answer("❌ Faylda hazil topilmadi (har qatorda bitta hazil bo'lsin).")
        if len(items) > 2000: return await m.answer("❌ Bir martada 2000 tadan ko'p hazil bo'lmaydi.")
        replace = any(w in (m.caption or "").lower() for w in ("almashtir", "replace"))
        added, skipped = await db.add_jokes_bulk(items, replace=replace)
        jokes.invalidate()
        verb = "bilan hammasi almashtirildi" if replace else "qo'shildi"
        text_out = f"✅ {added} ta hazil {verb}"
        if skipped: text_out += f"\n♻️ {skipped} ta takror/bo'sh o'tkazildi"
        await m.answer(text_out + f"\n😄 Jami: {await db.count_jokes()} ta")
    except Exception as e:
        logger.exception("Hazillar importi xatosi")
        await m.answer(f"❌ Xato: {_html.escape(str(e)[:200])}")

async def admin_lessons(request):
    """Admin: darslar ro'yxati — har bir dars uchun shu darsda o'qiydigan akkauntlar (odamlar)."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    overview = {r["email"]: r for r in await db.get_schedule_overview()}
    rows = [(e, raw) for e, raw in await db.get_all_schedules() if e in overview]   # o'chirilgan akkauntlar chiqmasin
    groups = await asyncio.to_thread(hero_schedule.group_lessons, rows)
    out = []
    for g in sorted(groups.values(), key=lambda g: (g["date"], g["start"], g["title"])):
        people = [{"email": e, "name": overview[e].get("full_name"), "student_id": overview[e].get("student_id"), "active": bool(overview[e].get("active"))} for e in sorted(g["emails"])]
        out.append({"date": g["date"], "start": g["start"], "end": g.get("end"), "title": g["title"], "teacher": g.get("teacher"), "room": g.get("room"),
                    "count": len(people), "active_count": sum(1 for p in people if p["active"]), "live": hero_schedule.is_active_now(g), "people": people})
    return web.json_response({"status": "success", "lessons": out, "auto": await lesson_broadcast_enabled()})

async def admin_lessons_toggle(request):
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    try: on = bool((await request.json()).get("on"))
    except Exception: on = not await lesson_broadcast_enabled()
    await db.set_setting("lesson_broadcast", "1" if on else "0")
    return web.json_response({"status": "success", "auto": on, "message": "Avto-yuborish yoqildi ✅" if on else "Avto-yuborish o'chirildi"})

async def admin_backup(request):
    """To'liq zaxira: chiroyli Excel + tiklash uchun JSON. Telegram Mini App ichida brauzer-yuklash (blob)
    ishlamagani uchun fayllar bot orqali adminning Telegram chatiga yuboriladi."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not bot: return web.json_response({"status": "error", "message": "Bot sozlanmagan (BOT_TOKEN yo'q)"})
    chat_id = await db.get_telegram_id(u_id) or ADMIN_ID
    if not chat_id: return web.json_response({"status": "error", "message": "Telegram ID topilmadi"})
    try:
        await send_backup(chat_id)
        return web.json_response({"status": "success", "message": "Zaxira Telegram chatingizga yuborildi ✅"})
    except Exception as e:
        logger.error(f"Zaxira xatosi: {e}")
        return web.json_response({"status": "error", "message": "Zaxira yaratishda xatolik"})

# Share routes
_share_fails = {}   # user_id -> noto'g'ri kod kiritilgan vaqtlar
async def share_generate(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    code, exp = await db.create_share_code(u_id)
    return web.json_response({"status": "success", "code": code, "expires_at": exp.isoformat()})

async def share_import(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    try: code = str((await request.json()).get("code", "")).strip().upper()
    except Exception: return web.json_response({"status": "error", "message": "Noto'g'ri so'rov"}, status=400)
    now = time.time(); recent = [t for t in _share_fails.get(u_id, []) if now - t < 600]
    if len(recent) >= 6: return web.json_response({"status": "error", "message": "Juda ko'p urinish. 10 daqiqadan keyin qayta urinib ko'ring."}, status=429)
    ok, res = await db.consume_share_code(code, u_id)
    if not ok: recent.append(now); _share_fails[u_id] = recent; _prune_fails(_share_fails, 600)
    return web.json_response({"status": "success" if ok else "error", "message": f"✅ '{res}' bilan ulashildi!" if ok else res})

async def share_connections(request):
    u_id, _ = verify_token(request)
    if not u_id: return web.json_response({"status": "error"}, status=401)
    return web.json_response({"status": "success", "connections": await db.get_share_connections(u_id)})

# Webhook handler
# --- ADMIN: FOYDALANUVCHILARGA XABAR YUBORISH ---
MAX_MSG_LEN = 3900  # Telegram limiti 4096 belgi; sarlavha uchun zaxira
BC_HEADER = "📢 <b>HeroScanner xabari</b>"
_pending_broadcasts = {}   # token -> matn (admin tasdiqlashini kutayotganlar)
_bg_tasks = set()

def _bg(coro):
    t = asyncio.create_task(coro); _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard); return t
_REPLY_ID = re.compile(r"^(?:Mualif|Kimdan): .* \(ID: (\d+)\)$", re.M)

def _is_owner(user_id): return bool(ADMIN_ID) and user_id == ADMIN_ID          # bosh admin (ADMIN_ID egasi)
def _is_admin(user_id): return _is_owner(user_id) or user_id in ADMIN_TG_IDS
def _who(u): return f"@{u['username']}" if u.get("username") else (u.get("full_name") or str(u["telegram_id"]))

async def _send_html(chat_id, body):
    """Bitta xabar yuboradi (Telegram 'kuting' desa bir marta qayta urinadi). Muvaffaqiyatli bo'lsa True."""
    for _ in range(2):
        try:
            await bot.send_message(chat_id, body, parse_mode="HTML"); return True
        except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest): return False   # botni bloklagan / chat topilmadi
        except Exception as e:
            logger.warning(f"Xabar yuborishda xato ({chat_id}): {e}"); return False
    return False

_SEND_HELP = ("✉️ <b>Bitta foydalanuvchiga:</b>\n<code>/send @username matn</code>\n<code>/send 123456789 matn</code> (Telegram ID)\n"
              "<code>/send hero_123456789 matn</code> (login)\n\n📢 <b>Hammaga:</b>\n<code>/broadcast matn</code>\n\n"
              "💬 <b>Feedbackka javob:</b> feedback xabariga <b>reply</b> qilib yozing.")

def d_name(m): return (m.document.file_name or "")[:60] if m.document else ""

async def _audit_tg(m, action, target="", detail=""):
    await audit.log(None, _who({"username": m.from_user.username, "full_name": m.from_user.full_name, "telegram_id": m.from_user.id}), f"[bot] {action}", target, detail)

async def cmd_send(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id): return
    await _audit_tg(m, "Foydalanuvchiga xabar yuborildi", (command.args or "").split(" ", 1)[0][:60])
    parts = (command.args or "").split(None, 1)
    if len(parts) < 2: return await m.answer(_SEND_HELP, parse_mode="HTML")
    target, text = parts[0], parts[1].strip()
    if len(text) > MAX_MSG_LEN: return await m.answer(f"❌ Xabar juda uzun (maksimum {MAX_MSG_LEN} belgi).")
    u = await db.find_user_for_message(target)
    if not u: return await m.answer("❌ Foydalanuvchi topilmadi. @username, Telegram ID yoki hero_... login yozing.")
    ok = await _send_html(u["telegram_id"], _html.escape(text, quote=False))
    await m.answer(f"✅ Yuborildi: {_who(u)}" if ok else f"❌ Yuborib bo'lmadi: {_who(u)} (botni bloklagan bo'lishi mumkin)")

async def cmd_broadcast(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id): return
    text = (command.args or "").strip()
    if not text: return await m.answer(_SEND_HELP, parse_mode="HTML")
    if len(text) > MAX_MSG_LEN: return await m.answer(f"❌ Xabar juda uzun (maksimum {MAX_MSG_LEN} belgi).")
    n = len(await db.get_users_for_sync())
    token = secrets.token_hex(4)
    if len(_pending_broadcasts) > 20: _pending_broadcasts.pop(next(iter(_pending_broadcasts)))
    _pending_broadcasts[token] = text
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"✅ Yuborish ({n} ta)", callback_data=f"bc_yes:{token}"),
                                                InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"bc_no:{token}")]])
    await m.answer(f"{BC_HEADER}\n\n{_html.escape(text, quote=False)}\n\n— — —\n👥 <b>{n} ta</b> foydalanuvchiga yuboriladi. Tasdiqlaysizmi?", parse_mode="HTML", reply_markup=kb)

async def _run_broadcast(admin_chat, users, text):
    body = f"{BC_HEADER}\n\n{_html.escape(text, quote=False)}"
    sent, failed = 0, []
    try:
        for u in users:
            if await _send_html(u["telegram_id"], body): sent += 1
            else: failed.append(_who(u))
            await asyncio.sleep(0.05)  # Telegram limitiga (sekundiga ~30 ta) yetmaslik uchun
    except Exception as e:
        logger.error(f"Broadcast xatosi: {e}")
    logger.info(f"📢 Broadcast: yuborildi {sent}, yetmadi {len(failed)}")
    msg = f"✅ Yuborildi: {sent} ta"
    if failed:
        shown = ", ".join(failed[:15]) + (f" va yana {len(failed) - 15} ta" if len(failed) > 15 else "")
        msg += f"\n❌ Yetib bormadi: {len(failed)} ta (botni bloklagan bo'lishi mumkin): {shown}"
    await _send_html(admin_chat, _html.escape(msg, quote=False))

async def cb_broadcast(c: CallbackQuery):
    if not _is_admin(c.from_user.id): return await c.answer()
    action, _, token = (c.data or "").partition(":")
    text = _pending_broadcasts.pop(token, None)   # pop: ikki marta bosilsa yoki Telegram qayta yuborsa ham faqat bir marta ketadi
    if text is None: return await c.answer("Muddati o'tgan yoki allaqachon yuborilgan", show_alert=True)
    if action != "bc_yes":
        if c.message: await c.message.edit_text("❌ Bekor qilindi.")
        return await c.answer()
    await audit.log(None, c.from_user.username or c.from_user.id, "[bot] Hammaga xabar yuborildi (broadcast)", "", text[:80])
    users = await db.get_users_for_sync()
    if c.message: await c.message.edit_text(f"⏳ {len(users)} ta foydalanuvchiga yuborilmoqda...")
    await c.answer()
    # Fonda yuboramiz: webhook javobi kechikmasin (kechiksa Telegram so'rovni qayta yuborib, xabar takrorlanishi mumkin)
    t = asyncio.create_task(_run_broadcast(c.message.chat.id if c.message else c.from_user.id, users, text))
    _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)

async def admin_reply_feedback(m: Message):
    """Admin feedback yoki foydalanuvchi xabariga reply qilib yozsa — javob o'sha odamga ketadi (sarlavhasiz, oddiy matn)."""
    if not _is_admin(m.from_user.id): return await relay_to_admin(m)   # oddiy foydalanuvchining reply'i ham adminga yetkazilsin
    src = m.reply_to_message
    src_text = (src.text or src.caption or "") if src else ""
    if not src or not src.from_user or not src.from_user.is_bot or not ("Yangi Feedback" in src_text or "Yangi xabar" in src_text): return
    mt = _REPLY_ID.search(src_text)   # birinchi "Mualif:/Kimdan:" qatoridagi OXIRGI (ID: ...) — ism ichiga soxta ID yozib bo'lmaydi
    text = (m.text or "").strip()
    if not mt or not text: return
    if len(text) > MAX_MSG_LEN: return await m.answer(f"❌ Xabar juda uzun (maksimum {MAX_MSG_LEN} belgi).")
    ok = await _send_html(int(mt.group(1)), _html.escape(text, quote=False))
    await m.answer("✅ Javob yuborildi" if ok else "❌ Yuborib bo'lmadi (botni bloklagan bo'lishi mumkin)")

ADMIN_COMMANDS = [BotCommand(command="send", description="Foydalanuvchiga xabar: /send @user matn"),
                  BotCommand(command="broadcast", description="Hammaga xabar: /broadcast matn"),
                  BotCommand(command="admins", description="Adminlar ro'yxati"),
                  BotCommand(command="hazillar", description="Hazillarni tahrirlash / fayl yuklash"),
                  BotCommand(command="suhbat", description="Do'stlar bilan suhbat")]
OWNER_COMMANDS = ADMIN_COMMANDS + [BotCommand(command="import", description="Eski zaxira JSON ni bazaga qo'shish"),
                                   BotCommand(command="addadmin", description="Admin qo'shish: /addadmin @user"),
                                   BotCommand(command="removeadmin", description="Adminni olib tashlash: /removeadmin @user")]
_USER_CMDS = notify.user_commands("uz")   # adminlar menyusida ham oddiy buyruqlar ko'rinsin
ADMIN_COMMANDS = ADMIN_COMMANDS + _USER_CMDS
OWNER_COMMANDS = OWNER_COMMANDS + _USER_CMDS

async def _set_menu(chat_id, commands):
    try: await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as e: logger.warning(f"Buyruqlar menyusi o'rnatilmadi ({chat_id}): {e}")

async def cmd_admins(m: Message):
    if not _is_admin(m.from_user.id): return
    rows = await db.get_admin_rows(ADMIN_ID)
    lines = [f"• {_who(r)} — ID {r['telegram_id']}" + (" 👑 bosh admin" if r["telegram_id"] == ADMIN_ID else "") for r in rows]
    if ADMIN_ID and not any(r["telegram_id"] == ADMIN_ID for r in rows): lines.insert(0, f"• Bosh admin — ID {ADMIN_ID} 👑")
    await m.answer(f"👑 <b>Adminlar ({len(lines)} ta):</b>\n" + _html.escape("\n".join(lines), quote=False), parse_mode="HTML")

_ADD_HELP = ("👑 <b>Admin qo'shish:</b>\n<code>/addadmin @username</code>\n<code>/addadmin 123456789</code> (Telegram ID)\n\n"
             "<b>Olib tashlash:</b>\n<code>/removeadmin @username</code>\n\nFoydalanuvchi avval botga /start yuborgan bo'lishi kerak.")

async def cmd_addadmin(m: Message, command: CommandObject):
    if not _is_owner(m.from_user.id): return   # faqat bosh admin: oddiy admin boshqa admin qo'sha olmaydi
    args = (command.args or "").split()
    if not args: return await m.answer(_ADD_HELP, parse_mode="HTML")
    u = await db.find_user_for_message(args[0])
    if not u: return await m.answer("❌ Foydalanuvchi topilmadi. U avval botga /start yuborishi kerak.")
    if u["telegram_id"] == ADMIN_ID: return await m.answer("ℹ️ Bu siz — bosh adminsiz.")
    if u["telegram_id"] in ADMIN_TG_IDS: return await m.answer(f"ℹ️ {_who(u)} allaqachon admin.")
    if not await db.set_admin(u["telegram_id"], True): return await m.answer("❌ Saqlab bo'lmadi.")
    await refresh_admins()
    await _set_menu(u["telegram_id"], ADMIN_COMMANDS)
    await _send_html(u["telegram_id"], "👑 <b>Sizga admin huquqi berildi!</b>\n\nMini ilovani yopib qayta oching — <b>⚡ Admin</b> tugmasi chiqadi.\nBot buyruqlari: /send, /broadcast, /admins")
    await _audit_tg(m, "Admin qo'shildi", _who(u))
    await m.answer(f"✅ {_who(u)} admin qilindi.")

async def cmd_removeadmin(m: Message, command: CommandObject):
    if not _is_owner(m.from_user.id): return
    args = (command.args or "").split()
    if not args: return await m.answer(_ADD_HELP, parse_mode="HTML")
    u = await db.find_user_for_message(args[0])
    if not u: return await m.answer("❌ Foydalanuvchi topilmadi.")
    if u["telegram_id"] == ADMIN_ID: return await m.answer("❌ Bosh adminni olib tashlab bo'lmaydi.")
    if u["telegram_id"] not in ADMIN_TG_IDS: return await m.answer(f"ℹ️ {_who(u)} admin emas.")
    if not await db.set_admin(u["telegram_id"], False): return await m.answer("❌ Saqlab bo'lmadi.")
    await refresh_admins()
    try: await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=u["telegram_id"]))
    except Exception: pass
    await _audit_tg(m, "Admin olib tashlandi", _who(u))
    await m.answer(f"✅ {_who(u)} endi admin emas.")

# --- ESKI ZAXIRA (JSON) NI BAZAGA QO'SHISH ---
_pending_import = {}   # bosh admin Telegram ID -> /import yuborgan vaqt (keyingi JSON fayl import qilinadi)
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # Bot API orqali bot ko'pi bilan 20 MB fayl yuklab ola oladi

def _import_summary(r):
    lines = ["✅ <b>Import tugadi</b>", "",
             f"👥 Foydalanuvchilar: <b>{r['users_new']}</b> ta yangi, {r['users_existing']} ta allaqachon bor edi" + (f", ❌ {r['users_failed']} ta xato" if r['users_failed'] else ""),
             f"🔑 Hero akkauntlar: <b>{r['acc_new']}</b> ta yangi, {r['acc_skipped']} ta takror (o'tkazildi)" + (f", ❌ {r['acc_failed']} ta xato" if r['acc_failed'] else ""),
             f"📦 Arxiv: <b>{r['arch_new']}</b> ta yangi, {r['arch_skipped']} ta o'tkazildi",
             f"⏱ Skaner tarixi: <b>{r['scans_new']}</b> ta yangi, {r['scans_skipped']} ta o'tkazildi"]
    if r["shadows"]: lines.append(f"🔗 Ulashilgan ulanishlar: {r['shadows']} ta tiklandi")
    lines += ["", "ℹ️ Hero tokenlari zaxirada yo'q, shuning uchun akkauntlar dastlab <b>Nofaol</b> ko'rinadi. "
              "Foydalanuvchi panelda 🔄 bosishi yoki bot o'zi (token-healer) asta-sekin tiklashi bilan Aktiv bo'ladi.",
              "👤 Eski foydalanuvchilar o'zlarining eski login/paroli bilan kiraveradi."]
    return "\n".join(lines)

async def run_import(raw: bytes):
    """JSON baytlarni o'qib bazaga qo'shadi. Xatoda ValueError. Qaytaradi: hisobot dict."""
    try: data = json.loads(raw.decode("utf-8-sig"))
    except Exception: raise ValueError("Fayl to'g'ri JSON emas.")
    rep = await db.import_backup_data(data)
    await refresh_admins(); await refresh_blocked_users()
    logger.info(f"📥 Import: {rep}")
    return rep

async def cmd_import(m: Message):
    if not _is_owner(m.from_user.id): return   # faqat bosh admin: faylda parollar bor
    _pending_import[m.from_user.id] = time.time()
    await m.answer("📥 <b>Eski zaxirani qo'shish</b>\n\nEndi shu chatga zaxira <b>.json</b> faylini yuboring (HeroScanner_Zaxira_....json).\n"
                   "Mavjud ma'lumotlar o'chmaydi — faqat yo'qlari qo'shiladi.\n\n"
                   "⚠️ Fayl 20 MB dan katta bo'lsa — admin paneldagi <b>📥 Zaxirani import qilish</b> tugmasidan foydalaning.", parse_mode="HTML")

def _import_doc_filter(m: Message):
    if not m.document or not m.from_user or not _is_owner(m.from_user.id): return False
    cap = (m.caption or "").strip().lower()
    t0 = _pending_import.get(m.from_user.id)
    return cap.startswith("/import") or bool(t0 and time.time() - t0 < 600)

async def on_import_document(m: Message):
    _pending_import.pop(m.from_user.id, None)
    await _audit_tg(m, "Zaxira JSON import (bot)", d_name(m))
    d = m.document
    if not (d.file_name or "").lower().endswith(".json"):
        return await m.answer("❌ Faqat .json fayl kerak. Qaytadan /import yuboring.")
    if d.file_size and d.file_size > TG_DOWNLOAD_LIMIT:
        return await m.answer("❌ Fayl 20 MB dan katta — Telegram bot orqali yuklab bo'lmaydi. Admin paneldagi 📥 tugmasidan foydalaning.")
    wait = await m.answer("⏳ Import qilinmoqda...")
    try:
        buf = await bot.download(d)
        rep = await run_import(buf.read())
    except ValueError as e:
        return await wait.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception("Import xatosi")
        return await wait.edit_text(f"❌ Import xatosi: {_html.escape(str(e)[:300])}")
    await wait.edit_text(_import_summary(rep), parse_mode="HTML")

async def admin_import(request):
    """Admin paneldan JSON zaxirani yuklash (fayl so'rov tanasi sifatida keladi). Faqat bosh admin."""
    u_id, role = verify_token(request)
    if not u_id or role != "super_admin": return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
    if not _is_owner(await db.get_telegram_id(u_id) or 0): return web.json_response({"status": "error", "message": "Faqat bosh admin import qila oladi"}, status=403)
    raw = await request.read()
    try: rep = await run_import(raw)
    except ValueError as e: return web.json_response({"status": "error", "message": str(e)})
    except Exception as e:
        logger.exception("Import xatosi")
        return web.json_response({"status": "error", "message": f"Import xatosi: {str(e)[:200]}"})
    msg = (f"✅ Import tugadi: {rep['users_new']} yangi user, {rep['acc_new']} yangi akkaunt, "
           f"{rep['scans_new']} skaner, {rep['arch_new']} arxiv (takrorlar o'tkazildi).")
    return web.json_response({"status": "success", "message": msg, "report": rep})

# --- KUNLIK AVTO-ZAXIRA ---
try: BACKUP_HOUR = min(23, max(0, int(os.getenv("BACKUP_HOUR", "3"))))   # Toshkent vaqti bilan soat (standart: 03:00)
except ValueError: BACKUP_HOUR = 3
_backup_fails = {}   # sana -> muvaffaqiyatsiz urinishlar soni

async def send_backup(chat_id, auto=False):
    """To'liq zaxirani (Excel + JSON) Telegram chatga fayl qilib yuboradi. auto=True — ovozsiz (jimgina) va 'Kunlik' sarlavhasi bilan."""
    import backup
    data = await db.get_full_backup_data()
    xlsx_bytes, json_bytes, st, now_local = await asyncio.to_thread(backup.build_files, data)
    base = f"HeroScanner_Zaxira_{now_local:%Y-%m-%d_%H-%M}"
    title = "🗓 <b>Kunlik avto-zaxira</b>" if auto else "📊 <b>HeroScanner — to'liq zaxira (Excel)</b>"
    await bot.send_document(chat_id, BufferedInputFile(xlsx_bytes, filename=f"{base}.xlsx"), parse_mode="HTML", disable_notification=auto,
        caption=(f"{title}\n🕒 {now_local:%d.%m.%Y %H:%M} (Toshkent vaqti)\n\n"
                 f"👥 Foydalanuvchilar: {st['users']}\n🔑 Noyob Hero akkauntlar: {st['unique_accounts']}\n"
                 f"📦 Arxiv: {st['archived']}\n⏱ Skanerlar: {st['scans']}"))
    await bot.send_document(chat_id, BufferedInputFile(json_bytes, filename=f"{base}.json"), disable_notification=auto,
        caption="🗄 Bazani to'liq tiklash uchun JSON nusxa.\n⚠️ Ichida parollar bor — hech kimga bermang.")
    return st

async def daily_backup_tick():
    """Bitta tekshiruv: belgilangan soat kelgan va bugungi zaxira hali yuborilmagan bo'lsa — bosh adminga jimgina yuboradi.
    Sana bazada saqlanadi, shuning uchun qayta deploy/restart bo'lsa ham bir kunda ikki marta ketmaydi;
    server o'sha soatda o'chiq bo'lgan bo'lsa, yoqilishi bilan kechiktirib yuboradi."""
    if not bot or not ADMIN_ID: return False
    import backup
    now_local = backup.utc_now() + timedelta(hours=5)   # Toshkent vaqti
    today = now_local.strftime("%Y-%m-%d")
    if now_local.hour < BACKUP_HOUR or _backup_fails.get(today, 0) >= 3: return False
    if await db.get_setting("last_auto_backup") == today: return False
    try:
        st = await send_backup(ADMIN_ID, auto=True)
    except Exception as e:
        _backup_fails[today] = _backup_fails.get(today, 0) + 1
        logger.error(f"Avto-zaxira xatosi ({_backup_fails[today]}/3): {e}")
        return False
    await db.set_setting("last_auto_backup", today)
    logger.info(f"💾 Kunlik avto-zaxira yuborildi: {st['users']} foydalanuvchi, {st['unique_accounts']} noyob akkaunt")
    return True

async def daily_backup_loop():
    await asyncio.sleep(180)   # ishga tushgach biroz kutamiz
    while True:
        try: await daily_backup_tick()
        except Exception as e: logger.error(f"Avto-zaxira tsikli xatosi: {e}")
        await asyncio.sleep(600)   # har 10 daqiqada tekshiradi

# --- FOYDALANUVCHI XABARINI ADMINGA YETKAZISH ---
_relay_times = {}   # user_id -> oxirgi xabar vaqtlari (spamdan himoya)
_TYPE_UZ = {"photo": "rasm", "video": "video", "voice": "ovozli xabar", "audio": "audio", "document": "fayl", "sticker": "stiker",
            "video_note": "video xabar", "animation": "GIF", "contact": "kontakt", "location": "joylashuv", "poll": "so'rovnoma"}

def _sender_label(u):
    name = " ".join(x for x in (u.first_name, u.last_name) if x)
    if u.username: return f"{name} (@{u.username})" if name else f"@{u.username}"
    return name or str(u.id)

async def relay_to_admin(m: Message):
    """Foydalanuvchi botga yozgan xabarni (kimdan + nima deb yozgani) bosh adminga yetkazadi.
    Admin o'sha xabarga reply qilsa, javob shu foydalanuvchiga ketadi."""
    if not ADMIN_ID or not m.from_user or m.from_user.is_bot or _is_admin(m.from_user.id): return
    now = time.time()
    recent = [t for t in _relay_times.get(m.from_user.id, []) if now - t < 60]
    if len(recent) >= 8: return   # spamdan himoya: bir foydalanuvchidan daqiqasiga 8 tadan ortig'i o'tkazilmaydi
    recent.append(now); _relay_times[m.from_user.id] = recent
    text = m.text or m.caption or ""
    if len(text) > 3500: text = text[:3500] + "…"
    if m.text: content = text
    else:
        kind = str(m.content_type.value)
        content = f"[{_TYPE_UZ.get(kind, kind)}]" + (f" {text}" if text else "")
    await _send_html(ADMIN_ID, f"📨 <b>Yangi xabar</b>\n\nKimdan: {_html.escape(_sender_label(m.from_user), quote=False)} (ID: {m.from_user.id})\nXabar: {_html.escape(content, quote=False)}")
    if not m.text:   # rasm/ovoz/fayl va h.k. — o'zini ham ko'rsatamiz
        try: await bot.copy_message(ADMIN_ID, m.chat.id, m.message_id)
        except Exception as e: logger.warning(f"Media nusxalanmadi: {e}")

# --- BIR MARTALIK TOZALASH: avval yuborilgan '📞 Raqamimni ulashish' tugmasini foydalanuvchilar chatidan olib tashlash ---
async def _drop_reply_keyboard(chat_id):
    """Telegram'da chatdagi klaviaturani faqat yangi xabar orqali olib tashlash mumkin: jimgina yuboramiz va darhol o'chiramiz."""
    for _ in range(2):
        try:
            msg = await bot.send_message(chat_id, "✅", reply_markup=ReplyKeyboardRemove(), disable_notification=True)
            try: await bot.delete_message(chat_id, msg.message_id)
            except Exception: pass
            return True
        except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after + 1)
        except Exception: return False
    return False

async def remove_phone_keyboards():
    await asyncio.sleep(30)
    try:
        if not bot or await db.get_setting("phone_kb_removed"): return
        users = await db.get_users_for_sync()
        done = 0
        for u in users:
            if await _drop_reply_keyboard(u["telegram_id"]): done += 1
            await asyncio.sleep(0.05)
        if done or not users: await db.set_setting("phone_kb_removed", "1")
        logger.info(f"🧹 '📞 Raqamimni ulashish' tugmasi chatlardan olib tashlandi: {done}/{len(users)}")
    except Exception as e:
        logger.error(f"Tugmani tozalashda xato: {e}")

async def handle_webhook(request):
    if bot:
        try: await dp.feed_update(bot, Update(**await request.json()))
        except Exception as e: logger.error(f"Webhook xatosi: {e}")   # Telegram qayta-qayta yubormasligi uchun doim 200 qaytaramiz
    return web.Response()

_NO_CACHE = {"Cache-Control": "no-cache"}

async def index(request):
    p = os.path.join(BASE_DIR, "scanner.html")
    return web.FileResponse(p, headers=_NO_CACHE) if os.path.exists(p) else web.Response(text="Fayl topilmadi", status=404)

async def logo(request):
    p = os.path.join(BASE_DIR, "logo.jpg")
    return web.FileResponse(p, headers={"Cache-Control": "public, max-age=86400"}) if os.path.exists(p) else web.Response(status=404)

async def about(request):
    p = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(p): return web.Response(text="Fayl topilmadi", status=404)
    with open(p, encoding="utf-8") as f: page = f.read().replace("your_bot", BOT_USERNAME)
    return web.Response(text=page, content_type="text/html", headers=_NO_CACHE)

async def main():
    app = web.Application(client_max_size=100 * 1024 * 1024, middlewares=[audit.make_middleware(verify_token)])   # /api/admin/import katta JSON qabul qilishi uchun
    app.router.add_get("/", index); app.router.add_get("/logo.jpg", logo); app.router.add_get("/about", about)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    app.router.add_post("/api/auth/login", auth_login); app.router.add_get("/api/users", get_users_list)
    app.router.add_post("/api/users/add", add_user); app.router.add_post("/api/users/delete", delete_user)
    app.router.add_post("/api/users/edit", edit_user); app.router.add_post("/api/users/reload", reload_user_token) 
    app.router.add_get("/api/stats", get_stats); app.router.add_post("/api/scan", do_scan)
    app.router.add_post("/api/feedback", submit_feedback)
    
    app.router.add_post("/api/admin/set_shadow", admin_set_shadow); app.router.add_post("/api/admin/extend", admin_extend_trial)
    app.router.add_post("/api/admin/backup", admin_backup); app.router.add_post("/api/admin/import", admin_import); app.router.add_post("/api/admin/sync_usernames", admin_sync_usernames); app.router.add_post("/api/admin/toggle_block", admin_toggle_block); app.router.add_get("/api/admin/all_data", get_admin_all_data)
    app.router.add_get("/api/admin/transcripts", admin_transcripts); app.router.add_get("/api/admin/transcript", admin_transcript_detail); app.router.add_post("/api/admin/transcript_refresh", admin_transcript_refresh); app.router.add_get("/api/admin/transcript_file", admin_transcript_file); app.router.add_post("/api/admin/transcript_send", admin_transcript_send)
    app.router.add_get("/api/admin/jokes", admin_jokes); app.router.add_post("/api/admin/jokes_import", admin_jokes_import); app.router.add_post("/api/admin/jokes_update", admin_jokes_update); app.router.add_post("/api/admin/jokes_delete", admin_jokes_delete); app.router.add_post("/api/admin/jokes_send", admin_jokes_send)
    extras.register(app, {"verify_token": verify_token, "verify_hero_account": verify_hero_account, "spawn_schedule": _spawn_schedule, "bot": bot})
    app.router.add_get("/api/admin/report", admin_report); app.router.add_post("/api/admin/report_send", admin_report_send)
    app.router.add_get("/api/admin/lessons", admin_lessons); app.router.add_post("/api/admin/lessons_toggle", admin_lessons_toggle); app.router.add_get("/api/admin/schedules", admin_schedules); app.router.add_get("/api/admin/schedule", admin_schedule_detail); app.router.add_post("/api/admin/schedule_refresh", admin_schedule_refresh)

    app.router.add_post("/api/share/generate", share_generate); app.router.add_post("/api/share/import", share_import)
    app.router.add_get("/api/share/connections", share_connections)
    
    if bot and BOT_TOKEN: app.router.add_post(f"/webhook/{BOT_TOKEN}", handle_webhook)

    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start(); logger.info("✅ Veb-server ishga tushdi")
    await db.init_db()
    await jokes.seed_if_empty()
    for _name, _init in (('hazil ovozlari', jokes.init_tables), ('bildirishnomalar', notify.init_tables), ('jurnal', audit.init_table)):
        try: await _init()
        except Exception as e: logger.error(f"{_name} jadvallari yaratilmadi: {e}")
    try: await friends.init_tables()
    except Exception as e: logger.error(f"Suhbat jadvallari yaratilmadi: {e}")
    try: await refresh_admins()
    except Exception as e: logger.error(f"Adminlarni yuklashda xato: {e}")
    try: await refresh_blocked_users()
    except Exception as e: logger.error(f"Bloklanganlarni yuklashda xato: {e}")
    try:
        done, miss = await db.backfill_student_info()
        logger.info(f"🎓 Talabalar ro'yxati ({students.count()} qator): {done} ta akkauntga ism/ID yozildi, {miss} ta topilmadi")
    except Exception as e: logger.error(f"Ism/ID to'ldirishda xato: {e}")
    _bg(token_healer_loop())
    _bg(schedule_loop())
    _bg(username_sync_loop())
    _bg(daily_backup_loop())
    _bg(remove_phone_keyboards())

    if bot and WEBAPP_URL:
        try: await bot.set_webhook(f"{WEBAPP_URL}/webhook/{BOT_TOKEN}")
        except Exception as e: logger.error(f"Webhook o'rnatilmadi: {e}")
        @dp.message(Command("start", "login"))
        async def l(m: Message):
            friends._st.pop(m.from_user.id, None)   # yarim qolgan suhbat qadamini tozalaymiz
            u, p, ends = await db.get_or_create_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
            await notify.ensure_lang(m.from_user.id, m.from_user.language_code)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Panelga Kirish", web_app=WebAppInfo(url=WEBAPP_URL))]])
            if p:
                await m.answer(f"Xush kelibsiz!\n\n👤 Login: `{u}`\n🔑 Pass: `{p}`\n⏳ Muddat: {(ends + TASHKENT_OFFSET).strftime('%d-%m-%Y')}", parse_mode="Markdown", reply_markup=kb)
            else:
                await m.answer(f"Siz allaqachon ro'yxatdan o'tgansiz!\n\n👤 Login: `{u}`\n⏳ Muddat: {(ends + TASHKENT_OFFSET).strftime('%d-%m-%Y')}\n\nParolni unutgan bo'lsangiz /reset yuboring.", parse_mode="Markdown", reply_markup=kb)

        @dp.message(Command("reset"))
        async def r(m: Message):
            friends._st.pop(m.from_user.id, None)
            u, p = await db.reset_password(m.from_user.id)
            if u: await m.answer(f"🔑 Yangi parol berildi.\n\n👤 Login: `{u}`\n🔑 Pass: `{p}`", parse_mode="Markdown")
            else: await m.answer("Avval /start yuboring.")

        # Admin: foydalanuvchilarga xabar yuborish (faqat ADMIN_ID ishlata oladi)
        dp.message.register(cmd_send, Command("send"))
        dp.message.register(cmd_broadcast, Command("broadcast"))
        dp.message.register(cmd_admins, Command("admins"))
        dp.message.register(cmd_addadmin, Command("addadmin"))
        dp.message.register(cmd_removeadmin, Command("removeadmin"))
        await friends.setup(dp, bot, lambda: set(ADMIN_TG_IDS) | ({ADMIN_ID} if ADMIN_ID else set()))   # relay_to_admin'dan OLDIN
        await notify.setup(dp, bot, lambda: set(ADMIN_TG_IDS) | ({ADMIN_ID} if ADMIN_ID else set()), refresh_transcript)
        notify.start(_bg)
        dp.message.register(cmd_jokes, Command("hazillar"))
        dp.message.register(cmd_jokes_file, Command("hazillar_fayl"))
        dp.message.register(on_jokes_document, F.document, _jokes_doc_filter)   # relay_to_admin'dan OLDIN
        dp.message.register(cmd_import, Command("import"))
        dp.message.register(on_import_document, F.document, _import_doc_filter)   # relay_to_admin'dan OLDIN bo'lishi shart
        dp.callback_query.register(cb_broadcast, F.data.startswith("bc_"))
        dp.message.register(admin_reply_feedback, F.reply_to_message, F.text, ~F.text.startswith("/"))
        dp.message.register(relay_to_admin, F.chat.type == "private")   # eng oxirida: boshqa hech kim olmagan xabarlar
        if ADMIN_ID:  # "/" menyusida buyruqlar faqat adminlarga ko'rinadi
            await _set_menu(ADMIN_ID, OWNER_COMMANDS)
            for tid in ADMIN_TG_IDS - {ADMIN_ID}: await _set_menu(tid, ADMIN_COMMANDS)
    while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
    except Exception:
        logger.exception("Dastur kutilmagan xato bilan to'xtadi")
        raise
