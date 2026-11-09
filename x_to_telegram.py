"""
X (Twitter) home timeline -> Telegram channel forwarder.
Native photos/videos, full thread support (1/3, 1/, 🧵), long-post splitting, rate-limit aware.

Setup:
  pip install tweepy requests requests-toolbelt

Environment variables:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Run:
  python x_to_telegram.py
"""
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import tweepy
from requests_toolbelt import MultipartEncoder

STATE_FILE = Path("state.json")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
MIN_LIKES = int(os.getenv("MIN_LIKES", "0"))
THREAD_WAIT = int(os.getenv("THREAD_WAIT_MIN", "15")) * 60
MAX_THREAD_LOOKUPS = int(os.getenv("MAX_THREAD_LOOKUPS", "5"))
LIKES_WAIT = int(os.getenv("LIKES_WAIT_MIN", "60")) * 60
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))
MEDIA_ONLY = os.getenv("MEDIA_ONLY", "1") == "1"
SEND_CAPTIONS = os.getenv("SEND_CAPTIONS", "1") == "1"
MAX_VIDEO_BYTES = int(float(os.getenv("MAX_VIDEO_MB", "50")) * 1_000_000)

def need(name):
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing environment variable: {name}")
    return value

TG_API = f"https://api.telegram.org/bot{need('TELEGRAM_BOT_TOKEN')}"
TG_CHAT = need("TELEGRAM_CHAT_ID")
CAPTION_LIMIT = 1024
TEXT_LIMIT = 3000

FIELDS = dict(
    tweet_fields=["author_id", "created_at", "public_metrics", "attachments",
                  "referenced_tweets", "conversation_id", "note_tweet"],
    expansions=["author_id", "attachments.media_keys", "referenced_tweets.id",
                "referenced_tweets.id.author_id"],
    user_fields=["username", "name"],
    media_fields=["type", "url", "variants", "preview_image_url", "width", "height", "duration_ms"],
)

TL_POS = {}
REPOSTER = {}

# ---------------------------------------------------------------- state
def load_state():
    state = {"since_id": None, "pending": [], "fails": {}, "sent": [], "parts": []}
    try:
        loaded = json.loads(STATE_FILE.read_text())
        if isinstance(loaded, dict):
            state.update(loaded)
    except (OSError, ValueError):
        pass
    for key, kind in (("pending", list), ("fails", dict), ("sent", list), ("parts", list)):
        if not isinstance(state.get(key), kind):
            state[key] = kind()
    return state

def save_state(state):
    state["sent"] = state["sent"][-500:]
    state["parts"] = state["parts"][-500:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)

# ---------------------------------------------------------------- telegram
SESSION = requests.Session()

def retry_after(r):
    try:
        return int(r.json().get("parameters", {}).get("retry_after", 5))
    except (ValueError, TypeError, AttributeError):
        return 5

def check(r):
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text[:300]}")
    r.raise_for_status()

def tg(method, payload):
    while True:
        r = SESSION.post(f"{TG_API}/{method}", json={"chat_id": TG_CHAT, **payload}, timeout=60)
        if r.status_code == 429:
            time.sleep(retry_after(r) + 1)
            continue
        check(r)
        return r.json()

def send_text(text, preview=True):
    tg("sendMessage", {"text": text[:4096], "parse_mode": "HTML",
                       "disable_web_page_preview": not preview})

def tg_upload(method, field, path, payload):
    """Upload a local file to Telegram, streaming from disk so RAM use stays small."""
    while True:
        with open(path, "rb") as fh:
            fields = {"chat_id": TG_CHAT, **{k: str(v) for k, v in payload.items()},
                      field: (os.path.basename(path), fh, "video/mp4")}
            enc = MultipartEncoder(fields=fields)
            r = SESSION.post(f"{TG_API}/{method}", data=enc,
                             headers={"Content-Type": enc.content_type}, timeout=300)
        if r.status_code == 429:
            time.sleep(retry_after(r) + 1)
            continue
        check(r)
        return r.json()

def download_video(url):
    """Stream a video to a temp file. Returns its path, or None if it exceeds the size limit."""
    with SESSION.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        if int(r.headers.get("Content-Length") or 0) > MAX_VIDEO_BYTES:
            return None
        f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        size, ok = 0, False
        try:
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    break
                f.write(chunk)
            else:
                ok = True
        finally:
            f.close()
            if not ok:
                os.unlink(f.name)
        return f.name if ok else None

# ---------------------------------------------------------------- text helpers
def raw_text(tweet):
    note = (tweet.data or {}).get("note_tweet") or {}
    return note.get("text") or tweet.text

def clean(text):
    return re.sub(r"https://t\.co/\w+", "", text).strip()

def body_text(tweet, has_media):
    t = raw_text(tweet)
    if has_media:
        t = re.sub(r"\s*https://t\.co/\w+\s*$", "", t)
    return t.strip()

def split_text(text, size=TEXT_LIMIT):
    parts = []
    while len(text) > size:
        cut = size
        for sep in ("\n\n", "\n", ". ", " "):
            i = text.rfind(sep, 0, size)
            if i > size // 2:
                cut = i + (1 if sep == ". " else 0)
                break
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts

def split_escaped(text, limit=TEXT_LIMIT):
    size = limit
    while True:
        parts = split_text(text, size)
        if size <= 200 or all(len(html.escape(p)) <= limit for p in parts):
            return parts
        size = size * 2 // 3

IDX_START = re.compile(r"^\W{0,3}(\d{1,2})\s*/\s*(\d{1,2})?(?!\d)")
IDX_END = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})?\W{0,3}$")

def thread_info(tweet):
    t = clean(raw_text(tweet))
    for rx in (IDX_START, IDX_END):
        m = rx.search(t)
        if m and int(m.group(1)) == 1:
            total = int(m.group(2)) if m.group(2) else None
            if total is None or total >= 2:
                return True, total
    if "🧵" in t or re.search(r"thread[\s:👇⬇️↓]*$", t, re.I):
        return True, None
    return False, None

# ---------------------------------------------------------------- sending
def video_urls(media):
    variants = [v for v in (media.variants or []) if v.get("content_type") == "video/mp4" and v.get("url")]
    variants.sort(key=lambda v: v.get("bit_rate") or 0, reverse=True)
    return [v["url"] for v in variants]

def build_messages(tweet, users, has_media, header, note=""):
    pieces = split_escaped(body_text(tweet, has_media)) or [""]
    author = users.get(tweet.author_id)
    handle = author.username if author else "i"
    name = html.escape(author.name) if author else "Unknown"
    url = f"https://x.com/{handle}/status/{tweet.id}"
    likes = (tweet.public_metrics or {}).get("like_count", 0)
    reposter = REPOSTER.get(tweet.id)
    if header and reposter:
        note += f" · 🔁 @{reposter}"
    out = []
    for i, p in enumerate(pieces):
        s = html.escape(p)
        if header and i == 0:
            s = f"<b>{name}</b> (@{handle}){note}\n\n{s}"
        if header and i == len(pieces) - 1:
            s += f"\n\n❤️ {likes}  |  <a href=\"{url}\">Open on X</a>"
        out.append(s.strip())
    return out

def send_tweet(tweet, users, media_by_key, header=True, note=""):
    _send_tweet(tweet, users, media_by_key, header, note)
    time.sleep(2)

def _send_tweet(tweet, users, media_by_key, header=True, note=""):
    keys = (tweet.attachments or {}).get("media_keys", [])
    media = [media_by_key[k] for k in keys if k in media_by_key]
    photos = [m for m in media if m.type == "photo" and m.url]
    videos = [m for m in media if m.type in ("video", "animated_gif")]

    msgs = build_messages(tweet, users, bool(media), header, note) if (SEND_CAPTIONS or not media) else []
    single = len(msgs) == 1 and 0 < len(msgs[0]) <= CAPTION_LIMIT
    caption = {"caption": msgs[0], "parse_mode": "HTML"} if single else {}

    def send_remaining_text():
        if not single:
            for m in msgs:
                if m:
                    try:
                        send_text(m, preview=False)
                    except requests.RequestException as e:
                        print(f"Follow-up text for {tweet.id} failed: {e}")
                    time.sleep(0.5)

    try:
        if photos:
            urls = [m.url + "?name=large" for m in photos[:10]]
            if len(urls) == 1:
                tg("sendPhoto", {"photo": urls[0], **caption})
            else:
                group = [{"type": "photo", "media": u} for u in urls]
                if single:
                    group[0].update(caption)
                tg("sendMediaGroup", {"media": group})
            send_remaining_text()
            return

        if videos:
            v = videos[0]
            gif = v.type == "animated_gif"
            method, field = ("sendAnimation", "animation") if gif else ("sendVideo", "video")
            extra = {}
            if getattr(v, "width", None) and getattr(v, "height", None):
                extra.update(width=v.width, height=v.height)
            if getattr(v, "duration_ms", None):
                extra["duration"] = round(v.duration_ms / 1000)
            if not gif:
                extra["supports_streaming"] = "true"

            for url in video_urls(v):
                try:
                    path = download_video(url)
                except requests.RequestException:
                    continue
                if not path:
                    continue
                try:
                    tg_upload(method, field, path, {**caption, **extra})
                    send_remaining_text()
                    return
                except requests.HTTPError:
                    continue
                finally:
                    os.unlink(path)
            raise RuntimeError(f"no video quality under {MAX_VIDEO_BYTES // 1_000_000} MB")
    except Exception as e:
        print(f"Native media failed for {tweet.id} ({e}); falling back to link preview.")

    author = users.get(tweet.author_id)
    link = f"https://x.com/{author.username if author else 'i'}/status/{tweet.id}"
    for m in ([m for m in msgs if m] or [link]):
        send_text(m, preview=True)
        time.sleep(0.5)

# ---------------------------------------------------------------- threads
def tl_id(t):
    return int(TL_POS.get(t.id, t.id))

def parse(resp):
    includes = resp.includes or {}
    users = {u.id: u for u in includes.get("users", [])}
    media = {m.media_key: m for m in includes.get("media", [])}
    refs = {t.id: t for t in includes.get("tweets", [])}

    best = {}
    for t in resp.data or []:
        orig, by = t, None
        for ref in (t.referenced_tweets or []):
            if ref.type == "retweeted" and ref.id in refs:
                orig = refs[ref.id]
                by = users[t.author_id].username if t.author_id in users else None
        pos = int(t.id)
        if int(orig.id) not in best or pos > best[int(orig.id)][0]:
            best[int(orig.id)] = (pos, orig, by)

    tweets = []
    for pos, orig, by in best.values():
        if pos >= TL_POS.get(orig.id, 0):
            TL_POS[orig.id], REPOSTER[orig.id] = pos, by
        tweets.append(orig)
    return tweets, users, media

def collect_thread(client, root, users, media_by_key):
    author = users.get(root.author_id)
    if not author:
        return [root]
    resp = client.search_recent_tweets(
        query=f"conversation_id:{root.id} from:{author.username}",
        max_results=100, user_auth=True, **FIELDS)
    tweets, u2, m2 = parse(resp)
    users.update(u2)
    media_by_key.update(m2)

    child_of = {}
    for t in tweets:
        for ref in (t.referenced_tweets or []):
            if ref.type == "replied_to":
                child_of[str(ref.id)] = t
    chain, cur = [root], root
    while str(cur.id) in child_of:
        cur = child_of[str(cur.id)]
        chain.append(cur)
    return chain

def has_media(tweet, media_by_key):
    keys = (tweet.attachments or {}).get("media_keys", [])
    return any(k in media_by_key for k in keys)

def handle_tweet(client, tweet, users, media_by_key, budget, state):
    created = tweet.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()

    likes = (tweet.public_metrics or {}).get("like_count", 0)
    if likes < MIN_LIKES:
        return age >= LIKES_WAIT

    is_thread, total = thread_info(tweet)
    if not is_thread:
        if MEDIA_ONLY and not has_media(tweet, media_by_key):
            return True
        send_tweet(tweet, users, media_by_key)
        return True

    if total is None and age < THREAD_WAIT:
        return False
    if budget["left"] <= 0:
        return False
    budget["left"] -= 1

    chain = collect_thread(client, tweet, users, media_by_key)
    if (total and len(chain) < total) and age < THREAD_WAIT:
        return False
    if MEDIA_ONLY:
        chain = [t for t in chain if has_media(t, media_by_key)]
        if not chain:
            return True
    if len(chain) == 1:
        send_tweet(chain[0], users, media_by_key)
        return True

    for i, t in enumerate(chain):
        part = str(t.id)
        if part in state["parts"]:
            continue
        note = f" · 🧵 {len(chain)} parts" if i == 0 else ""
        send_tweet(t, users, media_by_key, header=(i == 0), note=note)
        state["parts"].append(part)
        save_state(state)
        time.sleep(1)
    return True

# ---------------------------------------------------------------- main
def safe_handle(client, t, users, media, budget, state):
    key = str(t.id)
    if key in state["sent"]:
        return "done"
    try:
        if not handle_tweet(client, t, users, media, budget, state):
            return "wait"
    except Exception as e:
        n = state["fails"].get(key, 0) + 1
        state["fails"][key] = n
        print(f"Error on post {key} (attempt {n}/3): {e}")
        if n < 3:
            return "retry"
    state["fails"].pop(key, None)
    state["sent"].append(key)
    return "done"

def fetch_timeline(client, since_id):
    tweets, users, media = {}, {}, {}
    token = None
    for _ in range(MAX_PAGES if since_id else 1):
        params = dict(max_results=100, exclude=["replies"], user_auth=True, **FIELDS)
        if since_id:
            params["since_id"] = since_id
        if token:
            params["pagination_token"] = token
        resp = client.get_home_timeline(**params)
        ts, u, m = parse(resp)
        users.update(u)
        media.update(m)
        for t in ts:
            tweets.setdefault(t.id, t)
        token = (resp.meta or {}).get("next_token")
        if not token:
            break
    if token and since_id:
        print(f"Warning: more than {MAX_PAGES * 100} new posts since the last poll; older ones skipped.")
    return list(tweets.values()), users, media

def poll_once(client, state):
    if len(TL_POS) > 5000:
        TL_POS.clear()
        REPOSTER.clear()
    budget = {"left": MAX_THREAD_LOOKUPS}

    if state["pending"]:
        state["pending"] = state["pending"][-100:]
        resp = client.get_tweets(ids=state["pending"], user_auth=True, **FIELDS)
        tweets, users, media = parse(resp)
        found = {str(t.id) for t in tweets}
        state["pending"] = [i for i in state["pending"] if i in found]
        for t in sorted(tweets, key=tl_id):
            if safe_handle(client, t, users, media, budget, state) == "done" and str(t.id) in state["pending"]:
                state["pending"].remove(str(t.id))
            save_state(state)

    tweets, users, media = fetch_timeline(client, state["since_id"])

    if state["since_id"] is None:
        if tweets:
            state["since_id"] = str(max(tl_id(t) for t in tweets))
            save_state(state)
            print("First run: baseline saved, forwarding starts with new posts.")
        return

    for t in sorted(tweets, key=tl_id):
        result = safe_handle(client, t, users, media, budget, state)
        if result == "retry":
            save_state(state)
            break
        if result == "wait" and str(t.id) not in state["pending"]:
            state["pending"].append(str(t.id))
        state["since_id"] = str(tl_id(t))
        save_state(state)

def main():
    client = tweepy.Client(
        consumer_key=need("X_API_KEY"),
        consumer_secret=need("X_API_SECRET"),
        access_token=need("X_ACCESS_TOKEN"),
        access_token_secret=need("X_ACCESS_SECRET"),
        wait_on_rate_limit=True,
    )
    state = load_state()

    while True:
        try:
            poll_once(client, state)
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
