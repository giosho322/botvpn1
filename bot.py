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
    Application, ContextTypes, MessageHandler, CallbackQueryHandler, filters
)
from threading import Thread
import time

# Конфигурация
DB = "autowgshop.db"
ADMIN_IDS = [1203425573]  # <-- твой Telegram ID
CRYPTO_WALLET = "TNDYy3v4a5b6c7d8e9f0g1h2i3j4k5l6m"  # Твой TRON-адрес
PRICE = 5  # USDT
WG_CONF = "/etc/wireguard/wg0.conf"
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
    payment_id = c.lastrowid
    conn.commit()
    conn.close()
    return payment_id

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
        sub_id, ip_octet, prev_end_date = row
        prev_end = datetime.datetime.strptime(prev_end_date, "%Y-%m-%d").date() if prev_end_date else now
        start_from = max(now, prev_end)
        end_date = start_from + datetime.timedelta(days=days)
        c.execute("UPDATE subs SET end_date=? WHERE id=?", (end_date, sub_id))
        conn.commit()
        conn.close()
        return ip_octet, end_date, False
    c.execute("SELECT MAX(ip_last_octet) FROM subs")
    last = c.fetchone()[0] or 1
    ip_octet = last + 1
    end_date = now + datetime.timedelta(days=days)
    c.execute(
        "INSERT INTO subs (user_id, config_name, ip_last_octet, start_date, end_date, public_key, private_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, config_name, ip_octet, now, end_date, public_key, private_key)
    )
    conn.commit()
    conn.close()
    return ip_octet, end_date, True

def db_user_configs(user_id):
    now = datetime.date.today()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT config_name, ip_last_octet, end_date, private_key FROM subs WHERE user_id=? AND end_date>=?",
              (user_id, now))
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
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_get_active_peers():
    today = datetime.date.today()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT public_key FROM subs WHERE end_date >= ?", (today,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def generate_keys():
    private_key = subprocess.getoutput("wg genkey")
    public_key = subprocess.getoutput(f"echo '{private_key}' | wg pubkey")
    return private_key, public_key

# Добавление и удаление пиров в WireGuard

def add_peer_to_wg(public_key, ip_octet):
    cmd = [
        "docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
        "peer", public_key,
        "allowed-ips", f"{WG_SUBNET}.{ip_octet}/32"
    ]
    subprocess.run(cmd, check=True)

def remove_peer_from_wg(public_key):
    cmd = [
        "docker", "exec", "wg-easy", "wg", "set", WG_INTERFACE,
        "peer", public_key, "remove"
    ]
    subprocess.run(cmd, check=True)

# Генерация клиентского конфига и QR-кода
def generate_client_config(private_key, ip_octet):
    return f"""[Interface]
PrivateKey = {private_key}
Address = {WG_SUBNET}.{ip_octet}/24
DNS = 1.1.1.1

[Peer]
PublicKey = {SERVER_PUBLIC_KEY}
Endpoint = {SERVER_ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

def generate_qr(config_text, path):
    img = qrcode.make(config_text)
    img.save(path)

# Клавиатуры
def get_main_keyboard(user_id):
    kb = [
        [KeyboardButton("🛒 Купить подписку"), KeyboardButton("📂 Мои конфиги")],
        [KeyboardButton("📋 Инструкция"), KeyboardButton("💬 Поддержка")]
    ]
    if user_id in ADMIN_IDS:
        kb.append([KeyboardButton("⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

# Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_add(user.id, user.username)
    await update.message.reply_text(
        "👋 Добро пожаловать в VPN Shop!\nВыберите действие:",
        reply_markup=get_main_keyboard(user.id)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user_add(user.id, user.username)
    text = update.message.text
    if text == "🛒 Купить подписку":
        pid = db_payment_add(user.id, CRYPTO_WALLET, PRICE)
        for admin in ADMIN_IDS:
            await context.bot.send_message(
                admin,
                f"Заявка #{pid} от @{user.username} ({user.id}) на {PRICE} USDT",  
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Принять и выдать конфиг", callback_data=f"approve_{pid}")]
                ])
            )
        await update.message.reply_text(
            f"Переведи <b>{PRICE} USDT</b> на адрес:\n<code>{CRYPTO_WALLET}</code>\nПосле оплаты жди подтверждения.",
            parse_mode="HTML"
        )
    elif text == "📂 Мои конфиги":
        configs = db_user_configs(user.id)
        if not configs:
            await update.message.reply_text("У вас нет активных конфигов.", reply_markup=get_main_keyboard(user.id))
            return
        for name, ip_oct, end, priv in configs:
            conf = generate_client_config(priv, ip_oct)
            cpath = f"{user.id}_{name}.conf"
            qpath = f"{user.id}_{name}.png"
            with open(cpath, "w") as f: f.write(conf)
            generate_qr(conf, qpath)
            await update.message.reply_document(InputFile(cpath), caption=f"{name} до {end}")
            await update.message.reply_photo(InputFile(qpath), caption="QR-код")
            os.remove(cpath); os.remove(qpath)
    elif text == "📋 Инструкция":
        await update.message.reply_text(
            "1. Установи WireGuard\n2. Получи конфиг или QR\n3. Импортируй и подключись"
        )
    elif text == "💬 Поддержка":
        await update.message.reply_text("@Youpulo")
    elif text == "⚙️ Админ-панель" and user.id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Заявки", callback_data="admin_requests")],
            [InlineKeyboardButton("Статистика", callback_data="admin_stats")]
        ])
        await update.message.reply_text("Админ-панель", reply_markup=kb)
    else:
        await update.message.reply_text("Выбери команду", reply_markup=get_main_keyboard(user.id))

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; user = q.from_user
    if user.id not in ADMIN_IDS:
        await q.answer("Нет доступа"); return
    data = q.data
    if data == "admin_stats":
        ids = db_users_stat()
        await q.edit_message_text(f"Всего: {len(ids)}; ID: {', '.join(map(str, ids))}")
    elif data == "admin_requests":
        rows = db_get_pending_payments()
        if not rows:
            await q.edit_message_text("Заявок нет")
            return
        txt = "Заявки:\n" + "\n".join(f"{pid}:{uid}:{amt}" for pid,uid,amt in rows)
        await q.edit_message_text(txt)
    elif data.startswith("approve_"):
        pid = int(data.split("_")[1])
        pay = db_get_payment(pid)
        if not pay:
            await q.answer("Не найдена"); return
        uid, amt = pay; name = f"sub_{pid}"
        priv, pub = generate_keys()
        ip_oct, end, is_new = db_sub_add(uid, name, pub, priv)
        try:
            add_peer_to_wg(pub, ip_oct)
        except Exception as e:
            await q.edit_message_text("WG error: " + str(e)); return
        conf = generate_client_config(priv, ip_oct)
        cpath = f"{uid}_{name}.conf"; qpath = f"{uid}_{name}.png"
        with open(cpath, "w") as f: f.write(conf)
        generate_qr(conf, qpath)
        await context.bot.send_document(uid, InputFile(cpath), caption=f"До {end}")
        await context.bot.send_photo(uid, InputFile(qpath), caption="QR")
        os.remove(cpath); os.remove(qpath)
        db_payment_set_status(pid, "confirmed", name)
        await q.edit_message_text(f"Выдано {uid}:{pid}")
    else:
        await q.answer()


def db_users_stat():
    conn = sqlite3.connect(DB); c = conn.cursor()
    c.execute("SELECT id FROM users"); ids = [r[0] for r in c.fetchall()]
    conn.close(); return ids

# Наблюдатель за пирами
def peer_watcher():
    while True:
        for pub in db_get_expired_peers():
            try:
                remove_peer_from_wg(pub)
            except Exception:
                pass
        active = db_get_active_peers()
        peers = subprocess.getoutput(f"docker exec wg-easy wg show {WG_INTERFACE} peers")
        for pub in active:
            if pub not in peers:
                try:
                    add_peer_to_wg(pub, db_get_peer_by_public_key(pub)[2])
                except:
                    pass
        time.sleep(1800)

# Основной запуск
if __name__ == "__main__":
    db_init()
    Thread(target=peer_watcher, daemon=True).start()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(CallbackQueryHandler(admin_callbacks))
    app.run_polling()
