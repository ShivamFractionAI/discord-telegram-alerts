# Discord to Telegram alerts

Watches `#app-issues`, `⛓・dev-rel`, `👮・mods-chat` and `💬・general` in the
Fraction AI Discord and pushes anything that looks like an issue to your Telegram, on a 5 minute loop, for free, with no
server to rent or babysit.

How it works: a small read-only Discord bot sits in the server as a pair of
eyes. Every 5 minutes GitHub Actions wakes up, asks Discord "anything new in
these channels since message X", keeps only the messages that match your
keywords or mention you, and forwards them to your Telegram bot. The "since
message X" bookmark lives in `state.json`, which is what stops the same
message alerting twice.

```
Discord channels  ->  read-only bot  ->  GitHub Actions cron  ->  Telegram bot  ->  your phone
```

## Status

The Discord application already exists.

* Name: **Support Relay** (`Support Relay#5445`)
* Application ID: `1544633846132383784`
* Message Content Intent: **on**. Presence and Server Members intents: off,
  the relay does not need them.
* Repo: https://github.com/ShivamFractionAI/discord-telegram-alerts
* Invite link, ready to hand an admin:
  `https://discord.com/oauth2/authorize?client_id=1544633846132383784&scope=bot&permissions=66560`

Still to do: create the Telegram bot, and put the three tokens into GitHub
secrets. Both are in Setup below.

## Before you start

This needs one yes from someone with Manage Server on the Fraction AI
Discord. `ADMIN-REQUEST.md` is written for them: it lists the two permissions,
what the bot provably cannot do, and how to revoke it. Send them that file or
the repo link below.

A message that works:

> Hey, can I add a read-only relay bot to a few channels? It is a personal
> alert setup, it forwards messages matching support keywords to my phone so I
> do not miss issues when I am away from Discord.
>
> Permissions: View Channels and Read Message History, in `app-issues`,
> `dev-rel`, `mods-chat` and `general` only. No Send Messages, no Manage anything, it
> never writes to the server. Invite link:
> https://discord.com/oauth2/authorize?client_id=1544633846132383784&scope=bot&permissions=66560
>
> Code and a full permission breakdown:
> https://github.com/ShivamFractionAI/discord-telegram-alerts
>
> Happy to scope it to just `app-issues` if that is easier.

Everything below is a 10 minute job once that is a yes.

## Cost

Zero, on a public repo. Actions minutes on standard runners are free and
unlimited for public repositories, and both the Discord and Telegram bot APIs
are free with no tier involved.

On a private repo it is different. You get 2000 minutes a month, each run is
billed as a minimum of one minute, and a 5 minute cron is about 8600 runs a
month, so the quota is gone in roughly a week. What happens next depends on
your account: with no payment method on file, GitHub blocks further usage and
the workflow simply stops, which is annoying but free. With a card on file,
overage bills at standard per-minute rates. So if you want this private,
either raise the cron to every 20 minutes and watch the quota, or move the
script to Cloudflare Workers cron, which is free at 1 minute resolution.

Public is the recommended setup. Nothing sensitive lives in the repo: the
tokens are in encrypted secrets, and the only committed state is the id of the
last message seen per channel.

## Files

| File | What it is |
| --- | --- |
| `watcher.py` | The whole relay. Standard library only, no pip install. |
| `config.json` | Which channels, which keywords, which mentions. Edit this. |
| `state.json` | The bookmark. The workflow writes it, you never touch it. |
| `.github/workflows/watch.yml` | The 5 minute schedule. |
| `test_watcher.py` | Offline tests. `python3 test_watcher.py`, no tokens needed. |
| `ADMIN-REQUEST.md` | The permission breakdown to hand a server admin. |

## Setup

Three accounts to wire together. Budget about 10 minutes.

### 1. Telegram bot (2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, pick any display
   name and a username ending in `bot`. Copy the token it gives you, this is
   `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message. Telegram bots
   cannot start a conversation with you, so this handshake is required or
   your alerts will silently go nowhere.
3. Get your chat id: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   look for `"chat":{"id":123456789`. That number is `TELEGRAM_CHAT_ID`.

### 2. Discord bot (5 minutes, needs Manage Server on Fraction AI)

1. ~~Create the application.~~ Done, see Status above.
2. **Bot** tab, click **Reset Token**, copy it. This is
   `DISCORD_BOT_TOKEN`. Treat it like a password.
3. ~~Enable Message Content Intent.~~ Done and verified. Without it every
   message would arrive with an empty `content` field and keyword matching
   would quietly match nothing.
4. **OAuth2 > URL Generator**. Scopes: `bot`. Bot permissions: **View
   Channels** and **Read Message History**, and nothing else. The relay
   never writes to Discord, so do not grant Send Messages.

   The app is already created and this link is ready to send. `66560` is
   exactly those two permission bits, `1024 + 65536`:

   ```
   https://discord.com/oauth2/authorize?client_id=1544633846132383784&scope=bot&permissions=66560
   ```

5. Open that URL, choose the Fraction AI server, authorise. Whoever has
   Manage Server has to be the one to click through this, so send them the
   link.
6. `👮・mods-chat` is almost certainly role-gated, so add the bot to that
   channel's permission list explicitly (channel settings > Permissions >
   add the bot, allow View Channel and Read Message History). A channel
   level deny beats a server level allow, so without this the run logs
   `Missing Access` for that channel and carries on with the others.

### 3. GitHub repo

1. ~~Create a public repo and commit these files.~~ Done:
   <https://github.com/ShivamFractionAI/discord-telegram-alerts>
2. ~~Set workflow permissions to read and write~~ so the run can save its
   bookmark. Done.
3. **Settings > Secrets and variables > Actions > New repository secret**,
   three times: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. This step is yours: tokens should go straight from
   where they are issued into the secret, without a detour through a chat
   window or a notes app.

### 4. First run

1. **Actions** tab, pick **Discord to Telegram alerts**, **Run workflow**.
   The first run only records where each channel currently is and sends
   nothing. That is deliberate, otherwise you would get the entire backlog
   in one burst.
2. Post a message containing the word `bug` in a watched channel, run the
   workflow again, and it should reach Telegram within seconds.

## Tuning `config.json`

Get the exact channel names first, since they carry emoji prefixes like
`⛓・dev-rel`:

```bash
DISCORD_BOT_TOKEN=... python3 watcher.py --list-channels
```

You can still write plain `dev-rel` in the config, the matcher strips emoji
and separators before comparing.

```jsonc
{
  "guild_id": "1226860543529451561",
  "keywords": ["bug", "not working", "stuck"],   // default list for every channel
  "watch_mention_ids": ["YOUR_DISCORD_USER_ID"], // and any role ids you care about
  "channels": {
    "app-issues": { "mode": "all" },
    "dev-rel":    { "mode": "keywords" },
    "mods-chat":  { "mode": "all" },
    "general":    { "mode": "keywords" }
  }
}
```

Modes:

| Mode | Sends |
| --- | --- |
| `keywords` | Keyword hits, plus any mention of you or your roles. The default. |
| `all` | Every human message in that channel. Use sparingly. |
| `mentions` | Only mentions of you or your roles. |
| `any` | Same as `keywords`. |

Per channel keys: `keywords` overrides the global list for that channel,
`include_bots` lets bot posts through (worth turning on for a channel that
is mostly automated posts, noisy everywhere else).

To alert on `@everyone`, put the literal string `"everyone"` in
`watch_mention_ids`.

To get your own Discord user id: Settings > Advanced > Developer Mode on,
then right click your name and Copy User ID.

## Testing without spamming yourself

```bash
python3 test_watcher.py          # 27 offline tests, no tokens, no network
DRY_RUN=1 DISCORD_BOT_TOKEN=... TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=x \
  python3 watcher.py             # polls Discord for real, prints instead of sending
```

The workflow also has a `dry_run` checkbox on manual runs.

## Known limits, so nothing surprises you later

* **5 minutes is the floor.** GitHub's cron will not go faster, and under
  load it often fires 5 to 15 minutes late. Fine for "someone reported a
  bug", not fine for "the site is down right now". For sub-minute alerting
  move the same script to Cloudflare Workers cron.
* **Scheduled workflows switch themselves off after 60 days of no repo
  activity.** GitHub emails you first, and re-enabling is one click. Pushing
  any commit resets the clock.
* **Message edits and deletions are invisible.** Polling only ever sees new
  messages, so an issue reported by editing an old message will not fire.
* **Threads and forum posts are separate channels.** Add the thread by name
  if you need it watched.
* **25 alerts per run is the ceiling.** Beyond that you get one "plus N
  more" line, which keeps a raid or a spam wave from flooding your phone and
  tripping Telegram's flood limits.
* **Never commit the tokens.** They belong in GitHub secrets. If one leaks,
  reset it in the Discord developer portal or via `/revoke` in BotFather.
