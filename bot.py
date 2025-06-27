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
    Application, ContextTypes, MessageHandler, CallbackQueryHandler, filters, CommandHandler
)
from threading import Thread
import time

DB = "autowgshop.db"
ADMIN_IDS = [1203425573]  # <-- твой Telegram ID
CRYPTO_WALLET = "12313"
PRICE = 5  # USDT
WG_CONF = "/root/.wg-easy/wg0.conf"
WG_INTERFACE = "wg0"
WG_SUBNET = "10.8.0"
SERVER_PUBLIC_KEY = "hRVLkkxJNDpYGiGdmg/YRFOAVPrwJMj9zHZeb1l9aQU="
SERVER_ENDPOINT = "80.74.28.21:51820"
DOCKER_CONTAINER = "wg-easy"  # Имя твоего контейнера! Проверь через `docker ps`

logging.basicConfig(level=logging.INFO)

# ... (все функции базы данных без изменений)

def generate_keys():
    private_key = subprocess.getoutput("wg genkey")
    public_key = subprocess.getoutput(f"echo '{private_key}' | wg pubkey")
    return private_key, public_key

# ------ вот эти две функции изменяем ------
def add_peer_to_wg(public_key, ip_octet):
    cmd = [
        "docker", "exec", DOCKER_CONTAINER,
        "wg", "set", WG_INTERFACE,
        "peer", public_key,
        "allowed-ips", f"{WG_SUBNET}.{ip_octet}/32"
    ]
    subprocess.run(cmd, check=True)

def remove_peer_from_wg(public_key):
    cmd = [
        "docker", "exec", DOCKER_CONTAINER,
        "wg", "set", WG_INTERFACE,
        "peer", public_key, "remove"
    ]
    subprocess.run(cmd, check=True)
# ------------------------------------------

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

def get_main_keyboard(user_id):
    kb = [
        [KeyboardButton("🛒 Купить подписку"), KeyboardButton("📂 Мои конфиги")],
        [KeyboardButton("📋 Инструкция"), KeyboardButton("💬 Поддержка")]
    ]
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
    user = update.effective_user
    db_user_add(user.id, user.username)
    text = update.message.text
    if text == "🛒 Купить подписку":
        payment_id = db_payment_add(user.id, CRYPTO_WALLET, PRICE)
        for admin in ADMIN_IDS:
            await context.bot.send_message(
                admin,
                f"Заявка #{payment_id} на оплату от @{user.username} (id {user.id}) на {PRICE} USDT.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Принять и выдать конфиг", callback_data=f"approve_{payment_id}")]
                ])
            )
        await update.message.reply_text(
            f"Для оплаты переведите <b>{PRICE} USDT</b> на адрес:\n<code>{CRYPTO_WALLET}</code>\n\nПосле оплаты ожидайте подтверждения.",
            parse_mode="HTML"
        )
    elif text == "📂 Мои конфиги":
        configs = db_user_configs(user.id)
        if not configs:
            await update.message.reply_text("У вас нет активных конфигов. Купите подписку!", reply_markup=get_main_keyboard(user.id))
            return
        for config_name, ip_octet, end_date, private_key in configs:
            conf = generate_client_config(private_key, ip_octet)
            conf_path = f"{user.id}_{config_name}.conf"
            qr_path = f"{user.id}_{config_name}.png"
            with open(conf_path, "w") as f:
                f.write(conf)
            generate_qr(conf, qr_path)
            await update.message.reply_document(document=InputFile(conf_path), caption=f"Конфиг: {config_name}\nДействителен до: {end_date}")
            await update.message.reply_photo(photo=InputFile(qr_path), caption="QR-код для WireGuard")
            os.remove(conf_path)
            os.remove(qr_path)
    elif text == "📋 Инструкция":
        await update.message.reply_text(
            "1. Скачайте и установите WireGuard на ПК или телефон.\n"
            "2. Получите свой конфиг или отсканируйте QR-код (можно сделать через WireGuard).\n"
            "3. Импортируйте конфиг в приложение.\n"
            "4. Подключитесь и пользуйтесь VPN.\n"
            "Если вопросы — пишите в поддержку."
        )
    elif text == "💬 Поддержка":
        await update.message.reply_text("Пишите сюда: @Youpulo")
    elif text == "⚙️ Админ-панель" and user.id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Заявки", callback_data="admin_requests")],
            [InlineKeyboardButton("Статистика", callback_data="admin_stats")]
        ])
        await update.message.reply_text("⚙️ Админ-панель", reply_markup=kb)
    else:
        await update.message.reply_text("Выберите действие:", reply_markup=get_main_keyboard(user.id))

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.answer("Нет доступа")
        return
    data = query.data
    if data == "admin_stats":
        ids = db_users_stat()
        await query.edit_message_text(f"Всего пользователей: {len(ids)}\nID: " + ", ".join(str(i) for i in ids))
    elif data == "admin_requests":
        rows = db_get_pending_payments()
        if not rows:
            await query.edit_message_text("Нет новых заявок.")
            return
        text = "Заявки на оплату:\n"
        for pid, uid, amount in rows:
            text += f"ID: {pid}, user: {uid}, сумма: {amount} USDT\n"
        await query.edit_message_text(text)
    elif data.startswith("approve_"):
        payment_id = int(data.split("_")[1])
        payment = db_get_payment(payment_id)
        if not payment:
            await query.answer("Заявка не найдена")
            return
        user_id, amount = payment
        config_name = f"sub_{payment_id}"
        private_key, public_key = generate_keys()
        ip_octet, end_date, is_new = db_sub_add(user_id, config_name, public_key, private_key)
        try:
            add_peer_to_wg(public_key, ip_octet)
        except Exception as e:
            await query.edit_message_text("Ошибка добавления peer в WG: " + str(e))
            return
        conf = generate_client_config(private_key, ip_octet)
        conf_path = f"{user_id}_{config_name}.conf"
        qr_path = f"{user_id}_{config_name}.png"
        with open(conf_path, "w") as f:
            f.write(conf)
        generate_qr(conf, qr_path)
        await context.bot.send_document(chat_id=user_id, document=InputFile(conf_path),
                                        caption=f"Ваша подписка активна до {end_date}.\nСпасибо за оплату!")
        await context.bot.send_photo(chat_id=user_id, photo=InputFile(qr_path), caption="QR-код для WireGuard")
        os.remove(conf_path)
        os.remove(qr_path)
        db_payment_set_status(payment_id, "confirmed", config_name)
        await query.edit_message_text(f"Конфиг выдан пользователю {user_id}, заявка {payment_id} закрыта.")
        await context.bot.send_message(user_id, "Ваша заявка обработана и конфиг отправлен!")
    else:
        await query.answer()

def db_users_stat():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id FROM users")
    ids = [row[0] for row in c.fetchall()]
    conn.close()
    return ids

def peer_watcher():
    while True:
        expired = db_get_expired_peers()
        for pubkey in expired:
            try:
                remove_peer_from_wg(pubkey)
            except Exception as e:
                logging.info(f"Ошибка удаления peer {pubkey}: {e}")
        active = db_get_active_peers()
        for pubkey in active:
            peer = db_get_peer_by_public_key(pubkey)
            if not peer:
                continue
            _, _, ip_octet, end_date, _ = peer
            peers_output = subprocess.getoutput(
                f"docker exec {DOCKER_CONTAINER} wg show {WG_INTERFACE} peers"
            )
            if pubkey not in peers_output:
                try:
                    add_peer_to_wg(pubkey, ip_octet)
                except Exception as e:
                    logging.info(f"Ошибка добавления peer {pubkey}: {e}")
        time.sleep(1800)  # Проверять каждые 30 минут

def main():
    db_init()
    Thread(target=peer_watcher, daemon=True).start()
    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(CallbackQueryHandler(admin_callbacks))
    app.run_polling()

if __name__ == "__main__":
    main()
