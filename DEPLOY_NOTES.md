# Herobot — ishga tushirish (muhim o'zgaruvchilar)

| O'zgaruvchi | Majburiy | Izoh |
|---|---|---|
| `BOT_TOKEN` | ha | Telegram bot tokeni |
| `ADMIN_ID` | ha | Bosh adminning Telegram ID'si |
| `WEBAPP_URL` | ha | Mini-ilova manzili (https://...) |
| `DATABASE_URL` | ha | PostgreSQL ulanishi |
| `ENCRYPTION_KEY` | **ha** | Hero parollarini shifrlash kaliti. Bo'lmasa bot ISHGA TUSHMAYDI. Bir marta yarating va **saqlab qo'ying** — yo'qolsa parollar tiklanmaydi: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET` | **ha (amalda)** | Bo'lmasa har restartda hamma qayta kirishi kerak bo'ladi. Yaratish: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `WEBHOOK_SECRET` | tavsiya | Webhook yo'li `/webhook/{WEBHOOK_SECRET}`. Bo'lmasa tokendan hash olinadi. Telegram'ga `secret_token` sarlavhasi ham o'rnatiladi |
| `BOT_USERNAME` | yo'q | Landing sahifasi uchun |
| `BACKUP_HOUR` | yo'q | Kunlik zaxira soati (Toshkent, standart 3) |
| `ALLOW_EPHEMERAL_KEY=1` | faqat sinov | Kalitsiz vaqtincha ishga tushirish (parollar restartda yo'qoladi) |

Eslatmalar:
- Zaxira JSON'dagi Hero parollari shifrlangan; tiklash uchun xuddi shu `ENCRYPTION_KEY` kerak. Excel'da parollar yashirilgan.
- Sinov bot va asosiy bot bir bazadan foydalansa, foydalanuvchilar sinov botga ham `/start` yuborishi kerak (Telegram qoidasi), aks holda suhbat xabari yetmaydi.
- Bazaga yangi ustun/indekslar ishga tushganda o'zi qo'shiladi.

Deploy tartibi:
1. BotFather'da `/revoke` → yangi tokenni Railway'da `BOT_TOKEN` ga qo'ying (eski token loglarda qolgan).
2. `JWT_SECRET`, `ENCRYPTION_KEY` (eskisi o'zgarmasin!), `WEBHOOK_SECRET` ni tekshiring.
3. Railway'da **faqat 1 replika** bo'lsin: kunlik zaxira, token-healer va jadval yangilash fon vazifalari bitta nusxada ishlashi kerak.
4. Deploy'dan keyin logda `/webhook/***` ko'rinishi va `getWebhookInfo` da xato yo'qligini tekshiring.

## Yangi o'zgarishlar (fixed2)

**Avtomatik yaratiladigan baza o'zgarishlari** (qo'lda hech narsa qilish shart emas):
- Yangi jadval `account_activity` (+ 2 indeks) — har skanerdagi har bir akkaunt natijasi. Birinchi ishga tushishda jadval bo'sh bo'lsa, `scan_logs` dan so'nggi 180 kun avtomatik to'ldiriladi (bir martalik). 365 kundan eskilari kuniga bir marta o'chiriladi.
- Yangi ustun `app_users.pw_changed_at` (parol almashgan vaqt).

**Admin panel**
- Yangi bo'lim «📒 Akkaunt tarixi» (ism/ID/email bo'yicha qidiruv; qaysi darsga, qachon, qanday yo'l bilan kirgani). `/api/admin/acct_detail` o'qilishi admin jurnaliga yoziladi.
- «Sharpa» (admin tomoni) olib tashlandi — uning o'rniga «Hamrohlar». Foydalanuvchilar orasidagi «Ulashish» ishlashda davom etadi.
- Bir martalik tozalash: ishga tushganda adminlarning `shadow_targets` ro'yxatidan faqat haqiqiy ulashilganlar (`share_codes` orqali) qoladi, qolgani olib tashlanadi; sonini logda ko'rasiz: «🧹 Admin sharpa tozalash: N ta yozuv olib tashlandi». Oddiy foydalanuvchilarga tegilmaydi.
  Eslatma: zaxiradan import qilinganda `share_codes` tiklanmaydi — import qilingan adminning ulashilgan ulanishlari keyingi restartda tozalanishi mumkin.
- «+30 kun» tugmasi: tasdiq so'raydi, cheksiz (NULL) muddatga tegmaydi, javobni tekshiradi, katakni o'sha joyda yangilaydi, foydalanuvchiga Telegram xabari yuboradi (bloklanganga yubormaydi).

**/reset yangi xatti-harakati**
- Darrov almashtirmaydi: «Yangi parol berilsinmi?» + [✅ Ha, yangi parol] [❌ Bekor]. Tasdiq 5 daqiqada eskiradi.
- Cheklov: 60 soniyada 1 marta, soatiga 5 martagacha (xotirada — restartda nolga qaytadi).
- Bloklangan foydalanuvchi parol ola olmaydi.
- Parol almashgach eski veb-sessiyalar (JWT) ishlamay qoladi. **Deploydan oldin berilgan tokenlarda `iat` yo'q**, shuning uchun ular faqat o'sha foydalanuvchi keyinchalik `/reset` qilgandagina yopiladi; boshqalar uzilmaydi.
- Obuna muddati hamon faqat loginda tekshiriladi (JWT 30 kun yashaydi) — o'zgartirilmagan.

**Skaner (0/N)**
- Hero «QR Code not found» desa — yangi sabab `qr_invalid` («QR kod topilmadi yoki eskirgan»), qayta urinishsiz. Akkaunt salomatligiga ta'sir qilmaydi (`not_in_lesson` kabi). Loglarda email `e***@domen` ko'rinishida.
- 0/N bo'lsa ham dars nomi akkauntlar jadvalidan olinadi (topilsa).

**Testlar**: `pip install -r requirements-dev.txt`, so'ng
`ALLOW_EPHEMERAL_KEY=1 BOT_TOKEN=123:abc DATABASE_URL=postgres://x ADMIN_ID=1 python3 -m pytest -q`

## fixed3

**Admin panel → «👥 Userlar»**
- Har bir userning ostida u ASOSIY (⭐) qilib belgilagan Gmail ko'rinadi (Telegram @username bilan birga; username bo'lmasa telefon/ism).
- Yangi tugma «⭐ Asosiy Gmail belgilaganlar (N)» — faqat asosiy akkaunt belgilagan userlarni qoldiradi; qayta bossangiz hammasi qaytadi.
- User ustiga bosilganda ochiladigan akkauntlar ro'yxatida asosiysi ⭐ bilan belgilanadi.
- Baza o'zgarmaydi (faqat `get_super_admin_data` so'rovi `username`, `primary_email`, `is_primary` ni ham qaytaradi).

## fixed5 — Dark mode (Claude ilovasi ranglari)

**Mavzu (light / dark)** — mini-ilova (`scanner.html`) va landing (`index.html`).
- Tugma: login ekranida va boshqaruv panelida (til tugmalari yonida), landing'da navbar'da. Tanlov brauzerda (`localStorage` → `h_theme`) saqlanadi, ikkala sahifa uchun umumiy.
- Tanlanmagan bo'lsa: Telegram ichida — Telegram mavzusi, brauzerda — tizim mavzusi. Telegram yoki tizim mavzusi o'zgarsa, o'zi tanlamagan foydalanuvchida avtomatik almashadi.
- Telegram sarlavha/fon rangi va brauzer `theme-color` ham mavzuga qarab almashadi. Sahifa yuklanishida oq miltillash yo'q.
- Yorug' rejim avvalgidek (asl nusxa bilan piksel-piksel solishtirilgan; farq faqat yangi tugma).

**Dark ranglari — Claude ilovasidan**
- Fon `#151515`, karta/kirish maydoni sirti `#20201e` — Claude ilovasi skrinshotidan o'lchangan qiymatlar.
- Matn `#faf9f5`, ikkinchi darajali `#b0aea5`, aksent `#d97757` (terrakota), asosiy tugma `#c6613f` — Anthropic brend ranglari.
- Aksent faqat asosiy amallar (kirish, skaner, zaxira), havola va sarlavha urg'usida; tanlangan tab/chip — jim kulrang to'ldirish.
- Yashil/qizil/sariq holat ranglari ham shu palitradan (Anthropic Green, Kraft).
- Harakat: Emil Kowalski qoidalari — ease-out `cubic-bezier(.23,1,.32,1)`, bosilganda `scale(.97)`, modal `scale(.95)→1` (200 ms), quyi panel `cubic-bezier(.32,.72,0,1)`; 300 ms dan oshmaydi; hover faqat sichqonchali qurilmada; `prefers-reduced-motion` da siljish o'rniga so'nish.
- Tugma ikonkalari: Lucide (sun / moon).

**Texnik**: qattiq yozilgan ranglar CSS o'zgaruvchilariga o'tkazildi (`--card`, `--pop`, `--deep`, `--accent-solid`, `--*-rgb` ...). Dark palitrani o'zgartirish uchun `:root[data-theme="dark"]` bloki yetarli. PDF sahifa ko'rinishi (transcript) ataylab oq. Baza va server kodi o'zgarmagan.

## fixed6 — QR URL'ni saqlash (tahlil uchun)
- `scan_logs.qr_url` ustuni (avto-migratsiya), har skanerda to'liq QR URL saqlanadi (asosiy skaner va avto/hamroh qo'shimcha skanerlar).
- Railway logida: `🔎 QR URL (user N): ...`
- Admin panel → Skaner tarixi → qatorni bosing → "QR URL" ko'rinadi.

## Yangi o'zgarishlar (fixed7) — Pullik rejim bo'limi
- Admin panelda yangi «💎 Pullik rejim» bo'limi: qolgan kun, «Free trial tugadi» belgisi (qizil qator), 7 kunda tugaydiganlar (sariq), qidiruv (ism/@username/ID/Gmail), filtrlar (Hammasi/Faol/Tugagan/Cheksiz).
- Muddat qo'shish: +7 / +30 / +90 yoki o'zingiz kiritgan kun (1–365). `/api/admin/extend` endi `days` parametrini qabul qiladi (bo'lmasa 30).
- «Userlar» bo'limidan Obuna va +30 kun ustunlari olib tashlandi (tushunarliroq bo'lishi uchun).
- Baza o'zgarmaydi, qo'lda hech narsa qilish shart emas.

## fixed8 — Skaner ma'lumoti ustuni
- «Tarix» bo'limida Dars/Vaqt o'rniga bitta ustun: fan + to'liq sana va vaqt (yil, soniyagacha, Toshkent). Oxirgi 300 ta skaner ko'rinadi (avval 50).
- «Userlar» bo'limida yangi «Oxirgi skaner» ustuni (fan va vaqt).
- Baza o'zgarmaydi: ma'lumot avvaldan `scan_logs` da saqlanib kelgan.

## fixed9 — 🔳 QR kodlar bo'limi
- Yangi jadval `qr_captures` (avtomatik yaratiladi): har skanerda QR ichidagi to'liq matn, host/yo'l/parametrlar, fan, natija va Hero javobining boshi saqlanadi. Begona (hero.study bo'lmagan) QR ham saqlanadi, lekin skanerlanmaydi. Eski skanerlar uchun faqat `scan_logs.qr_url` bor, ular bu jadvalga o'tmaydi.
- Admin panelda «🔳 QR kodlar»: qidiruv, qatorni bossangiz to'liq tafsilot.
- «Hammasini yuklab olish» — CSV (Excel uchun) + JSON fayllari Telegram chatingizga yuboriladi (Mini App ichida oddiy yuklash ishlamagani uchun, zaxira kabi).
