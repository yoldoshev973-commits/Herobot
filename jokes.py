"""Hazillar: bazada (jokes jadvali) saqlanadi, admin panel yoki bot orqali tahrirlanadi.
jokes.txt — faqat birinchi ishga tushishda bazani to'ldirish uchun boshlang'ich ro'yxat (yoki baza bo'sh bo'lsa zaxira).
Foydalanuvchilar 👍/👎 beradi: yoqqanlari ko'proq chiqadi, juda yoqmaganlari (-3 va past) ko'rsatilmaydi.
Hazillar aralashtirilib, ketma-ket beriladi — hammasi chiqib bo'lmaguncha bir hazil takrorlanmaydi."""
import os
import random
import logging
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db

logger = logging.getLogger(__name__)
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jokes.txt")
_FALLBACK = ["Skaner qilindi! Endi navbat sizga: o'rindiqdan turib, darsga kirish. 😄"]
_bag = []   # [(id|None, matn)]


def parse_jokes_text(text):
    """Matn -> hazillar ro'yxati: har qatorda bitta hazil; bo'sh va '#' bilan boshlangan qatorlar o'tkazib yuboriladi."""
    return [l.strip() for l in str(text or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]


def load_file_jokes():
    try:
        with open(_PATH, encoding="utf-8") as f:
            return parse_jokes_text(f.read()) or list(_FALLBACK)
    except Exception:
        return list(_FALLBACK)


def invalidate():
    """Hazillar yoki ovozlar o'zgargach chaqiriladi — keyingi hazil yangi ro'yxatdan olinadi."""
    _bag.clear()


async def init_tables():
    async with db.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joke_votes (
                joke_id INTEGER NOT NULL REFERENCES jokes(id) ON DELETE CASCADE, user_id INTEGER NOT NULL,
                vote SMALLINT NOT NULL, PRIMARY KEY (joke_id, user_id)
            );""")


async def seed_if_empty():
    """Baza bo'sh bo'lsa, jokes.txt dagi hazillarni bazaga yozadi (faqat bir marta)."""
    try:
        if await db.count_jokes() == 0:
            added, _ = await db.add_jokes_bulk(load_file_jokes())
            logger.info(f"😄 Hazillar bazaga yuklandi: {added} ta")
    except Exception as e:
        logger.warning(f"Hazillarni yuklashda xato: {e}")


async def scored_jokes():
    """[(id, matn, ball, 👍, 👎)] — ball = 👍 - 👎."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT j.id, j.text, COALESCE(SUM(CASE WHEN v.vote > 0 THEN 1 ELSE 0 END), 0) AS up, "
            "COALESCE(SUM(CASE WHEN v.vote < 0 THEN 1 ELSE 0 END), 0) AS down "
            "FROM jokes j LEFT JOIN joke_votes v ON v.joke_id = j.id GROUP BY j.id, j.text ORDER BY j.id DESC")
    return [(r["id"], r["text"], int(r["up"]) - int(r["down"]), int(r["up"]), int(r["down"])) for r in rows]


async def next_joke_item():
    """Navbatdagi hazil: (id, matn). Yoqqanlari 2-3 marta ko'p, ball <= -3 bo'lganlari umuman chiqmaydi."""
    if not _bag:
        items = []
        try:
            for jid, text, score, _, _ in await scored_jokes():
                copies = 0 if score <= -3 else 1 + (score >= 2) + (score >= 5)
                items += [(jid, text)] * copies
        except Exception as e:
            logger.warning(f"Hazillarni o'qib bo'lmadi: {e}")
        if not items:
            items = [(None, t) for t in load_file_jokes()]
        random.shuffle(items)
        _bag.extend(items)
    return _bag.pop()


async def next_joke():
    return (await next_joke_item())[1]


async def joke_of_the_day():
    """Hamma uchun bir xil, kun bo'yicha almashadigan hazil: (id, matn). Manfiy ballilar tanlanmaydi."""
    try:
        good = [(i, t) for i, t, s, _, _ in sorted(await scored_jokes(), key=lambda x: x[0]) if s >= 0]
    except Exception:
        good = []
    if not good:
        return None, load_file_jokes()[0]
    day = (datetime.utcnow() + timedelta(hours=5)).toordinal()
    return good[day % len(good)]


async def vote(joke_id, user_id, value):
    """value: 1 yoki -1. Bir xil ovozni qayta bossa — ovoz olib tashlanadi. -> (👍, 👎)"""
    if value not in (1, -1): return 0, 0
    async with db.pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM jokes WHERE id=$1", int(joke_id)): return 0, 0   # hazil o'chirilgan
        cur = await conn.fetchval("SELECT vote FROM joke_votes WHERE joke_id=$1 AND user_id=$2", int(joke_id), int(user_id))
        if cur == value:
            await conn.execute("DELETE FROM joke_votes WHERE joke_id=$1 AND user_id=$2", int(joke_id), int(user_id))
        else:
            await conn.execute("INSERT INTO joke_votes (joke_id, user_id, vote) VALUES ($1,$2,$3) "
                               "ON CONFLICT (joke_id, user_id) DO UPDATE SET vote=$3", int(joke_id), int(user_id), int(value))
        up, down = await counts(joke_id, conn)
    invalidate()
    return up, down


async def counts(joke_id, conn=None):
    async def q(c):
        r = await c.fetchrow("SELECT COALESCE(SUM(CASE WHEN vote>0 THEN 1 ELSE 0 END),0) AS up, COALESCE(SUM(CASE WHEN vote<0 THEN 1 ELSE 0 END),0) AS down "
                             "FROM joke_votes WHERE joke_id=$1", int(joke_id))
        return int(r["up"]), int(r["down"])
    if conn:
        return await q(conn)
    async with db.pool.acquire() as c:
        return await q(c)


def vote_kb(joke_id, up=0, down=0):
    """Hazil tagidagi 👍/👎 tugmalari (joke_id None bo'lsa — tugmasiz)."""
    if joke_id is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"👍 {up}" if up else "👍", callback_data=f"jk:{joke_id}:1"),
        InlineKeyboardButton(text=f"👎 {down}" if down else "👎", callback_data=f"jk:{joke_id}:-1")]])
