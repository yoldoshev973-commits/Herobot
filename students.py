"""Talabalar ro'yxati (students.json — PDF'dan olingan): email bo'yicha ism-familya va Student ID topish."""
import json
import os
import re
import unicodedata

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "students.json")
_DATA = {}
try:
    with open(_PATH, encoding="utf-8") as f:
        _DATA = json.load(f)
except Exception:
    _DATA = {}


def norm_email(email):
    """Kichik harf, diakritiksiz, apostrofsiz — g'/gʻ/g‘/g` kabi yozuv farqlari bir xil deb olinadi."""
    e = unicodedata.normalize("NFKD", (email or "").strip().lower())
    e = "".join(ch for ch in e if not unicodedata.combining(ch))
    e = re.sub(r"[ʻʼ‘’`´'′\"]", "", e)
    return re.sub(r"\s+", "", e)


def lookup(email):
    """-> {'full_name', 'student_id', 'ambiguous'} yoki None.
    Bir xil email bir necha talabaga to'g'ri kelsa (ro'yxatda 75 ta shunday email bor), ismlar ' / ' bilan
    birlashtiriladi va ambiguous=True bo'ladi — qaysi biri ekanini aniq bilib bo'lmaydi."""
    rows = _DATA.get(norm_email(email))
    if not rows:
        return None
    names = [f"{r['first']} {r['last']}".strip() for r in rows]
    ids = [r["id"] for r in rows]
    return {"full_name": " / ".join(names), "student_id": " / ".join(ids), "ambiguous": len(rows) > 1}


def count():
    return sum(len(v) for v in _DATA.values())
