import os
import subprocess
import random
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

WG_DIR = "/root/.wg-easy"
WG_CONF = os.path.join(WG_DIR, "wg0.conf")
WG_INTERFACE = "wg0"
WG_EASY_CONTAINER = "wg-easy"
WG_NETWORK_PREFIX = "10.8.0"
SERVER_PUBLIC_KEY = "7hdEHuQ+4iW8PRDWmi19IHInhOFL/Y7cHPNEwcvHMSA="
ENDPOINT = "80.74.28.21:51820"
DNS = "1.1.1.1"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

def get_used_ips():
    """Parse wg0.conf to find assigned IPs."""
    used = set()
    if os.path.exists(WG_CONF):
        with open(WG_CONF) as f:
            for line in f:
                if line.strip().startswith("AllowedIPs"):
                    ip = line.split("=")[1].strip().split("/")[0]
                    used.add(ip)
    return used

def get_free_ip():
    used = get_used_ips()
    for i in range(2, 255):
        candidate = f"{WG_NETWORK_PREFIX}.{i}"
        if candidate not in used:
            return candidate
    raise Exception("No free IPs available")

def add_client(update: Update, context: CallbackContext):
    try:
        args = update.message.text.split()
        if len(args) < 2:
            update.message.reply_text("Usage: /add <client_name>")
            return
        client_name = args[1]

        # Generate keys
        private_key = subprocess.getoutput("wg genkey")
        public_key = subprocess.getoutput(f"echo '{private_key}' | wg pubkey")

        client_ip = get_free_ip()

        # Add peer to wg0.conf
        with open(WG_CONF, "a") as f:
            f.write(
                f"\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = {client_ip}/32\n"
            )

        # Generate client config
        client_conf = f"""[Interface]
PrivateKey = {private_key}
Address = {client_ip}/24
DNS = {DNS}

[Peer]
PublicKey = {SERVER_PUBLIC_KEY}
Endpoint = {ENDPOINT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        client_conf_path = os.path.join(WG_DIR, f"{client_name}.conf")
        with open(client_conf_path, "w") as f:
            f.write(client_conf)

        # Restart WireGuard inside the container
        os.system(f"docker exec {WG_EASY_CONTAINER} wg-quick down {WG_INTERFACE}")
        os.system(f"docker exec {WG_EASY_CONTAINER} wg-quick up {WG_INTERFACE}")

        # Send config to user
        with open(client_conf_path, "rb") as doc:
            update.message.reply_document(document=doc, filename=f"{client_name}.conf")

    except Exception as e:
        update.message.reply_text(f"Error: {e}")

def main():
    updater = Updater(BOT_TOKEN)
    updater.dispatcher.add_handler(CommandHandler("add", add_client))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
