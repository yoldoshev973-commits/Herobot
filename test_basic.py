"""Asosiy testlar (DB va tarmoqsiz).
Ishga tushirish:
  ALLOW_EPHEMERAL_KEY=1 BOT_TOKEN=123:abc DATABASE_URL=postgres://x ADMIN_ID=1 python3 -m pytest -q
"""
import asyncio
import os
import sys
import time
from datetime import datetime

os.environ.setdefault("ALLOW_EPHEMERAL_KEY", "1")
os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ.setdefault("DATABASE_URL", "postgres://x")
os.environ.setdefault("ADMIN_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import database as db
import hero_schedule
import main


# ---------------------------------------------------------------- _lesson_from_url
def test_lesson_from_url_path_segment():
    assert main._lesson_from_url("https://api.hero.study/qr/code-active/physics-101") == "physics 101"


def test_lesson_from_url_only_generic_words():
    assert main._lesson_from_url("https://api.hero.study/qr/code-active") is None


def test_lesson_from_url_query_param():
    assert main._lesson_from_url("https://api.hero.study/scan?lesson=Fizika%20A") == "Fizika A"


# ---------------------------------------------------------------- companions_for_lesson
def _group(emails, title="Fizika"):
    g = {"date": "2026-10-14", "start": "09:00", "end": "10:20", "title": title, "emails": set(emails)}
    return {hero_schedule.lesson_key(g): g}


def test_companions_only_same_active_lesson():
    now = datetime(2026, 10, 14, 9, 30, tzinfo=hero_schedule.TZ)
    groups = _group({"a@x", "b@x", "c@x"})
    assert hero_schedule.companions_for_lesson(["b@x", "d@x"], groups, {"a@x"}, now=now) == ["b@x"]


def test_companions_nobody_when_lesson_unknown():
    now = datetime(2026, 10, 14, 9, 30, tzinfo=hero_schedule.TZ)
    groups = _group({"a@x", "b@x"})
    assert hero_schedule.companions_for_lesson(["b@x"], groups, set(), now=now) == []


def test_companions_not_active_time():
    now = datetime(2026, 10, 14, 15, 0, tzinfo=hero_schedule.TZ)
    assert hero_schedule.companions_for_lesson(["b@x"], _group({"a@x", "b@x"}), {"a@x"}, now=now) == []


# ---------------------------------------------------------------- _why_lines
def test_why_lines_all_not_in_lesson():
    items = [{"ok": False, "reason": "not_in_lesson"}, {"ok": False, "reason": "not_in_lesson"}]
    out = main._why_lines(items, "uz")
    assert len(out) == 1 and out[0] == main.i18n.t("uz", "why_all_not_in")


def test_why_lines_partial_not_in_lesson():
    items = [{"ok": True}, {"ok": False, "reason": "not_in_lesson"}]
    out = main._why_lines(items, "uz")
    assert out == [main.i18n.t("uz", "why_not_in", n=1)]


def test_why_lines_qr_invalid():
    out = main._why_lines([{"ok": False, "reason": "qr_invalid"}, {"ok": False, "reason": "qr_invalid"}], "uz")
    assert out == [main.i18n.t("uz", "why_qr_invalid")]
    assert "QR" in out[0]


def test_why_lines_mixed_reasons():
    items = [{"ok": False, "reason": "token"}, {"ok": False, "reason": "login"}, {"ok": False, "reason": "network"}]
    out = main._why_lines(items, "uz")
    assert out == [main.i18n.t("uz", "why_token", n=2), main.i18n.t("uz", "why_hero", n=1)]


# ---------------------------------------------------------------- akkaunt tarixi
def test_split_lesson_kind():
    assert db._split_lesson_kind("Fizika · avto") == ("Fizika", "avto")
    assert db._split_lesson_kind("Fizika · hamroh") == ("Fizika", "hamroh")
    assert db._split_lesson_kind("Fizika") == ("Fizika", "manual")
    assert db._split_lesson_kind(None) == (None, "manual")


def test_activity_rows():
    details = [
        {"email": " A@X.com ", "ok": True},
        {"email": "b@x.com", "ok": False, "reason": "qr_invalid", "msg": "QR kod topilmadi yoki eskirgan"},
        {"ok": True},            # email yo'q — o'tkazib yuboriladi
        "xato",                  # element dict emas
    ]
    rows = db._activity_rows(details, 7, 99, "Fizika", "avto")
    assert rows == [("a@x.com", 7, 99, "Fizika", "avto", True, None, None),
                    ("b@x.com", 7, 99, "Fizika", "avto", False, "qr_invalid", "QR kod topilmadi yoki eskirgan")]
    assert db._activity_rows(None, 1, 1, None, "manual") == []


# ---------------------------------------------------------------- JWT / parol almashtirish
class _Req:
    def __init__(self, token): self.headers = {"Authorization": f"Bearer {token}"}


def _tok(**extra):
    return jwt.encode({"user_id": 5, "role": "user", "exp": int(time.time()) + 3600, **extra}, main.JWT_SECRET, algorithm="HS256")


def test_verify_token_old_session_closed_after_reset():
    saved = main._PW_CHANGED
    try:
        main._PW_CHANGED = {5: int(time.time()) - 100}
        assert main.verify_token(_Req(_tok(iat=int(time.time()) - 500)))[0] is None   # parol almashgunga qadar berilgan
        assert main.verify_token(_Req(_tok()))[0] is None                              # iat yo'q
        assert main.verify_token(_Req(_tok(iat=int(time.time()) - 50)))[0] == 5        # keyin berilgan — yaroqli
        main._PW_CHANGED = {}
        assert main.verify_token(_Req(_tok()))[0] == 5                                 # lug'atda yo'q — iat shart emas
    finally:
        main._PW_CHANGED = saved


def test_reset_rate_limit():
    main._reset_hits.clear()
    assert main._reset_limit_msg(42, record=True) is None
    assert main._reset_limit_msg(42) is not None          # 60 soniya ichida ikkinchisi
    main._reset_hits[42] = [time.time() - 120 - i for i in range(main.RESET_HOUR_MAX)]
    assert main._reset_limit_msg(42) is not None          # soatiga 5 ta tugagan
    main._reset_hits.clear()


def test_mask_email():
    assert main._mask_email("student@gmail.com") == "s***@gmail.com"
    assert "student" not in main._mask_email("student@gmail.com")


# ---------------------------------------------------------------- webhook
def _webhook_status(headers):
    async def run():
        app = web.Application()
        app.router.add_post("/webhook/x", main.handle_webhook)
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/webhook/x", json={"update_id": 1}, headers=headers)
            return r.status
    return asyncio.run(run())


def test_webhook_no_header_403():
    assert _webhook_status({}) == 403


def test_webhook_wrong_header_403():
    assert _webhook_status({"X-Telegram-Bot-Api-Secret-Token": "noto'g'ri"}) == 403


def test_webhook_correct_header_200():
    assert _webhook_status({"X-Telegram-Bot-Api-Secret-Token": main.WEBHOOK_HEADER_SECRET}) == 200
