import os
import sys
import json
import time
import gc
import shutil
import asyncio
import requests
import subprocess
import logging
import uuid
import socket
import re
import html
from pyrogram import Client, filters, enums
from pyrogram.types import Message

# ==========================================
# LOGGING & ENVIRONMENT CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "6789039689"))
ALLEN_USERNAME = os.getenv("ALLEN_USERNAME", "").strip()
ALLEN_PASSWORD = os.getenv("ALLEN_PASSWORD", "").strip()
ALLEN_ACCESS_TOKEN = os.getenv("ALLEN_ACCESS_TOKEN", "").strip()

if not TG_BOT_TOKEN:
    logger.warning("TG_BOT_TOKEN is empty! Pyrogram will hang in CMD waiting for manual input.")

AUTH_FILE = "authorized_users.json"
SESSION_FILE = "allen_session.json"
DOWNLOAD_DIR = "./downloads"
RUNTIME_ALLEN_TOKEN = ""

def load_authorized_users():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                return set(int(u) for u in json.load(f))
        except Exception as e:
            logger.error(f"Error loading auth file: {e}")
    return {OWNER_ID}

def save_authorized_users(users_set):
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(list(users_set), f)
    except Exception as e:
        logger.error(f"Error saving auth file: {e}")

AUTHORIZED_USERS = load_authorized_users()

def is_user_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in AUTHORIZED_USERS

# ==========================================
# SESSION MANAGEMENT (TOKEN & CREDS)
# ==========================================
def save_allen_session(data):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving session: {e}")

def get_allen_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading session: {e}")
    return {}

def get_allen_token():
    if RUNTIME_ALLEN_TOKEN:
        return RUNTIME_ALLEN_TOKEN
    session = get_allen_session()
    return session.get("access_token") or session.get("token") or ALLEN_ACCESS_TOKEN

def get_allen_device_id():
    """Use one stable device identity for login and every authenticated request."""
    session = get_allen_session()
    device_id = session.get("device_id") or os.getenv("ALLEN_DEVICE_ID", "").strip()
    if not device_id:
        # Stable across Heroku deploys without exposing the bot token itself.
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"allen-bot:{TG_BOT_TOKEN}"))
        session["device_id"] = device_id
        save_allen_session(session)
    return device_id

def allen_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": get_allen_device_id(),
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Origin": "https://allen.in",
        "Referer": "https://allen.in/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": 'Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": "Windows",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "X-Client-Type": "web",
        "X-Locale": "en",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    }

def allen_content_headers(token):
    """Match Allen web's content-request fingerprint exactly.

    X-Device-Id belongs to login/session requests. Sending it to pages/getPage
    can make that endpoint return HTTP 200 with an empty widgets list.
    """
    headers = allen_headers(token).copy()
    headers.pop("X-Device-Id", None)
    # Allen's navigation renderer content-negotiates these headers. Removing
    # Accept/Accept-Language makes /library-web return a misleading 404 even
    # with a valid token, so preserve the browser fingerprint exactly.
    return headers

def fetch_student_info(token):
    """Fetch student profile + enrolled courses/batches from Allen Digital."""
    r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                     headers=allen_content_headers(token), timeout=25)
    if r.status_code == 401:
        if ALLEN_USERNAME and ALLEN_PASSWORD:
            token = allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
            r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_content_headers(token), timeout=25)
        if r.status_code == 401:
            raise ValueError("Allen session expire hai aur automatic login configure nahi hai.")
    data = r.json()
    if data.get("status") != 200 or not data.get("data"):
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    return data["data"]

def get_or_login_allen_token(force_login=False):
    """Restore a configured token, or sign in automatically after a restart/expiry."""
    token = None if force_login else get_allen_token()
    if token:
        check = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_content_headers(token), timeout=25)
        if check.status_code != 401:
            return token
    if ALLEN_USERNAME and ALLEN_PASSWORD:
        return allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
    raise ValueError(
        "Allen session saved nahi hai. Permanent login ke liye Heroku Config Vars me "
        "ALLEN_USERNAME aur ALLEN_PASSWORD set karo, phir redeploy karo.")

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=8,
    parse_mode=enums.ParseMode.HTML
)

ACTIVE_JOBS = {}
MAX_TG_MSG_LEN = 4000
ALLEN_BASE_URL = "https://api.allen-live.in/api/v1"

def cleanup_workspace():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for item in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, item)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"Cleanup lock on {item} (ignoring): {e}")

# ==========================================
# ID * PASS RECON & AUTHENTICATION ENGINE
# ==========================================

def _host_resolves(url):
    try:
        host = url.split('//', 1)[1].split('/')[0]
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


def allen_login_idpass(username, password):
    """Authenticate via ALLEN Digital API (api.allen-live.in).

    Endpoint: POST /api/v1/auth/username
    Requires a DeviceID (uuid) in both header and payload.
    Success: {"status":200,"data":{"access_token":...,"refresh_token":...}}
    """
    username = str(username).strip()
    password = str(password).strip()
    if not username or not password:
        raise ValueError("Username and password cannot be empty")

    device_id = get_allen_device_id()
    url = "https://api.allen-live.in/api/v1/auth/username"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://allen.in",
        "Referer": "https://allen.in/",
        "X-Device-Id": device_id,
    }
    payload = {
        "username": username,
        "password": password,
        "persona_type": "STUDENT",
        "identity_type": "FORM_ID",
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=20)
    except Exception as e:
        raise ValueError(f"Login failed (network error): {e}")
    try:
        data = res.json()
    except Exception:
        data = {}
    inner = data.get("data") or {}
    token = (data.get("access_token") or data.get("token")
             or inner.get("access_token") or inner.get("token") or inner.get("accessToken")
             or res.headers.get("X-ACCESS-TOKEN") or res.headers.get("x-access-token"))
    refresh = (data.get("refresh_token") or inner.get("refresh_token") or inner.get("refreshToken")
               or res.headers.get("X-REFRESH-TOKEN") or res.headers.get("x-refresh-token") or "")
    if res.status_code == 200 and token:
        global ALLEN_BASE_URL, RUNTIME_ALLEN_TOKEN
        ALLEN_BASE_URL = "https://api.allen-live.in/api/v1"
        RUNTIME_ALLEN_TOKEN = token
        session = {"access_token": token, "token": token,
                   "refresh_token": refresh,
                   "device_id": device_id,
                   "username": username, "host": "api.allen-live.in",
                   "login_at": int(time.time())}
        save_allen_session(session)
        return token
    reason = data.get("reason") or data.get("message") or res.text[:200]
    raise ValueError(f"Login failed (HTTP {res.status_code}): {reason}")



# ==========================================
# BATCH CONTENT DISCOVERY (pages/getPage API)
# ==========================================
from urllib.parse import urlencode

ALLEN_PAGE_URL = "https://api.allen-live.in/api/v1/pages/getPage"
ALLEN_TAXONOMY = os.getenv("ALLEN_TAXONOMY", "1739171216OJ")
ALLEN_SUBJECTS = [("Physics", "1160"), ("Chemistry", "746"), ("Mathematics", "1264")]
SUBJECT_ALIASES = {
    "p": "Physics", "phy": "Physics", "physics": "Physics",
    "c": "Chemistry", "chem": "Chemistry", "chemistry": "Chemistry",
    "m": "Mathematics", "math": "Mathematics", "maths": "Mathematics",
    "mathematics": "Mathematics",
}
# speed tuning (env-overridable)
DL_THREADS = os.getenv("DL_THREADS", "48")
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3"))


def normalize_subject(raw):
    """'phy' -> 'Physics'; 'all'/None -> None (= all subjects)."""
    if not raw:
        return None
    key = str(raw).strip().lower()
    if key in ("all", "*", "sab"):
        return None
    if key not in SUBJECT_ALIASES:
        raise ValueError("Subject galat hai. Use: physics / chemistry / maths / all")
    return SUBJECT_ALIASES[key]
DONE_FILE = "uploaded_contents.json"


def load_done():
    if os.path.exists(DONE_FILE):
        try:
            with open(DONE_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def mark_done(content_id, done_set):
    done_set.add(content_id)
    try:
        with open(DONE_FILE, "w") as f:
            json.dump(list(done_set), f)
    except Exception as e:
        logger.warning(f"Could not persist progress: {e}")


# ---- Uploaded-message tracking + auto-delete on new login ----
UPLOAD_LOG_FILE = "uploaded_messages.json"


def load_upload_log():
    if os.path.exists(UPLOAD_LOG_FILE):
        try:
            with open(UPLOAD_LOG_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): [int(m) for m in v if m] for k, v in data.items()
                        if isinstance(v, list)}
        except Exception:
            pass
    return {}


def save_upload_log(log):
    try:
        with open(UPLOAD_LOG_FILE, "w") as f:
            json.dump(log, f)
    except Exception as e:
        logger.warning(f"Could not persist upload log: {e}")


def log_uploaded_message(chat_id, message_id):
    if not message_id:
        return
    log = load_upload_log()
    bucket = log.setdefault(str(chat_id), [])
    if message_id not in bucket:
        bucket.append(message_id)
    save_upload_log(log)


async def purge_uploaded_messages(client):
    """Naya /login => pehle upload kiye saare channel messages delete (revoke=True)."""
    log = load_upload_log()
    total = 0
    for chat_id, ids in log.items():
        if not chat_id.lstrip("-").isdigit() or not ids:
            continue
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await client.delete_messages(int(chat_id), chunk, revoke=True)
                total += len(chunk)
            except Exception as e:
                logger.warning(f"Could not delete uploads in chat {chat_id}: {e}")
    for stale in (UPLOAD_LOG_FILE, DONE_FILE):
        try:
            os.remove(stale)
        except OSError:
            pass
    return total


def build_caption(item, file_path, duration=0):
    """Caption format:
    [🎥]Vid Id : <content_id>
    File Title : <lecture title + real extension>
    Batch Name : <course name>
    Topic Name : <Subject - Topic>
    Extracted By ➤ Courier Well"""
    raw = str(item.get("title") or "Lecture")
    m = re.match(r"^\[[^\]]+\]\s*", raw)
    file_title = raw[m.end():].strip() if m else raw.strip()
    file_title = re.sub(r'[\\/:*?"<>|]+', " - ", file_title).strip() or "Lecture"
    ext = os.path.splitext(file_path)[1] or ""
    file_title = f"{file_title}{ext}"

    vid = str(item.get("id") or "").strip()
    if vid.startswith("http"):
        vid = vid.split("/")[-1].split("?")[0] or vid

    batch = str(item.get("batch") or "Allen Batch").strip() or "Allen Batch"
    subject = str(item.get("subject") or "").strip()
    topic = str(item.get("topic") or "").strip()
    if topic and topic != subject:
        topic_line = f"{subject} - {topic}" if subject else topic
    else:
        topic_line = subject or topic or "Topic"

    lines = [f"File Title : {file_title}"]
    lines += [f"Batch Name : {batch}", f"Topic Name : {topic_line}", "Extracted By ➤ Courier Well"]
    caption = "\n".join(lines)
    return html.escape(caption)


def allen_get_page(page_url, token):
    r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                      headers=allen_content_headers(token), timeout=30)
    if r.status_code == 401:
        if ALLEN_USERNAME and ALLEN_PASSWORD:
            token = allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
            r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                              headers=allen_content_headers(token), timeout=30)
        if r.status_code == 401:
            raise ValueError("Allen session expire hai aur automatic login configure nahi hai.")
    try:
        data = r.json()
    except Exception:
        raise ValueError(f"Allen page error (HTTP {r.status_code}): {r.text[:150]}")
    if data.get("status") != 200:
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    page_data = data.get("data") or {}
    widgets = ((page_data.get("page_content") or {}).get("widgets")
               if isinstance(page_data, dict) else None)
    if not widgets:
        logger.warning(
            "Allen empty page: http=%s status=%s reason=%s page_url=%s data_keys=%s",
            r.status_code, data.get("status"), data.get("reason"), page_url,
            list(page_data.keys()) if isinstance(page_data, dict) else [])
    return page_data


def _content_section(action):
    """Turn Allen's card_type into a short, stable channel/index section name."""
    current = ((action.get("tracking_params") or {}).get("current") or {})
    card_type = str(current.get("card_type") or "").lower()
    if "live lecture" in card_type or "class notes" in card_type:
        return "Live Lectures & Notes"
    if "concept video" in card_type:
        return "Concept Videos"
    if "race" in card_type:
        return "RACE & Solutions"
    if "exercise" in card_type or "rpp" in card_type:
        return "Exercises & Solutions"
    if "study module" in card_type:
        return "Study Modules"
    return "Other Material"


def _walk_page(node, contents, chapters):
    """Recursively collect playable contents and chapter/topic links from a page tree."""
    if isinstance(node, dict):
        for key in ("content_action", "card_action"):
            action = node.get(key)
            if isinstance(action, dict):
                d = action.get("data") or {}
                uri, title = d.get("uri"), d.get("title")
                if uri and title and str(uri).startswith("http"):
                    subtitle = node.get("subtitle") or ""
                    name = f"{str(subtitle)[:10]} {title}".strip() if subtitle else title
                    contents.append({"id": d.get("content_id") or uri, "title": name, "url": uri,
                                      "section": _content_section(action)})
        action = node.get("action")
        if isinstance(action, dict):
            action_data = action.get("data") or {}
            q = action_data.get("query") or {}
            cur = (action.get("tracking_params") or {}).get("current") or {}
            if q.get("topic_id"):
                # Allen embeds the authoritative topic request in every chapter
                # card.  Its batch_id can differ from the request that opened the
                # subject page, so preserve and replay it instead of guessing.
                query = {str(k): str(v) for k, v in q.items() if v is not None}
                topic_uri = action_data.get("uri") or "/topic-details"
                chapters.append({"topic_id": str(q["topic_id"]),
                                 "topic_name": cur.get("topic_name") or "Topic",
                                 "subject_id": cur.get("subject_id") or q.get("subject_id"),
                                 "page_url": str(topic_uri) + "?" + urlencode(query)})
        for value in node.values():
            _walk_page(value, contents, chapters)
    elif isinstance(node, list):
        for value in node:
            _walk_page(value, contents, chapters)


def _allen_internal_urls(node, wanted_uri=None, with_labels=False):
    """Collect exact internal page URLs emitted by Allen's page renderer."""
    found = []
    if isinstance(node, dict):
        for key in ("action", "content_action", "card_action"):
            action = node.get(key)
            if not isinstance(action, dict):
                continue
            data = action.get("data") or {}
            uri = data.get("uri")
            query = data.get("query")
            if isinstance(uri, str) and uri.startswith("/"):
                if wanted_uri is None or uri.split("?", 1)[0] == wanted_uri:
                    if isinstance(query, dict) and query:
                        clean = {str(k): str(v) for k, v in query.items() if v is not None}
                        page_url = uri.split("?", 1)[0] + "?" + urlencode(clean)
                    else:
                        page_url = uri
                    if with_labels:
                        tracking = action.get("tracking_params") or {}
                        current = tracking.get("current") or {}
                        label = (current.get("subject_name") or data.get("title") or
                                 node.get("title") or node.get("name") or "")
                        found.append({"url": page_url, "label": str(label)})
                    else:
                        found.append(page_url)
        for value in node.values():
            found.extend(_allen_internal_urls(value, wanted_uri, with_labels))
    elif isinstance(node, list):
        for value in node:
            found.extend(_allen_internal_urls(value, wanted_uri, with_labels))
    if with_labels:
        unique = {}
        for item in found:
            unique[item["url"]] = item
        return list(unique.values())
    return list(dict.fromkeys(found))


def _discover_subject_urls(token):
    """Ask Allen's own library pages for authoritative course/subject URLs."""
    discovered, queue, visited = [], ["/library-web", "/explore"], set()
    while queue and len(visited) < 8:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            page = allen_get_page(page_url, token)
        except Exception as exc:
            logger.warning("Allen navigation discovery skipped %s: %s", page_url, exc)
            continue
        discovered.extend(_allen_internal_urls(page, "/subject-details", with_labels=True))
        for next_url in _allen_internal_urls(page):
            path = next_url.split("?", 1)[0]
            if path in ("/library-web", "/library", "/explore") and next_url not in visited:
                queue.append(next_url)
    unique = {}
    for item in discovered:
        unique[item["url"]] = item
    return list(unique.values())


def _query_value(page_url, key):
    from urllib.parse import parse_qs, urlsplit
    values = parse_qs(urlsplit(page_url).query).get(key) or []
    return values[0] if values else ""


def _course_params(batch_ids, selected_batches, course_id, stream, subject_id,
                   topic_id=None, taxonomy_id=None):
    params = {
        "batch_id": ",".join(batch_ids),
        "selected_batch_list": ",".join(selected_batches),
        "selected_course_id": course_id,
        "stream": stream,
        "subject_id": subject_id,
        "taxonomy_id": taxonomy_id or ALLEN_TAXONOMY,
    }
    if topic_id:
        params["topic_id"] = topic_id
    return urlencode(params)


def _taxonomy_candidates(info, course):
    """Return account/course taxonomy IDs, with the known web value as fallback."""
    # Try the known working web taxonomy before any optional account metadata.
    found = [ALLEN_TAXONOMY, "1739171216OJ"]

    def scan(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower().replace("_", "") in ("taxonomy", "taxonomyid"):
                    if child and isinstance(child, (str, int)):
                        found.append(str(child))
                elif isinstance(child, (dict, list)):
                    scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)

    scan(course)
    scan(info.get("student_detail") or {})
    return list(dict.fromkeys(v for v in found if v))


def fetch_batch_contents(batch_id=None, token=None, subject=None):
    """All downloadable items of one batch, or of ALL purchased batches when batch_id is None/'all'."""
    if not token:
        raise ValueError("No token. /login ya /token pehle karo.")
    info = fetch_student_info(token)
    student = info.get("student_detail") or {}
    student_stream = student.get("stream") or ""
    courses = info.get("course_details") or []

    wants_all = (not batch_id) or str(batch_id).strip().lower() in ("all", "*")
    if not wants_all:
        matched = [c for c in courses if batch_id in
                   (list(c.get("enrolled_batches") or []) + list(c.get("unenrolled_batches") or []))]
        if not matched:
            known = sum((list(c.get("enrolled_batches") or []) for c in courses), [])
            raise ValueError(
                "Batch ID account ke enrolled batches me nahi mila. /mybatches se ✅ wala exact ID copy karo. "
                f"Found {len(known)} enrolled batch(es).")
        courses = matched

    items, seen = [], set()
    # The renderer can return account-specific batch subsets that are not present
    # as a simple enrolled/unenrolled combination. Prefer those server-generated
    # URLs and keep constructed URLs only as compatibility fallbacks.
    discovered_subject_urls = _discover_subject_urls(token)
    logger.info("Allen navigation discovery found %s subject URL(s)", len(discovered_subject_urls))

    def _add(prefix, found, batch_name, subject, topic):
        for it in found:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append({"id": it["id"], "title": f"{prefix} {it['title']}".strip(), "url": it["url"],
                          "batch": batch_name, "subject": subject, "topic": topic,
                          "section": it.get("section") or "Other Material"})

    for course in courses:
        enrolled_batches = list(dict.fromkeys(str(v) for v in (course.get("enrolled_batches") or []) if v))
        unenrolled_batches = list(dict.fromkeys(str(v) for v in (course.get("unenrolled_batches") or []) if v))
        selected_batches = list(dict.fromkeys(enrolled_batches + unenrolled_batches))
        if not selected_batches:
            continue
        # Match Allen's working web downloader first: both fields receive the
        # complete course batch list and the stream comes from student_detail.
        # Narrower variants are fallbacks for accounts whose page configuration
        # is scoped to an enrolled or explicitly selected batch.
        batch_variants = [(selected_batches, selected_batches)]
        if enrolled_batches:
            batch_variants.append((enrolled_batches, selected_batches))
        if not wants_all:
            chosen = [str(batch_id)]
            batch_variants.append((chosen, selected_batches))
            batch_variants.append((chosen, chosen))
        # Keep order while removing duplicate request variants.
        batch_variants = list(dict.fromkeys(
            (tuple(batch_ids), tuple(selected_list))
            for batch_ids, selected_list in batch_variants
        ))
        cname = course.get("course_name") or "Course"
        cid = course.get("course_id") or ""
        # Current studentInfo responses can expose the API enum on the student
        # and a display value on the course. Try both rather than assuming one.
        stream_variants = list(dict.fromkeys(str(v) for v in (
            student_stream, course.get("stream_enum_name"),
            course.get("stream"), course.get("stream_name")
        ) if v))
        if not stream_variants:
            stream_variants = [""]
        for sname, sid in ALLEN_SUBJECTS:
            if subject and sname != subject:
                continue
            contents, chapters = [], []
            working_context = None
            last_error = None
            # Subject IDs and taxonomy IDs are account/session specific. Match
            # the server-provided subject label, then replay its exact URL.
            wanted_names = {sname.lower()}
            if sname == "Mathematics":
                wanted_names.add("maths")
            exact_urls = [item["url"] for item in discovered_subject_urls
                          if _query_value(item["url"], "selected_course_id") == str(cid)
                          and item.get("label", "").strip().lower() in wanted_names]
            if not wants_all:
                exact_urls = [url for url in exact_urls if str(batch_id) in
                              (_query_value(url, "batch_id") + "," +
                               _query_value(url, "selected_batch_list")).split(",")]
            for page_url in exact_urls:
                try:
                    page = allen_get_page(page_url, token)
                    trial_contents, trial_chapters = [], []
                    _walk_page(page, trial_contents, trial_chapters)
                    if trial_contents or trial_chapters:
                        contents, chapters = trial_contents, trial_chapters
                        working_context = ([], [], "", "")
                        break
                except Exception as e:
                    last_error = e
            if not working_context:
                for taxonomy_id in _taxonomy_candidates(info, course):
                    for stream in stream_variants:
                        for batch_ids_tuple, selected_list_tuple in batch_variants:
                            batch_ids, selected_list = list(batch_ids_tuple), list(selected_list_tuple)
                            try:
                                page = allen_get_page("/subject-details?" + _course_params(
                                    batch_ids, selected_list, cid, stream, sid,
                                    taxonomy_id=taxonomy_id), token)
                                trial_contents, trial_chapters = [], []
                                _walk_page(page, trial_contents, trial_chapters)
                                if trial_contents or trial_chapters:
                                    contents, chapters = trial_contents, trial_chapters
                                    working_context = (batch_ids, selected_list, taxonomy_id, stream)
                                    break
                            except Exception as e:
                                last_error = e
                        if working_context:
                            break
                    if working_context:
                        break
            if not working_context:
                logger.warning(f"subject {sname} returned no chapters/content: {last_error or 'empty page'}")
                continue
            _add(f"[{cname} | {sname}]", contents, cname, sname, sname)
            batch_ids, selected_list, taxonomy_id, stream = working_context
            seen_chapters = set()
            for ch in chapters:
                chapter_key = ch.get("page_url") or ch.get("topic_id")
                if not chapter_key or chapter_key in seen_chapters:
                    continue
                seen_chapters.add(chapter_key)
                # Prefer the exact URL returned by Allen in the subject page.
                # Reconstruct only for older response shapes that omit it.
                topic_page_url = ch.get("page_url") or ("/topic-details?" + _course_params(
                    batch_ids, selected_list, cid, stream,
                    ch.get("subject_id") or sid, ch["topic_id"], taxonomy_id))
                try:
                    tpage = allen_get_page(topic_page_url, token)
                except Exception as e:
                    logger.warning(f"topic {ch.get('topic_name')} skipped: {e}")
                    continue
                tcontents, tch = [], []
                _walk_page(tpage, tcontents, tch)
                _add(f"[{sname} | {ch.get('topic_name')}]", tcontents, cname, sname, ch.get("topic_name"))
    return items


def download_file(url, output_path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return output_path


def _find_output(work_dir, output_name):
    best = None
    for file in os.listdir(work_dir):
        if file.startswith(output_name) and file.lower().endswith((".mp4", ".mkv", ".ts", ".m4a")):
            p = os.path.join(work_dir, file)
            if os.path.isfile(p) and os.path.getsize(p) > 0 and (not best or os.path.getsize(p) > os.path.getsize(best)):
                best = p
    return best


RUNNING_PROCS = {}  # work_dir -> set(Popen) so /stop can kill running downloads/encodes
STOPPED_DIRS = set()


def _run(cmd, timeout, work_dir):
    """subprocess.run replacement that /stop can kill instantly."""
    key = os.path.abspath(work_dir)
    if key in STOPPED_DIRS:
        raise RuntimeError("Stopped by user")
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "0"
    env["NO_COLOR"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, env=env)
    RUNNING_PROCS.setdefault(key, set()).add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        RUNNING_PROCS.get(key, set()).discard(proc)
    if key in STOPPED_DIRS:
        raise RuntimeError("Stopped by user")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill_job_processes(work_dir):
    key = os.path.abspath(work_dir)
    STOPPED_DIRS.add(key)
    for proc in list(RUNNING_PROCS.get(key, set())):
        try:
            proc.kill()
        except Exception:
            pass


def _fmt_duration(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s_ = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s_:02d}"


def probe_video(path):
    """Return (duration_seconds, width, height) via ffprobe."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        dur = float((d.get("format") or {}).get("duration") or 0)
        return int(dur), int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0, 0, 0


def download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    """Allen CDN URLs are already signed (hdnts). Sending the API Bearer token
    to the CDN makes it reject the request, so it is NOT sent. Falls back to
    ffmpeg if N_m3u8DL-RE fails."""
    work_dir = work_dir or DOWNLOAD_DIR
    os.makedirs(work_dir, exist_ok=True)
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36"
    cmd = [
        "N_m3u8DL-RE", m3u8_url,
        "--save-name", output_name,
        "--save-dir", work_dir,
        "--tmp-dir", work_dir,
        "-sv", "best", "-sa", "best",
        "-M", "format=mp4",
        "--thread-count", str(DL_THREADS),
        "--download-retry-count", "10",
        "--del-after-done",
        "--no-log",
        "-H", f"User-Agent: {ua}",
        "-H", "Origin: https://allen.in",
        "-H", "Referer: https://allen.in/",
    ]
    err = ""
    for attempt in range(2):
        try:
            r = _run(cmd, 3600, work_dir)
            out = _find_output(work_dir, output_name)
            if r.returncode == 0 and out:
                return out
            err = (r.stderr or r.stdout or "")[-300:]
            logger.warning(f"N_m3u8DL-RE failed ({r.returncode}) try {attempt+1}: {err}")
        except Exception as e:
            if "Stopped by user" in str(e):
                raise
            err = str(e)
            logger.warning(f"N_m3u8DL-RE error: {e}")

    # Fallback: ffmpeg direct copy (with reconnect), 2 tries
    out_path = os.path.join(work_dir, f"{output_name}.mp4")
    fcmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10",
        "-user_agent", ua,
        "-headers", "Origin: https://allen.in\r\nReferer: https://allen.in/\r\n",
        "-i", m3u8_url,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        out_path,
    ]
    ferr = ""
    for attempt in range(2):
        r = _run(fcmd, 3600, work_dir)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        ferr = (r.stderr or r.stdout or "").strip()
    if "403" in ferr or "Forbidden" in ferr:
        raise RuntimeError("Download failed: lecture link expired (403). Batch dobara chalao, naya link milega.")
    raise RuntimeError(f"Download failed. RE: {err[-120:]} | ffmpeg: {ferr[-200:]}")

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    return await asyncio.to_thread(download_m3u8, m3u8_url, output_name, bearer_token, work_dir)

def prepare_video_for_upload(source_path):
    """Burn the channel watermark into the video and create its Telegram cover."""
    stem, _ = os.path.splitext(source_path)
    video_path = stem + ".watermarked.mp4"
    thumb_path = stem + ".cover.jpg"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    # A text watermark is independent of external logo URLs or expired media links.
    label = ("drawtext=fontfile=" + font + ":text=courierWell:"
             "fontcolor=white@0.80:fontsize=h/36:"
             "borderw=2:bordercolor=black@0.60:x=w-tw-24:y=24,"
             "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", source_path,
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", label,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
           "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", video_path]
    try:
        result = _run(cmd, 7200, os.path.dirname(source_path))
        if result.returncode != 0 or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
            # Retry once with plain settings before giving up.
            simple = ["ffmpeg", "-y", "-loglevel", "error", "-i", source_path,
                      "-vf", label, "-c:v", "libx264", "-preset", "ultrafast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", video_path]
            result = _run(simple, 7200, os.path.dirname(source_path))
        if result.returncode != 0 or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
            raise RuntimeError(f"Video watermark failed: {result.stderr[-300:]}")
        # Extract a frame from the watermarked video: cover and video match.
        for seek in ("2", "0"):
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", seek,
                 "-i", video_path, "-frames:v", "1", "-vf", "scale=640:-2",
                 "-q:v", "3", thumb_path], capture_output=True, text=True, timeout=90)
            if result.returncode == 0 and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
                return video_path, thumb_path
        raise RuntimeError(f"Video thumbnail failed: {result.stderr[-300:]}")
    except Exception:
        for path in (video_path, thumb_path):
            if os.path.exists(path):
                os.remove(path)
        raise


TG_UPLOAD_LIMIT = 1950 * 1024 * 1024  # stay safely under Telegram's 2000 MB cap

def reduce_video_size(source_path, duration=0):
    """Re-encode only when the file is over the Telegram limit; sized to fit."""
    if not os.path.isfile(source_path) or os.path.getsize(source_path) <= TG_UPLOAD_LIMIT:
        return source_path
    if not duration:
        duration, _, _ = probe_video(source_path)
    if not duration or duration <= 0:
        duration = 3600
    # Target total bitrate so the result lands under the limit (90% safety margin).
    target_bits = TG_UPLOAD_LIMIT * 0.90 * 8
    total_kbps = int(target_bits / duration / 1000)
    audio_kbps = 96
    video_kbps = max(total_kbps - audio_kbps, 300)
    stem, _ = os.path.splitext(source_path)
    out_path = stem + ".reduced.mp4"
    for vf in ("scale='min(1280,iw)':-2", "scale=trunc(iw/2)*2:trunc(ih/2)*2"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", source_path,
               "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
               "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
               "-movflags", "+faststart", out_path]
        result = _run(cmd, 7200, os.path.dirname(source_path))
        if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            if os.path.getsize(out_path) <= TG_UPLOAD_LIMIT:
                return out_path
    if os.path.exists(out_path):
        os.remove(out_path)
    raise RuntimeError("Video 2000MB se badi hai aur compress karke bhi fit nahi hui.")


async def async_upload_to_telegram(app_client, target_chat_id, file_path, caption, thumb_path=None, duration=0, width=0, height=0):
    """Native async upload (tgcrypto) with FloodWait auto-retry."""
    from pyrogram.errors import FloodWait
    is_video = file_path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov"))
    for attempt in range(5):
        try:
            if is_video:
                msg = await app_client.send_video(chat_id=target_chat_id, video=file_path,
                                                  caption=caption, supports_streaming=True, thumb=thumb_path,
                                                  duration=duration or 0, width=width or 0, height=height or 0)
            else:
                msg = await app_client.send_document(chat_id=target_chat_id, document=file_path,
                                                     caption=caption)
            log_uploaded_message(target_chat_id, getattr(msg, "id", None))
            return msg
        except FloodWait as fw:
            wait = int(getattr(fw, "value", 10) or 10)
            logger.warning(f"FloodWait {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait + 1)
    raise RuntimeError("Telegram FloodWait: upload baar baar ruk raha hai")

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

@app.on_message(filters.command("auth") & filters.private)
async def handle_auth(client: Client, message: Message):
    logger.info(f"/auth triggered by {message.from_user.id}")
    if message.from_user.id != OWNER_ID:
        await message.reply_text("<blockquote><i>🚫 Owner-only command.</i></blockquote>")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/auth add &lt;user_id&gt;</code> or <code>/auth remove &lt;user_id&gt;</code></i></blockquote>")
        return

    action, uid_str = args[1].lower(), args[2]
    try:
        target_uid = int(uid_str)
        if action == "add":
            AUTHORIZED_USERS.add(target_uid)
            save_authorized_users(AUTHORIZED_USERS)
            await message.reply_text(f"<blockquote><i>✅ User <code>{target_uid}</code> Authorized!</i></blockquote>")
        elif action == "remove":
            AUTHORIZED_USERS.discard(target_uid)
            save_authorized_users(AUTHORIZED_USERS)
            await message.reply_text(f"<blockquote><i>❌ User <code>{target_uid}</code> Revoked!</i></blockquote>")
    except ValueError:
        await message.reply_text("<blockquote><i>⚠️ Invalid User ID.</i></blockquote>")


def _schedule_delete(chat_id, message_id, delay=20):
    """Delete a message after `delay` seconds (best-effort, never raises)."""
    async def _del():
        try:
            await asyncio.sleep(delay)
            await app.delete_messages(chat_id, message_id)
        except Exception as e:
            logger.warning(f"Auto-delete msg {message_id} failed: {e}")
    try:
        asyncio.get_running_loop().create_task(_del())
    except RuntimeError:
        pass

@app.on_message(filters.command("login") & (filters.group | filters.channel | filters.private))
async def handle_login(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    logger.info(f"/login triggered by {user_id}")

    if message.from_user and not is_user_authorized(message.from_user.id):
        logger.warning(f"Unauthorized access attempt by {user_id}")
        await message.reply_text("<blockquote><i>🚫 Access Denied. Contact Admin.</i></blockquote>")
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or "*" not in args[1]:
        await message.reply_text("<blockquote><i>⚠️ Format incorrect!\nUsage: <code>/login username*password</code></i></blockquote>")
        return

    username, password = (part.strip() for part in args[1].strip().split("*", 1))
    if not username or not password:
        await message.reply_text("<blockquote><i>⚠️ Username and password cannot be empty.</i></blockquote>")
        return
    try:
        await message.delete()  # credentials wali message turant hatao
    except Exception as e:
        logger.warning(f"/login command delete failed: {e}")
    status_msg = await message.reply_text("<blockquote><i>🔑 Authenticating directly with Allen Servers...</i></blockquote>")

    try:
        token = await asyncio.to_thread(allen_login_idpass, username, password)
        try:
            purged = await purge_uploaded_messages(app)
            logger.info(f"Auto-delete on new login: {purged} message(s) removed")
        except Exception as pe:
            logger.warning(f"Auto-delete failed: {pe}")
            purged = 0
        purged_note = f"\n🗑️ Purane {purged} uploads channel se delete ho gaye." if purged else ""
        await status_msg.edit_text(f"<blockquote><i>🔎 Login successful. Ab actual lectures verify ho rahe hain...{purged_note}</i></blockquote>")
        sample = await asyncio.to_thread(fetch_batch_contents, None, token, "Physics")
        if not sample:
            await status_msg.edit_text(
                "<blockquote><i>⚠️ <b>Login valid hai, lekin Allen ne lecture page empty bheja.</b>\n"
                "Session save ho gaya; dobara login karne ki zarurat nahi. "
                "Course mapping abhi match nahi hui.</i></blockquote>")
            _schedule_delete(status_msg.chat.id, status_msg.id)
            return
        await status_msg.edit_text(
            f"<blockquote><i>🎉 <b>Login + Lecture Test Successful!</b>\n\n"
            f"✅ {len(sample)} Physics items mile. Session saved hai.\n"
            "Ab <code>/mybatches</code> ya <code>/batch &lt;ID&gt; physics</code> chalao.</i></blockquote>")
        _schedule_delete(status_msg.chat.id, status_msg.id)
    except Exception as e:
        logger.error(f"Login pipeline failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Login Failed:</b>\n<code>{str(e)}</code></i></blockquote>")
        _schedule_delete(status_msg.chat.id, status_msg.id)


@app.on_message(filters.command("token") & (filters.group | filters.channel | filters.private))
async def handle_token(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    logger.info(f"/token triggered by {user_id}")

    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied. Contact Admin.</i></blockquote>")
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/token &lt;ALLEN_JWT_TOKEN&gt;</code></i></blockquote>")
        return

    token = args[1].strip()
    try:
        await message.delete()  # token wali message turant hatao
    except Exception as e:
        logger.warning(f"/token command delete failed: {e}")
    if token.count(".") != 2:
        await message.reply_text("<blockquote><i>⚠️ Ye valid JWT token nahi lagta. Poora token paste karo.</i></blockquote>")
        return

    try:
        # validate token against Allen API before saving
        def _check():
            r = requests.put(
                "https://api.allen-live.in/api/v1/user/profile",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={}, timeout=20,
            )
            return r
        r = await asyncio.to_thread(_check)
        if r.status_code == 401:
            await message.reply_text("<blockquote><i>❌ Token invalid ya expire ho chuka hai. Naya token nikaalo.</i></blockquote>")
            return

        session = {"access_token": token, "token": token, "username": "token-login",
                   "refresh_token": "", "device_id": get_allen_device_id(),
                   "host": "api.allen-live.in"}
        save_allen_session(session)
        ok_msg = await message.reply_text("<blockquote><i>🎉 <b>Token Saved! Session Active.</b>\n\nNow run: <code>/mybatches</code> to see your batches</i></blockquote>")
        _schedule_delete(ok_msg.chat.id, ok_msg.id)
    except Exception as e:
        logger.error(f"Token save failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Token Error:</b> <code>{str(e)}</code></i></blockquote>")


@app.on_message(filters.command("mybatches") & (filters.group | filters.channel | filters.private))
async def handle_mybatches(client: Client, message: Message):
    logger.info(f"/mybatches triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    status_msg = await message.reply_text("<blockquote><i>🔄 <b>Fetching your purchased batches...</b></i></blockquote>")
    try:
        token = await asyncio.to_thread(get_or_login_allen_token)
        info = await asyncio.to_thread(fetch_student_info, token)
        student = info.get("student_detail") or {}
        courses = info.get("course_details") or []
        if not courses:
            await status_msg.edit_text("<blockquote><i>❌ Is account me koi course/batch nahi mila.</i></blockquote>")
            return
        lines = [f"👤 <b>{student.get('first_name','')} {student.get('last_name','')}</b> ({student.get('stream_display_name','')})\n"]
        for c in courses:
            lines.append(f"📚 <b>{c.get('course_name','Course')}</b> (Course ID: <code>{c.get('course_id')}</code>)")
            lines.append(f"   🗓 {c.get('start_date','?')} → {c.get('end_date','?')} | Session: {c.get('session','?')}")
            for b in c.get("enrolled_batches") or []:
                lines.append(f"   ✅ Batch ID: <code>{b}</code>")
            for b in c.get("unenrolled_batches") or []:
                lines.append(f"   ➖ (unenrolled) <code>{b}</code>")
            lines.append("")
        lines.append("Poora batch: <code>/batch &lt;BATCH_ID&gt;</code>\nSubject-wise: <code>/batch &lt;BATCH_ID&gt; physics</code>\nSab ek saath: <code>/downloadall</code>\nHelp: <code>/subjects</code>")
        text = "\n".join(lines)
        if len(text) > MAX_TG_MSG_LEN:
            text = text[:MAX_TG_MSG_LEN] + "\n... (truncated)"
        await status_msg.edit_text(f"<blockquote>{text}</blockquote>")
    except Exception as e:
        logger.error(f"/mybatches failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Error:</b> <code>{str(e)}</code></i></blockquote>")

def _job_key(chat_id, batch_id, subject):
    return f"{chat_id}|{batch_id or 'ALL'}|{subject or 'ALL'}"


async def run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg):
    """Pipeline: parallel downloads feeding a single uploader -> max speed per job.
    Multiple jobs (different batches / channels) can run at the same time."""
    key = _job_key(target_chat_id, batch_id, subject)
    work_dir = os.path.join(DOWNLOAD_DIR, str(abs(hash(key)) % 10**8))
    STOPPED_DIRS.discard(os.path.abspath(work_dir))
    ACTIVE_JOBS[key] = {"running": True, "chat": message.chat.id, "work_dir": work_dir, "tasks": []}
    section_index = []  # (subject, chapter, section, message link)
    announced_sections = {}
    os.makedirs(work_dir, exist_ok=True)
    done = load_done()
    ok = failed = 0

    try:
        items = await asyncio.to_thread(fetch_batch_contents, batch_id, token, subject)
        pending = [i for i in items if i["id"] not in done]
        if not items:
            await status_msg.edit_text(
                "<blockquote><i>❌ Allen library me is batch/subject ka lecture link nahi mila. "
                "Login/session valid hai—dobara login mat karein. ✅ Batch ID ko <code>/mybatches</code> se check karein.</i></blockquote>")
            return
        if not pending:
            await status_msg.edit_text("<blockquote><i>✅ Ye sab pehle hi upload ho chuka hai.</i></blockquote>")
            return

        total = len(pending)
        await status_msg.edit_text(
            f"<blockquote><i>🚀 <b>{total} new items</b> (total {len(items)}) — turbo mode ON "
            f"({MAX_PARALLEL_DOWNLOADS} parallel downloads, {DL_THREADS} threads each)</i></blockquote>")

        UPLOAD_PARALLELISM = int(os.getenv("UPLOAD_PARALLELISM", "3"))
        queue = asyncio.Queue(maxsize=MAX_PARALLEL_DOWNLOADS + UPLOAD_PARALLELISM + 2)
        # Bound downloaded files too: a slow first lecture must not fill the disk
        # while later parallel downloads complete ahead of it.
        slots = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS + UPLOAD_PARALLELISM + 2)
        index_iter = iter(list(enumerate(pending, start=1)))
        lock = asyncio.Lock()

        def running():
            return ACTIVE_JOBS.get(key, {}).get("running", False)

        async def downloader():
            nonlocal failed
            while running():
                await slots.acquire()
                async with lock:
                    nxt = next(index_iter, None)
                if nxt is None:
                    slots.release()
                    return
                idx, item = nxt
                title = item.get("title") or f"Lecture_{idx}"
                clean = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip()[:70] or f"item_{idx}"
                clean = f"{idx}_{clean}"
                url = item.get("url")
                path = None
                try:
                    if not url:
                        raise ValueError("Lecture URL missing")
                    if ".m3u8" in url:
                        path = await async_download_m3u8(url, clean, token, work_dir)
                    else:
                        ext = os.path.splitext(url.split("?")[0])[1] or ".pdf"
                        path = await asyncio.to_thread(download_file, url, os.path.join(work_dir, clean + ext))
                except Exception as e:
                    if not running():
                        slots.release()
                        return
                    failed += 1
                    logger.error(f"Download failed ({title}): {e}")
                    await message.reply_text(
                        f"<blockquote><i>⚠️ Skipped: <b>{title}</b>\n<code>{str(e)[:150]}</code></i></blockquote>")
                # Even failures have an index, so the uploader can advance in order.
                await queue.put((idx, item, title, path))

        def item_group(item):
            subj = str(item.get("subject") or "").strip()
            topic = str(item.get("topic") or "").strip()
            chapter = topic or subj or "Chapter"
            section = str(item.get("section") or "Other Material").strip()
            return subj, chapter, section

        async def announce_group(item):
            group = item_group(item)
            if group in announced_sections:
                return
            subj, chapter, section = group
            batch_name = str(item.get("batch") or "Allen Batch").strip()
            heading = (f"<b>{html.escape(batch_name)}</b>\n"
                       f"📚 <b>{html.escape(subj)} - {html.escape(chapter)}</b>\n\n"
                       f"🔷 <b>{html.escape(section)}</b>")
            try:
                cm = await app.send_message(target_chat_id, heading)
                log_uploaded_message(target_chat_id, getattr(cm, "id", None))
                announced_sections[group] = getattr(cm, "link", None)
                section_index.append((subj, chapter, section, getattr(cm, "link", None)))
                try:
                    await cm.pin(disable_notification=True)
                except Exception as pe:
                    logger.warning(f"Section pin failed: {pe}")
            except Exception as ce:
                announced_sections[group] = None
                section_index.append((subj, chapter, section, None))
                logger.warning(f"Section header failed: {ce}")

        async def uploader():
            nonlocal ok, failed
            next_index = 1
            ready = {}
            in_flight = set()

            async def upload_one(idx, item, title, path):
                nonlocal ok, failed
                video_path = thumb_path = None
                try:
                    if path is None:
                        return
                    upload_path = path
                    if path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov")):
                        video_path, thumb_path = await asyncio.to_thread(prepare_video_for_upload, path)
                        upload_path = video_path
                    dur = w = h = 0
                    if upload_path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov")):
                        dur, w, h = await asyncio.to_thread(probe_video, upload_path)
                        if os.path.getsize(upload_path) > TG_UPLOAD_LIMIT:
                            reduced = await asyncio.to_thread(reduce_video_size, upload_path, dur)
                            if reduced != upload_path:
                                upload_path = reduced
                                video_path = reduced
                                dur, w, h = await asyncio.to_thread(probe_video, upload_path)
                    caption = build_caption(item, upload_path, dur)
                    await async_upload_to_telegram(app, target_chat_id, upload_path, caption, thumb_path, dur, w, h)
                    mark_done(item["id"], done)
                    ok += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Upload failed ({title}): {e}")
                    await message.reply_text(
                        f"<blockquote><i>\u26A0\uFE0F Upload fail: <b>{title}</b>\n<code>{str(e)[:150]}</code></i></blockquote>")
                finally:
                    for output in (path, video_path, thumb_path):
                        if output and os.path.exists(output):
                            try:
                                os.remove(output)
                            except Exception:
                                pass
                    slots.release()
                    gc.collect()

            while next_index <= total and running():
                try:
                    result = await asyncio.wait_for(queue.get(), timeout=5)
                    ready[result[0]] = result
                except asyncio.TimeoutError:
                    if all(d.done() for d in downloaders) and queue.empty():
                        break
                    continue
                while next_index in ready:
                    if len(in_flight) >= UPLOAD_PARALLELISM:
                        done_tasks, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                        in_flight -= done_tasks
                        continue
                    idx, item, title, path = ready.pop(next_index)
                    next_index += 1
                    group = item_group(item)
                    if group not in announced_sections:
                        # Finish the old section before pinning the next heading.
                        if in_flight:
                            await asyncio.gather(*in_flight, return_exceptions=True)
                            in_flight.clear()
                        await announce_group(item)
                    in_flight.add(asyncio.create_task(upload_one(idx, item, title, path)))
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

        downloaders = [asyncio.create_task(downloader()) for _ in range(MAX_PARALLEL_DOWNLOADS)]
        up = asyncio.create_task(uploader())
        ACTIVE_JOBS[key]["tasks"] = downloaders + [up]
        await asyncio.gather(*downloaders, return_exceptions=True)
        try:
            await up
        except asyncio.CancelledError:
            pass

        stopped = not running()
        if section_index:
            rows = []
            last_index_chapter = None
            chapter_no = 0
            for subj, chapter, section, link in section_index:
                chapter_key = (subj, chapter)
                if chapter_key != last_index_chapter:
                    chapter_no += 1
                    rows.append(f"\n<b>{chapter_no}. 📚 {html.escape(subj)} - {html.escape(chapter)}</b>")
                    last_index_chapter = chapter_key
                label = "🔷 " + html.escape(section)
                rows.append(f'   <a href="{link}">{label}</a>' if link else f"   {label}")
            head = "<b>📚 Batch Index</b>" + (" (stopped)" if stopped else "")
            chunk = head
            for row in rows:
                if len(chunk) + len(row) + 1 > 3900:
                    im = await app.send_message(target_chat_id, chunk, disable_web_page_preview=True)
                    log_uploaded_message(target_chat_id, getattr(im, "id", None))
                    chunk = head + " (contd.)"
                chunk += "\n" + row
            im = await app.send_message(target_chat_id, chunk, disable_web_page_preview=True)
            log_uploaded_message(target_chat_id, getattr(im, "id", None))
        if stopped:
            await message.reply_text(
                f"<blockquote><i>🛑 Stopped. Uploaded: {ok} | Failed: {failed}</i></blockquote>")
            return

        await message.reply_text(
            f"<blockquote><i>✅ <b>Done!</b> Uploaded: {ok} | Failed: {failed}\n"
            f"Scope: {batch_id or 'ALL batches'} | {subject or 'All subjects'}</i></blockquote>")
    except Exception as e:
        logger.error(f"Batch job failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Batch Error:</b> <code>{str(e)}</code></i></blockquote>")
    finally:
        ACTIVE_JOBS.pop(key, None)
        shutil.rmtree(work_dir, ignore_errors=True)


@app.on_message(filters.command(["batch", "downloadall"]) & (filters.group | filters.channel | filters.private))
async def handle_batch(client: Client, message: Message):
    logger.info(f"/batch triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    try:
        token = await asyncio.to_thread(get_or_login_allen_token)
    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <b>Session Error:</b> <code>{str(e)}</code></i></blockquote>")
        return

    # /batch <BATCH_ID> [subject] [-c <channel_id>]     (BATCH_ID optional = all batches)
    parts = (message.text or "").split()[1:]
    target_chat_id = message.chat.id
    if "-c" in parts:
        i = parts.index("-c")
        try:
            target_chat_id = int(parts[i + 1])
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
            return
        parts = parts[:i] + parts[i + 2:]

    batch_id, subject_raw = None, None
    for token_arg in parts:
        low = token_arg.lower()
        if low in SUBJECT_ALIASES or low in ("all", "*", "sab"):
            if subject_raw is None and not (low in ("all", "*") and batch_id is None):
                subject_raw = low
            elif batch_id is None:
                batch_id = None
        elif batch_id is None:
            batch_id = token_arg

    try:
        subject = normalize_subject(subject_raw)
    except ValueError as e:
        await message.reply_text(f"<blockquote><i>⚠️ {e}</i></blockquote>")
        return

    key = _job_key(target_chat_id, batch_id, subject)
    if ACTIVE_JOBS.get(key, {}).get("running"):
        await message.reply_text("<blockquote><i>⚠️ Ye job already chal raha hai.</i></blockquote>")
        return

    scope = f"{batch_id or 'ALL batches'} | {subject or 'All subjects'}"
    dest = "yahin" if target_chat_id == message.chat.id else f"<code>{target_chat_id}</code>"
    status_msg = await message.reply_text(
        f"<blockquote><i>🔄 <b>Fetching:</b> {scope}\nUpload → {dest}</i></blockquote>")

    # fire-and-forget so multiple batches/channels download simultaneously
    asyncio.create_task(run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg))


@app.on_message(filters.command("subjects") & (filters.group | filters.channel | filters.private))
async def handle_subjects(client: Client, message: Message):
    await message.reply_text(
        "<blockquote><i>📚 <b>Subject-wise download</b>\n\n"
        "<code>/batch &lt;BATCH_ID&gt; physics</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; chemistry</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; maths</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; all</code>  (poora batch)\n"
        "<code>/downloadall</code>  (saare batches, saare subjects)\n"
        "<code>/downloadall physics</code>\n\n"
        "Kisi channel me bhejna ho:\n"
        "<code>/batch &lt;BATCH_ID&gt; physics -c -100xxxxxxxxxx</code>\n"
        "(Bot ko us channel me admin banao. Alag-alag batches alag channels me ek saath chal sakte hain.)\n\n"
        "<code>/jobs</code> — chal rahe kaam dekho | <code>/stop</code> — sab rok do</i></blockquote>")


@app.on_message(filters.command("debug") & (filters.group | filters.channel | filters.private))
async def handle_debug(client: Client, message: Message):
    """Dump raw Allen responses into a file so the content format can be fixed."""
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    token = get_allen_token()
    if not token:
        await message.reply_text("<blockquote><i>⚠️ Pehle /login ya /token karo.</i></blockquote>")
        return
    parts = (message.text or "").split()[1:]
    want_batch = parts[0] if parts else None
    status = await message.reply_text("<blockquote><i>🔍 Debug data collect ho raha hai...</i></blockquote>")

    def _collect():
        out = {"student_info": None, "navigation_subject_urls": [], "pages": []}
        r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo", headers=allen_content_headers(token), timeout=25)
        try:
            info = r.json()
        except Exception:
            info = {"raw": r.text[:2000]}
        out["student_info"] = {"http": r.status_code, "body": info}
        out["navigation_subject_urls"] = _discover_subject_urls(token)
        data = (info or {}).get("data") or {}
        stream = (data.get("student_detail") or {}).get("stream") or ""
        courses = data.get("course_details") or []
        if want_batch:
            courses = [c for c in courses if want_batch in (c.get("enrolled_batches") or []) + (c.get("unenrolled_batches") or [])] or courses
        for c in courses[:1]:
            selected_batches = list(c.get("enrolled_batches") or []) + list(c.get("unenrolled_batches") or [])
            batch_ids = [want_batch] if want_batch else selected_batches
            for sname, sid in ALLEN_SUBJECTS:
                page_url = "/subject-details?" + _course_params(
                    batch_ids, selected_batches, c.get("course_id") or "", stream, sid)
                rr = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url}, headers=allen_content_headers(token), timeout=30)
                try:
                    body = rr.json()
                except Exception:
                    body = {"raw": rr.text[:3000]}
                out["pages"].append({"subject": sname, "page_url": page_url, "http": rr.status_code, "body": body})
        return out

    try:
        out = await asyncio.to_thread(_collect)
        txt = json.dumps(out, indent=1, ensure_ascii=False).replace(token, "<TOKEN>")
        # strip personal info
        for f in ("email", "phone", "dob"):
            txt = __import__("re").sub(rf'"{f}": "[^"]*"', f'"{f}": "***"', txt)
        path = os.path.join(DOWNLOAD_DIR, "allen_debug.json")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(txt)
        summary = "\n".join(f"{pg['subject']}: HTTP {pg['http']} | {str((pg['body'] or {}).get('reason',''))[:60]}" for pg in out["pages"]) or "No course found"
        await message.reply_document(path, caption=f"<blockquote><i>🧪 Debug file\n{summary}\n\nYe file Lovable chat me bhejo.</i></blockquote>")
        await status.delete()
        os.remove(path)
    except Exception as e:
        await status.edit_text(f"<blockquote><i>❌ Debug error: <code>{str(e)[:300]}</code></i></blockquote>")


@app.on_message(filters.command("jobs"))
async def handle_jobs(client: Client, message: Message):
    mine = [k for k, v in ACTIVE_JOBS.items() if v.get("running")]
    if not mine:
        await message.reply_text("<blockquote><i>💤 Koi job nahi chal raha.</i></blockquote>")
        return
    await message.reply_text("<blockquote><i>⚙️ <b>Running jobs:</b>\n" +
                             "\n".join(f"• <code>{k}</code>" for k in mine) + "</i></blockquote>")


@app.on_message(filters.command("stop"))
async def handle_stop(client: Client, message: Message):
    stopped = 0
    for k, v in list(ACTIVE_JOBS.items()):
        if v.get("running") and (v.get("chat") == message.chat.id or message.from_user and message.from_user.id == OWNER_ID):
            v["running"] = False
            if v.get("work_dir"):
                kill_job_processes(v["work_dir"])
            for t in v.get("tasks") or []:
                if not t.done():
                    t.cancel()
            stopped += 1
    if stopped:
        await message.reply_text(f"<blockquote><i>🛑 {stopped} job(s) rok diye.</i></blockquote>")
    else:
        await message.reply_text("<blockquote><i>⚠️ Koi active task nahi hai.</i></blockquote>")


@app.on_message(filters.command("id"))
async def show_id(client: Client, message: Message):
    await message.reply_text(f"<blockquote><i>🆔 Chat ID: <code>{message.chat.id}</code></i></blockquote>")


def main():
    # validate runtime environment and fail fast if missing
    missing = []
    if not TG_API_ID or TG_API_ID == 0:
        missing.append("TG_API_ID")
    if not TG_API_HASH:
        missing.append("TG_API_HASH")
    if not TG_BOT_TOKEN:
        missing.append("TG_BOT_TOKEN")
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    logger.info("Initializing workspace cleanup...")
    cleanup_workspace()
    if ALLEN_USERNAME and ALLEN_PASSWORD:
        try:
            token = get_or_login_allen_token()
            sample = fetch_batch_contents(None, token, "Physics")
            logger.info("Allen startup check passed: %s Physics items found", len(sample))
        except Exception as e:
            logger.error("Allen startup check failed: %s", e)
    logger.info("Workspace clean. Booting Pyrogram engine...")
    app.run()

if __name__ == "__main__":
    main()
