import asyncpg
import os
import random
import secrets
import hmac
import string
import json
import logging
import bcrypt
from datetime import datetime, timedelta
import students
from cryptography.fernet import Fernet

DATABASE_URL = os.getenv("DATABASE_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
pool = None
logger = logging.getLogger(__name__)

# Shifrlash sozlamalari. Kalit bo'lmasa har ishga tushganda yangi tasodifiy kalit paydo bo'lib, saqlangan Hero parollari
# qayta ochilmay qolardi — shuning uchun kalitsiz ishga tushmaymiz (faqat sinov uchun ALLOW_EPHEMERAL_KEY=1).
if ENCRYPTION_KEY:
    try: cipher_suite = Fernet(ENCRYPTION_KEY.strip().encode())
    except Exception: raise RuntimeError("ENCRYPTION_KEY noto'g'ri. To'g'ri kalit (44 belgi) olish: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
elif os.getenv("ALLOW_EPHEMERAL_KEY") == "1":
    cipher_suite = Fernet(Fernet.generate_key())
    logger.warning("⚠️ ENCRYPTION_KEY yo'q — vaqtinchalik kalit ishlatilyapti: qayta ishga tushganda parollar yo'qoladi (faqat sinov uchun!)")
else:
    raise RuntimeError("ENCRYPTION_KEY o'rnatilmagan! Hero parollari shu kalit bilan shifrlanadi. Yangi kalit yarating: "
                       "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
                       "va uni hosting (Railway) o'zgaruvchilariga ENCRYPTION_KEY sifatida qo'shing, SAQLAB QO'YING. "
                       "(Faqat sinov uchun: ALLOW_EPHEMERAL_KEY=1)")

def _looks_encrypted(v): return isinstance(v, str) and v.startswith("gAAAA")

def encrypt_pass(password: str) -> str:
    if not password: return password
    return cipher_suite.encrypt(password.encode()).decode()

def decrypt_pass(encrypted_password: str) -> str:
    if not encrypted_password: return encrypted_password
    try: return cipher_suite.decrypt(encrypted_password.encode()).decode()
    except Exception:
        if _looks_encrypted(encrypted_password):   # shifrlangan, lekin kalit mos emas — shifrni parol sifatida yubormaymiz
            logger.error("Parolni shifrdan ochib bo'lmadi: ENCRYPTION_KEY o'zgargan bo'lishi mumkin")
            return ""
        return encrypted_password   # Eski shifrlanmagan parollar uchun himoya

async def init_db():
    global pool
    try:
        if pool is None:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, server_settings={'timezone': 'UTC'})
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id SERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE, login VARCHAR(50) UNIQUE,
                    password VARCHAR(100), created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS hero_accounts (
                    id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
                    email VARCHAR(200), hero_password VARCHAR(200), bearer_token TEXT, UNIQUE(user_id, email)
                );
                CREATE TABLE IF NOT EXISTS scan_logs (
                    id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
                    success_count INTEGER, scanned_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS archived_accounts (
                    id SERIAL PRIMARY KEY, user_id INTEGER, email VARCHAR(200), 
                    hero_password VARCHAR(200), deleted_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS share_codes (
                    id SERIAL PRIMARY KEY, from_user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
                    code VARCHAR(10) UNIQUE NOT NULL, expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT FALSE, used_by INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bot_settings (key VARCHAR(64) PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS hero_schedules (
                    email VARCHAR(200) PRIMARY KEY, raw JSONB, range_begin BIGINT, range_end BIGINT,
                    lesson_count INTEGER DEFAULT 0, fetched_at TIMESTAMP, checked_at TIMESTAMP DEFAULT NOW(), error TEXT
                );
                CREATE TABLE IF NOT EXISTS jokes (
                    id SERIAL PRIMARY KEY, text TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW()
                );
                DELETE FROM jokes a USING jokes b WHERE a.id > b.id AND md5(a.text) = md5(b.text);
                CREATE UNIQUE INDEX IF NOT EXISTS jokes_text_uniq ON jokes (md5(text));
                CREATE TABLE IF NOT EXISTS hero_transcripts (
                    email VARCHAR(200) PRIMARY KEY, raw JSONB, content_type VARCHAR(100), course_count INTEGER DEFAULT 0,
                    fetched_at TIMESTAMP, checked_at TIMESTAMP DEFAULT NOW(), error TEXT
                );
            """)
            await conn.execute("""
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP DEFAULT NOW() + INTERVAL '30 days';
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS shadow_targets JSONB DEFAULT '[]'::jsonb;
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS username VARCHAR(64);
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(32);
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS full_name VARCHAR(128);
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;
                ALTER TABLE app_users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE;
                ALTER TABLE scan_logs ADD COLUMN IF NOT EXISTS total_count INTEGER DEFAULT 0;
                ALTER TABLE scan_logs ADD COLUMN IF NOT EXISTS duration FLOAT DEFAULT 0.0;
                ALTER TABLE scan_logs ADD COLUMN IF NOT EXISTS details JSONB;
                ALTER TABLE scan_logs ADD COLUMN IF NOT EXISTS lesson VARCHAR(160);
                ALTER TABLE hero_accounts DROP COLUMN IF EXISTS proxy_url;
                ALTER TABLE hero_accounts ADD COLUMN IF NOT EXISTS student_id VARCHAR(64);
                ALTER TABLE hero_accounts ADD COLUMN IF NOT EXISTS full_name VARCHAR(300);
                ALTER TABLE archived_accounts ADD COLUMN IF NOT EXISTS student_id VARCHAR(64);
                ALTER TABLE archived_accounts ADD COLUMN IF NOT EXISTS full_name VARCHAR(300);
                ALTER TABLE hero_accounts ALTER COLUMN full_name TYPE TEXT;
                ALTER TABLE hero_accounts ALTER COLUMN student_id TYPE TEXT;
                ALTER TABLE archived_accounts ALTER COLUMN full_name TYPE TEXT;
                ALTER TABLE archived_accounts ALTER COLUMN student_id TYPE TEXT;
                CREATE INDEX IF NOT EXISTS scan_logs_scanned_idx ON scan_logs (scanned_at);
                CREATE INDEX IF NOT EXISTS scan_logs_user_idx ON scan_logs (user_id, scanned_at);
                CREATE INDEX IF NOT EXISTS hero_accounts_email_idx ON hero_accounts (email);
            """)
        logger.info("✅ Database tayyor.")
    except Exception as e:
        logger.error(f"❌ Baza xatosi: {e}")
        raise   # baza bo'lmasa bot ishlay olmaydi: aniq xato bilan to'xtaymiz

async def get_or_create_user(telegram_id, username=None, full_name=None):
    """Returns (login, plaintext_password_or_None, trial_ends_at).
    plaintext_password is only non-None for a brand-new user (right after creation) —
    since the password is stored hashed, it can't be recovered for existing users.
    Call reset_password() if an existing user needs a new one.
    If username is given, it's (re)saved every time so a changed Telegram @handle stays current."""
    login = f"hero_{telegram_id}"
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT login, trial_ends_at FROM app_users WHERE telegram_id=$1", telegram_id)
        if user:
            if username: await conn.execute("UPDATE app_users SET username=$1 WHERE telegram_id=$2", username, telegram_id)
            if full_name: await conn.execute("UPDATE app_users SET full_name=$1 WHERE telegram_id=$2", full_name, telegram_id)
            return user['login'], None, user['trial_ends_at']
        password = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
        pass_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await conn.execute("INSERT INTO app_users (telegram_id, login, password, username, full_name) VALUES ($1, $2, $3, $4, $5)", telegram_id, login, pass_hash, username, full_name)
        new_user = await conn.fetchrow("SELECT trial_ends_at FROM app_users WHERE telegram_id=$1", telegram_id)
        return login, password, new_user['trial_ends_at']

async def get_users_for_sync():
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT telegram_id, username, full_name FROM app_users ORDER BY id")]

async def get_blocked_ids():
    async with pool.acquire() as conn:
        return [r["id"] for r in await conn.fetch("SELECT id FROM app_users WHERE is_blocked = TRUE")]

async def set_blocked(user_id, flag):
    async with pool.acquire() as conn:
        res = await conn.execute("UPDATE app_users SET is_blocked=$1 WHERE id=$2", bool(flag), int(user_id))
    return res == "UPDATE 1"

async def get_admin_rows(owner_id):
    """Adminlar: bosh admin (ADMIN_ID) va is_admin belgisi qo'yilganlar."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, telegram_id, username, full_name, login, is_admin FROM app_users WHERE is_admin = TRUE OR telegram_id = $1 ORDER BY id", int(owner_id or 0))
    return [dict(r) for r in rows]

async def set_admin(telegram_id, flag):
    async with pool.acquire() as conn:
        res = await conn.execute("UPDATE app_users SET is_admin=$1 WHERE telegram_id=$2", bool(flag), int(telegram_id))
    return res == "UPDATE 1"

async def find_user_for_message(query):
    """Xabar yuborish uchun bitta foydalanuvchini topadi: @username, Telegram ID yoki hero_... login bo'yicha."""
    q = (query or "").strip()
    if not q: return None
    async with pool.acquire() as conn:
        if q.isdigit() and len(q) <= 15:
            row = await conn.fetchrow("SELECT telegram_id, username, full_name, login FROM app_users WHERE telegram_id=$1", int(q))
        elif q.lower().startswith("hero_"):
            row = await conn.fetchrow("SELECT telegram_id, username, full_name, login FROM app_users WHERE lower(login)=lower($1)", q)
        else:
            row = await conn.fetchrow("SELECT telegram_id, username, full_name, login FROM app_users WHERE lower(username)=lower($1)", q.lstrip("@"))
    return dict(row) if row else None

async def sync_user_profile(telegram_id, username, full_name):
    """Telegram'dan olingan hozirgi profilni yozadi (username olib tashlangan bo'lsa NULL qiladi). Parolga tegmaydi."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE app_users SET username=$1, full_name=$2 WHERE telegram_id=$3", username, full_name, telegram_id)

async def get_setting(key):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key=$1", key)
    return row["value"] if row else None

async def set_setting(key, value):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, str(value))

async def get_user_contact(user_id):
    """Returns telegram_id/login/username/phone_number for one app user, used to identify who sent feedback."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, telegram_id, login, username, phone_number, full_name, is_blocked FROM app_users WHERE id=$1", int(user_id))
    return dict(row) if row else None

async def reset_password(telegram_id):
    """Generates a new plaintext password, stores its bcrypt hash, and returns (login, plaintext_password)."""
    password = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    pass_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE app_users SET password=$1 WHERE telegram_id=$2 RETURNING login", pass_hash, telegram_id)
    return (row['login'], password) if row else (None, None)

async def verify_login(login, password, admin_id):
    if not login or not password: return None, "Bo'sh maydon"
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, telegram_id, password, trial_ends_at, is_admin, is_blocked FROM app_users WHERE login=$1", login)
        if not user: return None, "Login yoki parol xato"
        stored = user['password'] or ''
        try:
            ok = bool(stored) and bcrypt.checkpw(password.encode(), stored.encode())
        except (ValueError, AttributeError):
            ok = bool(stored) and hmac.compare_digest(password.encode(), stored.encode())  # eski, shifrlanmagan qatorlar uchun himoya
        if not ok: return None, "Login yoki parol xato"
        if user['is_blocked'] and user['telegram_id'] != admin_id: return None, "BLOCKED"
        role = "super_admin" if (user['telegram_id'] == admin_id or user['is_admin']) else "user"
        if role != "super_admin" and user['trial_ends_at'] and user['trial_ends_at'] < datetime.utcnow(): return None, "TRIAL_ENDED"
        return {"id": user['id'], "role": role}, "OK"

async def get_telegram_id(user_id):
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT telegram_id FROM app_users WHERE id=$1", int(user_id))

async def extend_user_trial(target_id):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE app_users SET trial_ends_at = GREATEST(trial_ends_at, NOW()) + INTERVAL '30 days' WHERE id=$1", int(target_id))

async def add_hero_account(user_id, email, password, token="NO_TOKEN"):
    email = (email or "").strip().lower()
    enc_pass = encrypt_pass(password)
    st = students.lookup(email) or {}
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO hero_accounts (user_id, email, hero_password, bearer_token, student_id, full_name) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (user_id, email) DO UPDATE SET hero_password=$3, bearer_token=$4, student_id=$5, full_name=$6", int(user_id), email, enc_pass, token, st.get("student_id"), st.get("full_name"))

async def backfill_student_info():
    """Bazada allaqachon bor akkauntlar (ism/ID yo'q) uchun ro'yxatdan ism va ID ni to'ldiradi. Qaytaradi: (yangilandi, topilmadi)."""
    done = miss = 0
    async with pool.acquire() as conn:
        for tbl in ("hero_accounts", "archived_accounts"):
            rows = await conn.fetch(f"SELECT id, email FROM {tbl} WHERE full_name IS NULL OR student_id IS NULL")
            for r in rows:
                st = students.lookup(r["email"])
                if not st: miss += 1; continue
                await conn.execute(f"UPDATE {tbl} SET student_id=$1, full_name=$2 WHERE id=$3", st["student_id"], st["full_name"], r["id"])
                done += 1
    return done, miss

async def edit_hero_account(user_id, acc_id, new_email, new_pass):
    new_email = (new_email or "").strip().lower()
    enc_pass = encrypt_pass(new_pass)
    st = students.lookup(new_email) or {}
    async with pool.acquire() as conn:
        try:
            res = await conn.execute("UPDATE hero_accounts SET email=$1, hero_password=$2, bearer_token='NO_TOKEN', student_id=$3, full_name=$4 WHERE id=$5 AND user_id=$6", new_email, enc_pass, st.get("student_id"), st.get("full_name"), int(acc_id), int(user_id))
        except asyncpg.UniqueViolationError:   # shu email allaqachon qo'shilgan
            return False
        return res == "UPDATE 1"

async def update_bearer_token(email, token):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE hero_accounts SET bearer_token=$1 WHERE email=$2", token, email)

async def get_all_hero_accounts_raw():
    """Barcha foydalanuvchilarning barcha Hero akkauntlari (parol hal qilingan holda) — fondagi token-tiklash tsikli uchun."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, email, hero_password, bearer_token FROM hero_accounts")
    res = []
    for r in rows:
        d = dict(r)
        d['hero_password'] = decrypt_pass(d['hero_password'])
        res.append(d)
    return res

async def delete_hero_account(user_id, account_id):
    async with pool.acquire() as conn:
        acc = await conn.fetchrow("SELECT email, hero_password, student_id, full_name FROM hero_accounts WHERE id=$1 AND user_id=$2", int(account_id), int(user_id))
        if acc:
            await conn.execute("INSERT INTO archived_accounts (user_id, email, hero_password, student_id, full_name) VALUES ($1, $2, $3, $4, $5)", int(user_id), acc['email'], acc['hero_password'], acc['student_id'], acc['full_name']) # hero_password allaqachon shifrlangan
            res = await conn.execute("DELETE FROM hero_accounts WHERE id=$1 AND user_id=$2", int(account_id), int(user_id))
            return res == "DELETE 1"
        return False

async def admin_delete_accounts(email):
    """Admin: shu email bilan barcha hero_accounts qatorlarini ARXIVGA ko'chirib o'chiradi (📦 Arxiv bo'limidan qaytariladi);
    jadval va transcript keshi ham tozalanadi. -> o'chirilgan qatorlar soni"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT user_id, email, hero_password, student_id, full_name FROM hero_accounts WHERE email=$1", email)
            for r in rows:
                await conn.execute("INSERT INTO archived_accounts (user_id, email, hero_password, student_id, full_name) VALUES ($1, $2, $3, $4, $5)",
                                   r["user_id"], r["email"], r["hero_password"], r["student_id"], r["full_name"])
            await conn.execute("DELETE FROM hero_accounts WHERE email=$1", email)
            await conn.execute("DELETE FROM hero_schedules WHERE email=$1", email)
            await conn.execute("DELETE FROM hero_transcripts WHERE email=$1", email)
    return len(rows)

async def get_user_stats(user_id):
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM hero_accounts WHERE user_id=$1", int(user_id))
        last = await conn.fetchrow("SELECT success_count, total_count FROM scan_logs WHERE user_id=$1 ORDER BY scanned_at DESC LIMIT 1", int(user_id))
        return {"total_accounts": total or 0, "last_success": last['success_count'] if last else 0, "last_total": last['total_count'] if last else 0}

async def save_detailed_scan(user_id, success, total, duration, details, lesson=None):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO scan_logs (user_id, success_count, total_count, duration, details, lesson) VALUES ($1, $2, $3, $4, $5, $6)", int(user_id), success, total, duration, json.dumps(details), lesson)

async def get_hero_accounts(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id, email, hero_password, bearer_token, student_id, full_name FROM hero_accounts WHERE user_id=$1 ORDER BY id DESC", int(user_id))

async def get_active_tokens(user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM hero_accounts WHERE user_id=$1", int(user_id))
        res = []
        for r in rows:
            d = dict(r)
            d['hero_password'] = decrypt_pass(d['hero_password'])
            res.append(d)
        return res

async def get_account_creds(email):
    """Bitta email bo'yicha (parol hal qilingan) akkaunt ma'lumoti — jadvalni qo'lda yangilash uchun."""
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT email, hero_password, bearer_token FROM hero_accounts WHERE email=$1 ORDER BY id DESC LIMIT 1", email)
    if not r: return None
    d = dict(r); d['hero_password'] = decrypt_pass(d['hero_password'])
    return d

async def save_schedule_ok(email, raw, begin, end, count):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO hero_schedules (email, raw, range_begin, range_end, lesson_count, fetched_at, checked_at, error) "
            "VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), NULL) "
            "ON CONFLICT (email) DO UPDATE SET raw=$2, range_begin=$3, range_end=$4, lesson_count=$5, fetched_at=NOW(), checked_at=NOW(), error=NULL",
            email, json.dumps(raw), int(begin), int(end), int(count))

async def save_schedule_error(email, error):
    """Xatoda eski jadval o'chmaydi — faqat xato matni va tekshirilgan vaqt yangilanadi."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO hero_schedules (email, checked_at, error) VALUES ($1, NOW(), $2) "
            "ON CONFLICT (email) DO UPDATE SET checked_at=NOW(), error=$2", email, str(error)[:300])

async def get_schedule(email):
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT email, raw, range_begin, range_end, lesson_count, fetched_at, checked_at, error FROM hero_schedules WHERE email=$1", email)
    return dict(r) if r else None

async def get_schedule_overview():
    """Har bir noyob Hero akkaunt uchun: ism/ID, faolmi, jadval nechta dars va qachon yangilangan."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (a.email) a.email, a.full_name, a.student_id, "
            "(a.bearer_token IS NOT NULL AND a.bearer_token <> 'NO_TOKEN' AND a.bearer_token NOT LIKE 'ERROR%') AS active, "
            "s.lesson_count, s.fetched_at, s.checked_at, s.error "
            "FROM hero_accounts a LEFT JOIN hero_schedules s ON s.email = a.email ORDER BY a.email, a.id DESC")
    return [parse_db_row(r) for r in rows]

async def save_transcript_ok(email, raw, content_type, count):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO hero_transcripts (email, raw, content_type, course_count, fetched_at, checked_at, error) "
            "VALUES ($1, $2, $3, $4, NOW(), NOW(), NULL) "
            "ON CONFLICT (email) DO UPDATE SET raw=$2, content_type=$3, course_count=$4, fetched_at=NOW(), checked_at=NOW(), error=NULL",
            email, json.dumps(raw), (content_type or "")[:100], int(count))

async def save_transcript_error(email, error):
    """Xatoda eski transcript o'chmaydi — faqat xato matni va tekshirilgan vaqt yangilanadi."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO hero_transcripts (email, checked_at, error) VALUES ($1, NOW(), $2) "
            "ON CONFLICT (email) DO UPDATE SET checked_at=NOW(), error=$2", email, str(error)[:300])

async def get_transcript(email):
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT email, raw, content_type, course_count, fetched_at, checked_at, error FROM hero_transcripts WHERE email=$1", email)
    return dict(r) if r else None

async def get_transcript_overview():
    """Har bir noyob Hero akkaunt uchun: ism/ID, faolmi, transcript nechta fan va qachon yangilangan."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (a.email) a.email, a.full_name, a.student_id, "
            "(a.bearer_token IS NOT NULL AND a.bearer_token <> 'NO_TOKEN' AND a.bearer_token NOT LIKE 'ERROR%') AS active, "
            "t.course_count, t.fetched_at, t.checked_at, t.error "
            "FROM hero_accounts a LEFT JOIN hero_transcripts t ON t.email = a.email ORDER BY a.email, a.id DESC")
    return [parse_db_row(r) for r in rows]

async def get_scan_report_rows(since_utc, until_utc, limit=5000):
    """Berilgan oraliqdagi (UTC) barcha skanerlar: kim, qaysi dars, qaysi akkauntlar, natija, vaqt."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT l.id, l.user_id, {_DISPLAY_SQL} as login, u.full_name as tg_name, l.success_count, l.total_count, l.duration, l.lesson, l.scanned_at, l.details "
            "FROM scan_logs l JOIN app_users u ON l.user_id = u.id WHERE l.scanned_at >= $1 AND l.scanned_at < $2 ORDER BY l.scanned_at DESC LIMIT $3",
            since_utc, until_utc, int(limit))
    return [dict(r) for r in rows]

JOKE_MAX_LEN = 500

def _clean_joke(t):
    return " ".join(str(t or "").split())[:JOKE_MAX_LEN].strip()

async def get_jokes():
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT id, text FROM jokes ORDER BY id DESC")]

async def count_jokes():
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM jokes") or 0

async def add_jokes_bulk(items, replace=False):
    """items: matnlar ro'yxati. replace=True bo'lsa avval hammasi o'chiriladi. -> (qo'shildi, takror/bo'sh o'tkazildi)"""
    clean, seen = [], set()
    for t in items:
        t = _clean_joke(t)
        if t and t not in seen: seen.add(t); clean.append(t)
    added = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            if replace: await conn.execute("DELETE FROM jokes")
            for t in clean:
                added += 1 if (await conn.execute("INSERT INTO jokes (text) VALUES ($1) ON CONFLICT DO NOTHING", t)) == "INSERT 0 1" else 0
    return added, len(items) - added

async def update_joke(joke_id, text):
    text = _clean_joke(text)
    if not text: return "empty"
    async with pool.acquire() as conn:
        try:
            res = await conn.execute("UPDATE jokes SET text=$1 WHERE id=$2", text, int(joke_id))
        except Exception:
            return "duplicate"
    return "ok" if res == "UPDATE 1" else "missing"

async def delete_joke(joke_id):
    async with pool.acquire() as conn:
        return await conn.execute("DELETE FROM jokes WHERE id=$1", int(joke_id)) == "DELETE 1"

async def get_shadow_admins(target_user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id FROM app_users WHERE shadow_targets @> $1::jsonb", json.dumps([int(target_user_id)]))

async def set_shadow_target(admin_id, target_id):
    async with pool.acquire() as conn:
        curr = await conn.fetchval("SELECT shadow_targets FROM app_users WHERE id=$1", int(admin_id))
        if not curr: targets = []
        elif isinstance(curr, str): targets = json.loads(curr)
        else: targets = list(curr)
        tid = int(target_id)
        if tid in targets: targets.remove(tid)
        else: targets.append(tid)
        await conn.execute("UPDATE app_users SET shadow_targets=$1::jsonb WHERE id=$2", json.dumps(targets), int(admin_id))

def parse_db_row(row):
    if not row: return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime): d[k] = v.isoformat()
    return d

# Har joyda foydalanuvchini "hero_..." login o'rniga Telegram @username yoki telefon raqami bilan ko'rsatish uchun.
# Ikkalasi ham bo'lmasa — Telegram ismi, u ham bo'lmasa login'ga qaytadi.
_DISPLAY_SQL = "CASE WHEN u.username IS NOT NULL AND u.username <> '' THEN '@' || u.username WHEN u.phone_number IS NOT NULL AND u.phone_number <> '' THEN u.phone_number WHEN u.full_name IS NOT NULL AND u.full_name <> '' THEN u.full_name ELSE u.login END"

async def get_super_admin_data(admin_id):
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM app_users") or 0
        # Bir xil email turli userlarga qo'shilgan bo'lishi mumkin (takrorlangan akkaunt) — ularni bitta deb sanaymiz.
        total_heroes = await conn.fetchval("SELECT COUNT(DISTINCT email) FROM hero_accounts") or 0
        # 'Bugun' — Toshkent kuni bo'yicha (UTC+5); baza vaqti UTC
        today_scans = await conn.fetchval("SELECT COUNT(*) FROM scan_logs WHERE (scanned_at + INTERVAL '5 hours')::date = (NOW() AT TIME ZONE 'UTC' + INTERVAL '5 hours')::date") or 0
        
        try: 
            raw_shadows = await conn.fetchval("SELECT shadow_targets FROM app_users WHERE id=$1", int(admin_id))
            my_shadows = json.loads(raw_shadows) if isinstance(raw_shadows, str) else (list(raw_shadows) if raw_shadows else [])
        except: my_shadows = []

        users_data = await conn.fetch(f"SELECT u.id, {_DISPLAY_SQL} as login, u.telegram_id, u.trial_ends_at, u.created_at, u.is_blocked, COUNT(a.id) as hero_count FROM app_users u LEFT JOIN hero_accounts a ON u.id = a.user_id GROUP BY u.id ORDER BY u.created_at DESC")
        
        accounts_data = await conn.fetch(f"SELECT {_DISPLAY_SQL} as tg_login, a.email, a.hero_password, a.full_name, a.student_id FROM hero_accounts a JOIN app_users u ON a.user_id = u.id ORDER BY a.id DESC")
        accs = []
        for a in accounts_data:
            d = parse_db_row(a)
            d['hero_password'] = decrypt_pass(d['hero_password'])
            accs.append(d)

        logs_data = await conn.fetch(f"SELECT {_DISPLAY_SQL} as login, l.success_count, l.total_count, l.duration, l.lesson, l.scanned_at, l.details FROM scan_logs l JOIN app_users u ON l.user_id = u.id ORDER BY l.scanned_at DESC LIMIT 50")
        
        archived_data = await conn.fetch(f"SELECT {_DISPLAY_SQL} as tg_login, a.email, a.hero_password, a.full_name, a.student_id, a.deleted_at FROM archived_accounts a JOIN app_users u ON a.user_id = u.id ORDER BY a.id DESC")
        archs = []
        for ar in archived_data:
            d = parse_db_row(ar)
            d['hero_password'] = decrypt_pass(d['hero_password'])
            archs.append(d)

        return {
            "stats": {"total_users": total_users, "total_heroes": total_heroes, "today_scans": today_scans},
            "my_shadows": my_shadows,
            "users": [parse_db_row(u) for u in users_data], 
            "accounts": accs, 
            "logs": [dict(parse_db_row(l), details=_enrich_details(_jsonish(l["details"]))) for l in logs_data],
            "archived": archs
        }

def _jsonish(v):
    """asyncpg JSONB'ni ba'zan matn (str) qilib beradi — uni haqiqiy obyektga aylantiramiz."""
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return v
    return v

def _enrich_details(details):
    """Eski skaner yozuvlarida faqat email bor — ro'yxatdan ism/ID ni qo'shib beradi (bazaga tegmaydi)."""
    if not isinstance(details, list): return details
    out = []
    for d in details:
        if isinstance(d, dict) and not d.get("name"):
            st = students.lookup(d.get("email")) or {}
            d = dict(d, name=st.get("full_name"), student_id=st.get("student_id"))
        out.append(d)
    return out

def _import_pw(pw):
    """Zaxiradagi parol: yangi zaxirada shifrlangan (ENCRYPTION_KEY bilan ochiladi), eski zaxirada ochiq matn bo'lishi mumkin."""
    if not pw: return None
    if _looks_encrypted(pw):
        d = decrypt_pass(pw)
        return d or None
    return pw

async def get_full_backup_data():
    """Zaxira (backup) uchun barcha jadvallarning TO'LIQ ma'lumoti (hech qanday LIMIT'siz).
    Hero parollari SHIFRLANGAN holda qaytadi (ochiq matn Telegram'ga ketmasligi uchun); tiklash uchun ENCRYPTION_KEY kerak."""
    async with pool.acquire() as conn:
        users = [dict(r) for r in await conn.fetch("SELECT id, telegram_id, login, password, username, phone_number, full_name, is_admin, is_blocked, created_at, trial_ends_at, shadow_targets FROM app_users ORDER BY id")]
        accounts = [dict(r) for r in await conn.fetch("SELECT id, user_id, email, hero_password, bearer_token, student_id, full_name FROM hero_accounts ORDER BY id")]
        scans = [dict(r) for r in await conn.fetch("SELECT id, user_id, success_count, total_count, duration, lesson, details, scanned_at FROM scan_logs ORDER BY id")]
        archived = [dict(r) for r in await conn.fetch("SELECT id, user_id, email, hero_password, student_id, full_name, deleted_at FROM archived_accounts ORDER BY id")]
        shares = [dict(r) for r in await conn.fetch("SELECT id, from_user_id, code, expires_at, used, used_by, created_at FROM share_codes ORDER BY id")]
    for u in users: u["shadow_targets"] = _jsonish(u.get("shadow_targets")) or []
    for x in accounts + archived:   # eski (shifrlanmagan) qatorlar ham zaxiraga ochiq matn bo'lib tushmasin
        if x["hero_password"] and not _looks_encrypted(x["hero_password"]): x["hero_password"] = encrypt_pass(x["hero_password"])
    for s in scans: s["details"] = _jsonish(s.get("details"))
    return {"app_users": users, "hero_accounts": accounts, "scan_logs": scans, "archived_accounts": archived, "share_codes": shares}

async def create_share_code(user_id):
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM share_codes WHERE from_user_id=$1 AND used=FALSE", int(user_id))
        expires_at = await conn.fetchval("INSERT INTO share_codes (from_user_id, code, expires_at) VALUES ($1, $2, NOW() + INTERVAL '24 hours') RETURNING expires_at", int(user_id), code)
    return code, expires_at

async def consume_share_code(code, importer_id):
    async with pool.acquire() as conn:
        peek = await conn.fetchrow("SELECT from_user_id FROM share_codes WHERE code=$1 AND used=FALSE AND expires_at > NOW()", code.upper())
        if not peek: return False, "Kod topilmadi yoki muddati o'tgan"
        if peek['from_user_id'] == int(importer_id): return False, "O'z kodingizni qo'llay olmaysiz"
        # atomik band qilish: ikki kishi bir vaqtda kiritsa ham kod faqat bittasiga tegadi
        row = await conn.fetchrow("UPDATE share_codes SET used=TRUE, used_by=$1 WHERE code=$2 AND used=FALSE AND expires_at > NOW() RETURNING id, from_user_id", int(importer_id), code.upper())
        if not row: return False, "Kod topilmadi yoki muddati o'tgan"
        sharer_id = row['from_user_id']

        curr = await conn.fetchval("SELECT shadow_targets FROM app_users WHERE id=$1", sharer_id)
        if not curr: targets = []
        elif isinstance(curr, str): targets = json.loads(curr)
        else: targets = list(curr)
        tid = int(importer_id)
        if tid not in targets: targets.append(tid)
        
        await conn.execute("UPDATE app_users SET shadow_targets=$1::jsonb WHERE id=$2", json.dumps(targets), sharer_id)
        sharer_login = await conn.fetchval("SELECT login FROM app_users WHERE id=$1", sharer_id)
    return True, sharer_login

async def get_share_connections(user_id):
    uid = int(user_id)
    async with pool.acquire() as conn:
        my_targets_raw = await conn.fetchval("SELECT shadow_targets FROM app_users WHERE id=$1", uid)
        if not my_targets_raw: my_targets = []
        elif isinstance(my_targets_raw, str): my_targets = json.loads(my_targets_raw)
        else: my_targets = list(my_targets_raw)

        sharers = await conn.fetch("SELECT id, login FROM app_users WHERE shadow_targets @> $1::jsonb AND id != $2", json.dumps([uid]), uid)
        result = [{"login": s['login'], "type": "shared_to_me"} for s in sharers]
        
        if my_targets:
            rows = await conn.fetch("SELECT login FROM app_users WHERE id = ANY($1::int[])", my_targets)
            for r in rows: result.append({"login": r['login'], "type": "shared_from_me"})
    return result


# ---------------------------------------------------------------------------
# ESKI ZAXIRA (JSON) NI BAZAGA QO'SHISH (IMPORT)
# ---------------------------------------------------------------------------
def _to_dt(v):
    """ISO matn -> oddiy (tz'siz, UTC) datetime. Bo'sh/xato bo'lsa None."""
    if v is None or v == "": return None
    if isinstance(v, datetime): return v.replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            from datetime import timezone
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None

def _as_list(v):
    v = _jsonish(v)
    return v if isinstance(v, list) else []

async def import_backup_data(data):
    """backup.py yaratgan JSON zaxirani MAVJUD bazaga QO'SHADI (hech narsani o'chirmaydi, ustidan yozmaydi).

    - Foydalanuvchilar telegram_id bo'yicha solishtiriladi: yangi bo'lsa — eski login va parol hash'i bilan
      qo'shiladi (ular eski parol bilan kiraveradi); allaqachon bor bo'lsa — parol o'zgarmaydi, faqat
      obuna muddati uzoqroq bo'lganini oladi va admin/blok belgilari qo'shiladi.
    - Hero akkauntlar (user_id, email) bo'yicha: bor bo'lsa o'tkazib yuboriladi. Parol joriy ENCRYPTION_KEY bilan
      qayta shifrlanadi. Token zaxirada yo'q — 'NO_TOKEN' bo'ladi (keyin avtomatik/qo'lda tiklanadi).
    - Arxiv va skaner tarixi takrorlanmaydi (bir xil yozuv bo'lsa o'tkazib yuboriladi).
    - Ulashish kodlari (24 soatlik) import qilinmaydi; 'shadow_targets' (ulashilgan ulanishlar) yangi id'larga o'giriladi.
    Qaytaradi: hisobot dict. Xato bo'lsa ValueError (matni foydalanuvchiga ko'rsatiladi)."""
    if not isinstance(data, dict) or not isinstance(data.get("app_users"), list):
        raise ValueError("Bu HeroScanner zaxira JSON fayli emas ('app_users' topilmadi).")
    fmt = (data.get("meta") or {}).get("format")
    if fmt and fmt != "heroscanner-backup":
        raise ValueError(f"Noma'lum zaxira formati: {fmt}")

    rep = {"users_new": 0, "users_existing": 0, "users_failed": 0,
           "acc_new": 0, "acc_skipped": 0, "acc_failed": 0,
           "arch_new": 0, "arch_skipped": 0,
           "scans_new": 0, "scans_skipped": 0,
           "shadows": 0}
    id_map = {}   # eski app_users.id -> yangi app_users.id

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ---- 1) Foydalanuvchilar ----
            for u in data["app_users"]:
                try:
                    old_id, tg = u.get("id"), u.get("telegram_id")
                    if tg is None: rep["users_failed"] += 1; continue
                    tg = int(tg)
                    trial = _to_dt(u.get("trial_ends_at"))
                    created = _to_dt(u.get("created_at")) or datetime.utcnow()
                    row = await conn.fetchrow("SELECT id, trial_ends_at FROM app_users WHERE telegram_id=$1", tg)
                    if row:
                        new_id = row["id"]
                        async with conn.transaction():
                            await conn.execute(
                                "UPDATE app_users SET trial_ends_at=GREATEST(trial_ends_at, COALESCE($1, trial_ends_at)), "
                                "is_admin=(is_admin OR $2), is_blocked=(is_blocked OR $3), "
                                "username=COALESCE(username, $4), full_name=COALESCE(full_name, $5), phone_number=COALESCE(phone_number, $6) "
                                "WHERE id=$7",
                                trial, bool(u.get("is_admin")), bool(u.get("is_blocked")),
                                u.get("username"), u.get("full_name"), u.get("phone_number"), new_id)
                        rep["users_existing"] += 1
                    else:
                        login = u.get("login") or f"hero_{tg}"
                        async with conn.transaction():   # savepoint: bitta xato butun importni buzmasin
                            new_id = await conn.fetchval(
                                "INSERT INTO app_users (telegram_id, login, password, username, phone_number, full_name, is_admin, is_blocked, created_at, trial_ends_at) "
                                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10, NOW() + INTERVAL '30 days')) RETURNING id",
                                tg, login, u.get("password"), u.get("username"), u.get("phone_number"), u.get("full_name"),
                                bool(u.get("is_admin")), bool(u.get("is_blocked")), created, trial)
                        rep["users_new"] += 1
                    if old_id is not None: id_map[int(old_id)] = new_id
                except Exception as e:
                    rep["users_failed"] += 1
                    logger.warning(f"Import: foydalanuvchi o'tkazib yuborildi ({u.get('telegram_id')}): {e}")

            # ---- 2) Hero akkauntlar ----
            for a in data.get("hero_accounts") or []:
                try:
                    uid = id_map.get(int(a.get("user_id"))) if a.get("user_id") is not None else None
                    email = (a.get("email") or "").strip().lower()
                    pw = _import_pw(a.get("hero_password"))
                    if not uid or not email or not pw: rep["acc_failed"] += 1; continue
                    st = students.lookup(email) or {}
                    async with conn.transaction():
                        res = await conn.execute(
                            "INSERT INTO hero_accounts (user_id, email, hero_password, bearer_token, student_id, full_name) "
                            "VALUES ($1,$2,$3,'NO_TOKEN',$4,$5) ON CONFLICT (user_id, email) DO NOTHING",
                            uid, email, encrypt_pass(pw), a.get("student_id") or st.get("student_id"), a.get("full_name") or st.get("full_name"))
                    if res.endswith(" 1"): rep["acc_new"] += 1
                    else: rep["acc_skipped"] += 1
                except Exception as e:
                    rep["acc_failed"] += 1
                    logger.warning(f"Import: akkaunt o'tkazib yuborildi ({a.get('email')}): {e}")

            # ---- 3) Arxiv ----
            for a in data.get("archived_accounts") or []:
                try:
                    uid = id_map.get(int(a.get("user_id"))) if a.get("user_id") is not None else None
                    email = (a.get("email") or "").strip().lower()
                    if not uid or not email: rep["arch_skipped"] += 1; continue
                    deleted = _to_dt(a.get("deleted_at")) or datetime.utcnow()
                    async with conn.transaction():
                        exists = await conn.fetchval("SELECT 1 FROM archived_accounts WHERE user_id=$1 AND email=$2 AND deleted_at=$3", uid, email, deleted)
                        if not exists:
                            await conn.execute(
                                "INSERT INTO archived_accounts (user_id, email, hero_password, student_id, full_name, deleted_at) VALUES ($1,$2,$3,$4,$5,$6)",
                                uid, email, encrypt_pass(_import_pw(a.get("hero_password")) or ""), a.get("student_id"), a.get("full_name"), deleted)
                    rep["arch_skipped" if exists else "arch_new"] += 1
                except Exception as e:
                    rep["arch_skipped"] += 1
                    logger.warning(f"Import: arxiv yozuvi o'tkazib yuborildi: {e}")

            # ---- 4) Skaner tarixi ----
            for s in data.get("scan_logs") or []:
                try:
                    uid = id_map.get(int(s.get("user_id"))) if s.get("user_id") is not None else None
                    at = _to_dt(s.get("scanned_at"))
                    if not uid or not at: rep["scans_skipped"] += 1; continue
                    det = s.get("details")
                    async with conn.transaction():
                        exists = await conn.fetchval("SELECT 1 FROM scan_logs WHERE user_id=$1 AND scanned_at=$2", uid, at)
                        if not exists:
                            await conn.execute(
                                "INSERT INTO scan_logs (user_id, success_count, total_count, duration, lesson, details, scanned_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                                uid, int(s.get("success_count") or 0), int(s.get("total_count") or 0), float(s.get("duration") or 0),
                                s.get("lesson"), json.dumps(det) if det is not None else None, at)
                    rep["scans_skipped" if exists else "scans_new"] += 1
                except Exception as e:
                    rep["scans_skipped"] += 1
                    logger.warning(f"Import: skaner yozuvi o'tkazib yuborildi: {e}")

            # ---- 5) Ulashilgan ulanishlar (shadow_targets) — eski id -> yangi id ----
            for u in data["app_users"]:
                try:
                    me = id_map.get(int(u.get("id"))) if u.get("id") is not None else None
                    old_targets = _as_list(u.get("shadow_targets"))
                    if not me or not old_targets: continue
                    mapped = [id_map[int(t)] for t in old_targets if str(t).lstrip("-").isdigit() and int(t) in id_map]
                    if not mapped: continue
                    curr = _as_list(await conn.fetchval("SELECT shadow_targets FROM app_users WHERE id=$1", me))
                    merged = list(dict.fromkeys([int(x) for x in curr] + mapped))
                    if len(merged) != len(curr):
                        await conn.execute("UPDATE app_users SET shadow_targets=$1::jsonb WHERE id=$2", json.dumps(merged), me)
                        rep["shadows"] += len(merged) - len(curr)
                except Exception as e:
                    logger.warning(f"Import: shadow_targets o'tkazib yuborildi: {e}")
    return rep


# ---------------------------------------------------------------------------
# DARSLAR BO'YICHA AVTO-YUBORISH uchun yordamchilar
# ---------------------------------------------------------------------------
async def get_all_schedules():
    """Barcha saqlangan dars jadvallari: [(email, xom_javob)]."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT email, raw FROM hero_schedules WHERE raw IS NOT NULL")
    return [(r["email"], _jsonish(r["raw"])) for r in rows]

async def get_accounts_by_emails(emails, owner_tg_id=0):
    """Berilgan emaillar bo'yicha akkauntlar (parol hal qilingan) + egasining Telegram ID'si.
    Bloklangan yoki obunasi tugagan (admin bo'lmagan) foydalanuvchilarning akkauntlari kirmaydi."""
    emails = list(emails)
    if not emails: return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT a.id, a.user_id, a.email, a.hero_password, a.bearer_token, a.student_id, a.full_name, u.telegram_id "
            "FROM hero_accounts a JOIN app_users u ON u.id = a.user_id "
            "WHERE a.email = ANY($1::text[]) AND u.is_blocked = FALSE "
            "AND (u.is_admin = TRUE OR u.telegram_id = $2 OR u.trial_ends_at > NOW()) ORDER BY a.id DESC",
            emails, int(owner_tg_id or 0))
    out = []
    for r in rows:
        d = dict(r); d["hero_password"] = decrypt_pass(d["hero_password"]); out.append(d)
    return out
