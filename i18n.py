"""Uch tilli matnlar (uz / en / ru). t(lang, "kalit", **qiymatlar) — topilmasa o'zbekchasi qaytadi."""

LANGS = ("uz", "en", "ru")
LANG_NAMES = {"uz": "🇺🇿 O'zbekcha", "en": "🇬🇧 English", "ru": "🇷🇺 Русский"}


def pick(code):
    """Telegram language_code -> bizning til."""
    c = (code or "").lower()[:2]
    return c if c in LANGS else "uz"


S = {
    # --- skaner natijasi
    "scan_ok": {"uz": "✅ Skanerdan o'tdi!", "en": "✅ Scan complete!", "ru": "✅ Сканирование выполнено!"},
    "lesson": {"uz": "📚 Dars: {t}", "en": "📚 Lesson: {t}", "ru": "📚 Занятие: {t}"},
    "passed": {"uz": "🦸 Darsga kirganlar ({n}):", "en": "🦸 Checked in ({n}):", "ru": "🦸 Отмечены на занятии ({n}):"},
    "failed": {"uz": "❌ Kirmadi ({n}):", "en": "❌ Not checked in ({n}):", "ru": "❌ Не отмечены ({n}):"},
    "tz": {"uz": "(Toshkent)", "en": "(Tashkent)", "ru": "(Ташкент)"},
    "auto_in": {"uz": "✅ Boshqa foydalanuvchi QR skaner qilgani uchun akkauntingiz darsga avtomatik kirdi.",
                "en": "✅ Another user scanned the QR, so your account was checked in automatically.",
                "ru": "✅ Другой пользователь отсканировал QR, поэтому ваш аккаунт отмечен автоматически."},
    "scan_none": {"uz": "❌ Hech bir akkaunt darsga kirmadi", "en": "❌ No account was checked in", "ru": "❌ Ни один аккаунт не отмечен"},
    "why_not_in": {"uz": "ℹ️ {n} ta akkauntning tokeni ishlayapti, lekin bu dars ularning jadvalida yo'q — shuning uchun kirmadi.",
                   "en": "ℹ️ {n} account(s) have a working token, but this lesson is not in their schedule — that's why they weren't checked in.",
                   "ru": "ℹ️ У {n} аккаунт(ов) токен работает, но этого занятия нет в их расписании — поэтому не отмечены."},
    "why_all_not_in": {"uz": "ℹ️ Barcha akkauntlarning tokeni ishlayapti, lekin Hero hech birini qabul qilmadi — QR eskirgan yoki bu dars ularning jadvalida yo'q. Yangi QR bilan urinib ko'ring.",
                       "en": "ℹ️ All tokens are working, but Hero accepted none of them — the QR may be expired or this lesson is not in their schedules. Try a fresh QR.",
                       "ru": "ℹ️ Все токены работают, но Hero не принял ни один — QR мог устареть или этого занятия нет в их расписании. Попробуйте свежий QR."},
    "why_qr_invalid": {"uz": "⚠️ QR kod eskirgan bo'lishi mumkin — yangisini skaner qiling.",
                       "en": "⚠️ The QR code may have expired — scan a fresh one.",
                       "ru": "⚠️ QR-код мог устареть — отсканируйте новый."},
    "why_token": {"uz": "⚠️ {n} ta akkauntning tokeni ishlamayapti — mini-ilovada akkauntni yangilang yoki parolni qayta kiriting.",
                  "en": "⚠️ The token of {n} account(s) is not working — refresh the account in the mini-app or re-enter the password.",
                  "ru": "⚠️ Токен {n} аккаунт(ов) не работает — обновите аккаунт в мини-приложении или введите пароль заново."},
    "why_hero": {"uz": "⏳ {n} ta akkaunt uchun Hero javob bermadi (server/tarmoq) — birozdan keyin qayta urinib ko'ring.",
                 "en": "⏳ Hero did not respond for {n} account(s) (server/network) — try again in a moment.",
                 "ru": "⏳ Hero не ответил для {n} аккаунт(ов) (сервер/сеть) — повторите попытку чуть позже."},
    "acc_removed": {"uz": "🗑 <b>{name}</b> akkaunti admin tomonidan o'chirildi (yaroqsiz yoki nofaol edi). Kerak bo'lsa mini-ilovada qayta qo'shing.",
                    "en": "🗑 The account <b>{name}</b> was removed by an admin (it was invalid or inactive). Re-add it in the mini-app if needed.",
                    "ru": "🗑 Аккаунт <b>{name}</b> удалён администратором (он был недействителен или неактивен). При необходимости добавьте его снова в мини-приложении."},
    "vote_thanks": {"uz": "Rahmat! 🙌", "en": "Thanks! 🙌", "ru": "Спасибо! 🙌"},
    "joke_daily": {"uz": "🌟 <b>Kunning hazili</b>", "en": "🌟 <b>Joke of the day</b>", "ru": "🌟 <b>Шутка дня</b>"},
    # --- eslatmalar
    "remind": {"uz": "⏰ <b>{title}</b> — {start} da boshlanadi (≈{mins} daq.){place}\n{extra}",
               "en": "⏰ <b>{title}</b> starts at {start} (in ≈{mins} min){place}\n{extra}",
               "ru": "⏰ <b>{title}</b> начнётся в {start} (через ≈{mins} мин){place}\n{extra}"},
    "remind_auto": {"uz": "ℹ️ Darsdoshlaringizdan biri QR skaner qilsa, akkauntingiz avtomatik kiritiladi.",
                    "en": "ℹ️ If a classmate scans the QR, your account is checked in automatically.",
                    "ru": "ℹ️ Если кто-то из одногруппников отсканирует QR, ваш аккаунт отметится автоматически."},
    "remind_manual": {"uz": "📷 QR ochilgach, botda skanerlashni unutmang.",
                      "en": "📷 Don't forget to scan the QR once it opens.",
                      "ru": "📷 Не забудьте отсканировать QR, когда он откроется."},
    "room": {"uz": " · 🚪 {r}", "en": " · 🚪 {r}", "ru": " · 🚪 {r}"},
    # --- baholar
    "grade_head": {"uz": "🎓 <b>Yangi baho!</b> — {name}", "en": "🎓 <b>New grade!</b> — {name}", "ru": "🎓 <b>Новая оценка!</b> — {name}"},
    "grade_new": {"uz": "📘 {title} — <b>{g}</b>", "en": "📘 {title} — <b>{g}</b>", "ru": "📘 {title} — <b>{g}</b>"},
    "grade_chg": {"uz": "📘 {title}: {old} → <b>{g}</b>", "en": "📘 {title}: {old} → <b>{g}</b>", "ru": "📘 {title}: {old} → <b>{g}</b>"},
    "gpa_chg": {"uz": "📈 GPA: {old} → <b>{new}</b>", "en": "📈 GPA: {old} → <b>{new}</b>", "ru": "📈 GPA: {old} → <b>{new}</b>"},
    # --- salomatlik
    "health_user": {"uz": "⚠️ <b>{name}</b> akkauntida muammo bor.\n{reason}\n🔧 Mini-ilovada akkauntni o'chirib qayta qo'shing yoki parolni yangilang.",
                    "en": "⚠️ There is a problem with the account <b>{name}</b>.\n{reason}\n🔧 In the mini-app, remove and re-add the account or update the password.",
                    "ru": "⚠️ Проблема с аккаунтом <b>{name}</b>.\n{reason}\n🔧 В мини-приложении удалите и добавьте аккаунт заново или обновите пароль."},
    "r_login": {"uz": "Login yoki parol noto'g'ri (Hero kirishni rad etdi).", "en": "Wrong login or password (Hero rejected the sign-in).",
                "ru": "Неверный логин или пароль (Hero отклонил вход)."},
    "r_notoken": {"uz": "Token yo'q — akkaunt nofaol.", "en": "No token — the account is inactive.", "ru": "Нет токена — аккаунт неактивен."},
    "r_failing": {"uz": "So'nggi {n} ta skanerda ketma-ket kirmadi ({msg}).", "en": "Failed {n} scans in a row ({msg}).",
                  "ru": "Не удалось отметиться {n} раз подряд ({msg})."},
    # --- davomat
    "att_title": {"uz": "📊 <b>Davomat</b> — {name}", "en": "📊 <b>Attendance</b> — {name}", "ru": "📊 <b>Посещаемость</b> — {name}"},
    "att_overall": {"uz": "Umumiy: <b>{a}/{t}</b> ({p}%)\n{bar}", "en": "Overall: <b>{a}/{t}</b> ({p}%)\n{bar}", "ru": "Всего: <b>{a}/{t}</b> ({p}%)\n{bar}"},
    "att_period": {"uz": "Oxirgi 7 kun: {a7}/{t7} · 30 kun: {a30}/{t30}", "en": "Last 7 days: {a7}/{t7} · 30 days: {a30}/{t30}",
                   "ru": "Последние 7 дней: {a7}/{t7} · 30 дней: {a30}/{t30}"},
    "att_streak": {"uz": "🔥 Ketma-ket kirgan: {n} dars", "en": "🔥 Current streak: {n} lessons", "ru": "🔥 Подряд без пропусков: {n}"},
    "att_subjects": {"uz": "Fanlar bo'yicha:", "en": "By subject:", "ru": "По предметам:"},
    "att_missed": {"uz": "Qoldirilganlar:", "en": "Missed:", "ru": "Пропущено:"},
    "att_note": {"uz": "ℹ️ Faqat shu bot orqali skaner qilingan darslar hisobga olinadi.",
                 "en": "ℹ️ Only lessons scanned through this bot are counted.", "ru": "ℹ️ Учитываются только занятия, отсканированные через этого бота."},
    "att_none": {"uz": "Jadvalda o'tgan darslar topilmadi. Mini-ilovada akkauntni ulang va jadval yangilanishini kuting.",
                 "en": "No past lessons found in your schedule. Connect your account in the mini-app and wait for the schedule to sync.",
                 "ru": "В расписании нет прошедших занятий. Подключите аккаунт в мини-приложении и дождитесь синхронизации."},
    "no_acc": {"uz": "Sizda Hero akkaunt yo'q. Avval mini-ilovada akkaunt qo'shing.", "en": "You have no Hero account yet. Add one in the mini-app first.",
               "ru": "У вас нет аккаунта Hero. Сначала добавьте его в мини-приложении."},
    "start_first": {"uz": "Avval /start yuboring.", "en": "Send /start first.", "ru": "Сначала отправьте /start."},
    # --- haftalik
    "wk_head": {"uz": "📅 <b>Haftalik xulosa</b> — {name}", "en": "📅 <b>Weekly summary</b> — {name}", "ru": "📅 <b>Итоги недели</b> — {name}"},
    "wk_line": {"uz": "✅ Bu hafta: <b>{a}/{t}</b> dars ({p}%)\n{bar}", "en": "✅ This week: <b>{a}/{t}</b> lessons ({p}%)\n{bar}",
                "ru": "✅ За неделю: <b>{a}/{t}</b> занятий ({p}%)\n{bar}"},
    "wk_best": {"uz": "🌟 Zo'r! Bu hafta bitta dars ham qoldirmadingiz.", "en": "🌟 Great! You didn't miss a single lesson this week.",
                "ru": "🌟 Отлично! На этой неделе ни одного пропуска."},
    "wk_worst": {"uz": "⚠️ Eng ko'p qoldirilgan: {title}", "en": "⚠️ Most missed: {title}", "ru": "⚠️ Чаще всего пропущено: {title}"},
    # --- sozlamalar
    "set_title": {"uz": "⚙️ <b>Bildirishnomalar</b>\n\nKerakmaganini o'chirib qo'ying:", "en": "⚙️ <b>Notifications</b>\n\nTurn off what you don't need:",
                  "ru": "⚙️ <b>Уведомления</b>\n\nОтключите ненужное:"},
    "set_remind": {"uz": "⏰ Dars eslatmasi", "en": "⏰ Lesson reminders", "ru": "⏰ Напоминания о занятиях"},
    "set_weekly": {"uz": "📅 Haftalik xulosa", "en": "📅 Weekly summary", "ru": "📅 Итоги недели"},
    "set_grades": {"uz": "🎓 Yangi baho xabari", "en": "🎓 New grade alerts", "ru": "🎓 Новые оценки"},
    "set_lang": {"uz": "🌐 Til: {l}", "en": "🌐 Language: {l}", "ru": "🌐 Язык: {l}"},
    "lang_choose": {"uz": "🌐 Tilni tanlang:", "en": "🌐 Choose your language:", "ru": "🌐 Выберите язык:"},
    "lang_set": {"uz": "✅ Til o'zgartirildi.", "en": "✅ Language changed.", "ru": "✅ Язык изменён."},
}


def t(lang, key, **kw):
    d = S.get(key)
    if not d:
        return key
    s = d.get(lang) or d["uz"]
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError):
        return s
