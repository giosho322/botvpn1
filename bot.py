import os
import sqlite3
import datetime
import logging
import subprocess
import qrcode
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from threading import Thread
import time

# Конфигурация
DB = "autowgshop.db"
ADMIN_IDS = [1203425573]
CRYPTO_WALLET = "Ожидайте я скоро приму вас"
PRICE = 5
WG_INTERFACE = "wg0"
WG_SUBNET = "10.8.0"
SERVER_PUBLIC_KEY = "7hdEHuQ+4iW8PRDWmi19IHInhOFL/Y7cHPNEwcvHMSA="
SERVER_ENDPOINT = "80.74.28.21:51820"

logging.basicConfig(level=logging.INFO)

# Инициализация базы
def db_init():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        config_name TEXT,
        ip_last_octet INTEGER,
        start_date DATE,
        end_date DATE,
        public_key TEXT,
        private_key TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        wallet TEXT,
        amount REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT,
        config_name TEXT
    )''')
    conn.commit()
    conn.close()

# Функции работы с БД
def db_user_add(user_id, username, is_admin=False):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, username, is_admin) VALUES (?, ?, ?)",
              (user_id, username, int(is_admin)))
    conn.commit()
    conn.close()

def db_payment_add(user_id, wallet, amount):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, wallet, amount, status) VALUES (?, ?, ?, 'pending')",
              (user_id, wallet, amount))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid

def db_payment_set_status(payment_id, status, config_name=None):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if config_name:
        c.execute("UPDATE payments SET status=?, config_name=? WHERE id=?",
                  (status, config_name, payment_id))
    else:
        c.execute("UPDATE payments SET status=? WHERE id=?", (status, payment_id))
    conn.commit()
    conn.close()

def db_get_pending_payments():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, user_id, amount FROM payments WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_payment(payment_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id, amount FROM payments WHERE id=?", (payment_id,))
    row = c.fetchone()
    conn.close()
    return row

def db_sub_add(user_id, config_name, public_key, private_key, days=30):
    now = datetime.date.today()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, ip_last_octet, end_date FROM subs WHERE user_id=? AND config_name=?",
              (user_id, config_name))
    row = c.fetchone()
    if row:
        sid, octet, prev = row
        prev_date = datetime.datetime.strptime(prev, "%Y-%m-%d").date() if prev else now
        start = max(now, prev_date)
        end = start + datetime.timedelta(days=days)
        c.execute("UPDATE subs SET end_date=? WHERE id=?", (end, sid))
        conn.commit()
        conn.close()
        return octet, end, False
    c.execute("SELECT MAX(ip_last_octet) FROM subs")
    last = c.fetchone()[0] or 1
    octet = last + 1
    end = now + datetime.timedelta(days=int(days))
    c.execute(
        "INSERT INTO subs (user_id, config_name, ip_last_octet, start_date, end_date, public_key, private_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, config_name, octet, now, end, public_key, private_key)
    )
    conn.commit()
    conn.close()
    return octet, end, True

def db_user_configs(user_id):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT config_name, ip_last_octet, end_date, private_key FROM subs WHERE user_id=? AND end_date>=?",
              (user_id, today))
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_peer_by_public_key(public_key):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id, config_name, ip_last_octet, end_date, private_key FROM subs WHERE public_key=?",
              (public_key,))
    row = c.fetchone()
    conn.close()
    return row

def db_get_expired_peers():
    today = datetime.date.today()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT public_key FROM subs WHERE end_date < ?", (today,))
    keys = [r[0] for r in c.fetchall()]
    conn.close()
    return keys

def db_get_active_peers():
    today = datetime.date.today()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT public_key FROM subs WHERE end_date >= ?", (today,))
    keys = [r[0] for r in c.fetchall()]
    conn.close()
    return keys

def generate_keys():
    priv = subprocess.getoutput("wg genkey")
    pub = subprocess.getoutput(f"echo '{priv}' | wg pubkey")
    return priv, pub

def add_peer_to_wg(public_key, ip_octet,):
    cmd = ["docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
           "peer", public_key, "allowed-ips", f"{WG_SUBNET}.{ip_octet}/32"]
    subprocess.run(cmd, check=True)

def remove_peer_from_wg(public_key):
    cmd = ["docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
           "peer", public_key, "remove"]
    subprocess.run(cmd, check=True)

def generate_client_config(private_key, ip_octet,):
    return f"""[Interface]
PrivateKey = {private_key}
Address = {WG_SUBNET}.{ip_octet}/32
DNS = 1.1.1.1

[Peer]
PublicKey = {SERVER_PUBLIC_KEY}
Endpoint = {SERVER_ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

def generate_qr(text, path):
    qrcode.make(text).save(path)

def get_main_keyboard(user_id):
    kb = [[KeyboardButton("🛒 Купить подписку"), KeyboardButton("📂 Мои конфиги")],
          [KeyboardButton("📋 Инструкция"), KeyboardButton("💬 Поддержка")]]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton("⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_add(user.id, user.username)
    await update.message.reply_text(
        "👋 Добро пожаловать в VPN Shop!\nВыберите действие:",
        reply_markup=get_main_keyboard(user.id)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    db_user_add(user.id, user.username)

    if text == "🛒 Купить подписку":
        pid = db_payment_add(user.id, CRYPTO_WALLET, PRICE)
        for admin in ADMIN_IDS:
            await context.bot.send_message(
                admin,
                f"Заявка #{pid} от @{user.username} ({user.id}) на {PRICE} USDT",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Принять и выдать конфиг", callback_data=f"approve_{pid}")]]
                )
            )
        await update.message.reply_text(
            f"Переведи <b>{PRICE} USDT</b> на адрес:\n<code>{CRYPTO_WALLET}</code>\nПосле оплаты жди подтверждения.",
            parse_mode="HTML"
        )

    elif text == "📂 Мои конфиги":
        configs = db_user_configs(user.id)
        if not configs:
            return await update.message.reply_text("У вас нет активных конфигов.", reply_markup=get_main_keyboard(user.id))
        
        for name, octet, end, priv, in configs:
            conf = generate_client_config(priv, octet)
            cfile = f"{user.id}_{name}.conf"
            qfile = f"{user.id}_{name}.png"
    
            with open(cfile, "w", encoding="utf-8") as f_conf:
                f_conf.write(conf)
    
            generate_qr(conf, qfile)
    
            try:
                with open(cfile, "rb") as f:
                    await context.bot.send_document(
                        chat_id=user.id,
                        document=InputFile(f, filename=f"{name}.conf"),
                        caption=f"{name} до {end}"
                    )
                with open(qfile, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=user.id,
                        photo=InputFile(f, filename=f"{name}.png"),
                        caption="QR-код"
                    )
            except Exception as e:
                await update.message.reply_text(f"Ошибка отправки файла: {e}")
            finally:
                if os.path.exists(cfile): os.remove(cfile)
                if os.path.exists(qfile): os.remove(qfile)

    elif text == "📋 Инструкция":
        await update.message.reply_text(
            f"📋 Инструкция по настройке VPN:\n\n"
            "1️⃣ Установка WireGuard:\n"
            "• На ПК — скачайте клиент: https://www.wireguard.com/install/\n"
            "• На смартфоне — установите приложение WireGuard из магазина приложений.\n\n"
            "2️⃣ Получение конфига:\n"
            "• Нажмите «📂 Мои конфиги» и скачайте .conf или отсканируйте QR-код.\n\n"
            "3️⃣ Импорт конфига:\n"
            "• В клиенте WireGuard выберите «Import from file» или «+ → Create from QR code».\n\n"
            "4️⃣ Подключение:\n"
            "• Активируйте туннель и дождитесь статуса Active.\n\n"
            "5️⃣ Проверка IP:\n"
            "• Зайдите на https://ipleak.net/ — ваш IP должен начинаться с 10.\n\n"
            "Если есть вопросы — пиши в поддержку: @Youpulo"
        )

    elif text == "💬 Поддержка":
        await update.message.reply_text(
            f"💬 Поддержка:\n\n"
            f"Если у тебя возникли проблемы с оплатой, подключением или просто не знаешь, с чего начать — не стесняйся, напиши нам.\n\n"
            f"👨‍💻 Контакты: @Youpulo\n\n"
            f"Отвечаем быстро, даже ночью. Только не тупи, сразу пиши суть проблемы и свой ID: <code>{user.id}</code>",
            parse_mode="HTML"
        )

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if user.id not in ADMIN_IDS:
        return await query.answer("Нет доступа")
    data = query.data
    if data == "admin_stats":
        ids = db_users_stat()
        return await query.edit_message_text(f"Всего пользователей: {len(ids)}; ID: {', '.join(map(str, ids))}")
    if data == "admin_requests":
        rows = db_get_pending_payments()
        if not rows:
            return await query.edit_message_text("Заявок нет")
        text = "".join(f"ID {pid} от {uid}: {amt} USDT\n" for pid, uid, amt in rows)
        return await query.edit_message_text(text)
    if data.startswith("approve_"):
        pid = int(data.split("_")[1])
        pay = db_get_payment(pid)
        if not pay:
            return await query.answer("Не найдена заявку")
        uid, _ = pay
        name = f"sub_{pid}"
        priv, pub = generate_keys()
        octet, end, _ = db_sub_add(
            user_id=uid,
            config_name=name,
            public_key=pub,
            private_key=priv,
            days=30
        )
        try:
            add_peer_to_wg(pub, octet)
            subprocess.run(["docker", "exec", "wg-easy", "wg-quick", "save", "wg0"], check=True)
        except Exception as e:
            logging.error(f"Ошибка обновления сервера: {str(e)}")
            await query.edit_message_text(f"Ошибка на сервере: {str(e)}")
            return
        
        try:
            conf = generate_client_config(priv, octet)
            cfile = f"{user.id}_{name}.conf"
            
            with open(cfile, "w", encoding='utf-8') as f:
                f.write(conf)
            
            with open(cfile, "rb") as f:
                await context.bot.send_document(
                    chat_id=uid,
                    document=InputFile(f, filename=f"wg_{name}.conf"),
                    caption=f"Конфиг до {end}"
                )
            
            qfile = f"{user.id}_{name}.png"
            generate_qr(conf, qfile)
            
            if os.path.exists(qfile):
                with open(qfile, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=uid,
                        photo=InputFile(f, filename=f"wg_{name}.png"),
                        caption="QR-код для импорта"
                    )
            
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")
            await context.bot.send_message(
                chat_id=user.id,
                text=f"Произошла ошибка: {str(e)}"
            )
        finally:
            for f in [cfile, qfile]:
                if os.path.exists(f):
                    os.remove(f)
        
        db_payment_set_status(pid, "confirmed", name)
        return await query.edit_message_text(f"Выполнено: конфиг выдан ID {pid}")

def db_users_stat():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id FROM users")
    ids = [r[0] for r in c.fetchall()]
    conn.close()
    return ids

def peer_watcher():
    while True:
        for pub in db_get_expired_peers():
            try:
                remove_peer_from_wg(pub)
            except:
                pass
        active = db_get_active_peers()
        peers = subprocess.getoutput(f"docker exec wg-easy wg show {WG_INTERFACE} peers")
        for pub in active:
            if pub not in peers:
                try:
                    peer_data = db_get_peer_by_public_key(pub)
                    if peer_data:
                        _, _, octet, _, _,
                        add_peer_to_wg(pub, octet)
                except:
                    pass
        time.sleep(1800)

if __name__ == "__main__":
    db_init()
    Thread(target=peer_watcher, daemon=True).start()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(CallbackQueryHandler(admin_callbacks))
    app.run_polling()
