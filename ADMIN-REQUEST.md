# For the server admin

Someone on your team wants to add this to the server. Here is exactly what it
does and what it can reach, so you can approve or refuse in about a minute.

## What it is

A read-only relay. Every 5 minutes it asks Discord "any new messages in these
specific channels since the last one I saw", keeps the ones matching a keyword
list or an @mention, and forwards them to one person's private Telegram chat.
It is a personal notification tool, not a moderation or analytics bot.

## Permissions it asks for

| Permission | Why |
| --- | --- |
| View Channels | To see the channels it is added to |
| Read Message History | To fetch messages posted since its last check |

That is the entire list. The invite link below carries permission integer
`66560`, which is those two bits and nothing else. Decode it and you will
find no write permission in there.

```
https://discord.com/oauth2/authorize?client_id=1544633846132383784&scope=bot&permissions=66560
```

App name: **Support Relay**, application id `1544633846132383784`.

## What it cannot do

* It cannot send, edit, delete or react to any message. Send Messages is not
  requested, so Discord will reject the attempt at the API level even if the
  code tried.
* It cannot kick, ban, timeout, or change any member or role.
* It cannot create, rename, or delete channels, roles, webhooks or invites.
* It cannot read any channel it has not been explicitly added to. Channel
  level permissions override server level ones, so it sees only what you
  grant it.
* It cannot read DMs.

## Where the data goes

Messages go to one Telegram chat belonging to the person who set it up, and
nowhere else. There is no database, no third party service, and no analytics.
The only thing stored anywhere is the id of the last message seen per channel,
which is a number used to avoid duplicate alerts.

It runs on GitHub Actions on that person's own account. The Discord token
lives in an encrypted GitHub secret.

## The code

`watcher.py` is about 300 lines with no dependencies beyond the Python
standard library, so it is readable in one sitting. The only two Discord
endpoints it ever calls are:

* `GET /guilds/{id}/channels` to look up channel ids by name
* `GET /channels/{id}/messages` to fetch new messages

There are no write calls anywhere in the code. `grep -n "POST\|PATCH\|DELETE" watcher.py`
returns only the Telegram send call.

## If you want to limit it further

Add the bot to a single channel, `app-issues` say, instead of all four. It degrades gracefully:
channels it cannot read are logged and skipped, and the rest keep working.

## To revoke

Server Settings > Integrations > select the bot > Remove, or just remove its
channel permissions. It stops immediately and has no other way in.
