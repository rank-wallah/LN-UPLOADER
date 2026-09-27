import os
import sys
import json
import time
import subprocess
import requests
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# ENVIRONMENT CONFIGURATION (NO HARDCODED SECRETS)
# ==========================================
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FRIENDS_BEARER_TOKEN = os.getenv("FRIENDS_BEARER_TOKEN", "")

# Initialize Pyrogram Bot Client
app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=16
)

# Active status flags & runtime config
PROCESSING_ACTIVE = False

def progress_bar(current, total, status):
    percent = (current / total) * 100
    speed_bar = f"[{'=' * int(percent // 10)}{' ' * (10 - int(percent // 10))}] {percent:.1f}%"
    print(f"\r{status}: {speed_bar}", end="", flush=True)

def download_m3u8(m3u8_url, output_name):
    print(f"\n[+] Starting high-speed download for: {output_name}")
    cmd = [
        "N_m3u8DL-RE",
        m3u8_url,
        "--save-name", output_name,
        "--save-dir", "./downloads",
        "--auto-select",
        "--thread-count", "16",
        "--download-retry-count", "5"
    ]
    subprocess.run(cmd, check=True)
    return os.path.join("./downloads", f"{output_name}.mp4")

def upload_to_telegram(app_client, file_path, caption):
    print(f"\n[+] Uploading file to Telegram: {file_path}")
    def progress(current, total):
        progress_bar(current, total, "Uploading")

    app_client.send_video(
        chat_id=TG_CHAT_ID,
        video=file_path,
        caption=caption,
        progress=progress
    )
    print("\n[+] Upload completed successfully.")

def process_subject(json_file):
    global FRIENDS_BEARER_TOKEN
    if not os.path.exists(json_file):
        print(f"[-] File not found: {json_file}")
        return

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs("./downloads", exist_ok=True)

    headers = {}
    if FRIENDS_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {FRIENDS_BEARER_TOKEN}"

    for index, item in enumerate(data, start=1):
        title = item.get("title", f"Video_{index}")
        m3u8_url = item.get("url")

        if not m3u8_url:
            continue

        clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()

        try:
            downloaded_path = download_m3u8(m3u8_url, clean_title)
            upload_to_telegram(app, downloaded_path, f"**{title}**")
            
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

# Telegram Bot Commands for Dynamic Control
@app.on_message(filters.command("settoken"))
def set_bearer_token(client: Client, message: Message):
    global FRIENDS_BEARER_TOKEN
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        FRIENDS_BEARER_TOKEN = args[1].strip()
        message.reply_text("✅ Friends Bearer Token updated successfully.")
        print(f"[+] Friends Bearer Token updated via Telegram command.")
    else:
        message.reply_text("⚠️ Usage: `/settoken <YOUR_BEARER_TOKEN>`")

@app.on_message(filters.command("startdownload"))
def start_download_process(client: Client, message: Message):
    global PROCESSING_ACTIVE
    if PROCESSING_ACTIVE:
        message.reply_text("⚠️ Processing is already active.")
        return

    PROCESSING_ACTIVE = True
    message.reply_text("🚀 Starting download and upload queue...")
    
    for subject_json in ["physics.json", "chemistry.json", "maths.json"]:
        print(f"\n[+] Processing JSON targets: {subject_json}")
        process_subject(subject_json)

    PROCESSING_ACTIVE = False
    message.reply_text("✅ All tasks finished successfully.")

@app.on_message(filters.command("status"))
def check_status(client: Client, message: Message):
    token_status = "Set" if FRIENDS_BEARER_TOKEN else "Not Set"
    message.reply_text(f"📊 **Status:**\n• Token: `{token_status}`\n• Running: `{PROCESSING_ACTIVE}`")

def main():
    print("[+] Bot initialized and listening for commands...")
    app.run()

if __name__ == "__main__":
    main()
