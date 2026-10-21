"""Hisob-kitoblar: davomat va akkaunt salomatligi.

Davomat — dars jadvali (hero_schedules) va skaner tarixi (scan_logs) solishtirib hisoblanadi:
dars boshlanishidan 30 daqiqa oldin yoki tugaguncha akkaunt skaner orqali kirgan bo'lsa — "kirgan" hisoblanadi.
Faqat shu bot orqali skaner qilingan darslar ko'rinadi.
"""
import asyncio
import time
from datetime import datetime, timedelta

import database as db
import hero_schedule

TZ_OFF = timedelta(hours=5)
EARLY_MIN, LATE_MIN, DEFAULT_LEN_MIN = 30, 15, 90
LOG_DAYS = 75


_cache = {"groups": (0, None), "logs": (0, None)}


def now_local():
    return datetime.utcnow() + TZ_OFF


def _dt(date, hm):
    try:
        return datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def lesson_times(g):
    st = _dt(g.get("date"), g.get("start"))
    if not st:
        return None, None
    en = _dt(g.get("date"), g.get("end")) if g.get("end") else None
    if not en or en <= st:
        en = st + timedelta(minutes=DEFAULT_LEN_MIN)
    return st, en


def lesson_id(g):
    return f"{g.get('date')}|{g.get('start')}|{(g.get('title') or '').strip().lower()}"[:120]


# ----------------------------------------------------------------- ma'lumot keshlari

async def all_groups():
    """Barcha akkauntlar jadvalidan yig'ilgan darslar {kalit: dars} (2 daqiqa keshlanadi)."""
    t, g = _cache["groups"]
    if g is None or time.time() - t > 120:
        rows = await db.get_all_schedules()
        g = await asyncio.to_thread(hero_schedule.group_lessons, rows)
        _cache["groups"] = (time.time(), g)
    return g


async def _scan_items():
    """[(vaqt_toshkent, [(email, ok, msg), ...]), ...] — oxirgi LOG_DAYS kun (5 daqiqa keshlanadi)."""
    t, rows = _cache["logs"]
    if rows is not None and time.time() - t < 300:
        return rows
    since = datetime.utcnow() - timedelta(days=LOG_DAYS)
    async with db.pool.acquire() as conn:
        raw = await conn.fetch("SELECT scanned_at, details, lesson FROM scan_logs WHERE scanned_at >= $1 ORDER BY scanned_at DESC LIMIT 20000", since)

    def parse():
        out = []
        for r in raw:
            items = db._jsonish(r["details"]) or []
            if isinstance(items, list):
                auto = str(r["lesson"] or "").endswith((" · avto", " · hamroh"))   # avto-yuborish: kirmaganlar ko'pincha boshqa guruh — akkaunt xatosi emas
                out.append((r["scanned_at"] + TZ_OFF, [(i.get("email"), bool(i.get("ok")), i.get("msg")) for i in items
                                                       if isinstance(i, dict) and i.get("email") and not (i.get("reason") in ("not_in_lesson", "qr_invalid") and not i.get("ok")) and not (auto and not i.get("ok"))]))
        return out
    rows = await asyncio.to_thread(parse)
    _cache["logs"] = (time.time(), rows)
    return rows


# ----------------------------------------------------------------- davomat

async def attendance(emails):
    """-> {email: {...}}. emails — shu foydalanuvchining akkaunt emaillari."""
    emails = set(emails)
    if not emails:
        return {}
    groups = await all_groups()
    logs = await _scan_items()
    ok_times = {e: [] for e in emails}
    for ts, items in logs:
        for e, ok, _ in items:
            if ok and e in ok_times:
                ok_times[e].append(ts)
    now = now_local()
    out = {}
    for e in emails:
        lessons = []
        for g in groups.values():
            if e not in g["emails"]:
                continue
            st, en = lesson_times(g)
            if not st or en > now:
                continue
            lo, hi = st - timedelta(minutes=EARLY_MIN), en + timedelta(minutes=LATE_MIN)
            lessons.append((st, g.get("title") or "Dars", any(lo <= t <= hi for t in ok_times[e])))
        lessons.sort(key=lambda x: x[0])
        total, att = len(lessons), sum(1 for _, _, a in lessons if a)
        subj = {}
        for st, title, a in lessons:
            s = subj.setdefault(title, [0, 0]); s[1] += 1; s[0] += 1 if a else 0
        def window(days):
            sel = [a for st, _, a in lessons if st >= now - timedelta(days=days)]
            return sum(1 for a in sel if a), len(sel)
        streak = 0
        for _, _, a in reversed(lessons):
            if not a: break
            streak += 1
        missed = [(st, title) for st, title, a in reversed(lessons) if not a][:5]
        out[e] = {"total": total, "attended": att, "pct": round(att * 100 / total) if total else None, "subjects": subj,
                  "week": window(7), "month": window(30), "streak": streak, "missed": missed}
    return out


def bar(p, n=10):
    p = max(0, min(100, p or 0))
    f = round(p * n / 100)
    return "█" * f + "░" * (n - f)


# ----------------------------------------------------------------- salomatlik (admin)

async def health_overview():
    """Barcha akkauntlar holati: token, so'nggi skanerlar va ketma-ket muvaffaqiyatsizliklar."""
    async with db.pool.acquire() as conn:
        accs = await conn.fetch(
            "SELECT DISTINCT ON (a.email) a.email, a.full_name, a.student_id, a.bearer_token, a.user_id, u.telegram_id, u.username, "
            "COALESCE(u.is_blocked, FALSE) AS blocked FROM hero_accounts a JOIN app_users u ON u.id = a.user_id ORDER BY a.email, a.id DESC")
    logs = await _scan_items()
    per = {}
    for ts, items in logs:   # yangidan eskiga
        for e, ok, msg in items:
            s = per.setdefault(e, {"ok": 0, "fail": 0, "streak_fail": 0, "streak_open": True, "last": None, "last_ok": None, "msg": None})
            if s["last"] is None:
                s["last"] = ts
            if ok:
                s["ok"] += 1; s["streak_open"] = False
                if s["last_ok"] is None: s["last_ok"] = ts
            else:
                s["fail"] += 1
                if s["streak_open"]:
                    s["streak_fail"] += 1
                    if s["msg"] is None: s["msg"] = msg
    rows, counts = [], {"ok": 0, "login_error": 0, "no_token": 0, "failing": 0, "idle": 0}
    for a in accs:
        if a["blocked"]:
            continue
        tok = a["bearer_token"] or ""
        s = per.get(a["email"])
        if str(tok).startswith("ERROR"):
            status = "login_error"
        elif not tok or tok == "NO_TOKEN":
            status = "no_token"
        elif s and s["streak_fail"] >= 3:
            status = "failing"
        elif not s or not s["last"] or s["last"] < now_local() - timedelta(days=14):
            status = "idle"
        else:
            status = "ok"
        counts[status] += 1
        rows.append({"email": a["email"], "name": a["full_name"], "student_id": a["student_id"], "owner": ("@" + a["username"]) if a["username"] else a["telegram_id"],
                     "user_id": a["user_id"], "telegram_id": a["telegram_id"], "status": status,
                     "ok": s["ok"] if s else 0, "fail": s["fail"] if s else 0, "streak_fail": s["streak_fail"] if s else 0,
                     "msg": s["msg"] if s else None, "last": s["last"].strftime("%d.%m.%Y %H:%M") if s and s["last"] else None,
                     "last_ok": s["last_ok"].strftime("%d.%m.%Y %H:%M") if s and s["last_ok"] else None})
    # bugungi 0/N skanerlar
    zero_today = 0
    async with db.pool.acquire() as conn:
        t0 = datetime.utcnow() + TZ_OFF   # Toshkent hozirgi vaqti
        t0 = datetime(t0.year, t0.month, t0.day) - TZ_OFF   # Toshkent yarim kechasi (UTC'da)
        zero_today = await conn.fetchval("SELECT COUNT(*) FROM scan_logs WHERE scanned_at >= $1 AND success_count = 0 AND total_count > 0 AND COALESCE(lesson,'') NOT LIKE '% · avto' AND COALESCE(lesson,'') NOT LIKE '% · hamroh'", t0) or 0
        scans_today = await conn.fetchval("SELECT COUNT(*) FROM scan_logs WHERE scanned_at >= $1 AND COALESCE(lesson,'') NOT LIKE '% · avto' AND COALESCE(lesson,'') NOT LIKE '% · hamroh'", t0) or 0
    order = {"login_error": 0, "no_token": 1, "failing": 2, "idle": 3, "ok": 4}
    rows.sort(key=lambda r: (order[r["status"]], -r["streak_fail"], (r["name"] or r["email"]).lower()))
    return {"counts": counts, "total": len(rows), "accounts": rows, "zero_today": zero_today, "scans_today": scans_today,
            "last_backup": await db.get_setting("last_auto_backup")}
