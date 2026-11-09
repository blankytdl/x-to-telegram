# X → Telegram forwarder

Forwards posts from your X (Twitter) **home timeline** to a Telegram channel.

- Native photos, videos and GIFs (videos are downloaded and uploaded, best quality under the size limit)
- Thread support (`1/3`, `1/`, `🧵`): the whole thread is collected and sent in order
- Long posts (X Premium) are split into several messages
- Reposts are shown as the original post, with the reposter credited, and deduplicated
- Survives restarts (`state.json`), retries failed posts 3 times, respects X and Telegram rate limits

## Requirements

- Python 3.9+
- An X developer app with **Read** access and user-context (OAuth 1.0a) keys
- Thread collection uses recent search, which needs an X API plan that includes it. Without it, threads fall back to being sent as a single post.
- A Telegram bot (from [@BotFather](https://t.me/BotFather)) added as an **admin** of your channel

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env with your keys
```

The script reads normal environment variables; it does not read `.env` by itself. Load the file before running:

**Linux / macOS**
```bash
set -a; source .env; set +a
python x_to_telegram.py
```

**Windows (PowerShell)**
```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#\s].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
}
python x_to_telegram.py
```

**Hosting (Render, Railway, etc.):** add the variables in the dashboard instead of using a `.env` file, and run it as a background worker with start command `python x_to_telegram.py`. Use a persistent disk if you want `state.json` to survive redeploys (otherwise the first run re-baselines, which is harmless).

## Settings

Required: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

| Variable | Default | Meaning |
|---|---:|---|
| `POLL_SECONDS` | 300 | How often to check the timeline |
| `MIN_LIKES` | 0 | Only forward posts that have at least this many likes. New posts are re-checked while they gather likes |
| `LIKES_WAIT_MIN` | 60 | How long a post below `MIN_LIKES` keeps being re-checked before it is dropped |
| `THREAD_WAIT_MIN` | 15 | How long to wait for the rest of a thread of unknown length |
| `MAX_THREAD_LOOKUPS` | 5 | Max thread lookups per poll (protects your X rate limit) |
| `MAX_PAGES` | 10 | Max timeline pages (100 posts each) fetched per poll |
| `MAX_VIDEO_MB` | 50 | Largest video to upload (Telegram bot limit is 50) |
| `MEDIA_ONLY` | 1 | `1` = only forward posts that have photos/videos |
| `SEND_CAPTIONS` | 1 | `0` = send media alone, with no author/text/link |

## How it behaves

- **First run:** saves a baseline and forwards nothing. Only posts that appear afterwards are sent.
- **State:** progress is stored in `state.json` next to the script. Delete it to start over.
- **Failures:** a post that fails is retried on the next polls, up to 3 attempts, then skipped so it cannot block the rest.
- **Fallback:** if native media upload fails, a link post with Telegram's preview is sent instead.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing environment variable: ...` | The variables were not loaded (see Setup) |
| `Telegram error 400 ... chat not found` | Wrong `TELEGRAM_CHAT_ID`, or the bot is not a channel admin |
| `Telegram error 403` | The bot was removed from the channel or lacks permission to post |
| `Error: 401` / `403` from X | Wrong keys, or tokens generated before enabling Read permission. Regenerate them |
| `Error: 429` or long pauses | X rate limit; the script waits automatically |
| Nothing is forwarded | First run only sets the baseline; also check `MEDIA_ONLY` and `MIN_LIKES` |
| Threads arrive as single posts | Your X plan has no recent-search access |

## Files

- `x_to_telegram.py` — the forwarder
- `.env.example` — template for your settings
- `requirements.txt` — Python dependencies
