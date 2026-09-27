import os
import sys
import json
import time
import subprocess
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# ENVIRONMENT CONFIGURATION
# ==========================================
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=16
)

# In-memory session tracking per channel
ACTIVE_JOBS = {}

def progress_bar(current, total, status):
    percent = (current / total) * 100
    speed_bar = f"[{'=' * int(percent // 10)}{' ' * (10 - int(percent // 10))}] {percent:.1f}%"
    print(f"\r{status}: {speed_bar}", end="", flush=True)

def download_m3u8(m3u8_url, output_name, bearer_token=None):
    print(f"\n[+] Starting download: {output_name}")
    os.makedirs("./downloads", exist_ok=True)
    
    cmd = [
        "N_m3u8DL-RE",
        m3u8_url,
        "--save-name", output_name,
        "--save-dir", "./downloads",
        "--auto-select",
        "--thread-count", "16",
        "--download-retry-count", "5"
    ]
    
    if bearer_token:
        cmd.extend(["--header", f"Authorization: Bearer {bearer_token}"])

    subprocess.run(cmd, check=True)
    return os.path.join("./downloads", f"{output_name}.mp4")

def upload_to_telegram(app_client, target_chat_id, file_path, caption):
    print(f"\n[+] Uploading to target: {target_chat_id}")
    def progress(current, total):
        progress_bar(current, total, "Uploading")

    app_client.send_video(
        chat_id=target_chat_id,
        video=file_path,
        caption=caption,
        progress=progress
    )
    print("\n[+] Upload completed successfully.")

def process_batch(target_chat_id, data_items, bearer_token=None):
    for index, item in enumerate(data_items, start=1):
        if not ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
            print(f"[-] Processing cancelled for chat {target_chat_id}")
            break

        title = item.get("title", f"Video_{index}")
        m3u8_url = item.get("url")

        if not m3u8_url:
            continue

        clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()

        try:
            downloaded_path = download_m3u8(m3u8_url, clean_title, bearer_token)
            upload_to_telegram(app, target_chat_id, downloaded_path, f"**{title}**")
            
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

# ==========================================
# TELEGRAM BOT INTERACTIVE COMMANDS
# ==========================================

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
def handle_batch_upload(client: Client, message: Message):
    """
    Usage: Send /batch <BEARER_TOKEN> along with attached JSON file or replied JSON file
    Works in private messages or directly inside target public/private channels.
    """
    target_chat_id = message.chat.id
    
    # Parse Bearer Token parameter
    args = message.text.split(maxsplit=1)
    bearer_token = args[1].strip() if len(args) > 1 else None

    # Retrieve attached JSON file
    doc = message.document or (message.reply_to_message.document if message.reply_to_message else None)
    if not doc or not doc.file_name.endswith(".json"):
        message.reply_text("⚠️ Please reply to or attach a valid `.json` batch file with `/batch <OPTIONAL_BEARER_TOKEN>`.")
        return

    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        message.reply_text("⚠️ A task is already actively uploading to this channel.")
        return

    # Download local copy of JSON
    json_path = message.download()
    with open(json_path, "r", encoding="utf-8") as f:
        data_items = json.load(f)
    os.remove(json_path)

    ACTIVE_JOBS[target_chat_id] = {"running": True, "token": bearer_token}
    message.reply_text(f"🚀 Started batch upload to chat `{target_chat_id}` with {len(data_items)} items.")

    # Execute processing loop asynchronously in thread/blocking runner
    try:
        process_batch(target_chat_id, data_items, bearer_token)
        message.reply_text("✅ Batch processing completed successfully for this channel.")
    finally:
        ACTIVE_JOBS[target_chat_id] = {"running": False, "token": None}

@app.on_message(filters.command("stop") & (filters.group | filters.channel | filters.private))
def stop_batch_upload(client: Client, message: Message):
    target_chat_id = message.chat.id
    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        ACTIVE_JOBS[target_chat_id]["running"] = False
        message.reply_text("🛑 Cancellation signal sent to current channel task.")
    else:
        message.reply_text("⚠️ No active task running in this chat.")

@app.on_message(filters.command("id"))
def show_chat_id(client: Client, message: Message):
    message.reply_text(f"🆔 **Chat ID:** `{message.chat.id}`")

def main():
    print("[+] Multi-Channel Downloader Engine Started...")
    app.run()

if __name__ == "__main__":
    main()
