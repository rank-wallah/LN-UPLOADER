import os
import sys
import json
import time
import subprocess
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

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
            
            # Quoted & Italicized Caption Style
            formatted_caption = (
                f"> *{title}*\n"
                f">\n"
                f"> _Uploaded via Downloader Engine_"
            )
            
            upload_to_telegram(app, target_chat_id, downloaded_path, formatted_caption)
            
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

# ==========================================
# TELEGRAM BOT STYLED UI & COMMANDS
# ==========================================

@app.on_message(filters.command(["start", "help"]) & (filters.group | filters.channel | filters.private))
def start_and_help_handler(client: Client, message: Message):
    """
    Styled Start & Help UI Menu using Blockquotes and Italics.
    """
    ui_text = (
        f"> _⚡ **Multi-Channel Video Downloader Bot**_\n"
        f">\n"
        f"> _This engine processes `.json` batch files, downloads m3u8 streams using N_m3u8DL-RE, and uploads high-speed MP4 videos directly to your target public/private channels or groups._\n"
        f">\n"
        f"> 🛠 *__Available Commands:__*\n"
        f">\n"
        f"> 1️⃣ `/batch <BEARER_TOKEN>`\n"
        f"> _Reply to or attach a `.json` file to start batch downloading. Token is optional._\n"
        f">\n"
        f"> 2️⃣ `/stop`\n"
        f"> _Cancel active downloading and uploading task in the current channel._\n"
        f">\n"
        f"> 3️⃣ `/id`\n"
        f"> _Fetch current chat/channel ID for private channel management._\n"
        f">\n"
        f"> 4️⃣ `/help`\n"
        f"> _Display this instructions panel._\n"
        f">\n"
        f"> 🔒 *__Private Channel Deployment:__*\n"
        f"> _Add this bot as an **Administrator** in your target private channel with full permissions to post videos, then execute `/batch` directly inside the channel or forward the batch file with the chat ID._"
    )
    
    message.reply_text(
        text=ui_text,
        disable_web_page_preview=True
    )

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
def handle_batch_upload(client: Client, message: Message):
    target_chat_id = message.chat.id
    
    args = message.text.split(maxsplit=1)
    bearer_token = args[1].strip() if len(args) > 1 else None

    doc = message.document or (message.reply_to_message.document if message.reply_to_message else None)
    if not doc or not doc.file_name.endswith(".json"):
        error_msg = (
            f"> _⚠️ **Invalid Request**_\n"
            f">\n"
            f"> _Please reply to or attach a valid `.json` batch file with the command `/batch <OPTIONAL_BEARER_TOKEN>`._"
        )
        message.reply_text(error_msg)
        return

    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        already_running_msg = (
            f"> _⚠️ **Task Already Active**_\n"
            f">\n"
            f"> _A batch task is currently running in this chat. Send `/stop` first to cancel it._"
        )
        message.reply_text(already_running_msg)
        return

    json_path = message.download()
    with open(json_path, "r", encoding="utf-8") as f:
        data_items = json.load(f)
    os.remove(json_path)

    ACTIVE_JOBS[target_chat_id] = {"running": True, "token": bearer_token}
    
    start_msg = (
        f"> _🚀 **Batch Process Triggered**_\n"
        f">\n"
        f"> _Target Chat ID: `{target_chat_id}`_\n"
        f"> _Total Queue Items: `{len(data_items)}`_\n"
        f"> _Status: Running..._"
    )
    message.reply_text(start_msg)

    try:
        process_batch(target_chat_id, data_items, bearer_token)
        complete_msg = (
            f"> _✅ **Task Completed**_\n"
            f">\n"
            f"> _All items from the batch file have been processed and uploaded successfully._"
        )
        message.reply_text(complete_msg)
    finally:
        ACTIVE_JOBS[target_chat_id] = {"running": False, "token": None}

@app.on_message(filters.command("stop") & (filters.group | filters.channel | filters.private))
def stop_batch_upload(client: Client, message: Message):
    target_chat_id = message.chat.id
    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        ACTIVE_JOBS[target_chat_id]["running"] = False
        stop_msg = (
            f"> _🛑 **Cancellation Initiated**_\n"
            f">\n"
            f"> _Sent stop signal to active queue processing for this channel._"
        )
        message.reply_text(stop_msg)
    else:
        no_task_msg = (
            f"> _⚠️ **No Active Task**_\n"
            f">\n"
            f"> _There are no active downloading tasks currently running in this chat._"
        )
        message.reply_text(no_task_msg)

@app.on_message(filters.command("id"))
def show_chat_id(client: Client, message: Message):
    id_msg = (
        f"> _🆔 **Current Chat Metadata**_\n"
        f">\n"
        f"> _Chat ID: `{message.chat.id}`_\n"
        f"> _Chat Type: `{message.chat.type}`_"
    )
    message.reply_text(id_msg)

def main():
    print("[+] Multi-Channel Downloader Engine Started...")
    app.run()

if __name__ == "__main__":
    main()
