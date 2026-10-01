#!/usr/bin/env python3
"""
GitHub Release -> Telegram Channel APK forwarder.

When a new release is published on your GitHub repo, this bot:
  1. posts the release changelog to your Telegram channel, and
  2. uploads the release's .apk asset as a document to the same channel.

Two operating modes (set the MODE env var):

  webhook  (recommended)  Run an HTTP server. GitHub pushes a "release"
                          event to your /webhook endpoint the moment you
                          publish a release. Real-time, no polling.

  poll                    No public URL needed. The bot asks the GitHub
                          API every POLL_INTERVAL seconds whether a new
                          release exists. Good for a laptop / local box.

All configuration is via environment variables - see README.md / env.example.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time

import requests

# --------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()

MODE = os.environ.get("MODE", "webhook").strip().lower()

# webhook mode
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
WEBHOOK_PORT = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "8080")))

# poll mode
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()          # "owner/repo"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))      # seconds
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# optional
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()        # for private repos / rate limits
ASSET_FILTER = os.environ.get("ASSET_FILTER", "").strip()        # regex; default = first *.apk
SEND_CHANGELOG = os.environ.get("SEND_CHANGELOG", "true").lower() != "false"
# Forward pre-releases too? Set false to only post full releases.
INCLUDE_PRERELEASES = os.environ.get("INCLUDE_PRERELEASES", "true").lower() != "false"

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096
TG_UPLOAD_LIMIT = 49 * 1024 * 1024   # Bot API caps document uploads at 50 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("release-bot")


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------

def _tg(method, data=None, files=None, timeout=300):
    """Call a Telegram Bot API method and return its result."""
    resp = requests.post(f"{TG_API}/{method}", data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Non-JSON reply from Telegram: {resp.text[:200]}")
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description')}")
    return payload.get("result")


def _chunks(text, limit):
    """Split text into <=limit-char pieces on line boundaries."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_message(text):
    """Send one or more plain-text messages to the channel."""
    for piece in _chunks(text, TG_MESSAGE_LIMIT):
        if piece.strip():
            _tg("sendMessage", data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": piece,
                "disable_web_page_preview": "true",
            })


def send_document(path, filename, caption=""):
    """Upload a file to the channel with an optional caption."""
    if len(caption) > TG_CAPTION_LIMIT:
        caption = caption[: TG_CAPTION_LIMIT - 1] + "…"
    with open(path, "rb") as fh:
        return _tg("sendDocument", data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
        }, files={"document": (filename, fh)})


# --------------------------------------------------------------------------
# GitHub helpers
# --------------------------------------------------------------------------

def _gh_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def download_asset(asset, dest_path):
    """Download a release asset to dest_path. Uses the API URL so private
    repos (and rate-limited calls) work with GITHUB_TOKEN."""
    headers = {"Accept": "application/octet-stream"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    with requests.get(asset["url"], headers=headers, stream=True,
                      allow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for block in resp.iter_content(chunk_size=256 * 1024):
                fh.write(block)
    return dest_path


def pick_apk(assets):
    """Return the APK asset to send, honouring ASSET_FILTER if set."""
    apks = [a for a in assets if a.get("name", "").lower().endswith(".apk")]
    if ASSET_FILTER:
        rx = re.compile(ASSET_FILTER)
        apks = [a for a in apks if rx.search(a.get("name", ""))]
    return apks[0] if apks else None


# --------------------------------------------------------------------------
# Core: turn one release payload into a channel post
# --------------------------------------------------------------------------

def handle_release(rel):
    """Given a GitHub release object, post changelog + APK to Telegram."""
    if rel.get("draft"):
        log.info("Ignoring draft release %s", rel.get("tag_name"))
        return
    if rel.get("prerelease") and not INCLUDE_PRERELEASES:
        log.info("Ignoring pre-release %s", rel.get("tag_name"))
        return

    tag = rel.get("tag_name", "?")
    title = rel.get("name") or tag
    body = (rel.get("body") or "").strip()
    url = rel.get("html_url", "")
    assets = rel.get("assets", [])

    log.info("Handling release %s (%d asset(s))", tag, len(assets))

    header = f"\U0001F680 New release: {title}\nTag: {tag}"
    if url:
        header += f"\n{url}"

    if SEND_CHANGELOG and body:
        send_message(f"{header}\n\n{body}")

    apk = pick_apk(assets)
    if not apk:
        log.warning("No matching .apk asset in release %s", tag)
        send_message(f"{header}\n\n(No matching .apk asset was attached to this release.)")
        return

    size = apk.get("size", 0)
    caption = f"{title} ({tag})"
    if body:
        caption += f"\n\n{body}"

    if size > TG_UPLOAD_LIMIT:
        mb = size / (1024 * 1024)
        log.warning("APK %s is %.1f MB - over the 50 MB upload limit", apk["name"], mb)
        send_message(
            f"{header}\n\n"
            f"APK {apk['name']} is {mb:.1f} MB, above Telegram's 50 MB bot upload "
            f"limit, so it can't be attached here.\nDownload: {apk.get('browser_download_url', '')}"
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, apk["name"])
        log.info("Downloading %s (%.1f MB)", apk["name"], size / (1024 * 1024))
        download_asset(apk, dest)
        log.info("Uploading %s to Telegram", apk["name"])
        send_document(dest, apk["name"], caption)

    log.info("Done with release %s", tag)


# --------------------------------------------------------------------------
# Mode 1: webhook server
# --------------------------------------------------------------------------

def _verify_signature(raw_body, signature_header):
    if not GITHUB_WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET is empty - signature NOT verified")
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def run_webhook():
    from flask import Flask, request, abort  # imported lazily

    app = Flask(__name__)

    @app.get("/")
    def health():
        return "release-bot is running", 200

    @app.post("/webhook")
    def webhook():
        raw = request.get_data()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(raw, sig):
            log.warning("Rejected webhook: bad signature")
            abort(403)

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return "pong", 200
        if event != "release":
            return "ignored", 200

        try:
            payload = json.loads(raw)
        except ValueError:
            abort(400)

        action = payload.get("action", "")
        # GitHub fires different actions depending on how a release is
        # created: 'published' (draft published), 'created' (published
        # without a draft), 'prereleased' (created as a pre-release), and
        # 'released' (a pre-release promoted). Handle them all; drafts and
        # (optionally) pre-releases are filtered out inside handle_release.
        if action not in ("published", "created", "prereleased", "released"):
            log.info("Ignoring release action '%s'", action)
            return "ignored", 200

        try:
            handle_release(payload.get("release", {}))
        except Exception as exc:                       # noqa: BLE001
            log.exception("Failed to handle release: %s", exc)
            return "error", 500
        return "ok", 200

    log.info("Webhook server listening on port %d (endpoint: /webhook)", WEBHOOK_PORT)
    app.run(host="0.0.0.0", port=WEBHOOK_PORT)


# --------------------------------------------------------------------------
# Mode 2: polling
# --------------------------------------------------------------------------

def _load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh).get("last_tag")
    except (OSError, ValueError):
        return None


def _save_state(tag):
    with open(STATE_FILE, "w") as fh:
        json.dump({"last_tag": tag}, fh)


def _latest_release():
    resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers=_gh_headers(), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run_poll():
    if not GITHUB_REPO:
        sys.exit("GITHUB_REPO (owner/repo) is required in poll mode.")

    last = _load_state()
    if last is None:
        # First run: remember the current latest so we don't re-post old releases.
        try:
            last = _latest_release().get("tag_name")
            _save_state(last)
            log.info("Baseline set to current latest release: %s", last)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Could not set baseline yet: %s", exc)

    log.info("Polling %s every %ds", GITHUB_REPO, POLL_INTERVAL)
    while True:
        try:
            rel = _latest_release()
            tag = rel.get("tag_name")
            if tag and tag != last:
                handle_release(rel)
                _save_state(tag)
                last = tag
        except Exception as exc:                        # noqa: BLE001
            log.warning("Poll error: %s", exc)
        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID (see README.md).")
    if MODE == "poll":
        run_poll()
    elif MODE == "webhook":
        run_webhook()
    else:
        sys.exit(f"Unknown MODE '{MODE}' - use 'webhook' or 'poll'.")


if __name__ == "__main__":
    main()
