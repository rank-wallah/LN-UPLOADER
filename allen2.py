import os
import sys
import json
import time
import subprocess
import requests
from pyrogram import Client

# ==========================================
# ENVIRONMENT CONFIGURATION
# ==========================================
TG_API_ID = int(os.getenv("TG_API_ID", "33020321"))
TG_API_HASH = os.getenv("TG_API_HASH", "d870b78e4b663aad6eb358fbdec2807d")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "8991581483:AAHDBtqMfJjckOPpnijBpsNNknvQAh_xOwY")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "@JEELeaderOnlineCourse")
FRIENDS_BEARER_TOKEN = os.getenv("FRIENDS_BEARER_TOKEN", "")

# Initialize Pyrogram Bot Client with high-speed workers
app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=16
)

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
    if not os.path.exists(json_file):
        print(f"[-] File not found: {json_file}")
        return

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs("./downloads", exist_ok=True)

    for index, item in enumerate(data, start=1):
        title = item.get("title", f"Video_{index}")
        m3u8_url = item.get("url")

        if not m3u8_url:
            continue

        clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()
        output_file_path = f"./downloads/{clean_title}.mp4"

        try:
            downloaded_path = download_m3u8(m3u8_url, clean_title)
            upload_to_telegram(app, downloaded_path, f"**{title}**")
            
            if os.path.exists(downloaded_path):
                os.remove(downloaded_path)
        except Exception as e:
            print(f"\n[-] Error processing {title}: {str(e)}")

def main():
    print("[+] Starting Allen Downloader Worker Process...")
    app.start()
    
    # Process target JSON configurations sequentially
    for subject_json in ["physics.json", "chemistry.json", "maths.json"]:
        print(f"\n==========================================")
        print(f"[+] Processing JSON targets: {subject_json}")
        print(f"==========================================")
        process_subject(subject_json)

    print("\n[+] All tasks finished. Execution completed.")
    app.stop()

if __name__ == "__main__":
    main()
