#!/usr/bin/env python3
"""
Discord -> Telegram alert relay.

Polls a set of Discord channels over the REST API and forwards matching
messages to a Telegram chat. Designed to run on a schedule (GitHub Actions
cron) with no server and no dependencies beyond the Python standard library.

Required environment variables:
    DISCORD_BOT_TOKEN   Bot token from the Discord developer portal
    TELEGRAM_BOT_TOKEN  Bot token from @BotFather
    TELEGRAM_CHAT_ID    Numeric chat id to deliver alerts to

Optional:
    CONFIG_PATH   defaults to config.json
    STATE_PATH    defaults to state.json
    DRY_RUN       set to 1 to print alerts instead of sending them
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DISCORD_API = "https://discord.com/api/v10"
TELEGRAM_API = "https://api.telegram.org"

USER_AGENT = "DiscordTelegramAlerts/1.0 (+https://github.com)"

# Per channel, per run. Applied PER CHANNEL rather than globally so a burst in
# one busy channel cannot starve the others: anything over the cap is deferred
# to the next run rather than dropped, and the channel's cursor stays put.
MAX_ALERTS_PER_CHANNEL = 12

# Telegram allows about 1 message per second per chat. Going faster reliably
# earns a 429, and the retry waits then blow the job timeout.
SEND_INTERVAL_SECONDS = 1.1

# Stop sending after this long and defer the rest, so the job always finishes
# well inside its timeout and always gets to save its position.
SEND_BUDGET_SECONDS = 150

# Telegram's hard cap is 4096 characters. Leave room: html.escape expands
# text (one apostrophe becomes six characters), so the escaped payload can be
# many times the length of the raw message.
SAFE_TEXT_LIMIT = 3800

# Discord returns at most 100 messages per request.
MESSAGE_PAGE_LIMIT = 100


class HttpError(Exception):
    def __init__(self, status: int, body: str, headers=None):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def _request(method: str, url: str, headers=None, payload=None, timeout=30):
    data = None
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise HttpError(exc.code, body, dict(exc.headers or {})) from None


def request_with_retry(method, url, headers=None, payload=None, attempts=4):
    """Retry on 429 (rate limit) and 5xx with the delay the server asks for."""
    delay = 1.0
    for attempt in range(attempts):
        try:
            return _request(method, url, headers=headers, payload=payload)
        except HttpError as exc:
            last = attempt == attempts - 1
            if exc.status == 429:
                wait = _retry_after(exc)
                if last:
                    raise
                log(f"rate limited on {redact(url)}, waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            if 500 <= exc.status < 600 and not last:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def _retry_after(exc: HttpError) -> float:
    header = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    try:
        body = json.loads(exc.body)
        if "retry_after" in body:
            return min(float(body["retry_after"]), 60.0)
        if "parameters" in body and "retry_after" in body["parameters"]:
            return min(float(body["parameters"]["retry_after"]), 60.0)
    except (ValueError, TypeError, KeyError):
        pass
    return 5.0


def redact(text: str) -> str:
    """Strip a Telegram bot token out of anything before it reaches a log.

    The token lives in the Telegram URL path, so any log line mentioning a
    URL would otherwise print it. GitHub masks secrets in logs on a
    best-effort basis; that is not something to depend on in a public repo.
    """
    return re.sub(r"/bot[^/\s]+/", "/bot***/", text)


def log(message: str) -> None:
    print(f"[relay] {redact(message)}", flush=True)


# ---------------------------------------------------------------- config/state

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        # A run killed mid-write leaves truncated JSON. Starting from the
        # default re-baselines rather than crash-looping forever.
        log(f"{path} is not valid JSON, starting from a clean state")
        return default


def save_json(path, value):
    """Write atomically so a killed job cannot leave a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------------- discord

class Discord:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bot {token}"}

    def guild_channels(self, guild_id: str):
        return request_with_retry(
            "GET", f"{DISCORD_API}/guilds/{guild_id}/channels", headers=self.headers
        )

    def messages_after(self, channel_id: str, after=None, limit=MESSAGE_PAGE_LIMIT):
        params = {"limit": str(min(limit, MESSAGE_PAGE_LIMIT))}
        if after:
            params["after"] = str(after)
        url = f"{DISCORD_API}/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}"
        messages = request_with_retry("GET", url, headers=self.headers)
        # Discord returns newest first; put them in chronological order.
        return list(reversed(messages))


def resolve_channels(all_channels, wanted_names):
    """Map configured channel names to ids, tolerating emoji and separators.

    Discord channel names in this server look like "⛓・dev-rel", so a plain
    equality check on "dev-rel" would miss. Match on a normalised form and
    fall back to a suffix match.
    """
    by_norm = {}
    collisions = set()
    for channel in all_channels:
        # 0 = text, 5 = announcement, 15 = forum. Others have no messages.
        if channel.get("type") not in (0, 5, 15):
            continue
        key = normalise(channel.get("name", ""))
        if key in by_norm:
            # Two channels normalise to the same name. Picking one by API
            # order would silently watch the wrong channel, so refuse both.
            collisions.add(key)
        by_norm.setdefault(key, channel)

    resolved = {}
    missing = []
    ambiguous = []
    for name in wanted_names:
        key = normalise(name)
        if key in collisions:
            ambiguous.append(name)
            continue
        match = by_norm.get(key)
        if match is None:
            # Only accept a fuzzy match when exactly one candidate exists,
            # and require it to be a suffix so "general" cannot bind to
            # "general-off-topic".
            candidates = [c for k, c in by_norm.items() if k.endswith(key)]
            match = candidates[0] if len(candidates) == 1 else None
        if match is None:
            missing.append(name)
        else:
            resolved[name] = match
    return resolved, missing, ambiguous


def normalise(name: str) -> str:
    """Strip emoji, separators and case so "⛓・dev-rel" matches "dev-rel"."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "-", name.lower())
    return cleaned.strip("-")


# ------------------------------------------------------------------- matching

def compile_keywords(keywords):
    if not keywords:
        return None
    # \b does not fire next to an apostrophe in "can't", so allow either a
    # word boundary or the start/end of the string around each phrase.
    parts = [re.escape(k.strip().lower()) for k in keywords if k.strip()]
    if not parts:
        return None
    return re.compile(r"(?:(?<=\W)|^)(?:" + "|".join(parts) + r")(?:(?=\W)|$)", re.IGNORECASE)


def message_matches(message, channel_cfg, watch_ids, keyword_re):
    """Return the reason this message should alert, or None."""
    if message.get("author", {}).get("bot") and not channel_cfg.get("include_bots", False):
        return None

    mode = channel_cfg.get("mode", "keywords")

    if mode == "all":
        return "all messages"

    content = message.get("content") or ""

    if mode in ("keywords", "any") and keyword_re and keyword_re.search(content):
        match = keyword_re.search(content)
        return f'keyword "{match.group(0).lower()}"'

    if mode in ("mentions", "any", "keywords"):
        mentioned = {u.get("id") for u in message.get("mentions", [])}
        mentioned |= set(message.get("mention_roles", []) or [])
        hit = mentioned & watch_ids
        if hit:
            return "mention"
        if message.get("mention_everyone") and "everyone" in watch_ids:
            return "@everyone"

    return None


# ------------------------------------------------------------------ telegram


class FatalTelegramError(Exception):
    """Telegram refuses everything: bad token, blocked bot, wrong chat id.

    Systemic, so it should fail the run loudly rather than be swallowed
    per message.
    """


def _alert_body(message, channel_name, guild_id, reason, content):
    """Build the HTML and plain-text forms of one alert."""
    author = message.get("author", {})
    name = author.get("global_name") or author.get("username") or "unknown"

    link = (
        f"https://discord.com/channels/{guild_id}/"
        f"{message.get('channel_id')}/{message.get('id')}"
    )

    # The relayed text goes inside <code>. Telegram does not auto-link URLs
    # inside a code span, so a hostile post cannot put a tappable phishing
    # link into an alert that otherwise looks like trusted tooling output.
    # The "open in Discord" anchor stays the only clickable thing.
    html_text = (
        f"<b>#{html.escape(channel_name)}</b> "
        f"<i>{html.escape(reason)}</i>\n"
        f"<b>{html.escape(name)}</b>: <code>{html.escape(content)}</code>\n"
        f'<a href="{html.escape(link)}">open in Discord</a>'
    )
    plain_text = f"#{channel_name} ({reason})\n{name}: {content}\n{link}"
    return html_text, plain_text


def format_alert(message, channel_name, guild_id, reason):
    """Render an alert that is guaranteed to fit inside Telegram's limit.

    Escaping expands text: one apostrophe becomes six characters. Truncating
    the raw content to a fixed length and escaping afterwards can therefore
    still produce a payload well over Telegram's 4096 character cap, which
    Telegram rejects with a 400. So shrink the raw content until the escaped
    result actually fits, rather than assuming it will.
    """
    content = (message.get("content") or "").strip()

    if not content:
        attachments = message.get("attachments") or []
        embeds = message.get("embeds") or []
        if attachments:
            content = f"({len(attachments)} attachment(s))"
        elif embeds:
            content = "(embed)"
        else:
            content = "(no text)"

    if len(content) > 900:
        content = content[:900].rstrip() + " ..."

    html_text, plain_text = _alert_body(message, channel_name, guild_id, reason, content)
    while len(html_text) > SAFE_TEXT_LIMIT and len(content) > 24:
        content = content[: max(24, int(len(content) * 0.6))].rstrip() + " ..."
        html_text, plain_text = _alert_body(
            message, channel_name, guild_id, reason, content
        )

    return {"html": html_text, "plain": plain_text[:SAFE_TEXT_LIMIT]}


def _post_telegram(token, chat_id, payload):
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    body = {"chat_id": chat_id, "disable_web_page_preview": True}
    body.update(payload)
    return request_with_retry("POST", url, payload=body, attempts=3)


def send_telegram(token, chat_id, alert, dry_run=False):
    """Deliver one alert. Returns True if it landed.

    A single undeliverable message must never stop the queue or wedge the
    relay: without this, one crafted post in a public channel could make
    every run die at the same message forever. So per message errors are
    logged and skipped, and only account level failures are raised.
    """
    if isinstance(alert, str):
        alert = {"html": alert, "plain": alert}

    if dry_run:
        print("---- would send ----")
        print(alert["html"])
        return True

    try:
        _post_telegram(token, chat_id, {"text": alert["html"], "parse_mode": "HTML"})
        return True
    except HttpError as exc:
        if exc.status in (401, 403, 404):
            raise FatalTelegramError(
                f"Telegram rejected the account or chat ({exc.status}). "
                "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, and that you "
                "have not blocked the bot."
            ) from None
        log(f"Telegram refused the formatted alert ({exc.status}), retrying as plain text")

    try:
        _post_telegram(token, chat_id, {"text": alert["plain"]})
        return True
    except HttpError as exc:
        if exc.status in (401, 403, 404):
            raise FatalTelegramError(
                f"Telegram rejected the account or chat ({exc.status})."
            ) from None
        log(f"dropping one undeliverable alert ({exc.status})")
        return False


# ----------------------------------------------------------------------- main

def run(config, state, discord, telegram_token, chat_id, dry_run=False):
    guild_id = str(config["guild_id"])
    channels_cfg = config.get("channels", {})
    global_keywords = config.get("keywords", [])
    watch_ids = set(str(i) for i in config.get("watch_mention_ids", []))

    try:
        all_channels = discord.guild_channels(guild_id)
    except HttpError as exc:
        if exc.status == 401:
            # A bad or revoked token is a real failure. Fail loudly so the run
            # goes red and you actually notice.
            log("Discord rejected the bot token (401). Reset it in the")
            log("developer portal and update the DISCORD_BOT_TOKEN secret.")
            raise
        if exc.status in (403, 404):
            # The bot is not in the server yet, or has been removed. A normal
            # waiting state, not a crash: exit clean so the run stays green
            # and the log says plainly what is missing.
            log(f"cannot read guild {guild_id}: {exc}")
            log("The bot is not in the server yet, or it lost View Channels.")
            log("Ask an admin to authorise the invite link, then this run")
            log("will start picking up channels on its own. Nothing to fix here.")
            return state, 0
        raise

    resolved, missing, ambiguous = resolve_channels(
        all_channels, list(channels_cfg.keys())
    )
    for name in missing:
        log(f"WARNING: channel '{name}' not found or bot cannot see it")
    for name in ambiguous:
        log(f"WARNING: '{name}' matches more than one channel, skipping it")
        log("Use the exact name from --list-channels to disambiguate.")

    seen = state.setdefault("last_message_id", {})
    deadline = time.monotonic() + SEND_BUDGET_SECONDS
    sent_total = 0

    for name, channel in resolved.items():
        channel_cfg = channels_cfg[name] or {}
        channel_id = channel["id"]
        keyword_re = compile_keywords(channel_cfg.get("keywords", global_keywords))
        after = seen.get(name)

        try:
            messages = discord.messages_after(channel_id, after=after)
        except HttpError as exc:
            log(f"could not read #{name}: {exc}")
            continue

        # First time we see a channel: record the position, do not replay the
        # backlog into Telegram.
        if after is None:
            baseline = messages[-1]["id"] if messages else channel.get("last_message_id")
            if baseline:
                seen[name] = str(baseline)
                log(f"#{name}: first run, baseline set at message {baseline}")
            else:
                log(f"#{name}: first run, channel is empty, nothing to baseline")
            continue

        if not messages:
            continue

        # The cursor only ever moves past a message once that message has
        # been dealt with. Advancing it up front (the obvious way to write
        # this) silently loses every message whose delivery then fails.
        alerts_here = 0
        handled = 0
        for message in messages:
            if alerts_here >= MAX_ALERTS_PER_CHANNEL:
                log(f"#{name}: hit the per-run cap, deferring the rest")
                break
            if time.monotonic() > deadline:
                log(f"#{name}: out of send budget, deferring the rest")
                break

            reason = message_matches(message, channel_cfg, watch_ids, keyword_re)
            if reason:
                alert = format_alert(message, name, guild_id, reason)
                if send_telegram(telegram_token, chat_id, alert, dry_run=dry_run):
                    sent_total += 1
                alerts_here += 1
                if not dry_run:
                    time.sleep(SEND_INTERVAL_SECONDS)

            handled += 1
            # Persist progress per message, so an exception later in the run
            # cannot re-deliver what already went out.
            seen[name] = message["id"]

        deferred = len(messages) - handled
        log(
            f"#{name}: {handled} message(s) processed, {alerts_here} alerted"
            + (f", {deferred} deferred to the next run" if deferred else "")
        )

    if not sent_total:
        log("no alerts this run")
    else:
        log(f"sent {sent_total} alert(s)")
    return state, sent_total


def list_channels(discord, guild_id):
    """Print every channel the bot can actually see, with its type and id.

    Run this once after inviting the bot so you can copy exact names into
    config.json instead of guessing at the emoji prefixes.
    """
    kinds = {0: "text", 2: "voice", 4: "category", 5: "announcement", 15: "forum"}
    channels = discord.guild_channels(guild_id)
    for channel in sorted(channels, key=lambda c: (c.get("position", 0), c.get("name", ""))):
        kind = kinds.get(channel.get("type"), str(channel.get("type")))
        if kind in ("voice", "category"):
            continue
        print(f'{channel["id"]}  {kind:12s}  {channel.get("name", "")}')
    print(f"\n{len(channels)} channel(s) visible to the bot", file=sys.stderr)


def main():
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    state_path = os.environ.get("STATE_PATH", "state.json")
    dry_run = os.environ.get("DRY_RUN") == "1"

    discord_token = os.environ.get("DISCORD_BOT_TOKEN")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    required = [("DISCORD_BOT_TOKEN", discord_token)]
    if "--list-channels" not in sys.argv:
        required += [
            ("TELEGRAM_BOT_TOKEN", telegram_token),
            ("TELEGRAM_CHAT_ID", chat_id),
        ]
    missing = [n for n, v in required if not v]
    if missing:
        log(f"missing environment variable(s): {', '.join(missing)}")
        return 1

    config = load_json(config_path, None)
    if config is None:
        log(f"config not found at {config_path}")
        return 1

    if "--list-channels" in sys.argv:
        list_channels(Discord(discord_token), str(config["guild_id"]))
        return 0

    state = load_json(state_path, {})

    exit_code = 0
    try:
        state, _ = run(
            config,
            state,
            Discord(discord_token),
            telegram_token,
            chat_id,
            dry_run=dry_run,
        )
    except FatalTelegramError as exc:
        log(str(exc))
        exit_code = 1
    finally:
        # Always persist progress, even if the run blew up partway, so the
        # messages already delivered are not delivered again. The workflow
        # commits this file with if: always() for the same reason.
        save_json(state_path, state)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
