import os
import sys
import json
import time
import gc
import shutil
import asyncio
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
    parse_mode=enums.ParseMode.HTML  # HTML mode for perfect UI styling
)

# In-memory session tracking per channel
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
    
    if bearer_token:
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
        is_new_chapter = item.get("is_new_chapter", False) or item.get("is_topic_head", False)

        if not m3u8_url:
            continue

        clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()

        try:
            downloaded_path = await async_download_m3u8(m3u8_url, clean_title, bearer_token)
            
            formatted_caption = (
                f"<blockquote><i><b>{title}</b>\n\n"
                f"Uploaded via Downloader Engine</i></blockquote>"
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
# OWNER AUTH MANAGEMENT COMMANDS
# ==========================================

@app.on_message(filters.command("auth"))
async def authorize_user(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("<blockquote><i>🚫 Only the Bot Owner can authorize users.</i></blockquote>")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/auth &lt;USER_ID&gt;</code></i></blockquote>")
        return

    target_id = int(args[1])
    AUTHORIZED_USERS.add(target_id)
    save_authorized_users(AUTHORIZED_USERS)
    
    msg = (
        f"<blockquote><i><b>✅ User Authorized Successfully</b>\n\n"
        f"User ID <code>{target_id}</code> has been granted full access.</i></blockquote>"
    )
    await message.reply_text(msg)

@app.on_message(filters.command("unauth"))
async def unauthorize_user(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("<blockquote><i>🚫 Only the Bot Owner can revoke authorizations.</i></blockquote>")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/unauth &lt;USER_ID&gt;</code></i></blockquote>")
        return

    target_id = int(args[1])
    if target_id == OWNER_ID:
        await message.reply_text("<blockquote><i>⚠️ You cannot revoke owner privileges.</i></blockquote>")
        return

    AUTHORIZED_USERS.discard(target_id)
    save_authorized_users(AUTHORIZED_USERS)
    
    msg = (
        f"<blockquote><i><b>🛑 User Revoked Successfully</b>\n\n"
        f"User ID <code>{target_id}</code> access has been removed.</i></blockquote>"
    )
    await message.reply_text(msg)

@app.on_message(filters.command("authlist"))
async def list_authorized_users(client: Client, message: Message):
    if not is_user_authorized(message.from_user.id):
        await message.reply_text(" catalog: <blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    users_str = "\n".join([f"• <code>{u}</code>" + (" (Owner)" if u == OWNER_ID else "") for u in AUTHORIZED_USERS])
    list_msg = (
        f"<blockquote><i><b>📋 Authorized Users List:</b>\n\n"
        f"{users_str}</i></blockquote>"
    )
    await message.reply_text(list_msg)

# ==========================================
# TELEGRAM BOT STYLED UI & COMMANDS
# ==========================================

@app.on_message(filters.command(["start", "help"]) & (filters.group | filters.channel | filters.private))
async def start_and_help_handler(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 <b>Access Denied</b>\n\nYou are not authorized to use this bot. Contact owner (<code>6789039689</code>) for access.</i></blockquote>")
        return

    # Clean HTML Quoted Layout
    ui_text = (
        "<blockquote><i>⚡ <b>Multi-Channel Video Downloader Bot</b>\n\n"
        "This engine processes <code>.json</code> batch files, downloads m3u8 streams using N_m3u8DL-RE, pins chapters/topics, auto-builds an index, and uploads high-speed MP4 videos directly to your target channel.\n\n"
        "🛠 <b>Available Commands:</b>\n\n"
        "1️⃣ <code>/batch &lt;BEARER_TOKEN&gt;</code>\n"
        "Reply to or attach a <code>.json</code> file to start batch downloading.\n\n"
        "2️⃣ <code>/stop</code>\n"
        "Cancel active downloading and uploading task.\n\n"
        "3️⃣ <code>/id</code>\n"
        "Fetch current chat/channel ID.\n\n"
        "4️⃣ <code>/help</code>\n"
        "Display this instructions panel.\n\n"
        "🔑 <b>Admin Controls (Owner Only):</b>\n"
        "• <code>/auth &lt;USER_ID&gt;</code> - Grant user access\n"
        "• <code>/unauth &lt;USER_ID&gt;</code> - Revoke user access\n"
        "• <code>/authlist</code> - List authorized users\n\n"
        "📑 <b>Index & Pinning System:</b>\n"
        "Every topic/chapter is tracked and auto-indexed with hyperlinked posts upon completion.</i></blockquote>"
    )
    
    await message.reply_text(
        text=ui_text,
        disable_web_page_preview=True
    )

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
async def handle_batch_upload(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    target_chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    bearer_token = args[1].strip() if len(args) > 1 else None

    doc = message.document or (message.reply_to_message.document if message.reply_to_message else None)
    if not doc or not doc.file_name.endswith(".json"):
        error_msg = (
            "<blockquote><i>⚠️ <b>Invalid Request</b>\n\n"
            "Please reply to or attach a valid <code>.json</code> batch file with the command <code>/batch &lt;BEARER_TOKEN&gt;</code>.</i></blockquote>"
        )
        await message.reply_text(error_msg)
        return

    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        already_running_msg = (
            "<blockquote><i>⚠️ <b>Task Already Active</b>\n\n"
            "A batch task is currently running in this chat. Send <code>/stop</code> first to cancel it.</i></blockquote>"
        )
        await message.reply_text(already_running_msg)
        return

    json_path = await message.download()
    with open(json_path, "r", encoding="utf-8") as f:
        data_items = json.load(f)
    os.remove(json_path)

    ACTIVE_JOBS[target_chat_id] = {"running": True, "token": bearer_token}
    
    start_msg = (
        f"<blockquote><i>🚀 <b>Batch Process Triggered</b>\n\n"
        f"• Target Chat ID: <code>{target_chat_id}</code>\n"
        f"• Total Queue Items: <code>{len(data_items)}</code>\n"
        f"• Status: Running...</i></blockquote>"
    )
    await message.reply_text(start_msg)

    try:
        await process_batch_async(target_chat_id, data_items, bearer_token)
        complete_msg = (
            "<blockquote><i>✅ <b>Batch Processing Completed</b>\n\n"
            "All items were processed and master index published successfully.</i></blockquote>"
        )
        await message.reply_text(complete_msg)
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
        stop_msg = "<blockquote><i>🛑 <b>Cancellation Initiated</b>\n\nStop signal sent to active queue.</i></blockquote>"
        await message.reply_text(stop_msg)
    else:
        no_task_msg = "<blockquote><i>⚠️ <b>No Active Task</b>\n\nThere are no active tasks running in this chat.</i></blockquote>"
        await message.reply_text(no_task_msg)

@app.on_message(filters.command("id"))
async def show_chat_id(client: Client, message: Message):
    id_msg = (
        f"<blockquote><i>🆔 <b>Current Chat Metadata</b>\n\n"
        f"• Chat ID: <code>{message.chat.id}</code>\n"
        f"• User ID: <code>{message.from_user.id if message.from_user else 'Channel'}</code>\n"
        f"• Chat Type: <code>{message.chat.type}</code></i></blockquote>"
    )
    await message.reply_text(id_msg)

def main():
    cleanup_workspace()
    print("[+] Optimized Multi-Channel Authorized Downloader Engine Started...")
    app.run()

if __name__ == "__main__":
    main()
