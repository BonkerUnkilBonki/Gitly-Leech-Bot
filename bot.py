#!/usr/bin/env python3
"""
GitHub Release -> Telegram Channel APK forwarder.

When a new release is published on your GitHub repo, this bot:
  1. posts the release changelog to your Telegram channel, and
  2. uploads the release's .apk asset as a document to the same channel.

Two operating modes (set the MODE env var):

  webhook  (recommended)  Run an HTTP server. GitHub pushes a "release"
                          event to your /webhook endpoint. Real-time.

  poll                    No public URL needed. The bot asks the GitHub
                          API every POLL_INTERVAL seconds.

Robustness notes
----------------
* GitHub fires SEVERAL release events for one release (e.g. "created" then
  "published", plus redeliveries). Every release is de-duplicated by its id,
  so the changelog and the APK are each sent exactly once.
* The webhook payload's asset list is often EMPTY at publish time - the APK
  is attached a moment later. So if no .apk is in the payload, the bot
  re-fetches the release from the GitHub API and retries for a while until
  the asset shows up.
* The webhook returns 200 to GitHub immediately and processes in a
  background thread, so a slow asset upload can't make GitHub time out and
  redeliver (which would otherwise cause duplicate posts).

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
import threading
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

# optional
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()        # for private repos / rate limits
ASSET_FILTER = os.environ.get("ASSET_FILTER", "").strip()        # regex; default = first *.apk
SEND_CHANGELOG = os.environ.get("SEND_CHANGELOG", "true").lower() != "false"
INCLUDE_PRERELEASES = os.environ.get("INCLUDE_PRERELEASES", "true").lower() != "false"
NOTIFY_NO_APK = os.environ.get("NOTIFY_NO_APK", "false").lower() == "true"

# how long to keep waiting for the .apk to appear after a release is published
APK_WAIT_ATTEMPTS = int(os.environ.get("APK_WAIT_ATTEMPTS", "9"))   # re-fetch tries
APK_WAIT_DELAY = int(os.environ.get("APK_WAIT_DELAY", "10"))        # seconds between tries

# state file: remembers what has already been posted (survives restarts)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SEEN_TTL = int(os.environ.get("SEEN_TTL", str(7 * 24 * 3600)))      # forget releases after 7 days

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
        caption = caption[: TG_CAPTION_LIMIT - 1] + "\u2026"
    with open(path, "rb") as fh:
        return _tg("sendDocument", data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
        }, files={"document": (filename, fh)})


# --------------------------------------------------------------------------
# GitHub helpers
# --------------------------------------------------------------------------

def _gh_headers(accept="application/vnd.github+json"):
    headers = {"Accept": accept}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _fetch_release(repo_full_name, release_id):
    """Fetch a single release (with its current, complete asset list)."""
    resp = requests.get(
        f"https://api.github.com/repos/{repo_full_name}/releases/{release_id}",
        headers=_gh_headers(), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def download_asset(asset, dest_path):
    """Download a release asset to dest_path via the API URL (works for
    public repos and, with a token, private ones)."""
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
# Persistent state: which releases have already been handled
# --------------------------------------------------------------------------

_state_lock = threading.Lock()


def _load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        log.warning("Could not persist state: %s", exc)


def _prune(rels):
    now = time.time()
    for key in [k for k, v in rels.items() if now - v.get("ts", now) > SEEN_TTL]:
        rels.pop(key, None)


def _rec(rid):
    """Current record for a release id."""
    with _state_lock:
        return dict(_load_state().get("releases", {}).get(str(rid), {}))


def _reserve(rid, field):
    """Atomically claim a one-shot action for a release. Returns True the
    first time, False on every later call - this is the de-duplication."""
    with _state_lock:
        state = _load_state()
        rels = state.setdefault("releases", {})
        rec = rels.setdefault(str(rid), {})
        if rec.get(field):
            return False
        rec[field] = True
        rec["ts"] = time.time()
        _prune(rels)
        _save_state(state)
        return True


def _mark(rid, **fields):
    with _state_lock:
        state = _load_state()
        rels = state.setdefault("releases", {})
        rec = rels.setdefault(str(rid), {})
        rec.update(fields)
        rec["ts"] = time.time()
        _prune(rels)
        _save_state(state)


# in-memory guard so two concurrent events for the same release don't both
# sit waiting for the same APK
_inflight_lock = threading.Lock()
_inflight = set()


def _begin_check(rid):
    with _inflight_lock:
        if str(rid) in _inflight:
            return False
        _inflight.add(str(rid))
        return True


def _end_check(rid):
    with _inflight_lock:
        _inflight.discard(str(rid))


# --------------------------------------------------------------------------
# Core: turn one release payload into a channel post
# --------------------------------------------------------------------------

def _resolve_apk(rel, repo_full_name):
    """Find the .apk asset. Uses the payload first; if it isn't there yet,
    re-fetches the release from the API and retries for a while."""
    apk = pick_apk(rel.get("assets", []))
    if apk:
        return apk

    release_id = rel.get("id")
    if not (repo_full_name and release_id):
        return None

    log.info("No .apk in webhook payload for %s - re-fetching (up to %ds)",
             rel.get("tag_name"), APK_WAIT_ATTEMPTS * APK_WAIT_DELAY)
    for attempt in range(1, APK_WAIT_ATTEMPTS + 1):
        try:
            fresh = _fetch_release(repo_full_name, release_id)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Re-fetch %d/%d failed: %s", attempt, APK_WAIT_ATTEMPTS, exc)
        else:
            apk = pick_apk(fresh.get("assets", []))
            if apk:
                log.info("Found %s on re-fetch attempt %d", apk["name"], attempt)
                return apk
        if attempt < APK_WAIT_ATTEMPTS:
            time.sleep(APK_WAIT_DELAY)
    return None


def handle_release(rel, repo_full_name=None):
    """Given a GitHub release object, post the changelog and the APK once."""
    if rel.get("draft"):
        log.info("Ignoring draft release %s", rel.get("tag_name"))
        return
    if rel.get("prerelease") and not INCLUDE_PRERELEASES:
        log.info("Ignoring pre-release %s", rel.get("tag_name"))
        return

    rid = rel.get("id") or rel.get("tag_name")
    tag = rel.get("tag_name", "?")
    title = rel.get("name") or tag
    body = (rel.get("body") or "").strip()
    url = rel.get("html_url", "")
    repo_full_name = repo_full_name or GITHUB_REPO or None

    log.info("Handling release %s (id=%s)", tag, rid)

    header = f"\U0001F680 New release: {title}\nTag: {tag}"
    if url:
        header += f"\n{url}"

    # --- changelog: post exactly once per release ---
    if SEND_CHANGELOG and body:
        if _reserve(rid, "changelog"):
            send_message(f"{header}\n\n{body}")
        else:
            log.info("Changelog for %s already posted - skipping", tag)

    # --- APK: send exactly once per release ---
    if _rec(rid).get("apk"):
        log.info("APK for %s already sent - skipping", tag)
        return
    if not _begin_check(rid):
        log.info("APK check already running for %s - skipping duplicate event", tag)
        return
    try:
        apk = _resolve_apk(rel, repo_full_name)
        if not apk:
            log.warning("No .apk asset found for %s after waiting", tag)
            if NOTIFY_NO_APK and _reserve(rid, "noapk"):
                send_message(f"{header}\n\n(No .apk asset was found for this release.)")
            return

        # claim the send slot; if a duplicate event already sent it, stop
        if not _reserve(rid, "apk"):
            log.info("APK for %s claimed by another event - skipping", tag)
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
                f"APK {apk['name']} is {mb:.1f} MB, above Telegram's 50 MB bot "
                f"upload limit, so it can't be attached here.\n"
                f"Download: {apk.get('browser_download_url', '')}"
            )
            return

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, apk["name"])
            log.info("Downloading %s (%.1f MB)", apk["name"], size / (1024 * 1024))
            download_asset(apk, dest)
            log.info("Uploading %s to Telegram", apk["name"])
            send_document(dest, apk["name"], caption)
        log.info("Sent %s for release %s", apk["name"], tag)
    finally:
        _end_check(rid)


def _safe_handle(rel, repo_full_name):
    try:
        handle_release(rel, repo_full_name)
    except Exception as exc:                            # noqa: BLE001
        log.exception("Failed to handle release: %s", exc)


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


# GitHub release actions that mean "something was published / changed".
# Drafts and (optionally) pre-releases are filtered out in handle_release.
RELEASE_ACTIONS = ("published", "created", "prereleased", "released", "edited")


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
        if action not in RELEASE_ACTIONS:
            log.info("Ignoring release action '%s'", action)
            return "ignored", 200

        repo = (payload.get("repository") or {}).get("full_name")
        release = payload.get("release", {})

        # Respond to GitHub IMMEDIATELY (its webhook timeout is 10s) and do
        # the slow work - downloading the APK - in the background. This is
        # what stops GitHub from timing out and redelivering.
        threading.Thread(
            target=_safe_handle, args=(release, repo), daemon=True
        ).start()
        return "ok", 200

    log.info("Webhook server listening on port %d (endpoint: /webhook)", WEBHOOK_PORT)
    app.run(host="0.0.0.0", port=WEBHOOK_PORT)


# --------------------------------------------------------------------------
# Mode 2: polling
# --------------------------------------------------------------------------

def _latest_release():
    resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers=_gh_headers(), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _set_last_tag(tag):
    with _state_lock:
        state = _load_state()
        state["last_tag"] = tag
        _save_state(state)


def run_poll():
    if not GITHUB_REPO:
        sys.exit("GITHUB_REPO (owner/repo) is required in poll mode.")

    last = _load_state().get("last_tag")
    if last is None:
        # First run: remember the current latest so we don't re-post old ones.
        try:
            last = _latest_release().get("tag_name")
            _set_last_tag(last)
            log.info("Baseline set to current latest release: %s", last)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Could not set baseline yet: %s", exc)

    log.info("Polling %s every %ds", GITHUB_REPO, POLL_INTERVAL)
    while True:
        try:
            rel = _latest_release()
            tag = rel.get("tag_name")
            if tag and tag != last:
                _safe_handle(rel, GITHUB_REPO)
                _set_last_tag(tag)
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
