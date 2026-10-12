"""Hero.study dars jadvali: API'dan olish va chiroyli ko'rinishga keltirish.

Xom javob bazaga o'zgarishsiz saqlanadi (hero_schedules.raw), ko'rsatishda esa `normalize()` orqali
o'qiladi — shuning uchun maydon nomlari kutilganidan farq qilsa, faqat shu fayldagi kalit ro'yxatlarini
to'g'rilash kifoya, jadvalni qayta yuklash shart emas.
"""
import os
import re
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://api.newuzbekistan.hero.study/v1/schedule/list"
TZ = timezone(timedelta(hours=5))   # Toshkent (UTC+5, yozgi vaqtsiz)

# beginTime/endTime birligi: "s" (soniya), "ms" (millisoniya) yoki "auto" (avval soniya, bo'sh chiqsa millisoniya)
TS_UNIT = os.getenv("SCHEDULE_TS_UNIT", "auto").lower()
try:
    WEEKS = max(1, min(8, int(os.getenv("SCHEDULE_WEEKS", "2"))))   # joriy haftadan boshlab necha hafta
except ValueError:
    WEEKS = 2


def week_range(weeks=WEEKS, now=None):
    """Joriy hafta dushanbasi 00:00 dan `weeks` hafta oxirigacha (Toshkent vaqti) -> (begin, end) UTC epoch soniyalarida."""
    now = now or datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = monday + timedelta(weeks=weeks) - timedelta(seconds=1)
    return int(monday.timestamp()), int(end.timestamp())


async def fetch_schedule(token, headers):
    """-> (status, data, error, begin, end). error: None | 'TOKEN_EXPIRED' | matn."""
    begin, end = week_range()
    units = [TS_UNIT] if TS_UNIT in ("s", "ms") else ["s", "ms"]
    last = (0, None, "Noma'lum xato", begin, end)
    h = dict(headers)
    h["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=True)) as session:
        for i, unit in enumerate(units):
            m = 1000 if unit == "ms" else 1
            url = f"{SCHEDULE_URL}?beginTime={begin * m}&endTime={end * m}&lang=en"
            try:
                async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status in (401, 403):
                        return r.status, None, "TOKEN_EXPIRED", begin, end
                    if r.status != 200:
                        last = (r.status, None, f"Hero javobi: HTTP {r.status}", begin, end)
                        continue
                    data = await r.json(content_type=None)
            except Exception as e:
                last = (0, None, f"Ulanishda xato: {str(e)[:120]}", begin, end)
                continue
            if extract_items(data) or i == len(units) - 1:
                return 200, data, None, begin, end
    return last


# ----------------------------------------------------------------- normalizer

TITLE_KEYS = ("subject", "subjectName", "subject_name", "lesson", "lessonName", "lesson_name", "course",
              "courseName", "course_name", "discipline", "name", "title", "topic")
TEACHER_KEYS = ("teacher", "teacherName", "teacher_name", "lecturer", "instructor", "professor", "employee", "tutor")
ROOM_KEYS = ("room", "roomName", "room_name", "auditorium", "classroom", "audience", "location", "place", "building")
TYPE_KEYS = ("type", "lessonType", "lesson_type", "kind", "format", "trainingType")
START_KEYS = ("beginTime", "begin_time", "begin", "startTime", "start_time", "start", "startDate", "from", "timeStart", "dateStart")
END_KEYS = ("endTime", "end_time", "end", "endDate", "to", "timeEnd", "dateEnd")
DATE_KEYS = ("date", "day", "lessonDate", "lesson_date")
TIME_KEYS = ("time", "lessonTime", "timeRange", "time_range", "hours")
NEST_KEYS = ("lessons", "schedule", "items", "list", "classes", "events", "pairs")
LIST_KEYS = ("data", "items", "list", "schedule", "lessons", "result", "results", "rows", "content")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—to]+\s*(\d{1,2}:\d{2})")


def _find_list(o, depth=0):
    """Javob ichidan dars yozuvlari ro'yxatini topadi (data/items/... ichida yoki kun bo'yicha guruhlangan bo'lsa ham)."""
    if depth > 4:
        return None
    if isinstance(o, list):
        ds = [x for x in o if isinstance(x, dict)]
        if not ds:
            return None
        out = []
        for d in ds:
            nested = next((d[k] for k in NEST_KEYS if isinstance(d.get(k), list) and d[k] and isinstance(d[k][0], dict)), None)
            if nested is None:
                out.append(d)
                continue
            parent = {k: v for k, v in d.items() if not isinstance(v, (list, dict))}
            out.extend({**parent, **x} for x in nested if isinstance(x, dict))
        return out
    if isinstance(o, dict):
        for k in LIST_KEYS:
            if k in o:
                r = _find_list(o[k], depth + 1)
                if r:
                    return r
        out = []
        for k, v in o.items():
            r = _find_list(v, depth + 1)
            if r:
                out.extend(({**x, "date": k} if _DATE_RE.match(str(k)) and "date" not in x else x) for x in r)
        return out or None
    return None


def extract_items(data):
    return _find_list(data) or []


def _text(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)):
        return None
    if isinstance(v, dict):
        for k in ("name", "title", "fullName", "full_name", "label", "nameEn", "shortName", "short_name"):
            t = _text(v.get(k))
            if t:
                return t
        parts = [_text(v.get(k)) for k in ("lastName", "last_name", "firstName", "first_name")]
        return " ".join(p for p in parts if p) or None
    if isinstance(v, list):
        return ", ".join(t for t in (_text(x) for x in v) if t) or None
    return None


def _pick(it, keys):
    for k in keys:
        t = _text(it.get(k))
        if t:
            return t
    return None


def _first(it, keys):
    for k in keys:
        if it.get(k) not in (None, ""):
            return it[k]
    return None


def _to_dt(v):
    """Son (soniya/millisoniya) yoki ISO matn -> Toshkent vaqtidagi datetime, bo'lmasa None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
        x = float(v)
        if x > 1e11:
            x /= 1000
        try:
            return datetime.fromtimestamp(x, TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
    return None


def _hm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(s or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def normalize_item(it):
    sd, ed = _to_dt(_first(it, START_KEYS)), _to_dt(_first(it, END_KEYS))
    date = start = end = None
    if sd:
        date, start = sd.strftime("%Y-%m-%d"), sd.strftime("%H:%M")
    if ed:
        end = ed.strftime("%H:%M")
        date = date or ed.strftime("%Y-%m-%d")
    if not start:   # "09:00" yoki "09:00-10:20" ko'rinishidagi matnlar
        start = _hm(_first(it, START_KEYS))
        end = end or _hm(_first(it, END_KEYS))
        rng = _RANGE_RE.search(str(_first(it, TIME_KEYS) or ""))
        if rng and not start:
            start, end = _hm(rng.group(1)), _hm(rng.group(2))
    if not date:
        dd = _to_dt(_first(it, DATE_KEYS))
        if dd:
            date = dd.strftime("%Y-%m-%d")
        else:
            m = _DATE_RE.match(str(_first(it, DATE_KEYS) or ""))
            date = m.group(0) if m else None
    return {"date": date, "start": start, "end": end,
            "title": _pick(it, TITLE_KEYS) or "Dars", "teacher": _pick(it, TEACHER_KEYS),
            "room": _pick(it, ROOM_KEYS), "type": _pick(it, TYPE_KEYS)}


def normalize(data):
    """Xom javob -> [{'date','start','end','title','teacher','room','type'}], sana/vaqt bo'yicha tartiblangan."""
    out = [normalize_item(x) for x in extract_items(data)]
    out.sort(key=lambda x: (x["date"] or "9999-99-99", x["start"] or "99:99"))
    return out


# ----------------------------------------------------------------- darslar bo'yicha guruhlash

def lesson_key(l):
    """Bir xil dars = bir xil sana + boshlanish vaqti + nom (o'qituvchi/xona turli yozilgan bo'lishi mumkin)."""
    return (l.get("date"), l.get("start"), (l.get("title") or "").strip().lower())


def group_lessons(rows):
    """rows: [(email, xom_javob)] -> {lesson_key: {date,start,end,title,teacher,room,type,emails:set}}.
    Sanasi yoki boshlanish vaqti noma'lum darslar tashlab yuboriladi (ularni vaqtga bog'lab bo'lmaydi)."""
    groups = {}
    for email, raw in rows:
        try:
            items = normalize(raw)
        except Exception:
            continue
        for l in items:
            if not l.get("date") or not l.get("start"):
                continue
            k = lesson_key(l)
            g = groups.get(k)
            if g is None:
                g = groups[k] = {**l, "emails": set()}
            else:
                for f in ("end", "teacher", "room", "type"):
                    if not g.get(f) and l.get(f):
                        g[f] = l[f]
            g["emails"].add(email)
    return groups


def _dt(date, hm):
    try:
        return datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None


def is_active_now(l, now=None, early_min=20, default_len_min=90):
    """Dars hozir ketyaptimi (QR odatda dars boshlanishidan biroz oldin ochiladi, shuning uchun `early_min` daqiqa oldindan)."""
    now = now or datetime.now(TZ)
    st = _dt(l.get("date"), l.get("start"))
    if not st:
        return False
    en = _dt(l.get("date"), l.get("end")) if l.get("end") else None
    if not en or en <= st:
        en = st + timedelta(minutes=default_len_min)
    return st - timedelta(minutes=early_min) <= now <= en


def today_str(now=None):
    return (now or datetime.now(TZ)).strftime("%Y-%m-%d")
