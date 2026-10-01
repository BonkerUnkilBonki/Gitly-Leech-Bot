# GitHub Release → Telegram APK Bot

Posts your GitHub release **changelog** and the release's **`.apk` file** to a
Telegram channel, automatically, every time you publish a release.

- `bot.py` — the bot (two modes: `webhook` and `poll`)
- `requirements.txt` — dependencies
- `env.example` — copy this to `.env` and fill it in
- `Dockerfile` — for container hosts

---

## How it works

```
you publish a release on GitHub
        │
        ▼
 GitHub sends a "release" event ──►  your bot  ──►  Telegram channel
        (webhook mode)                 │            • changelog message
   or the bot checks the API           │            • APK as a document
        every few minutes              │
        (poll mode)                    └── downloads the .apk asset
```

- **webhook mode** (recommended): GitHub pushes the event to your server the
  instant you publish. Real-time. Needs a public URL.
- **poll mode**: no public URL needed — good for running on a laptop or a small
  VPS. Checks the GitHub API every `POLL_INTERVAL` seconds.

---

## Step 1 — Create the Telegram bot and get its token

1. Open Telegram and chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, give it a name and a username (must end in `bot`).
3. BotFather replies with a **token** like
   `123456789:AAExampleTokenString`. Copy it → this is `TELEGRAM_BOT_TOKEN`.

## Step 2 — Add the bot to your channel

1. Open your channel → **Administrators** → **Add Admin**.
2. Search your bot's username and add it **with "Post Messages" permission**.
   (A bot can only post to a channel it is an admin of.)

## Step 3 — Get the channel ID

- **Public channel** (has a `@username`): just use `@yourchannelname` as
  `TELEGRAM_CHANNEL_ID`.
- **Private channel**: use its numeric id, e.g. `-1001234567890`.
  Easiest way to find it:
  1. Post any message in the channel.
  2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
  3. Look for `"chat":{"id":-100...,"title":"..."}` — that `id` is your
     `TELEGRAM_CHANNEL_ID`.

## Step 4 — Configure the bot

```bash
cp env.example .env
# then edit .env and fill in the values
```

At minimum set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, and `MODE`.

Install and run locally:

```bash
pip install -r requirements.txt

# webhook mode (serves on $PORT):
export $(grep -v '^#' .env | xargs)   # loads .env into the shell (bash)
python bot.py

# or, poll mode (no public URL needed):
MODE=poll GITHUB_REPO=owner/repo python bot.py
```

---

## Step 5 — Deploy on Render

This is the recommended path: Render hosts the bot, gives it an HTTPS URL, and
`render.yaml` configures most of it for you.

**R1. Put the files in a GitHub repo.** Create a repo (it can be the same one as
your app, or a separate `release-bot` repo) and push `bot.py`,
`requirements.txt`, `render.yaml`, and `.gitignore`. Never commit `.env` — the
`.gitignore` already excludes it.

```bash
git init && git add bot.py requirements.txt render.yaml .gitignore
git commit -m "telegram release bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

**R2. Create the service on Render.**

- **Blueprint (uses `render.yaml`):** Render Dashboard → **New +** →
  **Blueprint** → connect the repo → Render reads `render.yaml` and creates the
  web service. It will prompt you for the `sync: false` secrets.
- **Or manual:** **New +** → **Web Service** → connect the repo → Runtime
  **Python** → Build `pip install -r requirements.txt` → Start `python bot.py`
  → Health check path `/`.

**R3. Set the environment variables.** In the service → **Environment**, add:

| Key | Value |
| --- | --- |
| `MODE` | `webhook` |
| `TELEGRAM_BOT_TOKEN` | your (freshly revoked) BotFather token |
| `TELEGRAM_CHANNEL_ID` | `@BonkerUnkilBonki` |
| `GITHUB_WEBHOOK_SECRET` | a long random string (make one up) |
| `GITHUB_TOKEN` | optional — only for private repos |

Render sets `PORT` for you; the bot reads it automatically. Don't set it
yourself.

**R4. Deploy and copy your URL.** After the first deploy, Render shows a URL
like `https://telegram-release-bot.onrender.com`. Open it in a browser — you
should see `release-bot is running`.

**R5. Add the GitHub webhook.**

1. Repo → **Settings → Webhooks → Add webhook**.
2. **Payload URL:** `https://telegram-release-bot.onrender.com/webhook`
   (your actual Render URL + `/webhook`)
3. **Content type:** `application/json`
4. **Secret:** the **same** value you set as `GITHUB_WEBHOOK_SECRET`.
5. **Which events?** → **Let me select individual events** → tick only
   **Releases**.
6. **Add webhook.** GitHub sends a `ping`; check **Recent Deliveries** for a
   green `200`.

Then publish a release with an `.apk` attached and it lands in your channel.

> **Free-tier caveat.** Render's free web services **spin down after ~15 minutes
> of inactivity** and cold-start on the next request. GitHub's webhook POST can
> time out against a sleeping service, so a delivery may show as failed even
> though everything is configured right. Two fixes: (a) upgrade the service to a
> paid always-on instance (`plan: starter` in `render.yaml`), or (b) stay on
> free and manually **Redeliver** from the webhook's *Recent Deliveries* page
> when it happens. The bot itself is fine either way.

---

## Other deployment options

### Option B — Webhook mode elsewhere

Deploy `bot.py` on Railway, Fly.io, a VPS, or any host that gives you an HTTPS
URL and sets a `PORT` env var. Same webhook setup as R5 above.

### Option C — Poll mode (no public URL)

Run the bot anywhere that stays on (a VPS, a Raspberry Pi, a PC that's always
on). It needs no inbound URL and no GitHub webhook — just API access.

```bash
MODE=poll GITHUB_REPO=owner/repo TELEGRAM_BOT_TOKEN=... TELEGRAM_CHANNEL_ID=... python bot.py
```

The first run records the current latest release as a baseline (so it won't
re-post old ones). After that, each newly published release is forwarded.

To keep it running after you close the terminal:

```bash
nohup python bot.py > bot.log 2>&1 &
```

…or use a process manager like `systemd`, `pm2`, or `screen`/`tmux`.

### Option D — Docker

```bash
docker build -t release-bot .
docker run -d --env-file .env -p 8080:8080 release-bot
```

---

## Notes, limits and gotchas

- **50 MB upload cap.** The Telegram Bot API only lets bots upload files up to
  50 MB. If your APK is larger, the bot posts the changelog plus a direct
  download link instead of the file. To send bigger files you'd need a
  self-hosted Telegram Bot API server or to host the APK and send a link.
- **Caption length.** Telegram caps a file caption at 1024 characters, so the
  caption is trimmed. The full changelog is also sent as its own message
  (set `SEND_CHANGELOG=false` to turn that off).
- **Private repos** need `GITHUB_TOKEN` (a fine-grained personal access token
  with **Contents: read**). It also raises the API rate limit for public repos.
- **Changelog formatting** is sent as plain text on purpose — GitHub release
  notes are Markdown and Telegram's Markdown/HTML modes reject anything that
  isn't perfectly escaped, which would break the post. Plain text always works.
- **Signature check.** Webhook requests are verified against
  `GITHUB_WEBHOOK_SECRET` using `X-Hub-Signature-256`; bad signatures are
  rejected with 403.
- **Drafts vs pre-releases.** Drafts are never posted. Pre-releases **are**
  posted by default (set `INCLUDE_PRERELEASES=false` to skip them). GitHub
  sends a pre-release with the `prereleased` action rather than `published`, so
  the bot listens for `published`, `created`, `prereleased` and `released` to
  cover every way you can cut a release.
- **Multiple APKs** in one release: the first `*.apk` is sent. Set
  `ASSET_FILTER` (a regex) to pick a specific one, e.g. `ASSET_FILTER=release`.
- **One release = one post, guaranteed.** GitHub fires several events for a
  single release (`created`, `published`, and sometimes redeliveries). The bot
  de-duplicates by release id, so the changelog and the APK are each sent
  exactly once no matter how many events arrive.
- **APK attached after publishing.** The webhook payload's asset list is often
  empty at the instant a release is published. If no `.apk` is in the payload
  the bot re-fetches the release from the GitHub API and keeps checking for
  `APK_WAIT_ATTEMPTS x APK_WAIT_DELAY` seconds (default ~90s) until the asset
  appears. This is what stops the "No matching .apk asset" false alarm.
- **Fast 200 to GitHub.** The webhook responds immediately and does the
  download in a background thread, so a slow upload can't make GitHub time out
  and redeliver (which would otherwise cause duplicate posts).
