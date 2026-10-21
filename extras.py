"""Qo'shimcha endpointlar: salomatlik, jurnal, suhbat nazorati (admin) va davomat/til (foydalanuvchi)."""
import asyncio
import json
import logging
from datetime import timedelta

from aiohttp import web

import audit
import database as db
import friends
import i18n
import notify
import stats

logger = logging.getLogger(__name__)
TZ = timedelta(hours=5)


def _tk(dt):
    return (dt + TZ).strftime("%d.%m.%Y %H:%M") if dt else None


def register(app, deps):
    """deps: verify_token, verify_hero_account, spawn_schedule."""
    verify = deps["verify_token"]

    def admin_only(request):
        u, role = verify(request)
        return u if (u and role == "super_admin") else None

    def deny():
        return web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)

    def js(data):
        return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    # ---------- 🩺 salomatlik
    async def health(request):
        if not admin_only(request): return deny()
        return js({"status": "success", **await stats.health_overview()})

    async def health_fix(request):
        """Tokeni buzilgan akkauntlarni (yoki bitta emailni) parol bilan qayta login qilib tiklaydi."""
        if not admin_only(request): return deny()
        email = str((await _body(request)).get("email") or "").strip()
        ov = await stats.health_overview()
        targets = [a["email"] for a in ov["accounts"] if (a["email"] == email if email else a["status"] in ("login_error", "no_token"))][:25]
        fixed = failed = 0
        for e in targets:
            acc = await db.get_account_creds(e)
            if not acc or not acc.get("hero_password"):
                failed += 1; continue
            ok, res = await deps["verify_hero_account"](e, acc["hero_password"])
            if not ok and "ulanib bo'lmadi" in str(res):   # tarmoq xatosi — yaxshi tokenni buzmaymiz
                failed += 1; continue
            await db.update_bearer_token(e, res if ok else "ERROR:LOGIN_FAILED")
            if ok:
                fixed += 1; deps["spawn_schedule"](e, res, acc["hero_password"])
            else:
                failed += 1
            await asyncio.sleep(0.4)
        msg = f"✅ {fixed} ta tiklandi" + (f", ❌ {failed} ta tiklanmadi (login/parol xato)" if failed else "") if targets else "Tuzatiladigan akkaunt yo'q"
        return js({"status": "success" if (fixed or not targets) else "error", "message": msg})

    async def health_delete(request):
        """Akkauntni (yoki barcha yaroqsizlarini) arxivga ko'chirib o'chiradi; egalariga xabar yuboradi."""
        if not admin_only(request): return deny()
        d = await _body(request)
        email = str(d.get("email") or "").strip()
        if email:
            targets = [email]
        elif d.get("mode") == "invalid":
            ov = await stats.health_overview()
            targets = [a["email"] for a in ov["accounts"] if a["status"] in ("login_error", "no_token")][:100]
        else:
            return js({"status": "error", "message": "Akkaunt tanlanmagan"})
        deleted = 0
        for e in targets:
            async with db.pool.acquire() as conn:
                owners = await conn.fetch("SELECT DISTINCT u.telegram_id, a.full_name FROM hero_accounts a JOIN app_users u ON u.id = a.user_id "
                                          "WHERE a.email=$1 AND u.telegram_id IS NOT NULL", e)
            if await db.admin_delete_accounts(e):
                deleted += 1
                for o in owners:
                    try:
                        lg = await notify.lang_of(o["telegram_id"])
                        await notify.send(o["telegram_id"], i18n.t(lg, "acc_removed", name=notify.esc(o["full_name"] or e)))
                    except Exception as ex:
                        logger.warning(f"O'chirish xabari yuborilmadi: {ex}")
                    await asyncio.sleep(0.05)
        if not deleted: return js({"status": "error", "message": "O'chiriladigan akkaunt topilmadi"})
        return js({"status": "success", "message": f"🗑 {deleted} ta akkaunt arxivga o'chirildi (📦 Arxiv bo'limidan qaytarish mumkin)"})

    # ---------- 📜 jurnal
    async def audit_list(request):
        if not admin_only(request): return deny()
        try: limit = max(1, min(500, int(request.query.get("limit", 200))))
        except ValueError: limit = 200
        return js({"status": "success", "rows": await audit.recent(limit, (request.query.get("q") or "").strip())})

    # ---------- 🛡 suhbat nazorati
    async def chat_reports(request):
        if not admin_only(request): return deny()
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT m.id, m.created_at, m.from_uid, m.to_uid, m.anonymous, m.kind, m.lesson, m.text, COALESCE(m.reviewed, FALSE) AS reviewed,
                  su.telegram_id AS s_tg, su.username AS s_user, su.full_name AS s_name, ru.username AS r_user, ru.full_name AS r_name,
                  (SELECT full_name FROM hero_accounts WHERE user_id = m.from_uid AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS s_hero,
                  (SELECT COUNT(*) FROM friend_msgs x WHERE x.from_uid = m.from_uid AND x.reported) AS s_reports,
                  (SELECT COUNT(DISTINCT x.to_uid) FROM friend_msgs x WHERE x.from_uid = m.from_uid AND x.reported) AS s_reporters,
                  (SELECT until FROM friend_bans b WHERE b.user_id = m.from_uid AND b.until > NOW()) AS banned_until
                FROM friend_msgs m JOIN app_users su ON su.id = m.from_uid JOIN app_users ru ON ru.id = m.to_uid
                WHERE m.reported ORDER BY COALESCE(m.reviewed, FALSE), m.created_at DESC LIMIT 100""")
            bans = await conn.fetch("SELECT b.user_id, b.until, b.reason, b.by_admin, u.username, u.full_name FROM friend_bans b JOIN app_users u ON u.id = b.user_id "
                                    "WHERE b.until > NOW() ORDER BY b.until DESC")
            st = await conn.fetchrow("SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE reported) AS rep FROM friend_msgs WHERE created_at > NOW() - INTERVAL '7 days'")
        reports = [{"id": r["id"], "time": _tk(r["created_at"]), "from_uid": r["from_uid"], "anonymous": r["anonymous"], "kind": r["kind"], "lesson": r["lesson"],
                    "text": r["text"], "reviewed": r["reviewed"],
                    "sender": r["s_hero"] or r["s_name"] or (("@" + r["s_user"]) if r["s_user"] else "?"), "sender_user": r["s_user"], "sender_tg": r["s_tg"],
                    "receiver": r["r_name"] or (("@" + r["r_user"]) if r["r_user"] else "?"),
                    "sender_reports": r["s_reports"], "sender_reporters": r["s_reporters"], "banned_until": _tk(r["banned_until"])} for r in rows]
        return js({"status": "success", "reports": reports, "week": {"messages": st["n"], "reported": st["rep"]},
                   "bans": [{"user_id": b["user_id"], "until": _tk(b["until"]), "reason": b["reason"], "auto": not b["by_admin"],
                             "name": b["full_name"] or (("@" + b["username"]) if b["username"] else str(b["user_id"]))} for b in bans]})

    async def chat_ban(request):
        if not admin_only(request): return deny()
        try:
            d = await _body(request); uid, days = int(d.get("user_id")), max(1, min(365, int(d.get("days") or 3)))
        except (TypeError, ValueError):
            return js({"status": "error", "message": "Noto'g'ri so'rov"})
        u = await friends._user(uid)
        if not u: return js({"status": "error", "message": "Foydalanuvchi topilmadi"})
        await friends.ban(uid, days, str(d.get("reason") or "Admin qarori")[:150], by_admin=True)
        if deps.get("bot") and u.get("telegram_id"): await friends._safe_send(u["telegram_id"], f"⛔ Suhbat funksiyasi {days} kunga cheklandi.")
        return js({"status": "success", "message": f"⛔ {days} kunga chetlatildi"})

    async def chat_unban(request):
        if not admin_only(request): return deny()
        try: uid = int((await _body(request)).get("user_id"))
        except (TypeError, ValueError): return js({"status": "error", "message": "Noto'g'ri so'rov"})
        ok = await friends.unban(uid)
        return js({"status": "success" if ok else "error", "message": "✅ Chetlatish olib tashlandi" if ok else "Chetlatilmagan"})

    async def chat_review(request):
        if not admin_only(request): return deny()
        try: mid = int((await _body(request)).get("id"))
        except (TypeError, ValueError): return js({"status": "error", "message": "Noto'g'ri so'rov"})
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE friend_msgs SET reviewed=TRUE WHERE id=$1", mid)
        return js({"status": "success", "message": "✔️ Ko'rib chiqildi"})

    # ---------- 💬 barcha suhbatlar (kim kimga nima yozgan)
    _MSG_SELECT = """
        SELECT m.id, m.created_at, m.from_uid, m.to_uid, m.anonymous, m.kind, m.lesson, m.text, m.reply_to, m.reported, m.delivered, m.error,
          su.username AS s_user, su.full_name AS s_name, ru.username AS r_user, ru.full_name AS r_name,
          (SELECT full_name FROM hero_accounts WHERE user_id = m.from_uid AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS s_hero,
          (SELECT full_name FROM hero_accounts WHERE user_id = m.to_uid AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS r_hero
        FROM friend_msgs m JOIN app_users su ON su.id = m.from_uid JOIN app_users ru ON ru.id = m.to_uid"""

    def _msg_row(r):
        return {"id": r["id"], "time": _tk(r["created_at"]), "from_uid": r["from_uid"], "to_uid": r["to_uid"], "anonymous": r["anonymous"], "kind": r["kind"],
                "lesson": r["lesson"], "text": r["text"], "reply_to": r["reply_to"], "reported": bool(r["reported"]),
                "delivered": r["delivered"], "error": r["error"],
                "sender": r["s_hero"] or r["s_name"] or (("@" + r["s_user"]) if r["s_user"] else "?"), "sender_user": r["s_user"],
                "receiver": r["r_hero"] or r["r_name"] or (("@" + r["r_user"]) if r["r_user"] else "?"), "receiver_user": r["r_user"]}

    async def chat_log(request):
        """Barcha suhbat xabarlari (yangidan eskiga). ?q= qidiruv (ism, @username, matn), ?before=<id> keyingi sahifa."""
        if not admin_only(request): return deny()
        q = (request.query.get("q") or "").strip()[:60]
        try: before = int(request.query.get("before") or 0)
        except ValueError: before = 0
        args, where = [], []
        if before: args.append(before); where.append(f"m.id < ${len(args)}")
        if q:
            esc_q = q.lstrip('@').replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc_q}%"); n = len(args)
            where.append(f"(m.text ILIKE ${n} OR su.username ILIKE ${n} OR ru.username ILIKE ${n} OR su.full_name ILIKE ${n} OR ru.full_name ILIKE ${n} "
                         f"OR EXISTS (SELECT 1 FROM hero_accounts h WHERE h.user_id IN (m.from_uid, m.to_uid) AND h.full_name ILIKE ${n}))")
        sql = _MSG_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.id DESC LIMIT 61"
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        more = len(rows) > 60
        return js({"status": "success", "messages": [_msg_row(r) for r in rows[:60]], "more": more})

    async def chat_thread(request):
        """Ikki foydalanuvchi o'rtasidagi barcha xabarlar (ikki tomonlama), eskidan yangiga."""
        if not admin_only(request): return deny()
        try: a, b = int(request.query.get("a")), int(request.query.get("b"))
        except (TypeError, ValueError): return js({"status": "error", "message": "Noto'g'ri so'rov"})
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(_MSG_SELECT + " WHERE (m.from_uid=$1 AND m.to_uid=$2) OR (m.from_uid=$2 AND m.to_uid=$1) ORDER BY m.id ASC LIMIT 300", a, b)
        return js({"status": "success", "messages": [_msg_row(r) for r in rows]})

    # ---------- 🛡 admin: suhbatlar ro'yxati (juftliklar bo'yicha)
    async def chat_pairs(request):
        """Ikki odam o'rtasidagi har bir yozishma bitta qator: oxirgi xabar, xabarlar soni. ?q= qidiruv, ?before=<oxirgi xabar id>."""
        if not admin_only(request): return deny()
        q = (request.query.get("q") or "").strip()[:60]
        try: before = int(request.query.get("before") or 0)
        except ValueError: before = 0
        args, having, where = [], "", ""
        if before: args.append(before); having = f"HAVING MAX(id) < ${len(args)}"
        if q:
            esc_q = q.lstrip("@").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc_q}%"); n = len(args)
            where = (f"WHERE (ua.username ILIKE ${n} OR ub.username ILIKE ${n} OR ua.full_name ILIKE ${n} OR ub.full_name ILIKE ${n} "
                     f"OR EXISTS (SELECT 1 FROM hero_accounts h WHERE h.user_id IN (p.a, p.b) AND h.full_name ILIKE ${n}))")
        sql = f"""
            WITH p AS (SELECT LEAST(from_uid, to_uid) AS a, GREATEST(from_uid, to_uid) AS b, COUNT(*) AS n, MAX(id) AS last_id,
                         COUNT(*) FILTER (WHERE reported) AS rep, COUNT(*) FILTER (WHERE delivered IS FALSE) AS failed
                       FROM friend_msgs GROUP BY 1, 2 {having})
            SELECT p.*, l.created_at, l.text, l.from_uid AS l_from, l.anonymous AS l_anon, l.kind AS l_kind,
              ua.username AS a_user, ua.full_name AS a_name, ub.username AS b_user, ub.full_name AS b_name,
              (SELECT full_name FROM hero_accounts WHERE user_id = p.a AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS a_hero,
              (SELECT full_name FROM hero_accounts WHERE user_id = p.b AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS b_hero
            FROM p JOIN friend_msgs l ON l.id = p.last_id JOIN app_users ua ON ua.id = p.a JOIN app_users ub ON ub.id = p.b
            {where} ORDER BY p.last_id DESC LIMIT 41"""
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        def nm(hero, name, user): return hero or name or (("@" + user) if user else "?")
        out = [{"a": r["a"], "b": r["b"], "a_name": nm(r["a_hero"], r["a_name"], r["a_user"]), "a_user": r["a_user"],
                "b_name": nm(r["b_hero"], r["b_name"], r["b_user"]), "b_user": r["b_user"], "count": r["n"], "last_id": r["last_id"],
                "time": _tk(r["created_at"]), "text": (r["text"] or "")[:140], "last_from_a": r["l_from"] == r["a"], "last_anon": r["l_anon"],
                "reported": r["rep"], "failed": r["failed"]} for r in rows[:40]]
        return js({"status": "success", "pairs": out, "more": len(rows) > 40})

    # ---------- 💬 foydalanuvchi: mini-ilovadagi suhbat (botdagi /suhbat bilan bir xil qoidalar)
    async def _chat_me(request):
        u_id, _ = verify(request)
        if not u_id: return None, web.json_response({"status": "error", "message": "Kirish kerak"}, status=401)
        me = await friends._user(u_id)
        if not me or me["is_blocked"]: return None, web.json_response({"status": "error", "message": "Ruxsat yo'q"}, status=403)
        return me, None

    async def _body(request):
        try:
            d = await request.json()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _name3(hero, name, user): return hero or name or (("@" + user) if user else "Foydalanuvchi")

    async def chat_inbox(request):
        me, err = await _chat_me(request)
        if err: return err
        box = "out" if request.query.get("box") == "out" else "in"
        bn = await friends.ban_until(me["id"])
        async with db.pool.acquire() as conn:
            if box == "in":
                rows = await conn.fetch("""
                    SELECT m.id, m.created_at, m.kind, m.anonymous, m.lesson, m.text, m.reply_to, m.reported, m.from_uid, su.username AS s_user, su.full_name AS s_name,
                      (SELECT full_name FROM hero_accounts WHERE user_id = m.from_uid AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS s_hero,
                      EXISTS (SELECT 1 FROM friend_blocks b WHERE b.user_id = $1 AND b.blocked_uid = m.from_uid) AS blocked
                    FROM friend_msgs m JOIN app_users su ON su.id = m.from_uid WHERE m.to_uid = $1 ORDER BY m.id DESC LIMIT 60""", me["id"])
                items = [{"id": r["id"], "time": _tk(r["created_at"]), "kind": r["kind"], "anonymous": r["anonymous"], "lesson": r["lesson"], "text": r["text"],
                          "reported": bool(r["reported"]), "blocked": r["blocked"],
                          "who": "🎭 Anonim" if r["anonymous"] else _name3(r["s_hero"], r["s_name"], r["s_user"]),
                          "user": None if r["anonymous"] else r["s_user"]} for r in rows]
            else:
                rows = await conn.fetch("""
                    SELECT m.id, m.created_at, m.kind, m.anonymous, m.lesson, m.text, m.delivered, m.batch, ru.username AS r_user, ru.full_name AS r_name,
                      (SELECT full_name FROM hero_accounts WHERE user_id = m.to_uid AND full_name IS NOT NULL AND full_name <> '' ORDER BY id LIMIT 1) AS r_hero,
                      (SELECT o.anonymous FROM friend_msgs o WHERE o.id = m.reply_to) AS orig_anon
                    FROM friend_msgs m JOIN app_users ru ON ru.id = m.to_uid WHERE m.from_uid = $1 ORDER BY m.id DESC LIMIT 500""", me["id"])
                items, seen = [], {}
                for r in rows:
                    if r["kind"] == "req" and r["batch"]:
                        g = seen.get(r["batch"])
                        if g:
                            g["count"] += 1; g["delivered_n"] += 1 if r["delivered"] else 0; continue
                        g = {"id": r["id"], "time": _tk(r["created_at"]), "kind": "req", "anonymous": r["anonymous"], "lesson": r["lesson"], "text": r["text"],
                             "who": "", "count": 1, "delivered_n": 1 if r["delivered"] else 0, "delivered": True}
                        seen[r["batch"]] = g; items.append(g); continue
                    hide = r["kind"] == "reply" and r["orig_anon"]
                    items.append({"id": r["id"], "time": _tk(r["created_at"]), "kind": r["kind"], "anonymous": r["anonymous"], "lesson": r["lesson"], "text": r["text"],
                                  "who": "🎭 Anonim" if hide else _name3(r["r_hero"], r["r_name"], r["r_user"]), "user": None if hide else r["r_user"],
                                  "delivered": r["delivered"] is not False})
                items = items[:60]
        return js({"status": "success", "box": box, "items": items, "banned": friends._ban_text(bn) if bn else None})

    async def chat_send(request):
        me, err = await _chat_me(request)
        if err: return err
        if not friends._bot: return js({"status": "error", "message": "Bot sozlanmagan"})
        d = await _body(request)
        text, anon = str(d.get("text") or ""), (bool(d["anonymous"]) if "anonymous" in d else True)   # maydon yo'q bo'lsa — anonim (ism oshkor bo'lmasin)
        target = None
        if d.get("to_uid") is not None:   # darsdoshlar ro'yxatidan tanlangan
            try:
                g = await friends.lesson_by_key(me["id"], str(d.get("lesson") or ""))
                uid = int(d.get("to_uid"))
            except (TypeError, ValueError):
                return js({"status": "error", "message": "Noto'g'ri so'rov"})
            if not g: return js({"status": "error", "message": "Dars topilmadi (tugagan bo'lishi mumkin). Ro'yxatni yangilang."})
            if not any(c["uid"] == uid for c in await friends._classmates(me["id"], g, "send")):
                return js({"status": "error", "message": "Bu foydalanuvchiga xabar yuborib bo'lmaydi."})
            target = await friends._user(uid)
        else:
            target = await friends._find_by_username(str(d.get("username") or ""))
            if not target: return js({"status": "error", "message": "Bunday foydalanuvchi botda topilmadi. U avval botga /start yuborgan bo'lishi kerak."})
        ok, msg = await friends.api_send_dm(me, target, text, anon)
        return js({"status": "success" if ok else "error", "message": msg})

    async def chat_reply(request):
        me, err = await _chat_me(request)
        if err: return err
        if not friends._bot: return js({"status": "error", "message": "Bot sozlanmagan"})
        d = await _body(request)
        try: mid = int(d.get("id"))
        except (TypeError, ValueError): return js({"status": "error", "message": "Noto'g'ri so'rov"})
        ok, msg = await friends.api_send_reply(me, mid, str(d.get("text") or ""))
        return js({"status": "success" if ok else "error", "message": msg})

    async def chat_block(request):
        me, err = await _chat_me(request)
        if err: return err
        d = await _body(request)
        try: ok = await friends.block_sender(me, int(d.get("id")))
        except (TypeError, ValueError): ok = False
        return js({"status": "success" if ok else "error", "message": "🚫 Yuboruvchi bloklandi." if ok else "Xabar topilmadi."})

    async def chat_report(request):
        me, err = await _chat_me(request)
        if err: return err
        d = await _body(request)
        try: ok, msg = await friends.report_message(me, int(d.get("id")))
        except (TypeError, ValueError): ok, msg = False, "Noto'g'ri so'rov"
        return js({"status": "success" if ok else "error", "message": msg})

    async def chat_lessons(request):
        me, err = await _chat_me(request)
        if err: return err
        lessons = await friends._my_lessons(me["id"])
        return js({"status": "success", "lessons": [{"key": g["key"], "label": friends._lesson_label(g)} for g in lessons]})

    async def chat_classmates(request):
        me, err = await _chat_me(request)
        if err: return err
        g = await friends.lesson_by_key(me["id"], request.query.get("k") or "")
        if not g: return js({"status": "error", "message": "Dars topilmadi (tugagan bo'lishi mumkin)"})
        cm = await friends._classmates(me["id"], g, "find")
        return js({"status": "success", "label": friends._lesson_label(g),
                   "people": [{"uid": c["uid"], "name": c["full_name"] or (("@" + c["username"]) if c["username"] else "Foydalanuvchi"), "username": c["username"]} for c in cm]})

    async def chat_request(request):
        me, err = await _chat_me(request)
        if err: return err
        if not friends._bot: return js({"status": "error", "message": "Bot sozlanmagan"})
        d = await _body(request)
        g = await friends.lesson_by_key(me["id"], str(d.get("lesson") or ""))
        if not g: return js({"status": "error", "message": "Dars topilmadi (tugagan bo'lishi mumkin). Ro'yxatni yangilang."})
        ok, msg = await friends.api_send_request(me, g, str(d.get("text") or ""), (bool(d["anonymous"]) if "anonymous" in d else True))
        return js({"status": "success" if ok else "error", "message": msg})

    async def chat_settings(request):
        me, err = await _chat_me(request)
        if err: return err
        if request.method == "POST":
            d = await _body(request)
            for k in ("accept", "findable"):
                if k in d: await friends._set_pref(me["id"], k, bool(d[k]))
            if d.get("unblock_all"):
                async with db.pool.acquire() as conn: await conn.execute("DELETE FROM friend_blocks WHERE user_id=$1", int(me["id"]))
        async with db.pool.acquire() as conn:
            nb = await conn.fetchval("SELECT COUNT(*) FROM friend_blocks WHERE user_id=$1", int(me["id"])) or 0
        return js({"status": "success", **await friends._prefs(me["id"]), "blocked": nb})

    # ---------- foydalanuvchi: davomat va til
    async def attendance(request):
        u_id, _ = verify(request)
        if not u_id: return web.json_response({"status": "error"}, status=403)
        emails = await notify.user_emails(u_id)
        res = await stats.attendance(emails.keys())
        out = []
        for e, name in emails.items():
            r = res.get(e)
            if not r or not r["total"]: continue
            out.append({"email": e, "name": name, "attended": r["attended"], "total": r["total"], "pct": r["pct"], "week": r["week"], "month": r["month"], "streak": r["streak"],
                        "subjects": [{"title": k, "a": v[0], "t": v[1]} for k, v in sorted(r["subjects"].items(), key=lambda kv: kv[1][0] / kv[1][1])],
                        "missed": [{"time": st.strftime("%d.%m %H:%M"), "title": title} for st, title in r["missed"]]})
        return js({"status": "success", "accounts": out})

    async def lang(request):
        u_id, _ = verify(request)
        if not u_id: return web.json_response({"status": "error"}, status=403)
        if request.method == "POST":
            try: lg = (await request.json()).get("lang")
            except Exception: lg = None
            if lg not in i18n.LANGS: return js({"status": "error", "message": "Til noto'g'ri"})
            tg = await db.get_telegram_id(u_id)
            await notify.set_lang(u_id, tg, lg)
            return js({"status": "success", "lang": lg})
        return js({"status": "success", "lang": (await notify.get_prefs(u_id))["lang"]})

    r = app.router
    r.add_get("/api/admin/health", health); r.add_post("/api/admin/health_fix", health_fix); r.add_post("/api/admin/health_delete", health_delete)
    r.add_get("/api/admin/audit", audit_list)
    r.add_get("/api/admin/chat_reports", chat_reports); r.add_post("/api/admin/chat_ban", chat_ban)
    r.add_get("/api/admin/chat_pairs", chat_pairs)
    r.add_get("/api/chat/inbox", chat_inbox); r.add_post("/api/chat/send", chat_send); r.add_post("/api/chat/reply", chat_reply)
    r.add_post("/api/chat/block", chat_block); r.add_post("/api/chat/report", chat_report); r.add_get("/api/chat/lessons", chat_lessons)
    r.add_get("/api/chat/classmates", chat_classmates); r.add_post("/api/chat/request", chat_request)
    r.add_get("/api/chat/settings", chat_settings); r.add_post("/api/chat/settings", chat_settings)
    r.add_get("/api/admin/chat_log", chat_log); r.add_get("/api/admin/chat_thread", chat_thread)
    r.add_post("/api/admin/chat_unban", chat_unban); r.add_post("/api/admin/chat_review", chat_review)
    r.add_get("/api/attendance", attendance); r.add_get("/api/lang", lang); r.add_post("/api/lang", lang)
