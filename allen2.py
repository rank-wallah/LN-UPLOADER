import os
import sys
import json
import time
import gc
import shutil
import asyncio
import requests
import subprocess
from pyrogram import Client, filters, enums
from pyrogram.types import Message

# ==========================================
# ENVIRONMENT CONFIGURATION & OWNER AUTH
# ==========================================
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# Default Owner Telegram User ID
OWNER_ID = int(os.getenv("OWNER_ID", "6789039689"))

AUTH_FILE = "authorized_users.json"
DOWNLOAD_DIR = "./downloads"

def load_authorized_users():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                users = json.load(f)
                return set(int(u) for u in users)
        except Exception as e:
            print(f"[-] Error loading auth file: {e}")
    return {OWNER_ID}

def save_authorized_users(users_set):
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(list(users_set), f)
    except Exception as e:
        print(f"[-] Error saving auth file: {e}")

AUTHORIZED_USERS = load_authorized_users()

def is_user_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in AUTHORIZED_USERS

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=16,
    parse_mode=enums.ParseMode.HTML
)

ACTIVE_JOBS = {}
MAX_TG_MSG_LEN = 4000

def cleanup_workspace():
    """Removes temporary leftover downloads on restart or completion."""
    if os.path.exists(DOWNLOAD_DIR):
        try:
            shutil.rmtree(DOWNLOAD_DIR)
        except Exception as e:
            print(f"[-] Directory cleanup error: {e}")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==========================================
# ALLEN API AUTO-FETCH METHOD
# ==========================================
def fetch_allen_batch_json(token_or_id):
    """
    Fetches video batch payload automatically using Bearer token or Batch ID.
    Modify the URL and headers below as per Allen's current API endpoint format if needed.
    """
    headers = {
        "Authorization": f"Bearer {token_or_id}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    # If user provided a token/URL/ID, call Allen API endpoint
    # Adjust endpoint URL if you have a specific custom API path
    api_url = f"https://api.allen.ac.in/v1/batch/contents" if not token_or_id.startswith("http") else token_or_id
    
    response = requests.get(api_url, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    
    # Format API output to normalized item list
    data_items = []
    
    # Parser logic for API structure
    raw_list = data if isinstance(data, list) else data.get("data", data.get("items", []))
    for item in raw_list:
        title = item.get("title") or item.get("topic_name") or item.get("name") or "Lecture Video"
        url = item.get("url") or item.get("m3u8_url") or item.get("stream_url")
        is_new_chapter = item.get("is_new_chapter", False) or item.get("is_topic_head", False)
        
        if url:
            data_items.append({
                "title": title,
                "url": url,
                "is_new_chapter": is_new_chapter
            })
            
    return data_items

async def async_fetch_allen_batch_json(token_or_id):
    return await asyncio.to_thread(fetch_allen_batch_json, token_or_id)

# ==========================================
# DOWNLOAD & UPLOAD ENGINE
# ==========================================
def download_m3u8(m3u8_url, output_name, bearer_token=None):
    print(f"\n[+] Starting download: {output_name}")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    cmd = [
        "N_m3u8DL-RE",
        m3u8_url,
        "--save-name", output_name,
        "--save-dir", DOWNLOAD_DIR,
        "--auto-select",
        "--thread-count", "16",
        "--download-retry-count", "10",
        "--no-log"
    ]
    
    if bearer_token and not bearer_token.startswith("http"):
        cmd.extend(["--header", f"Authorization: Bearer {bearer_token}"])

    subprocess.run(cmd, check=True)
    
    output_path = os.path.join(DOWNLOAD_DIR, f"{output_name}.mp4")
    if not os.path.exists(output_path):
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(output_name):
                return os.path.join(DOWNLOAD_DIR, file)
                
    return output_path

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None):
    return await asyncio.to_thread(download_m3u8, m3u8_url, output_name, bearer_token)

def upload_to_telegram(app_client, target_chat_id, file_path, caption):
    print(f"\n[+] Uploading to target: {target_chat_id}")
    sent_msg = app_client.send_video(
        chat_id=target_chat_id,
        video=file_path,
        caption=caption,
        supports_streaming=True
    )
    print("\n[+] Upload completed successfully.")
    return sent_msg

async def async_upload_to_telegram(app_client, target_chat_id, file_path, caption):
    return await asyncio.to_thread(upload_to_telegram, app_client, target_chat_id, file_path, caption)

def send_chunked_messages(app_client, target_chat_id, header, index_items, pin_last=True):
    messages_to_send = []
    current_text = header + "\n\n"

    for idx, item in enumerate(index_items, start=1):
        line = f"• <a href='{item['link']}'>{item['title']}</a>\n"
        if len(current_text) + len(line) > MAX_TG_MSG_LEN:
            messages_to_send.append(current_text)
            current_text = line
        else:
            current_text += line

    if current_text.strip():
        messages_to_send.append(current_text)

    total_chunks = len(messages_to_send)
    last_sent_msg = None

    for i, msg_text in enumerate(messages_to_send, start=1):
        if total_chunks > 1:
            chunk_caption = f"<blockquote><i>{msg_text}\n<b>Part {i} of {total_chunks}</b></i></blockquote>"
        else:
            chunk_caption = f"<blockquote><i>{msg_text}</i></blockquote>"
        
        last_sent_msg = app_client.send_message(
            chat_id=target_chat_id,
            text=chunk_caption,
            disable_web_page_preview=True
        )

    if pin_last and last_sent_msg:
        try:
            app_client.pin_chat_message(target_chat_id, last_sent_msg.id)
        except Exception as e:
            print(f"[-] Could not pin final index: {e}")

def get_message_link(chat_id, msg_id):
    chat_str = str(chat_id)
    if chat_str.startswith("-100"):
        real_id = chat_str[4:]
        return f"https://t.me/c/{real_id}/{msg_id}"
    return f"https://t.me/c/{chat_id}/{msg_id}"

async def process_batch_async(target_chat_id, data_items, bearer_token=None):
    index_records = []

    for index, item in enumerate(data_items, start=1):
        if not ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
            print(f"[-] Processing cancelled for chat {target_chat_id}")
            break

        title = item.get("title", f"Topic_{index}")
        m3u8_url = item.get("url")
        is_new_chapter = item.get("is_new_chapter", False)

        if not m3u8_url:
            continue

        clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()

        try:
            downloaded_path = await async_download_m3u8(m3u8_url, clean_title, bearer_token)
            
            formatted_caption = (
                f"<blockquote><i><b>{title}</b>\n\n"
                f"Uploaded via Allen Downloader Engine</i></blockquote>"
            )
            
            sent_msg = await async_upload_to_telegram(app, target_chat_id, downloaded_path, formatted_caption)
            msg_link = get_message_link(target_chat_id, sent_msg.id)

            if is_new_chapter or index == 1:
                try:
                    await app.pin_chat_message(target_chat_id, sent_msg.id)
                except Exception as pin_err:
                    print(f"[-] Pinning failed: {pin_err}")

            index_records.append({"title": title, "link": msg_link})
            
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
            gc.collect()

        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

    if index_records and ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        index_header = "<b>📌 BATCH MASTER INDEX</b>\n\nBelow is the complete list of topics processed in this batch:"
        send_chunked_messages(app, target_chat_id, index_header, index_records, pin_last=True)

# ==========================================
# COMMAND HANDLERS
# ==========================================

@app.on_message(filters.command(["start", "help"]) & (filters.group | filters.channel | filters.private))
async def start_and_help_handler(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 <b>Access Denied</b>\n\nYou are not authorized to use this bot. Contact owner (<code>6789039689</code>) for access.</i></blockquote>")
        return

    ui_text = (
        "<blockquote><i>⚡ <b>Allen Token Downloader Bot</b>\n\n"
        "Send your Allen Bearer Token / API Link / Batch ID directly to start downloading without manual JSON files.\n\n"
        "🛠 <b>Usage:</b>\n\n"
        "1️⃣ <code>/batch &lt;TOKEN_OR_BATCH_ID&gt;</code>\n"
        "Auto-fetches batch contents from API and starts downloading.\n\n"
        "2️⃣ <code>/stop</code>\n"
        "Cancel active downloading task.\n\n"
        "3️⃣ <code>/id</code>\n"
        "Get current Chat ID.</i></blockquote>"
    )
    await message.reply_text(text=ui_text, disable_web_page_preview=True)

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
async def handle_batch_upload(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    target_chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        await message.reply_text("<blockquote><i>⚠️ <b>Missing Token/ID</b>\n\nUsage: <code>/batch YOUR_BEARER_TOKEN_OR_BATCH_ID</code></i></blockquote>")
        return

    token_or_id = args[1].strip()

    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        await message.reply_text("<blockquote><i>⚠️ <b>Task Already Active</b>\n\nA batch task is currently running in this chat. Send <code>/stop</code> first.</i></blockquote>")
        return

    status_msg = await message.reply_text("<blockquote><i>🔄 <b>Fetching Batch Data from Allen API...</b></i></blockquote>")

    try:
        # Fetching JSON dynamically using token
        data_items = await async_fetch_allen_batch_json(token_or_id)
        
        if not data_items:
            await status_msg.edit_text("<blockquote><i>❌ <b>No videos/m3u8 links found in the batch payload.</b> Check token validity.</i></blockquote>")
            return

        ACTIVE_JOBS[target_chat_id] = {"running": True, "token": token_or_id}
        
        start_msg = (
            f"<blockquote><i>🚀 <b>Batch Process Started</b>\n\n"
            f"• Target Chat ID: <code>{target_chat_id}</code>\n"
            f"• Total Queue Videos: <code>{len(data_items)}</code>\n"
            f"• Status: Downloading & Uploading...</i></blockquote>"
        )
        await status_msg.edit_text(start_msg)

        await process_batch_async(target_chat_id, data_items, token_or_id)
        
        complete_msg = (
            "<blockquote><i>✅ <b>Batch Processing Completed</b>\n\n"
            "All items downloaded, uploaded, pinned, and master index generated!</i></blockquote>"
        )
        await message.reply_text(complete_msg)

    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <b>Failed to fetch batch data:</b> <code>{str(e)}</code></i></blockquote>")
    finally:
        ACTIVE_JOBS[target_chat_id] = {"running": False, "token": None}

@app.on_message(filters.command("stop") & (filters.group | filters.channel | filters.private))
async def stop_batch_upload(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    target_chat_id = message.chat.id
    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        ACTIVE_JOBS[target_chat_id]["running"] = False
        await message.reply_text("<blockquote><i>🛑 <b>Cancellation Initiated</b>\n\nStop signal sent.</i></blockquote>")
    else:
        await message.reply_text("<blockquote><i>⚠️ <b>No Active Task</b></i></blockquote>")

@app.on_message(filters.command("id"))
async def show_chat_id(client: Client, message: Message):
    await message.reply_text(f"<blockquote><i>🆔 <b>Chat ID:</b> <code>{message.chat.id}</code></i></blockquote>")

def main():
    cleanup_workspace()
    print("[+] Allen Token Downloader Engine Started...")
    app.run()

if __name__ == "__main__":
    main()
