"""Do'stlar bilan suhbat (/suhbat).

  💬 Do'stga yozish          — @username bo'yicha, anonim yoki ism bilan
  🙋 Darsdoshlarga iltimos   — shu darsda o'qiydigan hammaga (jadval bo'yicha) bitta iltimos
  🔎 Darsdoshlarni topish    — bir xil darsda o'qiydiganlar ro'yxati, tanlab yozish
  ⚙️ Sozlamalar              — xabar qabul qilish / ro'yxatda ko'rinish / bloklanganlar

Xavfsizlik: har bir xabarda «Javob / Bloklash / Shikoyat» tugmalari bor; tezlik cheklovi; havolalar taqiqlangan;
anonim xabarda ham yuboruvchini qabul qiluvchi ko'rmaydi, lekin admin panelda barcha xabarlar (kim kimga yozgani) ko'rinadi — bu yuboruvchiga oldindan aytiladi.
Anonim xabarga qabul qiluvchi javob yoza oladi (↩️ Javob tugmasi yoki Telegram'ning reply'i) — javob ham anonim ketadi.
"""
import asyncio
import html
import logging
import random
import re
import secrets
import time
from datetime import timedelta

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BotCommandScopeDefault, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import database as db
import hero_schedule

logger = logging.getLogger(__name__)

MAX_LEN = 500
STATE_TTL = 600            # soniya: yarim qolgan suhbat qadami shuncha vaqt eslab qolinadi
MAX_RECIPIENTS = 40        # bitta iltimos ko'pi bilan shuncha darsdoshga ketadi
PAGE = 8
LIMIT_DM_10MIN, LIMIT_DM_DAY, LIMIT_REQ_HOUR = 8, 60, 3
_LINK = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.I)

TEMPLATES = [
    "🙏 Kimdir QR kodni skaner qilib yubora oladimi?",
    "📝 Dars konspektini (rasmini) tashlab bera olasizmi?",
    "❓ Bugungi dars nima haqida bo'ldi?",
    "📚 Uy vazifasi nima edi?",
]

_bot = None
_admin_ids = lambda: set()
_st = {}                                  # telegram_id -> {"step":..., "ts":..., ...}
_gcache = {"t": 0, "g": None}
TEXT_STEPS = {"dm_user", "dm_text", "rq_text", "reply"}
ANON_NOTE = ("🎭 <b>Anonim</b> — qabul qiluvchi ismingizni ko'rmaydi.\n"
             "↩️ Qabul qiluvchi sizga anonim javob yoza oladi.\n"
             "ℹ️ Xavfsizlik uchun admin barcha xabarlarni (kim kimga yozganini) ko'ra oladi.")


# ----------------------------------------------------------------- baza

async def init_tables():
    async with db.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friend_msgs (
                id SERIAL PRIMARY KEY, from_uid INTEGER NOT NULL, to_uid INTEGER NOT NULL,
                anonymous BOOLEAN NOT NULL DEFAULT TRUE, kind VARCHAR(10) NOT NULL DEFAULT 'dm',
                lesson TEXT, text TEXT NOT NULL, reply_to INTEGER, batch VARCHAR(24),
                reported BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS friend_msgs_from_idx ON friend_msgs (from_uid, created_at);
            CREATE TABLE IF NOT EXISTS friend_blocks (
                user_id INTEGER NOT NULL, blocked_uid INTEGER NOT NULL, PRIMARY KEY (user_id, blocked_uid)
            );
            CREATE TABLE IF NOT EXISTS friend_prefs (
                user_id INTEGER PRIMARY KEY, accept BOOLEAN DEFAULT TRUE, findable BOOLEAN DEFAULT TRUE
            );
            ALTER TABLE friend_msgs ADD COLUMN IF NOT EXISTS reviewed BOOLEAN DEFAULT FALSE;
            ALTER TABLE friend_msgs ADD COLUMN IF NOT EXISTS tg_msg_id BIGINT;
            ALTER TABLE friend_msgs ADD COLUMN IF NOT EXISTS delivered BOOLEAN;
            ALTER TABLE friend_msgs ADD COLUMN IF NOT EXISTS error TEXT;
            CREATE INDEX IF NOT EXISTS friend_msgs_to_tg_idx ON friend_msgs (to_uid, tg_msg_id);
            CREATE TABLE IF NOT EXISTS friend_bans (
                user_id INTEGER PRIMARY KEY, until TIMESTAMP NOT NULL, reason TEXT, by_admin BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW()
            );
        """)


AUTO_BAN_REPORTERS, AUTO_BAN_DAYS = 3, 3     # 3 xil odam shikoyat qilsa — 3 kunga avtomatik chetlatiladi


async def ban_until(uid):
    """Chetlatilgan bo'lsa — qaysi vaqtgacha (UTC), aks holda None."""
    async with db.pool.acquire() as conn:
        v = await conn.fetchval("SELECT until FROM friend_bans WHERE user_id=$1 AND until > NOW()", int(uid))
    return v


async def ban(uid, days, reason="", by_admin=True):
    async with db.pool.acquire() as conn:
        await conn.execute("INSERT INTO friend_bans (user_id, until, reason, by_admin) VALUES ($1, NOW() + make_interval(days => $2), $3, $4) "
                           "ON CONFLICT (user_id) DO UPDATE SET until = NOW() + make_interval(days => $2), reason=$3, by_admin=$4, created_at=NOW()",
                           int(uid), int(days), (reason or "")[:200], by_admin)


async def unban(uid):
    async with db.pool.acquire() as conn:
        return (await conn.execute("DELETE FROM friend_bans WHERE user_id=$1", int(uid))) == "DELETE 1"


def _ban_text(until):
    return f"⛔ Siz {(until + timedelta(hours=5)):%d.%m.%Y %H:%M} gacha suhbat funksiyasidan chetlatilgansiz."


async def _me(tg_id):
    async with db.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT id, telegram_id, username, full_name, COALESCE(is_blocked, FALSE) AS is_blocked "
                                "FROM app_users WHERE telegram_id=$1", int(tg_id))
    return dict(r) if r else None


async def _user(uid):
    async with db.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT id, telegram_id, username, full_name, COALESCE(is_blocked, FALSE) AS is_blocked "
                                "FROM app_users WHERE id=$1", int(uid))
    return dict(r) if r else None


async def _find_by_username(name):
    name = re.sub(r"^(https?://)?(t\.me/)?@?", "", (name or "").strip(), flags=re.I).strip("/ ")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", name):
        return None
    async with db.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT id, telegram_id, username, full_name, COALESCE(is_blocked, FALSE) AS is_blocked "
                                "FROM app_users WHERE lower(username)=lower($1) AND telegram_id IS NOT NULL ORDER BY id DESC LIMIT 1", name)
    return dict(r) if r else None


async def _display(uid):
    """Ko'rinadigan ism: Hero'dagi haqiqiy ism, bo'lmasa Telegram ismi yoki @username."""
    async with db.pool.acquire() as conn:
        n = await conn.fetchval("SELECT full_name FROM hero_accounts WHERE user_id=$1 AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1", int(uid))
        u = await conn.fetchrow("SELECT username, full_name FROM app_users WHERE id=$1", int(uid))
    name = n or (u and u["full_name"]) or (u and u["username"] and "@" + u["username"]) or "Foydalanuvchi"
    return name, (u["username"] if u else None)


async def _prefs(uid):
    async with db.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT accept, findable FROM friend_prefs WHERE user_id=$1", int(uid))
    return {"accept": r["accept"], "findable": r["findable"]} if r else {"accept": True, "findable": True}


async def _set_pref(uid, key, val):
    assert key in ("accept", "findable")
    async with db.pool.acquire() as conn:
        await conn.execute(f"INSERT INTO friend_prefs (user_id, {key}) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET {key}=$2", int(uid), val)


async def _is_blocked_by(receiver_uid, sender_uid):
    async with db.pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT 1 FROM friend_blocks WHERE user_id=$1 AND blocked_uid=$2", int(receiver_uid), int(sender_uid)))


async def _limit_error(uid, kind):
    async with db.pool.acquire() as conn:
        if kind == "req":
            n = await conn.fetchval("SELECT COUNT(DISTINCT batch) FROM friend_msgs WHERE from_uid=$1 AND kind='req' AND created_at > NOW() - INTERVAL '1 hour'", int(uid)) or 0
            return f"⏳ Bir soatda {LIMIT_REQ_HOUR} tadan ortiq iltimos yuborib bo'lmaydi. Keyinroq urinib ko'ring." if n >= LIMIT_REQ_HOUR else None
        n10 = await conn.fetchval("SELECT COUNT(*) FROM friend_msgs WHERE from_uid=$1 AND kind<>'req' AND created_at > NOW() - INTERVAL '10 minutes'", int(uid)) or 0
        n24 = await conn.fetchval("SELECT COUNT(*) FROM friend_msgs WHERE from_uid=$1 AND kind<>'req' AND created_at > NOW() - INTERVAL '1 day'", int(uid)) or 0
    if n10 >= LIMIT_DM_10MIN: return "⏳ Juda tez yozyapsiz. Bir necha daqiqadan keyin urinib ko'ring."
    if n24 >= LIMIT_DM_DAY: return "⏳ Bugungi xabar limiti tugadi. Ertaga davom ettirasiz."
    return None


# ----------------------------------------------------------------- darsdoshlar

_gcache_lock = asyncio.Lock()
_ulocks = {}   # foydalanuvchi_id -> Lock: bir foydalanuvchining yuborishlari ketma-ket (limitni parallel so'rovlar bilan aylanib o'tib bo'lmaydi)


def _ulock(uid):
    if len(_ulocks) > 5000: _ulocks.clear()
    return _ulocks.setdefault(int(uid), asyncio.Lock())


async def _groups():
    if _gcache["g"] is None or time.time() - _gcache["t"] > 90:
        async with _gcache_lock:
            if _gcache["g"] is None or time.time() - _gcache["t"] > 90:   # kutib turganda boshqasi yangilagan bo'lishi mumkin
                rows = await db.get_all_schedules()
                _gcache["g"] = await asyncio.to_thread(hero_schedule.group_lessons, rows)
                _gcache["t"] = time.time()
    return _gcache["g"]


async def _my_lessons(uid):
    async with db.pool.acquire() as conn:
        emails = {r["email"] for r in await conn.fetch("SELECT email FROM hero_accounts WHERE user_id=$1", int(uid))}
    if not emails: return []
    today = hero_schedule.today_str()
    now_t = hero_schedule.datetime.now(hero_schedule.TZ).strftime("%H:%M")
    def _not_finished(g):   # bugungi, allaqachon tugagan darslarni taklif qilmaymiz
        return not (g.get("date") == today and g.get("end") and str(g["end"]) < now_t)
    mine = [dict(g, key="|".join(str(x) for x in k)) for k, g in (await _groups()).items() if g["emails"] & emails and (g.get("date") or "") >= today and _not_finished(g)]
    mine.sort(key=lambda g: (g["date"], g["start"]))
    return mine[:10]


async def lesson_by_key(uid, key):
    """Dars ro'yxatidagi o'rniga barqaror kalit bo'yicha topadi (ro'yxat o'zgarib qolsa ham noto'g'ri darsga yuborilmaydi)."""
    for g in await _my_lessons(uid):
        if g.get("key") == key: return g
    return None


def _lesson_label(g):
    d = g["date"][8:10] + "." + g["date"][5:7]
    return f"{g.get('title') or 'Dars'} · {d} {g['start']}"


async def _classmates(uid, group, mode):
    """mode='find' — ro'yxatda ko'rinishga rozilar; mode='send' — xabar qabul qilishga rozilar va meni bloklamaganlar."""
    cond = "COALESCE(p.findable, TRUE)" if mode == "find" else "COALESCE(p.accept, TRUE) AND NOT EXISTS (SELECT 1 FROM friend_blocks b WHERE b.user_id=u.id AND b.blocked_uid=$2)"
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (a.user_id) a.user_id AS uid, a.full_name, u.username, u.telegram_id FROM hero_accounts a "
            "JOIN app_users u ON u.id = a.user_id LEFT JOIN friend_prefs p ON p.user_id = u.id "
            f"WHERE a.email = ANY($1::text[]) AND a.user_id <> $2 AND u.telegram_id IS NOT NULL AND COALESCE(u.is_blocked, FALSE) = FALSE AND {cond} "
            "ORDER BY a.user_id, a.id", list(group["emails"]), int(uid))
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (r["full_name"] or r["username"] or "~").lower())
    return out


# ----------------------------------------------------------------- yuborish

def _esc(t): return html.escape(str(t or ""), quote=False)


def _msg_kb(mid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Javob", callback_data=f"fr:rp:{mid}"),
        InlineKeyboardButton(text="🚫 Bloklash", callback_data=f"fr:bl:{mid}"),
        InlineKeyboardButton(text="⚠️ Shikoyat", callback_data=f"fr:rep:{mid}")]])


async def _send_ex(chat_id, text, kb=None):
    """-> (telegram message_id, None) yoki (None, xato matni). Xato sababi logga yoziladi."""
    err = "noma'lum xato"
    for _ in range(2):
        try:
            msg = await _bot.send_message(chat_id, text[:4000], parse_mode="HTML", reply_markup=kb)
            return msg.message_id, None
        except TelegramRetryAfter as e:
            err = "Telegram limiti"; await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning(f"Suhbat xabari yuborilmadi ({chat_id}): {type(e).__name__}: {e}")
            return None, f"{type(e).__name__}: {e}"[:300]
        except Exception as e:
            logger.warning(f"Suhbat xabari yuborilmadi ({chat_id}): {e}"); return None, f"{type(e).__name__}: {e}"[:300]
    return None, err


async def _safe_send(chat_id, text, kb=None):
    return (await _send_ex(chat_id, text, kb))[0] is not None


def _fail_text(err):
    return ("⚠️ Yetkazib bo'lmadi: qabul qiluvchi bu botni ishga tushirmagan yoki to'xtatgan.\n"
            "U botga /start yuborib, keyin qayta urinib ko'rsin.") if err else "⚠️ Yetkazib bo'lmadi."


async def _deliver(frm, to, text, anonymous, kind, lesson=None, reply_to=None, batch=None):
    """Xabarni bazaga yozadi va qabul qiluvchiga yuboradi. -> (yetkazildimi, xato matni)."""
    async with db.pool.acquire() as conn:
        mid = await conn.fetchval("INSERT INTO friend_msgs (from_uid, to_uid, anonymous, kind, lesson, text, reply_to, batch) "
                                  "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                                  int(frm["id"]), int(to["id"] if "id" in to else to["uid"]), anonymous, kind, lesson, text, reply_to, batch)
    who = ""
    if not anonymous:
        name, uname = await _display(frm["id"])
        who = f"<b>{_esc(name)}</b>" + (f" (@{_esc(uname)})" if uname else "")
    les = f"\n📚 <i>{_esc(lesson)}</i>" if lesson else ""
    if kind == "req":
        head = "🙋 <b>Anonim iltimos</b>" if anonymous else f"🙋 {who} darsdoshingizdan iltimos"
    elif kind == "reply":
        head = "↩️ <b>Anonim javob</b>" if anonymous else f"↩️ {who} javob berdi"
    else:
        head = "🎭 <b>Anonim xabar</b>" if anonymous else f"💬 {who} sizga yozdi"
    tail = "\n\n<i>↩️ Javob berish uchun pastdagi tugmani bosing yoki shu xabarga reply qiling.</i>"
    tg_id, err = await _send_ex(to["telegram_id"], f"{head}{les}\n\n{_esc(text)}{tail}", _msg_kb(mid))
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE friend_msgs SET delivered=$2, tg_msg_id=$3, error=$4 WHERE id=$1", mid, tg_id is not None, tg_id, err)
    except Exception as e:
        logger.warning(f"Suhbat holati yozilmadi: {e}")
    return tg_id is not None, err


# ----------------------------------------------------------------- holat (qadamlar)

def _get_state(tg_id):
    s = _st.get(tg_id)
    if s and time.time() - s["ts"] > STATE_TTL:
        _st.pop(tg_id, None); return None
    return s


def _set_state(tg_id, **kw):
    kw["ts"] = time.time(); _st[tg_id] = kw


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


MENU_KB = _kb([[("💬 Do'stga yozish", "fr:m:dm"), ("🙋 Darsdoshlarga iltimos", "fr:m:rq")],
               [("🔎 Darsdoshlarni topish", "fr:m:find")], [("⚙️ Sozlamalar", "fr:m:set")]])
CANCEL_KB = _kb([[("❌ Bekor qilish", "fr:x")]])
MENU_TEXT = ("💬 <b>Do'stlar bilan suhbat</b>\n\nDo'stingizga yozing (anonim yoki ismingiz bilan), darsdoshlaringizdan iltimos qiling "
             "yoki bir xil darsda o'qiydiganlarni toping.\n\n<i>Havola yuborish taqiqlangan. Haqorat va spam — bloklanish sababi.</i>")


async def _edit(c: CallbackQuery, text, kb=None):
    try:
        await c.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower(): return   # bir xil matn — dublikat xabar yubormaymiz
        await c.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ----------------------------------------------------------------- buyruqlar

async def cmd_suhbat(m: Message, command: CommandObject):
    me = await _me(m.from_user.id)
    if not me: return await m.answer("Avval /start yuboring.")
    if me["is_blocked"]: return
    _st.pop(m.from_user.id, None)
    bn = await ban_until(me["id"])
    if bn: return await m.answer(_ban_text(bn))
    args = (command.args or "").strip()
    if not args:
        return await m.answer(MENU_TEXT, parse_mode="HTML", reply_markup=MENU_KB)
    # /suhbat @username [matn]
    first, rest = (args.split(None, 1) + [""])[:2]
    ok, target = await _check_target(me, first)
    if not ok: return await m.answer(target)
    rest = rest.strip()
    if not rest:
        _set_state(m.from_user.id, step="dm_text", to=target["id"])
        return await m.answer(f"✍️ <b>{_esc(await _name_of(target))}</b> uchun xabar matnini yozing (500 belgigacha):", parse_mode="HTML", reply_markup=CANCEL_KB)
    err = _text_error(rest)
    if err: return await m.answer(err)
    _set_state(m.from_user.id, step="anon", kind="dm", to=target["id"], text=rest)
    await m.answer(await _anon_prompt("dm", target), parse_mode="HTML", reply_markup=_anon_kb())


async def _clear_on_command(m: Message):
    """Boshqa buyruq (/hazil, /davomat...) yozilsa, yarim qolgan suhbat qadami o'chadi — keyingi oddiy xabar adashib darsdoshga ketmasin."""
    cmd = (m.text or "").split()[0].split("@")[0].lower() if m.text else ""
    if cmd not in ("/suhbat", "/bekor") and m.from_user: _st.pop(m.from_user.id, None)
    raise SkipHandler


async def cmd_cancel(m: Message):
    if _st.pop(m.from_user.id, None): await m.answer("❌ Bekor qilindi.")


async def _name_of(row):
    n, u = await _display(row["id"])
    return n + (f" (@{u})" if u else "")


def _text_error(t):
    t = (t or "").strip()
    if not t: return "Matn bo'sh. Qaytadan yozing."
    if len(t) > MAX_LEN: return f"Matn juda uzun ({len(t)}). {MAX_LEN} belgigacha yozing."
    if _LINK.search(t): return "🚫 Havola yuborish taqiqlangan. Havolasiz yozing."
    return None


async def _check_target(me, username):
    """-> (True, user_row) yoki (False, xato matni)."""
    t = await _find_by_username(username)
    if not t or t["is_blocked"]: return False, "Bunday foydalanuvchi botda topilmadi. U avval botga /start yuborgan bo'lishi kerak."
    if t["id"] == me["id"]: return False, "O'zingizga yozib bo'lmaydi 🙂"
    if not (await _prefs(t["id"]))["accept"] or await _is_blocked_by(t["id"], me["id"]):
        return False, "Bu foydalanuvchiga xabar yuborib bo'lmaydi."
    return True, t


async def _anon_prompt(kind, target=None, n=None):
    to = f"👥 <b>{n}</b> ta darsdoshga" if kind == "req" else f"<b>{_esc(await _name_of(target))}</b> ga"
    return f"{to} yuboriladi. Qanday yuboramiz?\n\n{ANON_NOTE}"


def _anon_kb():
    return _kb([[("🎭 Anonim", "fr:an:1"), ("👤 Ismim bilan", "fr:an:0")], [("❌ Bekor qilish", "fr:x")]])


# ----------------------------------------------------------------- matn qadamlari

def _text_filter(m: Message):
    if not m.text or m.text.startswith("/") or not m.from_user or m.chat.type != "private": return False
    s = _get_state(m.from_user.id)
    return bool(s and s["step"] in TEXT_STEPS)


async def _native_reply_filter(m: Message):
    """Qabul qilingan suhbat xabariga Telegram'ning «reply» funksiyasi bilan javob yozilsa — {"rmid": friend_msgs.id}."""
    if not m.text or m.text.startswith("/") or not m.from_user or m.chat.type != "private" or not m.reply_to_message: return False
    s = _get_state(m.from_user.id)
    if s and s["step"] in TEXT_STEPS: return False
    try:
        async with db.pool.acquire() as conn:
            mid = await conn.fetchval("SELECT f.id FROM friend_msgs f JOIN app_users u ON u.id = f.to_uid "
                                      "WHERE u.telegram_id=$1 AND f.tg_msg_id=$2 ORDER BY f.id DESC LIMIT 1", int(m.from_user.id), int(m.reply_to_message.message_id))
    except Exception:
        return False
    return {"rmid": mid} if mid else False


async def on_native_reply(m: Message, rmid: int):
    me = await _me(m.from_user.id)
    if not me or me["is_blocked"]: return
    bn = await ban_until(me["id"])
    if bn: return await m.answer(_ban_text(bn))
    txt = (m.text or "").strip()
    err = _text_error(txt)
    if err: return await m.answer(err)
    await _send_reply(m, me, rmid, txt)


async def on_text(m: Message):
    s = _get_state(m.from_user.id)
    me = await _me(m.from_user.id)
    if not s or not me or me["is_blocked"]: return
    bn = await ban_until(me["id"])
    if bn:
        _st.pop(m.from_user.id, None); return await m.answer(_ban_text(bn))
    txt = (m.text or "").strip()
    step = s["step"]
    if step == "dm_user":
        ok, t = await _check_target(me, txt.split()[0] if txt else "")
        if not ok: return await m.answer(t + "\n\nBoshqa @username yuboring yoki /bekor.")
        _set_state(m.from_user.id, step="dm_text", to=t["id"])
        return await m.answer(f"✍️ <b>{_esc(await _name_of(t))}</b> uchun xabar matnini yozing (500 belgigacha):", parse_mode="HTML", reply_markup=CANCEL_KB)
    err = _text_error(txt)
    if err: return await m.answer(err)
    if step == "dm_text":
        t = await _user(s["to"])
        _set_state(m.from_user.id, step="anon", kind="dm", to=s["to"], text=txt)
        return await m.answer(await _anon_prompt("dm", t), parse_mode="HTML", reply_markup=_anon_kb())
    if step == "rq_text":
        rc = await _classmates(me["id"], s["group"], "send")
        _set_state(m.from_user.id, step="anon", kind="req", group=s["group"], text=txt)
        return await m.answer(await _anon_prompt("req", n=min(len(rc), MAX_RECIPIENTS)), parse_mode="HTML", reply_markup=_anon_kb())
    if step == "reply":
        await _send_reply(m, me, s["mid"], txt)


async def _send_reply(m: Message, me, mid, text):
    async with _ulock(me["id"]):
        return await _send_reply_inner(m, me, mid, text)


async def _send_reply_inner(m: Message, me, mid, text):
    async with db.pool.acquire() as conn:
        orig = await conn.fetchrow("SELECT id, from_uid, to_uid, anonymous, lesson FROM friend_msgs WHERE id=$1", int(mid))
    _st.pop(m.from_user.id, None)
    if not orig or orig["to_uid"] != me["id"]: return await m.answer("Xabar topilmadi.")
    target = await _user(orig["from_uid"])
    if not target or target["is_blocked"] or await _is_blocked_by(target["id"], me["id"]):
        return await m.answer("Bu foydalanuvchiga javob yuborib bo'lmaydi.")
    lim = await _limit_error(me["id"], "dm")
    if lim: return await m.answer(lim)
    ok, err = await _deliver(me, target, text, orig["anonymous"], "reply", orig["lesson"], reply_to=orig["id"])
    await m.answer("✅ Javob yuborildi." + ("\n🎭 Ismingiz ko'rsatilmadi." if orig["anonymous"] else "") if ok else _fail_text(err))


# ----------------------------------------------------------------- tugmalar

async def on_cb(c: CallbackQuery):
    parts = (c.data or "").split(":")
    act = parts[1] if len(parts) > 1 else ""
    me = await _me(c.from_user.id)
    if not me or me["is_blocked"]:
        return await c.answer("Avval /start yuboring", show_alert=True)
    tg = c.from_user.id
    if act not in ("x", "bl", "rep", "s", "rp"):
        bn = await ban_until(me["id"])
        if bn: return await c.answer(_ban_text(bn), show_alert=True)
    try:
        if act == "x":
            _st.pop(tg, None); await _edit(c, "❌ Bekor qilindi.\n\n/suhbat — menyu"); return await c.answer()
        if act == "m": return await _on_menu(c, me, parts[2] if len(parts) > 2 else "menu")
        if act in ("ls", "fl"): return await _on_lesson_pick(c, me, act, int(parts[2]))
        if act == "tp": return await _on_template(c, me, parts[2])
        if act == "pg": return await _on_page(c, int(parts[2]))
        if act == "wu": return await _on_write_to(c, me, int(parts[2]))
        if act == "an": return await _on_anon(c, me, parts[2] == "1")
        if act == "s": return await _on_setting(c, me, parts[2])
        if act in ("rp", "bl", "rep"): return await _on_msg_action(c, me, act, int(parts[2]))
    except (ValueError, IndexError):
        pass
    except Exception:
        logger.exception("Suhbat tugmasi xatosi")
        try: return await c.answer("Xatolik. Qayta urinib ko'ring.", show_alert=True)
        except Exception: return
    try: await c.answer()
    except Exception: pass


async def _on_menu(c, me, what):
    tg = c.from_user.id
    if what == "menu":
        _st.pop(tg, None); await _edit(c, MENU_TEXT, MENU_KB)
    elif what == "dm":
        _set_state(tg, step="dm_user")
        await _edit(c, "💬 Do'stingizning <b>@username</b>'ini yuboring.\n<i>(U botga /start yuborgan bo'lishi kerak.)</i>", CANCEL_KB)
    elif what in ("rq", "find"):
        lessons = await _my_lessons(me["id"])
        if not lessons:
            await _edit(c, "📭 Jadvalingizdan yaqin darslar topilmadi.\nMini-ilovada Hero akkauntingiz ulangan va jadval yangilangan bo'lishi kerak.", _kb([[("⬅️ Orqaga", "fr:m:menu")]]))
            return await c.answer()
        _set_state(tg, step="pick", lessons=lessons, mode=what)
        pre = "ls" if what == "rq" else "fl"
        title = "🙋 Qaysi dars bo'yicha iltimos qilamiz?" if what == "rq" else "🔎 Qaysi darsdagi darsdoshlarni ko'rmoqchisiz?"
        rows = [[(f"📚 {_lesson_label(g)}"[:60], f"fr:{pre}:{i}")] for i, g in enumerate(lessons)] + [[("⬅️ Orqaga", "fr:m:menu")]]
        await _edit(c, title, _kb(rows))
    elif what == "set":
        await _show_settings(c, me)
    await c.answer()


async def _on_lesson_pick(c, me, act, i):
    tg = c.from_user.id
    s = _get_state(tg)
    if not s or "lessons" not in s or i >= len(s["lessons"]): return await c.answer("Eskirgan. /suhbat dan qayta boshlang.", show_alert=True)
    g = s["lessons"][i]
    if act == "ls":   # iltimos
        rc = await _classmates(me["id"], g, "send")
        if not rc:
            return await c.answer("Bu darsda xabar qabul qiladigan boshqa foydalanuvchi topilmadi.", show_alert=True)
        _set_state(tg, step="tpl", group=g)
        rows = [[(t[:60], f"fr:tp:{k}")] for k, t in enumerate(TEMPLATES)] + [[("✍️ O'z matnim", "fr:tp:c")], [("⬅️ Orqaga", "fr:m:rq")]]
        await _edit(c, f"🙋 <b>{_esc(_lesson_label(g))}</b>\n👥 {min(len(rc), MAX_RECIPIENTS)} ta darsdosh\n\nIltimos matnini tanlang:", _kb(rows))
    else:             # topish
        cm = await _classmates(me["id"], g, "find")
        _set_state(tg, step="find", group=g, cm=cm, lessons=s["lessons"])
        await _show_page(c, 0)
    await c.answer()


async def _on_template(c, me, which):
    tg = c.from_user.id
    s = _get_state(tg)
    if not s or "group" not in s: return await c.answer("Eskirgan. /suhbat dan qayta boshlang.", show_alert=True)
    if which == "c":
        _set_state(tg, step="rq_text", group=s["group"])
        await _edit(c, "✍️ Iltimos matnini yozing (500 belgigacha):", CANCEL_KB)
    else:
        text = TEMPLATES[int(which)]
        rc = await _classmates(me["id"], s["group"], "send")
        _set_state(tg, step="anon", kind="req", group=s["group"], text=text)
        await _edit(c, f"«{_esc(text)}»\n\n" + await _anon_prompt("req", n=min(len(rc), MAX_RECIPIENTS)), _anon_kb())
    await c.answer()


async def _show_page(c, page):
    s = _get_state(c.from_user.id)
    cm = s["cm"]
    pages = max(1, (len(cm) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = cm[page * PAGE:(page + 1) * PAGE]
    head = f"🔎 <b>{_esc(_lesson_label(s['group']))}</b>\n"
    if not cm:
        return await _edit(c, head + "\nBu darsda ro'yxatda ko'rinishga rozi boshqa foydalanuvchi topilmadi.", _kb([[("⬅️ Orqaga", "fr:m:find")]]))
    lines = [f"{page * PAGE + k + 1}. {_esc(r['full_name'] or ('@' + r['username'] if r['username'] else 'Foydalanuvchi'))}" + (f" (@{_esc(r['username'])})" if r["username"] and r["full_name"] else "")
             for k, r in enumerate(chunk)]
    rows = [[(f"✉️ {page * PAGE + k + 1}. {(r['full_name'] or r['username'] or 'Yozish')[:24]}", f"fr:wu:{page * PAGE + k}")] for k, r in enumerate(chunk)]
    nav = []
    if page > 0: nav.append(("◀️", f"fr:pg:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "fr:pg:%d" % page))
    if page < pages - 1: nav.append(("▶️", f"fr:pg:{page + 1}"))
    rows += [nav, [("⬅️ Orqaga", "fr:m:find")]]
    _set_state(c.from_user.id, **{k: v for k, v in s.items() if k != "ts"})
    await _edit(c, f"{head}👥 Jami: {len(cm)} ta darsdosh\n\n" + "\n".join(lines) + "\n\nYozish uchun ismini bosing:", _kb(rows))


async def _on_page(c, page):
    s = _get_state(c.from_user.id)
    if not s or "cm" not in s: return await c.answer("Eskirgan. /suhbat dan qayta boshlang.", show_alert=True)
    await _show_page(c, page); await c.answer()


async def _on_write_to(c, me, k):
    tg = c.from_user.id
    s = _get_state(tg)
    if not s or "cm" not in s or k >= len(s["cm"]): return await c.answer("Eskirgan. /suhbat dan qayta boshlang.", show_alert=True)
    t = await _user(s["cm"][k]["uid"])
    if not t or t["is_blocked"] or not (await _prefs(t["id"]))["accept"] or await _is_blocked_by(t["id"], me["id"]):
        return await c.answer("Bu foydalanuvchiga xabar yuborib bo'lmaydi.", show_alert=True)
    _set_state(tg, step="dm_text", to=t["id"])
    await _edit(c, f"✍️ <b>{_esc(await _name_of(t))}</b> uchun xabar matnini yozing (500 belgigacha):", CANCEL_KB)
    await c.answer()


async def _on_anon(c, me, anonymous):
    async with _ulock(me["id"]):
        return await _on_anon_inner(c, me, anonymous)


async def _on_anon_inner(c, me, anonymous):
    tg = c.from_user.id
    s = _get_state(tg)
    if not s or s["step"] != "anon": return await c.answer("Eskirgan. /suhbat dan qayta boshlang.", show_alert=True)
    _st.pop(tg, None)   # darhol: ikki marta bosilsa ham xabar bir marta ketadi
    kind, text = s["kind"], s["text"]
    lim = await _limit_error(me["id"], "req" if kind == "req" else "dm")
    if lim:
        _st.pop(tg, None); await _edit(c, lim); return await c.answer()
    if kind == "dm":
        t = await _user(s["to"])
        if not t or t["is_blocked"] or not (await _prefs(t["id"]))["accept"] or await _is_blocked_by(t["id"], me["id"]):
            _st.pop(tg, None); await _edit(c, "Bu foydalanuvchiga xabar yuborib bo'lmaydi."); return await c.answer()
        _st.pop(tg, None)
        await _edit(c, "⏳ Yuborilmoqda...")
        ok, err = await _deliver(me, t, text, anonymous, "dm")
        await _edit(c, ("✅ " + ("Anonim x" if anonymous else "X") + "abar yuborildi.\nJavob shu chatga keladi.") if ok else _fail_text(err))
    else:
        g = s["group"]
        _st.pop(tg, None)
        rc = (await _classmates(me["id"], g, "send"))
        if len(rc) > MAX_RECIPIENTS: rc = random.sample(rc, MAX_RECIPIENTS)
        if not rc:
            await _edit(c, "Bu darsda xabar qabul qiladigan boshqa foydalanuvchi topilmadi."); return await c.answer()
        try: await c.answer()
        except Exception: pass
        await _edit(c, "⏳ Yuborilmoqda...")
        batch = f"{me['id']}-{int(time.time())}-{secrets.token_hex(2)}"
        sent = 0
        for r in rc:
            try:
                if (await _deliver(me, r, text, anonymous, "req", _lesson_label(g), batch=batch))[0]: sent += 1
            except Exception:
                logger.exception("Iltimos yuborishda xato")
            await asyncio.sleep(0.06)
        await _edit(c, f"✅ {sent} ta darsdoshga yuborildi.\nJavoblar shu chatga keladi." + ("\n🎭 Ismingiz ko'rsatilmadi." if anonymous else ""))
    try: await c.answer()
    except Exception: pass


async def _show_settings(c, me):
    p = await _prefs(me["id"])
    async with db.pool.acquire() as conn:
        nb = await conn.fetchval("SELECT COUNT(*) FROM friend_blocks WHERE user_id=$1", int(me["id"])) or 0
    acc_txt = "✅ yoqilgan" if p["accept"] else "❌ o'chirilgan"
    rows = [[(f"📥 Xabar qabul qilish: {acc_txt}", "fr:s:accept")],
            [(f"🔎 Darsdoshlar ro'yxatida ko'rinish: {'✅' if p['findable'] else '❌'}", "fr:s:find")]]
    if nb: rows.append([(f"🚫 Bloklanganlarni tozalash ({nb})", "fr:s:unb")])
    rows.append([("⬅️ Orqaga", "fr:m:menu")])
    await _edit(c, "⚙️ <b>Sozlamalar</b>\n\n• Xabar qabul qilishni o'chirsangiz, hech kim (darsdoshlar ham) sizga yoza olmaydi.\n"
                   "• Ro'yxatdan chiqsangiz, darsdoshlar sizni qidiruvda ko'rmaydi.", _kb(rows))


async def _on_setting(c, me, what):
    if what == "accept": await _set_pref(me["id"], "accept", not (await _prefs(me["id"]))["accept"])
    elif what == "find": await _set_pref(me["id"], "findable", not (await _prefs(me["id"]))["findable"])
    elif what == "unb":
        async with db.pool.acquire() as conn: await conn.execute("DELETE FROM friend_blocks WHERE user_id=$1", int(me["id"]))
    await _show_settings(c, me); await c.answer("Saqlandi")


async def _on_msg_action(c, me, act, mid):
    async with db.pool.acquire() as conn:
        m = await conn.fetchrow("SELECT id, from_uid, to_uid, anonymous, kind, lesson, text, reported FROM friend_msgs WHERE id=$1", mid)
    if not m or m["to_uid"] != me["id"]: return await c.answer("Xabar topilmadi.", show_alert=True)
    if act == "rp":
        _set_state(c.from_user.id, step="reply", mid=mid)
        await c.message.answer("↩️ Javobingizni yozing (500 belgigacha)." + ("\n\n" + ANON_NOTE if m["anonymous"] else "") + "\n\n/bekor — bekor qilish", parse_mode="HTML")
        return await c.answer()
    if act == "bl":
        await block_sender(me, mid)
        try: await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest: pass
        await c.message.answer("🚫 Yuboruvchi bloklandi. U endi sizga yoza olmaydi. (Sozlamalardan qaytarish mumkin.)")
        return await c.answer()
    if act == "rep":
        ok, text = await report_message(me, mid)
        await c.answer(text, show_alert=True)
        if ok:
            try: await c.message.edit_reply_markup(reply_markup=_msg_kb_after_report(mid))
            except TelegramBadRequest: pass


async def report_message(me, mid):
    """Xabarga shikoyat (bot va mini-ilova uchun umumiy). -> (muvaffaqiyatmi, foydalanuvchiga matn)."""
    async with db.pool.acquire() as conn:
        m = await conn.fetchrow("SELECT id, from_uid, to_uid, anonymous, kind, lesson, text, reported FROM friend_msgs WHERE id=$1", int(mid))
    if not m or m["to_uid"] != me["id"]: return False, "Xabar topilmadi."
    if m["reported"]: return False, "Shikoyat allaqachon yuborilgan."
    async with db.pool.acquire() as conn:
        if (await conn.execute("UPDATE friend_msgs SET reported=TRUE WHERE id=$1 AND NOT COALESCE(reported, FALSE)", int(mid))) != "UPDATE 1":
            return False, "Shikoyat allaqachon yuborilgan."
        since = "GREATEST(NOW() - INTERVAL '30 days', COALESCE((SELECT created_at FROM friend_bans WHERE user_id=$1), '-infinity'))"   # chetlatishdan oldingi shikoyatlar qayta sanalmaydi
        n = await conn.fetchval(f"SELECT COUNT(*) FROM friend_msgs WHERE from_uid=$1 AND reported AND created_at > {since}", int(m["from_uid"])) or 0
        reporters = await conn.fetchval(f"SELECT COUNT(DISTINCT to_uid) FROM friend_msgs WHERE from_uid=$1 AND reported AND created_at > {since}", int(m["from_uid"])) or 0
    sname, suser = await _display(m["from_uid"]); rname, ruser = await _display(m["to_uid"])
    sender = await _user(m["from_uid"])
    anon_txt = "ha" if m["anonymous"] else "yo'q"
    txt = (f"⚠️ <b>Suhbat shikoyati</b> (xabar #{m['id']})\n\n👤 Yuboruvchi: <b>{_esc(sname)}</b>" + (f" @{_esc(suser)}" if suser else "") +
           f" · ID <code>{sender['telegram_id'] if sender else '?'}</code>\n🎭 Anonim: {anon_txt}"
           f"\n📩 Shikoyat qilgan: {_esc(rname)}" + (f" @{_esc(ruser)}" if ruser else "") +
           (f"\n📚 {_esc(m['lesson'])}" if m["lesson"] else "") + f"\n\n📝 {_esc(m['text'])}\n\n📊 Shu yuboruvchiga so'nggi 30 kunda shikoyatlar: {n} ({reporters} kishidan)")
    if reporters >= AUTO_BAN_REPORTERS and not await ban_until(m["from_uid"]):
        await ban(m["from_uid"], AUTO_BAN_DAYS, f"Avtomatik: {reporters} kishi shikoyat qildi", by_admin=False)
        txt += f"\n\n🔒 Avtomatik {AUTO_BAN_DAYS} kunga suhbatdan chetlatildi (admin panel → 🛡 Suhbat)."
        if sender: await _safe_send(sender["telegram_id"], f"⛔ Ko'p shikoyat tufayli suhbat funksiyasi {AUTO_BAN_DAYS} kunga cheklandi.")
    for aid in _admin_ids():
        await _safe_send(aid, txt)
    return True, "⚠️ Shikoyat adminga yuborildi."


async def block_sender(me, mid):
    async with db.pool.acquire() as conn:
        m = await conn.fetchrow("SELECT from_uid, to_uid FROM friend_msgs WHERE id=$1", int(mid))
        if not m or m["to_uid"] != me["id"]: return False
        await conn.execute("INSERT INTO friend_blocks (user_id, blocked_uid) VALUES ($1,$2) ON CONFLICT DO NOTHING", int(me["id"]), int(m["from_uid"]))
    return True


# ----------------------------------------------------------------- mini-ilova (API) uchun umumiy funksiyalar

async def api_send_dm(me, target, text, anonymous):
    async with _ulock(me["id"]):
        return await _api_send_dm(me, target, text, anonymous)


async def _api_send_dm(me, target, text, anonymous):
    """-> (ok, matn). Bot bilan bir xil qoidalar: chetlatish, havola taqiqi, bloklar, limit."""
    bn = await ban_until(me["id"])
    if bn: return False, _ban_text(bn)
    err = _text_error(text)
    if err: return False, err
    if not target or target["is_blocked"]: return False, "Foydalanuvchi topilmadi."
    if target["id"] == me["id"]: return False, "O'zingizga yozib bo'lmaydi 🙂"
    if not (await _prefs(target["id"]))["accept"] or await _is_blocked_by(target["id"], me["id"]):
        return False, "Bu foydalanuvchiga xabar yuborib bo'lmadi."
    lim = await _limit_error(me["id"], "dm")
    if lim: return False, lim
    ok, e = await _deliver(me, target, text.strip(), bool(anonymous), "dm")
    if ok: return True, "✅ " + ("Anonim xabar" if anonymous else "Xabar") + " yuborildi."
    return False, _fail_text(e)


async def api_send_reply(me, mid, text):
    async with _ulock(me["id"]):
        return await _api_send_reply(me, mid, text)


async def _api_send_reply(me, mid, text):
    bn = await ban_until(me["id"])
    if bn: return False, _ban_text(bn)
    err = _text_error(text)
    if err: return False, err
    async with db.pool.acquire() as conn:
        orig = await conn.fetchrow("SELECT id, from_uid, to_uid, anonymous, lesson FROM friend_msgs WHERE id=$1", int(mid))
    if not orig or orig["to_uid"] != me["id"]: return False, "Xabar topilmadi."
    target = await _user(orig["from_uid"])
    if not target or target["is_blocked"] or await _is_blocked_by(target["id"], me["id"]):
        return False, "Bu foydalanuvchiga javob yuborib bo'lmaydi."
    lim = await _limit_error(me["id"], "dm")
    if lim: return False, lim
    ok, e = await _deliver(me, target, text.strip(), orig["anonymous"], "reply", orig["lesson"], reply_to=orig["id"])
    if ok: return True, "✅ Javob yuborildi." + (" Ismingiz ko'rsatilmadi." if orig["anonymous"] else "")
    return False, _fail_text(e)


async def api_send_request(me, group, text, anonymous):
    async with _ulock(me["id"]):
        return await _api_send_request(me, group, text, anonymous)


async def _api_send_request(me, group, text, anonymous):
    bn = await ban_until(me["id"])
    if bn: return False, _ban_text(bn)
    err = _text_error(text)
    if err: return False, err
    lim = await _limit_error(me["id"], "req")
    if lim: return False, lim
    rc = await _classmates(me["id"], group, "send")
    if len(rc) > MAX_RECIPIENTS: rc = random.sample(rc, MAX_RECIPIENTS)
    if not rc: return False, "Bu darsda xabar qabul qiladigan boshqa foydalanuvchi topilmadi."
    batch = f"{me['id']}-{int(time.time())}-{secrets.token_hex(2)}"
    sent = 0
    for r in rc:
        try:
            if (await _deliver(me, r, text.strip(), bool(anonymous), "req", _lesson_label(group), batch=batch))[0]: sent += 1
        except Exception:
            logger.exception("Iltimos yuborishda xato")
        await asyncio.sleep(0.06)
    if not sent: return False, "Hech kimga yetkazib bo'lmadi (darsdoshlar bu botni ishga tushirmagan bo'lishi mumkin)."
    return True, f"✅ {sent} ta darsdoshga yuborildi. Javoblar «Kelgan» bo'limiga tushadi."


def _msg_kb_after_report(mid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Javob", callback_data=f"fr:rp:{mid}"),
        InlineKeyboardButton(text="🚫 Bloklash", callback_data=f"fr:bl:{mid}")]])


# ----------------------------------------------------------------- ulash

async def setup(dp, bot, admin_ids):
    """main.py dan chaqiriladi: handlerlarni ro'yxatdan o'tkazadi (relay_to_admin'dan OLDIN bo'lishi shart)."""
    global _bot, _admin_ids
    _bot, _admin_ids = bot, admin_ids
    dp.message.register(_clear_on_command, F.text.startswith("/"), F.chat.type == "private")
    dp.message.register(cmd_suhbat, Command("suhbat"))
    dp.message.register(cmd_cancel, Command("bekor"))
    dp.message.register(on_text, _text_filter)
    dp.message.register(on_native_reply, _native_reply_filter)
    dp.callback_query.register(on_cb, F.data.startswith("fr:"))
    try:
        await bot.set_my_commands([BotCommand(command="suhbat", description="Do'stlar bilan suhbat")], scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning(f"/suhbat menyusi o'rnatilmadi: {e}")
