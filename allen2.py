import os
import sys
import json
import time
import gc
import shutil
import asyncio
import subprocess
from pyrogram import Client, filters
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
    workers=16
)

# In-memory session tracking per channel
ACTIVE_JOBS = {}
MAX_TG_MSG_LEN = 4000  # Safe margin below 4096 Telegram limit

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
    
    # Tuned for high throughput and network stability on cloud runners
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
        # Fallback search if extension differs
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(output_name):
                return os.path.join(DOWNLOAD_DIR, file)
                
    return output_path

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None):
    """Executes heavy download synchronously in thread pool to prevent blocking Pyrogram bot loop."""
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
    current_text = header + "\n"

    for idx, item in enumerate(index_items, start=1):
        line = f"> {idx}. ([{item['title']}]({item['link']}))\n"
        if len(current_text) + len(line) > MAX_TG_MSG_LEN:
            messages_to_send.append(current_text)
            current_text = "> \n" + line
        else:
            current_text += line

    if current_text.strip():
        messages_to_send.append(current_text)

    total_chunks = len(messages_to_send)
    last_sent_msg = None

    for i, msg_text in enumerate(messages_to_send, start=1):
        if total_chunks > 1:
            chunk_caption = f"{msg_text}\n> \n> _Part {i} of {total_chunks}_"
        else:
            chunk_caption = msg_text
        
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
            # Async non-blocking download
            downloaded_path = await async_download_m3u8(m3u8_url, clean_title, bearer_token)
            
            formatted_caption = (
                f"> *{title}*\n"
                f">\n"
                f"> _Uploaded via Downloader Engine_"
            )
            
            # Async non-blocking upload
            sent_msg = await async_upload_to_telegram(app, target_chat_id, downloaded_path, formatted_caption)
            msg_link = get_message_link(target_chat_id, sent_msg.id)

            if is_new_chapter or index == 1:
                try:
                    await app.pin_chat_message(target_chat_id, sent_msg.id)
                except Exception as pin_err:
                    print(f"[-] Pinning failed: {pin_err}")

            index_records.append({"title": title, "link": msg_link})
            
            # Immediate Garbage Collection & File Deletion for low RAM usage
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
            gc.collect()

        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

    # Send Master Index upon completion
    if index_records and ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        index_header = (
            f"> _📌 **BATCH MASTER INDEX**_\n"
            f">\n"
            f"> _Below is the complete list of chapters/topics processed in this batch with embedded direct post links:_\n"
            f">"
        )
        send_chunked_messages(app, target_chat_id, index_header, index_records, pin_last=True)

# ==========================================
# OWNER AUTH MANAGEMENT COMMANDS
# ==========================================

@app.on_message(filters.command("auth"))
async def authorize_user(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("> _🚫 Only the Bot Owner can authorize users._")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply_text("> _⚠️ Usage: `/auth <USER_ID>`_")
        return

    target_id = int(args[1])
    AUTHORIZED_USERS.add(target_id)
    save_authorized_users(AUTHORIZED_USERS)
    
    msg = (
        f"> _✅ **User Authorized Successfully**_\n"
        f">\n"
        f"> _User ID `{target_id}` has been granted full access to the bot._"
    )
    await message.reply_text(msg)

@app.on_message(filters.command("unauth"))
async def unauthorize_user(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("> _🚫 Only the Bot Owner can revoke authorizations._")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply_text("> _⚠️ Usage: `/unauth <USER_ID>`_")
        return

    target_id = int(args[1])
    if target_id == OWNER_ID:
        await message.reply_text("> _⚠️ You cannot revoke owner privileges._")
        return

    AUTHORIZED_USERS.discard(target_id)
    save_authorized_users(AUTHORIZED_USERS)
    
    msg = (
        f"> _🛑 **User Revoked Successfully**_\n"
        f">\n"
        f"> _User ID `{target_id}` access has been removed._"
    )
    await message.reply_text(msg)

@app.on_message(filters.command("authlist"))
async def list_authorized_users(client: Client, message: Message):
    if not is_user_authorized(message.from_user.id):
        await message.reply_text("> _🚫 Access Denied._")
        return

    users_str = "\n".join([f"> • `{u}`" + (" (Owner)" if u == OWNER_ID else "") for u in AUTHORIZED_USERS])
    list_msg = (
        f"> _📋 **Authorized Users List:**_\n"
        f">\n"
        f"{users_str}"
    )
    await message.reply_text(list_msg)

# ==========================================
# TELEGRAM BOT STYLED UI & COMMANDS
# ==========================================

@app.on_message(filters.command(["start", "help"]) & (filters.group | filters.channel | filters.private))
async def start_and_help_handler(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("> _🚫 **Access Denied**_\n>\n> _You are not authorized to use this bot. Contact the owner (`6789039689`) for access._")
        return

    ui_text = (
        f"> _⚡ **Multi-Channel Video Downloader Bot**_\n"
        f">\n"
        f"> _This engine processes `.json` batch files, downloads m3u8 streams using N_m3u8DL-RE, pins chapters/topics, auto-builds an index, and uploads high-speed MP4 videos directly to your target channel._\n"
        f">\n"
        f"> 🛠 *__Available Commands:__*\n"
        f">\n"
        f"> 1️⃣ `/batch <BEARER_TOKEN>`\n"
        f"> _Reply to or attach a `.json` file to start batch downloading with automatic indexing._\n"
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
        f"> 🔑 *__Admin Controls (Owner Only):__*\n"
        f"> _`/auth <USER_ID>` - Grant user access_\n"
        f"> _`/unauth <USER_ID>` - Revoke user access_\n"
        f"> _`/authlist` - List authorized users_\n"
        f">\n"
        f"> 📑 *__Index & Pinning System:__*\n"
        f"> _Every topic/chapter is tracked. Upon completion, a hyperlinked Index message is created and pinned formatted as `([TOPIC](https://t.me/c/...))`._"
    )
    
    await message.reply_text(
        text=ui_text,
        disable_web_page_preview=True
    )

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
async def handle_batch_upload(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("> _🚫 Access Denied._")
        return

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
        await message.reply_text(error_msg)
        return

    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        already_running_msg = (
            f"> _⚠️ **Task Already Active**_\n"
            f">\n"
            f"> _A batch task is currently running in this chat. Send `/stop` first to cancel it._"
        )
        await message.reply_text(already_running_msg)
        return

    json_path = await message.download()
    with open(json_path, "r", encoding="utf-8") as f:
        data_items = json.load(f)
    os.remove(json_path)

    ACTIVE_JOBS[target_chat_id] = {"running": True, "token": bearer_token}
    
    start_msg = (
        f"> _🚀 **Batch Process Triggered**_\n"
        f">\n"
        f"> _Target Chat ID: `{target_chat_id}`_\n"
        f"> _Total Queue Items: `{len(data_items)}`_\n"
        f"> _Index Auto-Generation: Enabled_\n"
        f"> _Status: Running..._"
    )
    await message.reply_text(start_msg)

    try:
        await process_batch_async(target_chat_id, data_items, bearer_token)
        complete_msg = (
            f"> _✅ **Batch Processing & Indexing Completed**_\n"
            f">\n"
            f"> _All items from the batch file were uploaded and pinned, and the master index has been published._"
        )
        await message.reply_text(complete_msg)
    finally:
        ACTIVE_JOBS[target_chat_id] = {"running": False, "token": None}

@app.on_message(filters.command("stop") & (filters.group | filters.channel | filters.private))
async def stop_batch_upload(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("> _🚫 Access Denied._")
        return

    target_chat_id = message.chat.id
    if ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
        ACTIVE_JOBS[target_chat_id]["running"] = False
        stop_msg = (
            f"> _🛑 **Cancellation Initiated**_\n"
            f">\n"
            f"> _Sent stop signal to active queue processing for this channel._"
        )
        await message.reply_text(stop_msg)
    else:
        no_task_msg = (
            f"> _⚠️ **No Active Task**_\n"
            f">\n"
            f"> _There are no active downloading tasks currently running in this chat._"
        )
        await message.reply_text(no_task_msg)

@app.on_message(filters.command("id"))
async def show_chat_id(client: Client, message: Message):
    id_msg = (
        f"> _🆔 **Current Chat Metadata**_\n"
        f">\n"
        f"> _Chat ID: `{message.chat.id}`_\n"
        f"> _User ID: `{message.from_user.id if message.from_user else 'Channel'}`_\n"
        f"> _Chat Type: `{message.chat.type}`_"
    )
    await message.reply_text(id_msg)

def main():
    cleanup_workspace()
    print("[+] Optimized Multi-Channel Authorized Downloader Engine Started...")
    app.run()

if __name__ == "__main__":
    main()
