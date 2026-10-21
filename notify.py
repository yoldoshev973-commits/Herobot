"""Bildirishnomalar va foydalanuvchi buyruqlari.

Fon vazifalar (hammasi bir martalik "once" kalit bilan takrorlanmaydi, qayta ishga tushsa ham):
  ⏰ dars eslatmasi (boshlanishidan ~10 daqiqa oldin)    
  🎓 yangi baho xabari (transcript o'zgarsa)               🩺 akkaunt muammosi haqida ogohlantirish + adminga kunlik xulosa
  📅 haftalik davomat xulosasi (yakshanba 18:00 dan keyin)
Buyruqlar: /davomat /hazil /sozlamalar /til  (+ /suhbat — friends.py)
"""
import asyncio
import hashlib
import html
import logging
import random
import re
from datetime import timedelta

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (BotCommand, BotCommandScopeDefault, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import database as db
import friends
import hero_transcript
import i18n
import jokes
import stats
from i18n import t

logger = logging.getLogger(__name__)

GRADES_INTERVAL = int(__import__("os").getenv("GRADES_INTERVAL_HOURS", "6")) * 3600
ctx = {"bot": None, "admin_ids": lambda: set(), "refresh_transcript": None}
_lang_cache = {}
_problem_seen = {}      # email -> ketma-ket nechta tekshiruvda muammo bo'lgani
PREF_KEYS = ("remind", "weekly", "grades")


def esc(v):
    return html.escape(str(v or ""), quote=False)


# ----------------------------------------------------------------- baza: sozlamalar va "bir marta" kalitlari

async def init_tables():
    async with db.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY, lang VARCHAR(2) DEFAULT 'uz', remind BOOLEAN DEFAULT TRUE,
                weekly BOOLEAN DEFAULT TRUE, grades BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS sent_notices (key VARCHAR(200) PRIMARY KEY, at TIMESTAMP DEFAULT NOW());
        """)
        await conn.execute("DELETE FROM sent_notices WHERE at < NOW() - INTERVAL '45 days'")


async def once(key):
    """Bu kalit bilan xabar hali yuborilmagan bo'lsa True (va belgilab qo'yadi). Qayta ishga tushganda ham takrorlanmaydi."""
    async with db.pool.acquire() as conn:
        return (await conn.execute("INSERT INTO sent_notices (key) VALUES ($1) ON CONFLICT DO NOTHING", key[:200])) == "INSERT 0 1"


async def get_prefs(uid):
    async with db.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT lang, remind, weekly, grades FROM user_prefs WHERE user_id=$1", int(uid))
    return dict(r) if r else {"lang": "uz", "remind": True, "weekly": True, "grades": True}


async def set_pref(uid, key, val):
    assert key in PREF_KEYS or key == "lang"
    async with db.pool.acquire() as conn:
        await conn.execute(f"INSERT INTO user_prefs (user_id, {key}) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET {key}=$2", int(uid), val)


async def set_lang(uid, tg_id, lang):
    if lang not in i18n.LANGS:
        return
    await set_pref(uid, "lang", lang)
    _lang_cache[tg_id] = lang


async def lang_of(tg_id):
    if tg_id in _lang_cache:
        return _lang_cache[tg_id]
    async with db.pool.acquire() as conn:
        v = await conn.fetchval("SELECT COALESCE(p.lang, 'uz') FROM app_users u LEFT JOIN user_prefs p ON p.user_id = u.id WHERE u.telegram_id=$1", int(tg_id))
    _lang_cache[tg_id] = v or "uz"
    return _lang_cache[tg_id]


async def ensure_lang(tg_id, language_code):
    """Foydalanuvchining tili hali tanlanmagan bo'lsa — Telegram tilidan o'rnatadi (/start da chaqiriladi)."""
    try:
        async with db.pool.acquire() as conn:
            uid = await conn.fetchval("SELECT id FROM app_users WHERE telegram_id=$1", int(tg_id))
            if uid and not await conn.fetchval("SELECT 1 FROM user_prefs WHERE user_id=$1", uid):
                lang = i18n.pick(language_code)
                await conn.execute("INSERT INTO user_prefs (user_id, lang) VALUES ($1, $2) ON CONFLICT DO NOTHING", uid, lang)
                _lang_cache[int(tg_id)] = lang
    except Exception as e:
        logger.warning(f"Til o'rnatilmadi: {e}")


async def owners_of(emails, primary_only=False):
    """{user_id: {"tg", "lang", "prefs", "emails": {email: ism}}} — shu akkauntlar egalari (bloklanmaganlar)."""
    emails = list(set(emails))
    if not emails:
        return {}
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT a.user_id, a.email, a.full_name, u.telegram_id, COALESCE(p.lang,'uz') AS lang, COALESCE(p.remind,TRUE) AS remind, "
            "COALESCE(p.weekly,TRUE) AS weekly, COALESCE(p.grades,TRUE) AS grades, a.is_primary, "
            "EXISTS(SELECT 1 FROM hero_accounts x WHERE x.user_id = a.user_id AND x.is_primary) AS has_primary "
            "FROM hero_accounts a JOIN app_users u ON u.id = a.user_id LEFT JOIN user_prefs p ON p.user_id = u.id "
            "WHERE a.email = ANY($1::text[]) AND u.telegram_id IS NOT NULL AND COALESCE(u.is_blocked, FALSE) = FALSE", emails)
    out = {}
    for r in rows:
        if primary_only and r["has_primary"] and not r["is_primary"]:
            continue   # ASOSIY akkaunti bor — eslatma faqat shu akkaunt darsida bo'lsa keladi
        o = out.setdefault(r["user_id"], {"tg": r["telegram_id"], "lang": r["lang"], "emails": {},
                                         "prefs": {k: r[k] for k in PREF_KEYS}})
        o["emails"][r["email"]] = r["full_name"] or r["email"]
    return out


async def user_emails(uid):
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT email, full_name, is_primary FROM hero_accounts WHERE user_id=$1 ORDER BY id", int(uid))
    prim = [r for r in rows if r["is_primary"]]
    if prim: rows = prim   # ASOSIY akkaunt belgilangan bo'lsa — davomat faqat o'shaniki
    return {r["email"]: (r["full_name"] or r["email"]) for r in rows}


def _cut(text, n=4000):
    """Uzun matnni qator chegarasida qirqadi (HTML teg o'rtasida emas)."""
    if len(text) <= n: return text
    i = text.rfind("\n", 0, n)
    return text[: i if i > n // 2 else n]


async def _send_status(tg_id, text, kb=None):
    """-> 'ok' | 'dead' (foydalanuvchi botni to'xtatgan/chat yo'q: qayta urinish foydasiz) | 'retry' (vaqtincha xato)."""
    plain = False
    for _ in range(3):
        try:
            if plain:
                await ctx["bot"].send_message(tg_id, re.sub(r"<[^>]+>", "", _cut(text)), parse_mode=None, reply_markup=kb)
            else:
                await ctx["bot"].send_message(tg_id, _cut(text), parse_mode="HTML", reply_markup=kb)
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            if not plain and "parse" in str(e).lower():   # HTML xato — oddiy matn sifatida bir marta urinib ko'ramiz
                plain = True; continue
            return "dead"
        except TelegramForbiddenError:
            return "dead"
        except Exception as e:
            logger.warning(f"Xabar yuborilmadi ({tg_id}): {e}"); return "retry"
    return "retry"


async def send(tg_id, text, kb=None):
    return (await _send_status(tg_id, text, kb)) == "ok"


async def release(key):
    """once() bilan band qilingan kalitni qaytaradi (xabar vaqtincha yuborilmagan bo'lsa, keyingi tekshiruvda qayta uriniladi)."""
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM sent_notices WHERE key=$1", key[:200])
    except Exception as e:
        logger.warning(f"Kalit bo'shatilmadi: {e}")


async def send_keyed(key, tg_id, text, kb=None):
    """Xabarni yuboradi; faqat VAQTINCHALIK xatoda (tarmoq/limit) kalitni bo'shatadi — eslatma yo'qolmaydi."""
    st = await _send_status(tg_id, text, kb)
    if st == "retry": await release(key)
    return st == "ok"


# ----------------------------------------------------------------- ⏰ dars va 📝 imtihon eslatmalari

async def reminder_tick():
    now = stats.now_local()
    today = now.strftime("%Y-%m-%d")
    due = []
    for g in (await stats.all_groups()).values():
        if g.get("date") != today:
            continue
        st, _ = stats.lesson_times(g)
        mins = (st - now).total_seconds() / 60 if st else None
        if mins is not None and 8 <= mins <= 16:
            due.append((g, int(round(mins))))
    if not due:
        return
    auto = (await db.get_setting("lesson_broadcast")) != "0"
    owners = await owners_of(set().union(*[g["emails"] for g, _ in due]), primary_only=True)
    for g, mins in due:
        for uid, o in owners.items():
            if not o["prefs"]["remind"] or not (set(o["emails"]) & g["emails"]):
                continue
            key = f"rem:{uid}:{stats.lesson_id(g)}"
            if not await once(key):
                continue
            lg = o["lang"]
            place = t(lg, "room", r=esc(g["room"])) if g.get("room") else ""
            await send_keyed(key, o["tg"], t(lg, "remind", title=esc(g.get("title") or "Dars"), start=g["start"], mins=mins, place=place,
                                  extra=t(lg, "remind_auto" if auto else "remind_manual")))
            await asyncio.sleep(0.05)


async def reminder_loop():
    await asyncio.sleep(75)
    while True:
        try:
            await reminder_tick()
        except Exception as e:
            logger.error(f"Eslatma tsikli xatosi: {e}")
        await asyncio.sleep(60)


# ----------------------------------------------------------------- 🎓 yangi baho

async def grade_alert(email, old_raw, new_raw):
    """refresh_transcript() dan chaqiriladi: eski va yangi transcript farqini egalariga yuboradi."""
    try:
        d = hero_transcript.diff(old_raw, new_raw)
        if not d["changes"] and not d["gpa"]:
            return
        sig = hashlib.sha1(repr([(k, c.get("title"), c.get("grade"), c.get("score")) for k, c, _ in d["changes"]] + [d["gpa"]]).encode()).hexdigest()[:14]
        for uid, o in (await owners_of([email])).items():
            gkey = f"gr:{uid}:{email}:{sig}"
            if not o["prefs"]["grades"] or not await once(gkey):
                continue
            lg = o["lang"]
            lines = [t(lg, "grade_head", name=esc(o["emails"].get(email, email)))]
            for kind, c, old in d["changes"][:12]:
                g = f"{c.get('grade') or ''} {('(' + str(c['score']) + ')') if c.get('score') is not None else ''}".strip() or "—"
                if kind == "changed":
                    og = f"{old.get('grade') or ''} {old.get('score') if old.get('score') is not None else ''}".strip() or "—"
                    lines.append(t(lg, "grade_chg", title=esc(c["title"]), old=esc(og), g=esc(g)))
                else:
                    lines.append(t(lg, "grade_new", title=esc(c["title"]), g=esc(g)))
            if d["gpa"]:
                lines.append(t(lg, "gpa_chg", old=d["gpa"][0], new=d["gpa"][1]))
            await send_keyed(gkey, o["tg"], "\n".join(lines))
    except Exception as e:
        logger.warning(f"Baho xabari xatosi ({email}): {e}")


async def grades_loop():
    """Har GRADES_INTERVAL soatda (standart 6) hamma faol akkaunt transcriptini sekin-asta yangilaydi; o'zgarsa grade_alert ishlaydi."""
    await asyncio.sleep(400)
    while True:
        try:
            async with db.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT ON (a.email) a.email FROM hero_accounts a JOIN app_users u ON u.id = a.user_id "
                    "WHERE COALESCE(u.is_blocked, FALSE) = FALSE AND a.bearer_token IS NOT NULL AND a.bearer_token <> 'NO_TOKEN' "
                    "AND a.bearer_token NOT LIKE 'ERROR%' ORDER BY a.email, a.id DESC")
            emails = [r["email"] for r in rows]
            random.shuffle(emails)
            for email in emails:
                try:
                    acc = await db.get_account_creds(email)
                    if acc and ctx["refresh_transcript"]:
                        await ctx["refresh_transcript"](email, acc.get("bearer_token"), acc.get("hero_password"))
                except Exception as e:
                    logger.warning(f"Transcript tekshiruvi xatosi ({email}): {e}")
                await asyncio.sleep(random.uniform(12, 40))
        except Exception as e:
            logger.error(f"Baho tsikli xatosi: {e}")
        await asyncio.sleep(GRADES_INTERVAL)


# ----------------------------------------------------------------- 🩺 salomatlik

async def health_tick():
    ov = await stats.health_overview()
    bad = [a for a in ov["accounts"] if a["status"] in ("login_error", "no_token", "failing")]
    now = stats.now_local()
    seen_now = set()
    for a in bad:
        seen_now.add(a["email"])
        _problem_seen[a["email"]] = _problem_seen.get(a["email"], 0) + 1
    for e in [e for e in _problem_seen if e not in seen_now]:
        _problem_seen.pop(e, None)               # tuzalgan — hisobni nolga qaytaramiz
    ordinal = now.date().toordinal() // 3        # bitta akkaunt haqida 3 kunda bir marta
    for a in bad:
        if _problem_seen.get(a["email"], 0) < 2 or not a.get("telegram_id"):
            continue                              # yangi qo'shilayotgan akkaunt vaqtincha tokensiz bo'lishi mumkin
        hkey = f"hl:{a['email']}:{ordinal}"
        if not await once(hkey):
            continue
        lg = await lang_of(a["telegram_id"])
        reason = (t(lg, "r_login") if a["status"] == "login_error" else t(lg, "r_notoken") if a["status"] == "no_token"
                  else t(lg, "r_failing", n=a["streak_fail"], msg=esc(a.get("msg") or "—")))
        await send_keyed(hkey, a["telegram_id"], t(lg, "health_user", name=esc(a["name"] or a["email"]), reason=reason))
        await asyncio.sleep(0.05)
    # adminlarga kunlik xulosa (09:00 dan keyin, bir marta)
    if bad and now.hour >= 9 and await once(f"hld:{now.strftime('%Y-%m-%d')}"):
        c = ov["counts"]
        lst = "\n".join(f"• {esc(a['name'] or a['email'])} — {a['status']}" + (f" ({a['streak_fail']}×)" if a["status"] == "failing" else "") for a in bad[:12])
        txt = (f"🩺 <b>Akkauntlar salomatligi</b>\n❌ Login xato: {c['login_error']}\n⛔ Tokensiz: {c['no_token']}\n⚠️ Ketma-ket kirmayotgan: {c['failing']}\n"
               f"📉 Bugun 0/N skanerlar: {ov['zero_today']} / {ov['scans_today']}\n\n{lst}" + (f"\n… va yana {len(bad) - 12} ta" if len(bad) > 12 else "") +
               "\n\nAdmin panel → 🩺 Salomatlik")
        for aid in ctx["admin_ids"]():
            await send(aid, txt)


async def health_loop():
    await asyncio.sleep(240)
    while True:
        try: await health_tick()
        except Exception as e: logger.error(f"Salomatlik tsikli xatosi: {e}")
        await asyncio.sleep(1800)


# ----------------------------------------------------------------- 📅 haftalik xulosa

def _name_line(lg, name):
    return esc(name)


async def weekly_tick():
    now = stats.now_local()
    if now.weekday() != 6 or now.hour < 18:
        return
    iso = now.isocalendar()
    async with db.pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT u.id, u.telegram_id FROM app_users u WHERE u.telegram_id IS NOT NULL AND COALESCE(u.is_blocked, FALSE) = FALSE "
            "AND EXISTS (SELECT 1 FROM hero_accounts a WHERE a.user_id = u.id)")
    for u in users:
        prefs = await get_prefs(u["id"])
        wkey = f"wk:{u['id']}:{iso[0]}-{iso[1]}"
        if not prefs["weekly"] or not await once(wkey):
            continue
        emails = await user_emails(u["id"])
        res = await stats.attendance(emails.keys())
        lg = prefs["lang"]
        parts = []
        for e, name in emails.items():
            r = res.get(e)
            if not r or not r["week"][1]:
                continue
            a, tt = r["week"]
            p = round(a * 100 / tt)
            txt = t(lg, "wk_head", name=esc(name)) + "\n" + t(lg, "wk_line", a=a, t=tt, p=p, bar=stats.bar(p))
            if r["streak"] > 1:
                txt += "\n" + t(lg, "att_streak", n=r["streak"])
            recent = [title for st, title in r["missed"] if st >= now - timedelta(days=7)]
            if recent:
                txt += "\n" + t(lg, "wk_worst", title=esc(max(set(recent), key=recent.count)))
            elif a == tt:
                txt += "\n" + t(lg, "wk_best")
            parts.append(txt)
        if parts:
            await send_keyed(wkey, u["telegram_id"], "\n\n".join(parts))
            await asyncio.sleep(0.1)


async def weekly_loop():
    await asyncio.sleep(500)
    while True:
        try: await weekly_tick()
        except Exception as e: logger.error(f"Haftalik xulosa xatosi: {e}")
        await asyncio.sleep(600)


# ----------------------------------------------------------------- buyruqlar

async def _ctx_user(m):
    me = await friends._me(m.from_user.id)
    if not me:
        await m.answer(t(i18n.pick(m.from_user.language_code), "start_first"))
        return None, None
    if me["is_blocked"]:
        return None, None
    await ensure_lang(m.from_user.id, m.from_user.language_code)
    return me, await lang_of(m.from_user.id)


async def cmd_attendance(m: Message):
    me, lg = await _ctx_user(m)
    if not me: return
    emails = await user_emails(me["id"])
    if not emails: return await m.answer(t(lg, "no_acc"))
    res = await stats.attendance(emails.keys())
    sent = 0
    for e, name in list(emails.items())[:5]:
        r = res.get(e)
        if not r or not r["total"]:
            continue
        sent += 1
        lines = [t(lg, "att_title", name=esc(name)), t(lg, "att_overall", a=r["attended"], t=r["total"], p=r["pct"], bar=stats.bar(r["pct"])),
                 t(lg, "att_period", a7=r["week"][0], t7=r["week"][1], a30=r["month"][0], t30=r["month"][1])]
        if r["streak"]:
            lines.append(t(lg, "att_streak", n=r["streak"]))
        subj = sorted(r["subjects"].items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[0]))[:8]
        if subj:
            lines.append("\n" + t(lg, "att_subjects"))
            for title, (a, tt) in subj:
                p = round(a * 100 / tt)
                lines.append(f"• {esc(title)} — {a}/{tt} ({p}%)" + (" ⚠️" if p < 75 else ""))
        if r["missed"]:
            lines.append("\n" + t(lg, "att_missed"))
            lines += [f"• {st:%d.%m %H:%M} — {esc(title)}" for st, title in r["missed"]]
        lines.append("\n" + t(lg, "att_note"))
        await m.answer("\n".join(lines)[:4000], parse_mode="HTML")
    if not sent:
        await m.answer(t(lg, "att_none"))


async def cmd_joke(m: Message):
    me, lg = await _ctx_user(m)
    if not me: return
    jid, text = await jokes.joke_of_the_day()
    up, down = await jokes.counts(jid) if jid else (0, 0)
    await m.answer(f"{t(lg, 'joke_daily')}\n\n{esc(text)}", parse_mode="HTML", reply_markup=jokes.vote_kb(jid, up, down))


def _settings_kb(prefs, lg):
    rows = [[InlineKeyboardButton(text=("✅ " if prefs[k] else "❌ ") + t(lg, f"set_{k}"), callback_data=f"nt:t:{k}")] for k in PREF_KEYS]
    rows.append([InlineKeyboardButton(text=t(lg, "set_lang", l=i18n.LANG_NAMES[lg]), callback_data="nt:lm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _lang_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=n, callback_data=f"nt:l:{c}")] for c, n in i18n.LANG_NAMES.items()])


async def cmd_settings(m: Message):
    me, lg = await _ctx_user(m)
    if not me: return
    await m.answer(t(lg, "set_title"), parse_mode="HTML", reply_markup=_settings_kb(await get_prefs(me["id"]), lg))


async def cmd_lang(m: Message):
    me, lg = await _ctx_user(m)
    if not me: return
    await m.answer(t(lg, "lang_choose"), reply_markup=_lang_kb())


async def on_settings_cb(c: CallbackQuery):
    me = await friends._me(c.from_user.id)
    if not me or me["is_blocked"]:
        return await c.answer()
    parts = c.data.split(":")
    prefs = await get_prefs(me["id"])
    lg = await lang_of(c.from_user.id)
    note = None
    try:
        if parts[1] == "t" and parts[2] in PREF_KEYS:
            prefs[parts[2]] = not prefs[parts[2]]
            await set_pref(me["id"], parts[2], prefs[parts[2]])
        elif parts[1] == "lm":
            await c.message.edit_text(t(lg, "lang_choose"), reply_markup=_lang_kb())
            return await c.answer()
        elif parts[1] == "l" and parts[2] in i18n.LANGS:
            await set_lang(me["id"], c.from_user.id, parts[2]); lg = parts[2]; prefs["lang"] = lg
            note = t(lg, "lang_set")
        await c.message.edit_text(t(lg, "set_title"), parse_mode="HTML", reply_markup=_settings_kb(prefs, lg))
    except TelegramBadRequest:
        pass
    await c.answer(note)


async def on_joke_vote(c: CallbackQuery):
    me = await friends._me(c.from_user.id)
    if not me or me["is_blocked"]:
        return await c.answer()
    try:
        _, jid, val = c.data.split(":")
        up, down = await jokes.vote(int(jid), me["id"], 1 if val == "1" else -1)
        try: await c.message.edit_reply_markup(reply_markup=jokes.vote_kb(int(jid), up, down))
        except TelegramBadRequest: pass
        await c.answer(t(await lang_of(c.from_user.id), "vote_thanks"))
    except Exception:
        await c.answer()


# ----------------------------------------------------------------- ulash

DESC = {
    "uz": {"davomat": "Davomat statistikasi", "suhbat": "Do'stlar bilan suhbat",
           "hazil": "Kunning hazili", "sozlamalar": "Bildirishnomalar", "til": "Tilni o'zgartirish"},
    "en": {"davomat": "Attendance stats", "suhbat": "Chat with friends",
           "hazil": "Joke of the day", "sozlamalar": "Notifications", "til": "Change language"},
    "ru": {"davomat": "Посещаемость", "suhbat": "Чат с друзьями",
           "hazil": "Шутка дня", "sozlamalar": "Уведомления", "til": "Сменить язык"},
}


def user_commands(lang="uz"):
    return [BotCommand(command=c, description=d) for c, d in DESC[lang].items()]


async def setup(dp, bot, admin_ids, refresh_transcript):
    """main.py dan chaqiriladi (relay_to_admin'dan OLDIN). Buyruqlarni ro'yxatdan o'tkazadi va menyuni o'rnatadi."""
    ctx.update(bot=bot, admin_ids=admin_ids, refresh_transcript=refresh_transcript)
    dp.message.register(cmd_attendance, Command("davomat"))
    dp.message.register(cmd_joke, Command("hazil"))
    dp.message.register(cmd_settings, Command("sozlamalar"))
    dp.message.register(cmd_lang, Command("til"))
    dp.callback_query.register(on_settings_cb, F.data.startswith("nt:"))
    dp.callback_query.register(on_joke_vote, F.data.startswith("jk:"))
    try:
        await bot.set_my_commands(user_commands("uz"), scope=BotCommandScopeDefault())
        for lg in ("en", "ru"):
            await bot.set_my_commands(user_commands(lg), scope=BotCommandScopeDefault(), language_code=lg)
    except Exception as e:
        logger.warning(f"Buyruqlar menyusi o'rnatilmadi: {e}")


def start(bg):
    """Fon vazifalarni ishga tushiradi (bg — main._bg)."""
    for coro in (reminder_loop(), grades_loop(), health_loop(), weekly_loop()):
        bg(coro)
