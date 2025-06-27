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

def generate_psk():
    return subprocess.getoutput("wg genpsk")

# Конфигурация
DB = "autowgshop.db"
ADMIN_IDS = [1203425573]
CRYPTO_WALLET = "TNDYy3v4a5b6c7d8e9f0g1h2i3j4k5l6m"
PRICE = 5
WG_INTERFACE = "wg0"
WG_SUBNET = "100.64.0"
SERVER_PUBLIC_KEY = "hRVLkkxJNDpYGiGdmg/YRFOAVPrwJMj9zHZeb1l9aQU="
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
        preshared_key TEXT
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
        (user_id, config_name, octet, now, end, public_key, private_key, preshared_key)
    )
    conn.commit()
    conn.close()
    return octet, end, True

def db_user_configs(user_id):
    today = datetime.date.today().isoformat()  # <-- теперь today строка '2025-06-27'
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT config_name, ip_last_octet, end_date, private_key, preshared_key FROM subs WHERE user_id=? AND end_date>=?",
              (user_id, today))
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_peer_by_public_key(public_key):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id, config_name, ip_last_octet, end_date, private_key, preshared_key FROM subs WHERE public_key=?",
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

def add_peer_to_wg(public_key, ip_octet, preshared_key=None):
    cmd = ["docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
           "peer", public_key, "allowed-ips", f"{WG_SUBNET}.{ip_octet}/32"]
    subprocess.run(cmd, check=True)

def remove_peer_from_wg(public_key):
    cmd = ["docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
           "peer", public_key, "remove"]
    if preshared_key:
        cmd += ["preshared-key", "/dev/stdin"]
        subprocess.run(cmd, input=preshared_key.encode(), check=True)
    else:
        subprocess.run(cmd, check=True)

def generate_client_config(priv, octet, psk=None):
    return f"""[Interface]\nPrivateKey = {priv}\nAddress = {WG_SUBNET}.{octet}/24\nDNS = 1.1.1.1\n\n[Peer]\nPublicKey = {SERVER_PUBLIC_KEY}\nEndpoint = {SERVER_ENDPOINT}\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"""
    if psk:
        config = config.replace("[Peer]", f"[Peer]\nPresharedKey = {psk}")
    return config
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
        # цикл должен идти ТОЛЬКО если конфиги есть!
        for name, octet, end, priv, psk in configs:
            print("ОТЛАДКА: отправляем конфиг для", name)
            conf = generate_client_config(priv, octet, psk)
            print("CONF CONTENT:\n", conf)
            cfile = f"{user.id}_{name}.conf"
            qfile = f"{user.id}_{name}.png"
    
            # Сохраняем конфиг
            with open(cfile, "w", encoding="utf-8") as f_conf:
                f_conf.write(conf)
            print("CONF FILE EXISTS:", os.path.exists(cfile), "SIZE:", os.path.getsize(cfile))
    
            # Генерируем QR
            generate_qr(conf, qfile)
            print("QR FILE EXISTS:", os.path.exists(qfile), "SIZE:", os.path.getsize(qfile))
    
            try:
                # Отправляем .conf
                with open(cfile, "rb") as f:
                    await context.bot.send_document(
                        chat_id=user.id,
                        document=InputFile(f, filename=f"{name}.conf"),
                        caption=f"{name} до {end}"
                    )
                # Отправляем QR-код
                with open(qfile, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=user.id,
                        photo=InputFile(f, filename=f"{name}.png"),
                        caption="QR-код"
                    )
            except Exception as e:
                print("ERROR SENDING FILES:", e)
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
        psk = generate_psk()
        octet, end, _ = db_sub_add(uid, name, pub, priv, psk)
        try:
            add_peer_to_wg(pub, octet, psk)
        except Exception as e:
            return await query.edit_message_text(f"Ошибка WG: {e}")
        conf = generate_client_config(priv, octet, psk)
        cfile = f"{uid}_{name}.conf"
        qfile = f"{uid}_{name}.png"
        with open(cfile, "w") as f: f.write(conf)
        generate_qr(conf, qfile)
        await context.bot.send_document(user.id, InputFile(cfile), caption=f"{name} до {end}")
        await context.bot.send_photo(user.id, InputFile(qfile), caption="QR-код")
        os.remove(cfile)
        os.remove(qfile)
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
                    _, _, octet, _, _, psk = db_get_peer_by_public_key(pub)
                    add_peer_to_wg(pub, octet, psk)
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
