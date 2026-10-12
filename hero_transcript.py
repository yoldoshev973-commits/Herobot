"""Hero.study transcript: API'dan olish va chiroyli ko'rinishga keltirish.

Manzil: https://api.newuzbekistanhero.study/v1/transcript/file?lang=en  (TRANSCRIPT_URL muhit o'zgaruvchisi bilan almashtirish mumkin).
Javob JSON bo'lishi ham, tayyor fayl (PDF va h.k.) bo'lishi ham mumkin — ikkalasi ham qo'llab-quvvatlanadi:
  • JSON  -> bazaga o'zgarishsiz saqlanadi, ko'rsatishda `normalize()` orqali semestrlar/fanlar/baholar jadvaliga aylantiriladi.
  • Fayl  -> bazaga faqat metama'lumot yoziladi, faylning o'zi admin so'raganda token bilan qayta olinadi (`fetch_file`).
Maydon nomlari kutilganidan farq qilsa, faqat shu fayldagi kalit ro'yxatlarini to'g'rilash kifoya.
"""
import os
import re
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Asosiy manzil Hero'ning boshqa API'lari (login, jadval) bilan bir xil domen; foydalanuvchi bergan manzil zaxira sifatida.
# TRANSCRIPT_URL berilsa — faqat shu ishlatiladi (bir nechtasini vergul bilan yozish mumkin).
_DEFAULT_URLS = (
    "https://api.newuzbekistan.hero.study/v1/transcript/file?lang=en",
    "https://api.newuzbekistanhero.study/v1/transcript/file?lang=en",
)
TRANSCRIPT_URLS = [u.strip() for u in os.getenv("TRANSCRIPT_URL", "").split(",") if u.strip()] or list(_DEFAULT_URLS)
TRANSCRIPT_URL = TRANSCRIPT_URLS[0]
MAX_FILE_BYTES = 15 * 1024 * 1024


def _host(url):
    return re.sub(r"^https?://([^/]+).*$", r"\1", url)


def _unwrap(body):
    """gzip yoki toza base64 (PDF) ko'rinishida kelgan javobni oddiy baytlarga aylantiradi."""
    import gzip, base64
    if body[:2] == b"\x1f\x8b":
        try: body = gzip.decompress(body)
        except Exception: pass
    st = body.strip()
    if st[:6] == b"JVBERi":   # "%PDF-" ning base64 ko'rinishi
        try: return base64.b64decode(st, validate=False)
        except Exception: pass
    return body


def _find_embedded_pdf(o, depth=0):
    """JSON ichida base64 (yoki data:application/pdf;base64,...) ko'rinishida yashirilgan PDF bo'lsa, baytlarini qaytaradi."""
    import base64
    if depth > 5:
        return None
    if isinstance(o, str):
        v = o.strip()
        if v.startswith("data:") and ";base64," in v[:80]:
            v = v.split(";base64,", 1)[1]
        if v[:6] == "JVBERi" and len(v) > 100:
            try:
                b = base64.b64decode(v, validate=False)
                return b if b[:5] == b"%PDF-" else None
            except Exception:
                return None
    elif isinstance(o, dict):
        for x in o.values():
            r = _find_embedded_pdf(x, depth + 1)
            if r: return r
    elif isinstance(o, list):
        for x in o[:20]:
            r = _find_embedded_pdf(x, depth + 1)
            if r: return r
    return None


async def _get_one(session, url, h, want_bytes):
    """-> (status, kind, payload, ctype, error, retry). retry=True bo'lsa keyingi manzilni sinab ko'ramiz."""
    import json as _json
    try:
        async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as r:
            if r.status in (401, 403):
                return r.status, None, None, None, "TOKEN_EXPIRED", False
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks, size = [], 0
            async for chunk in r.content.iter_chunked(65536):
                chunks.append(chunk); size += len(chunk)
                if size > MAX_FILE_BYTES:
                    break
            body = b"".join(chunks)
            if r.status != 200:
                snippet = body[:160].decode("utf-8", "replace").replace("\n", " ").strip()
                return r.status, None, None, ctype, f"{_host(url)} → HTTP {r.status}" + (f": {snippet}" if snippet else ""), True
            if len(body) > MAX_FILE_BYTES:
                return 200, None, None, ctype, "Fayl juda katta", False
    except Exception as e:
        return 0, None, None, None, f"{_host(url)} → ulanib bo'lmadi: {type(e).__name__} {str(e)[:100]}", True
    if not body:
        return 200, None, None, ctype, f"{_host(url)} → bo'sh javob", True
    body = _unwrap(body)
    if body[:5] == b"%PDF-":
        ctype = "application/pdf"
    head = body.lstrip()[:1]
    if ctype == "application/json" or ctype.endswith("+json") or head in (b"{", b"["):
        try:
            data = _json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            data = None
        if data is not None:
            blob = _find_embedded_pdf(data)
            if blob:
                return 200, "file", (blob if want_bytes else None), "application/pdf", None, False
            return 200, "json", data, ctype or "application/json", None, False
    if head == b"<" and b"html" in body[:300].lower():   # login sahifasi yoki xato HTML — fayl emas
        return 200, None, None, ctype, f"{_host(url)} → fayl o'rniga HTML sahifa qaytdi", True
    return 200, "file", (body if want_bytes else None), ctype or "application/octet-stream", None, False


async def _get(token, headers, want_bytes):
    """-> (status, kind, payload, content_type, error). kind: 'json' | 'file' | None. error: None | 'TOKEN_EXPIRED' | matn."""
    h = dict(headers)
    h["Authorization"] = f"Bearer {token}"
    h["Accept"] = "application/json, application/pdf, */*"
    errors, last_status = [], 0
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=True)) as session:
        for url in TRANSCRIPT_URLS:
            st, kind, payload, ctype, err, retry = await _get_one(session, url, h, want_bytes)
            if err is None or not retry:
                return st, kind, payload, ctype, err
            errors.append(err); last_status = st
            logger.warning(f"Transcript: {err}")
    return last_status, None, None, None, " | ".join(errors)[:290]


async def fetch_transcript(token, headers):
    """-> (status, kind, data, content_type, error). Fayl bo'lsa data = {'size', 'content_type'} (baytlar saqlanmaydi)."""
    st, kind, payload, ctype, err = await _get(token, headers, want_bytes=True)
    if err or kind is None:
        return st, kind, None, ctype, err
    if kind == "file":
        return st, "file", {"_file": True, "content_type": ctype, "size": len(payload)}, ctype, None
    return st, "json", payload, ctype, None


async def fetch_file(token, headers):
    """-> (status, bytes, content_type, error) — faylni to'g'ridan-to'g'ri yuklab berish uchun."""
    st, kind, payload, ctype, err = await _get(token, headers, want_bytes=True)
    if err:
        return st, None, ctype, err
    if kind == "json":
        return st, None, ctype, "Javob fayl emas, JSON"
    return st, payload, ctype, None


# ----------------------------------------------------------------- normalizer

TITLE_KEYS = ("subject", "subjectName", "subject_name", "course", "courseName", "course_name", "discipline",
              "disciplineName", "lesson", "name", "title", "module", "moduleName")
CODE_KEYS = ("code", "subjectCode", "subject_code", "courseCode", "course_code", "disciplineCode")
CREDIT_KEYS = ("credit", "credits", "ects", "creditHours", "credit_hours", "hours", "creditsCount")
GRADE_KEYS = ("grade", "mark", "letter", "letterGrade", "letter_grade", "gradeLetter", "result", "assessment", "evaluation")
SCORE_KEYS = ("score", "points", "point", "total", "percent", "percentage", "rating", "gradeValue", "grade_value", "ball")
GPA_KEYS = ("gpa", "cgpa", "sgpa", "averageGrade", "average", "avg", "avgScore", "gpaScore")
TERM_KEYS = ("semester", "semesterName", "semester_name", "term", "termName", "term_name", "period", "academicYear",
             "academic_year", "year", "course_year", "level", "stage")
STATUS_KEYS = ("status", "state", "passed", "isPassed", "is_passed")
NAME_KEYS = ("name", "title", "label", "semester", "term", "period")
LIST_KEYS = ("data", "items", "list", "transcript", "semesters", "terms", "subjects", "courses", "results", "records", "rows")
URL_KEYS = ("url", "link", "file", "fileUrl", "file_url", "href", "pdf", "path", "download")

_URL_RE = re.compile(r"^https?://", re.I)
_SKIP_SUMMARY = {"id", "userId", "user_id", "status", "code", "page", "limit", "offset", "total_count"}


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return float(s) if "." in s else int(s)
    return None


def _text(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ("name", "title", "label", "nameEn", "shortName", "short_name", "value"):
            t = _text(v.get(k))
            if t:
                return t
        return None
    if isinstance(v, list):
        return ", ".join(t for t in (_text(x) for x in v) if t) or None
    return None


def _pick(it, keys):
    for k in keys:
        if k in it:
            t = _text(it.get(k))
            if t:
                return t
    return None


def _first_num(it, keys):
    for k in keys:
        n = _num(it.get(k)) if not isinstance(it.get(k), (dict, list)) else None
        if n is not None:
            return n
    return None


def _looks_like_course(d):
    if not isinstance(d, dict):
        return False
    has_title = any(_text(d.get(k)) for k in TITLE_KEYS if not isinstance(d.get(k), (int, float)))
    has_mark = any(k in d for k in GRADE_KEYS + SCORE_KEYS + CREDIT_KEYS)
    return has_title and has_mark


def _course(d):
    status = None
    for k in STATUS_KEYS:
        if k in d and d[k] not in (None, ""):
            status = ("O'tgan" if d[k] is True else "O'tmagan" if d[k] is False else _text(d[k]))
            break
    grade = _pick(d, GRADE_KEYS)
    score = _first_num(d, SCORE_KEYS)
    if grade and _num(grade) is not None and score is None:   # "grade" aslida ball bo'lsa
        score, grade = _num(grade), None
    return {"code": _pick(d, CODE_KEYS), "title": _pick(d, TITLE_KEYS) or "Fan", "credits": _first_num(d, CREDIT_KEYS),
            "grade": grade, "score": score, "status": status}


def _term_name(d, fallback=None):
    for k in TERM_KEYS:
        if k in d and not isinstance(d[k], (list,)):
            t = _text(d[k])
            if t and not (_num(d[k]) is None and len(t) > 60):
                return (f"{k.capitalize()} {t}" if _num(d[k]) is not None and k in ("semester", "term", "year", "level", "stage", "course_year") else t)
    for k in NAME_KEYS:
        t = _text(d.get(k)) if not isinstance(d.get(k), (list,)) else None
        if t:
            return t
    return fallback


def _walk(o, groups, ctx=None, depth=0):
    """Fanlar ro'yxatlarini topadi; har bir ro'yxat uchun semestr nomi (ota-ona obyektdan) olinadi."""
    if depth > 6:
        return
    if isinstance(o, list):
        dicts = [x for x in o if isinstance(x, dict)]
        courses = [x for x in dicts if _looks_like_course(x)]
        if courses and len(courses) >= len(dicts) / 2:
            # har bir fan o'z semestr maydoniga ega bo'lishi mumkin (tekis ro'yxat)
            for c in courses:
                groups.append((_term_name(c, ctx), c))
            return
        for x in dicts:
            _walk(x, groups, ctx, depth + 1)
    elif isinstance(o, dict):
        name = _term_name(o, ctx) if depth else ctx
        for k, v in o.items():
            if isinstance(v, (list, dict)):
                sub_ctx = name
                if isinstance(v, dict) and re.match(r"^(semester|term|period|year)", str(k), re.I):
                    sub_ctx = _text(v.get("name")) or str(k)
                _walk(v, groups, sub_ctx, depth + 1)


def _find_summary(data):
    """Yuqori darajadagi (GPA, kreditlar kabi) raqamli ko'rsatkichlar."""
    out = {}
    roots = [data] if isinstance(data, dict) else []
    if isinstance(data, dict):
        for k in ("data", "result", "summary", "student", "transcript", "total", "stats"):
            if isinstance(data.get(k), dict):
                roots.append(data[k])
    for r in roots:
        for k, v in r.items():
            if k in _SKIP_SUMMARY or isinstance(v, (dict, list)):
                continue
            n = _num(v)
            if n is not None and (k.lower() in {x.lower() for x in GPA_KEYS + CREDIT_KEYS} or re.search(r"gpa|credit|average|total|passed|score", k, re.I)):
                out.setdefault(k, n)
    return out


def _find_file_url(data, depth=0):
    if depth > 4:
        return None
    if isinstance(data, str):
        return data if _URL_RE.match(data.strip()) else None
    if isinstance(data, dict):
        for k in URL_KEYS:
            u = _find_file_url(data.get(k), depth + 1) if k in data else None
            if u:
                return u
        for v in data.values():
            if isinstance(v, (dict, list)):
                u = _find_file_url(v, depth + 1)
                if u:
                    return u
    if isinstance(data, list):
        for v in data[:5]:
            u = _find_file_url(v, depth + 1)
            if u:
                return u
    return None


def normalize(data):
    """Xom javob -> {'summary': {...}, 'terms': [{'name','courses':[...],'credits','gpa'}], 'file_url', 'is_file'}."""
    if isinstance(data, dict) and data.get("_file"):
        return {"summary": {}, "terms": [], "file_url": None, "is_file": True,
                "content_type": data.get("content_type"), "size": data.get("size")}
    groups = []
    _walk(data, groups)
    order, terms = [], {}
    for name, raw in groups:
        key = name or "Fanlar"
        if key not in terms:
            terms[key] = {"name": key, "courses": []}
            order.append(key)
        terms[key]["courses"].append(_course(raw))
    out_terms = []
    for key in order:
        t = terms[key]
        cs = t["courses"]
        t["credits"] = sum(c["credits"] for c in cs if isinstance(c["credits"], (int, float))) or None
        out_terms.append(t)
    summary = _find_summary(data)
    if out_terms:
        total = sum(t["credits"] or 0 for t in out_terms)
        if total and not any(re.search(r"credit", k, re.I) for k in summary):
            summary["credits"] = total
    return {"summary": summary, "terms": out_terms, "file_url": _find_file_url(data), "is_file": False,
            "courses_count": sum(len(t["courses"]) for t in out_terms)}


# ----------------------------------------------------------------- o'zgarishlarni aniqlash

def _cmap(raw):
    """normalize(raw) -> {(semestr, fan_kaliti): kurs} — faqat bahosi yoki bali borlar."""
    out = {}
    for t in normalize(raw).get("terms", []):
        for c in t["courses"]:
            if c.get("grade") is None and c.get("score") is None:
                continue
            out[(t["name"], (c.get("code") or c["title"]).strip().lower())] = c
    return out


def _summary_gpa(raw):
    for k, v in (normalize(raw).get("summary") or {}).items():
        if re.search(r"gpa|average|avg", k, re.I):
            return v
    return None


def diff(old_raw, new_raw):
    """Eski va yangi transcriptni solishtiradi.
    -> {"changes": [("new"|"changed", kurs, eski_kurs|None)], "gpa": (eski, yangi)|None}
    Eski transcriptda baholangan fan umuman bo'lmasa (birinchi olish yoki format o'zgargan) — bo'sh qaytadi (spam bo'lmasin)."""
    try:
        old, new = _cmap(old_raw), _cmap(new_raw)
    except Exception:
        return {"changes": [], "gpa": None}
    if not old:
        return {"changes": [], "gpa": None}
    changes = []
    for k, c in new.items():
        o = old.get(k)
        if o is None:
            changes.append(("new", c, None))
        elif (o.get("grade"), o.get("score")) != (c.get("grade"), c.get("score")):
            changes.append(("changed", c, o))
    g_old, g_new = _summary_gpa(old_raw), _summary_gpa(new_raw)
    gpa = (g_old, g_new) if (g_old is not None and g_new is not None and g_old != g_new) else None
    return {"changes": changes, "gpa": gpa}
