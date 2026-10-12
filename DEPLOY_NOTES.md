# Herobot — ishga tushirish (muhim o'zgaruvchilar)

| O'zgaruvchi | Majburiy | Izoh |
|---|---|---|
| `BOT_TOKEN` | ha | Telegram bot tokeni |
| `ADMIN_ID` | ha | Bosh adminning Telegram ID'si |
| `WEBAPP_URL` | ha | Mini-ilova manzili (https://...) |
| `DATABASE_URL` | ha | PostgreSQL ulanishi |
| `ENCRYPTION_KEY` | **ha** | Hero parollarini shifrlash kaliti. Bo'lmasa bot ISHGA TUSHMAYDI. Bir marta yarating va **saqlab qo'ying** — yo'qolsa parollar tiklanmaydi: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET` | tavsiya | Bo'lmasa har restartda hamma qayta kirishi kerak bo'ladi |
| `BOT_USERNAME` | yo'q | Landing sahifasi uchun |
| `BACKUP_HOUR` | yo'q | Kunlik zaxira soati (Toshkent, standart 3) |
| `ALLOW_EPHEMERAL_KEY=1` | faqat sinov | Kalitsiz vaqtincha ishga tushirish (parollar restartda yo'qoladi) |

Eslatmalar:
- Zaxira JSON'dagi Hero parollari shifrlangan; tiklash uchun xuddi shu `ENCRYPTION_KEY` kerak. Excel'da parollar yashirilgan.
- Sinov bot va asosiy bot bir bazadan foydalansa, foydalanuvchilar sinov botga ham `/start` yuborishi kerak (Telegram qoidasi), aks holda suhbat xabari yetmaydi.
- Bazaga yangi ustun/indekslar ishga tushganda o'zi qo'shiladi.
