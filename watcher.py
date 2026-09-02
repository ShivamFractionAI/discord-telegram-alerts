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

# Hard ceiling so a burst in a busy channel cannot flood Telegram or trip its
# flood limits. Anything beyond this is collapsed into a single summary line.
MAX_ALERTS_PER_RUN = 25

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
                log(f"rate limited on {url}, waiting {wait:.1f}s")
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


def log(message: str) -> None:
    print(f"[relay] {message}", flush=True)


# ---------------------------------------------------------------- config/state

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().strip()
    if not text:
        return default
    return json.loads(text)


def save_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


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
    for channel in all_channels:
        # 0 = text, 5 = announcement, 15 = forum. Others have no messages.
        if channel.get("type") not in (0, 5, 15):
            continue
        by_norm.setdefault(normalise(channel.get("name", "")), channel)

    resolved = {}
    missing = []
    for name in wanted_names:
        key = normalise(name)
        match = by_norm.get(key)
        if match is None:
            candidates = [c for k, c in by_norm.items() if k.endswith(key) or key in k]
            match = candidates[0] if len(candidates) == 1 else None
        if match is None:
            missing.append(name)
        else:
            resolved[name] = match
    return resolved, missing


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

def format_alert(message, channel_name, guild_id, reason):
    author = message.get("author", {})
    name = author.get("global_name") or author.get("username") or "unknown"
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

    link = (
        f"https://discord.com/channels/{guild_id}/"
        f"{message.get('channel_id')}/{message.get('id')}"
    )

    return (
        f"<b>#{html.escape(channel_name)}</b> "
        f"<i>{html.escape(reason)}</i>\n"
        f"<b>{html.escape(name)}</b>: {html.escape(content)}\n"
        f'<a href="{html.escape(link)}">open in Discord</a>'
    )


def send_telegram(token, chat_id, text, dry_run=False):
    if dry_run:
        print("---- would send ----")
        print(text)
        return
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    request_with_retry(
        "POST",
        url,
        payload={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


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
            # The bot is not in the server yet, or has been removed. This is a
            # normal waiting state, not a crash: exit clean so the run stays
            # green and the log says plainly what is missing.
            log(f"cannot read guild {guild_id}: {exc}")
            log("The bot is not in the server yet, or it lost View Channels.")
            log("Ask an admin to authorise the invite link, then this run")
            log("will start picking up channels on its own. Nothing to fix here.")
            return state, 0
        raise

    resolved, missing = resolve_channels(all_channels, list(channels_cfg.keys()))
    for name in missing:
        log(f"WARNING: channel '{name}' not found or bot cannot see it")

    alerts = []
    seen = state.setdefault("last_message_id", {})

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

        if not messages:
            continue

        # First time we see a channel: record the position, do not replay the
        # backlog into Telegram.
        if after is None:
            seen[name] = messages[-1]["id"]
            log(f"#{name}: first run, baseline set at message {seen[name]}")
            continue

        seen[name] = messages[-1]["id"]

        for message in messages:
            reason = message_matches(message, channel_cfg, watch_ids, keyword_re)
            if reason:
                alerts.append((message, name, reason))

        log(f"#{name}: {len(messages)} new message(s)")

    if not alerts:
        log("no alerts this run")
        return state, 0

    overflow = 0
    if len(alerts) > MAX_ALERTS_PER_RUN:
        overflow = len(alerts) - MAX_ALERTS_PER_RUN
        alerts = alerts[:MAX_ALERTS_PER_RUN]

    for message, channel_name, reason in alerts:
        text = format_alert(message, channel_name, guild_id, reason)
        send_telegram(telegram_token, chat_id, text, dry_run=dry_run)
        time.sleep(0.05)

    if overflow:
        send_telegram(
            telegram_token,
            chat_id,
            f"<i>plus {overflow} more matching message(s) this cycle, "
            f"check Discord</i>",
            dry_run=dry_run,
        )

    log(f"sent {len(alerts)} alert(s)")
    return state, len(alerts)


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

    try:
        state, _ = run(
            config,
            state,
            Discord(discord_token),
            telegram_token,
            chat_id,
            dry_run=dry_run,
        )
    finally:
        # Always persist progress, even if a later channel blew up, so a
        # failure cannot cause the same messages to alert twice.
        save_json(state_path, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
