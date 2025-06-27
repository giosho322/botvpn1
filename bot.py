import os
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

WG_DIR = "/root/.wg-easy"
SERVER_PUBLIC_KEY = "7hdEHuQ+4iW8PRDWmi19IHInhOFL/Y7cHPNEwcvHMSA="
SERVER_ENDPOINT = "80.74.28.21:51820"

async def add_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client_name = update.message.text.split()[1]
        ip_last_octet = len(os.listdir(WG_DIR)) + 2

        private_key = subprocess.getoutput("wg genkey")
        public_key = subprocess.getoutput(f"echo '{private_key}' | wg pubkey")

        with open(f"{WG_DIR}/wg0.conf", "a") as f:
            f.write(f"\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = 10.8.0.{ip_last_octet}/32\n")

        client_conf = f"""[Interface]
PrivateKey = {private_key}
Address = 10.8.0.{ip_last_octet}/24
DNS = 1.1.1.1

[Peer]
PublicKey = {SERVER_PUBLIC_KEY}
Endpoint = {SERVER_ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        conf_file = f"{WG_DIR}/{client_name}.conf"
        with open(conf_file, "w") as f:
            f.write(client_conf)

        subprocess.run(["docker", "exec", "wg-easy", "wg-quick", "down", "wg0"])
        subprocess.run(["docker", "exec", "wg-easy", "wg-quick", "up", "wg0"])

        with open(conf_file, "rb") as f:
            await update.message.reply_document(document=f, caption=f"Конфиг для {client_name}")

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

application = Application.builder().token(os.getenv("BOT_TOKEN")).build()
application.add_handler(CommandHandler("add", add_client))
application.run_polling()
