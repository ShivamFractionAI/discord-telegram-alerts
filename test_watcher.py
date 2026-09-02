"""Offline tests for the relay. No network, no tokens needed: python3 test_watcher.py"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher


GUILD = "1226860543529451561"


def msg(mid, content="", author="alice", bot=False, mentions=(), roles=(),
        everyone=False, channel_id="111"):
    return {
        "id": str(mid),
        "channel_id": channel_id,
        "content": content,
        "author": {"id": "9", "username": author, "bot": bot},
        "mentions": [{"id": m} for m in mentions],
        "mention_roles": list(roles),
        "mention_everyone": everyone,
        "attachments": [],
        "embeds": [],
    }


class FakeDiscord:
    """Stands in for the Discord REST API."""

    def __init__(self, channels, messages):
        self._channels = channels
        self._messages = messages
        self.calls = []

    def guild_channels(self, guild_id):
        return self._channels

    def messages_after(self, channel_id, after=None, limit=100):
        self.calls.append((channel_id, after))
        batch = self._messages.get(channel_id, [])
        if after is None:
            return batch[-limit:]
        return [m for m in batch if int(m["id"]) > int(after)][:limit]


class Sent(list):
    def send(self, token, chat_id, alert, dry_run=False):
        self.append(alert["html"] if isinstance(alert, dict) else alert)
        return True


class NormaliseTests(unittest.TestCase):
    def test_strips_emoji_and_separators(self):
        self.assertEqual(watcher.normalise("⛓・dev-rel"), "dev-rel")
        self.assertEqual(watcher.normalise("🚨・scam-report"), "scam-report")
        self.assertEqual(watcher.normalise("lets-play😎"), "lets-play")
        self.assertEqual(watcher.normalise("💬・general"), "general")

    def test_resolves_decorated_channel_names(self):
        channels = [
            {"id": "111", "name": "⛓・dev-rel", "type": 0},
            {"id": "222", "name": "💬・general", "type": 0},
            {"id": "333", "name": "General Voice", "type": 2},
        ]
        resolved, missing, ambiguous = watcher.resolve_channels(
            channels, ["dev-rel", "general"]
        )
        self.assertEqual(resolved["dev-rel"]["id"], "111")
        self.assertEqual(resolved["general"]["id"], "222")
        self.assertEqual(missing, [])

    def test_reports_unknown_channel(self):
        resolved, missing, ambiguous = watcher.resolve_channels(
            [{"id": "111", "name": "⛓・dev-rel", "type": 0}], ["support"]
        )
        self.assertEqual(missing, ["support"])
        self.assertEqual(resolved, {})


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.kw = watcher.compile_keywords(["bug", "not working", "can't", "stuck"])

    def match(self, message, cfg=None, ids=frozenset()):
        return watcher.message_matches(message, cfg or {"mode": "keywords"}, set(ids), self.kw)

    def test_keyword_hit(self):
        self.assertIn("bug", self.match(msg(1, "there is a bug in the deposit modal")))

    def test_multiword_keyword(self):
        self.assertIsNotNone(self.match(msg(1, "swap is not working for me")))

    def test_apostrophe_keyword(self):
        self.assertIsNotNone(self.match(msg(1, "I can't connect my wallet")))

    def test_no_substring_false_positive(self):
        # "debugging" and "bugatti" must not trigger the "bug" keyword.
        self.assertIsNone(self.match(msg(1, "debugging the contract now")))
        self.assertIsNone(self.match(msg(1, "nice bugatti")))

    def test_clean_message_ignored(self):
        self.assertIsNone(self.match(msg(1, "gm everyone, great launch")))

    def test_bots_ignored_by_default(self):
        self.assertIsNone(self.match(msg(1, "bug detected in build", bot=True)))

    def test_bots_included_when_configured(self):
        cfg = {"mode": "keywords", "include_bots": True}
        self.assertIsNotNone(self.match(msg(1, "bug detected in build", bot=True), cfg))

    def test_all_mode_forwards_everything(self):
        self.assertEqual(self.match(msg(1, "gm"), {"mode": "all"}), "all messages")

    def test_user_mention(self):
        got = self.match(msg(1, "hey <@42> look", mentions=["42"]), ids={"42"})
        self.assertEqual(got, "mention")

    def test_role_mention(self):
        got = self.match(msg(1, "<@&77> please", roles=["77"]), ids={"77"})
        self.assertEqual(got, "mention")

    def test_other_peoples_mentions_ignored(self):
        self.assertIsNone(self.match(msg(1, "hey <@99>", mentions=["99"]), ids={"42"}))

    def test_everyone_opt_in(self):
        self.assertEqual(
            self.match(msg(1, "@everyone", everyone=True), ids={"everyone"}), "@everyone"
        )
        self.assertIsNone(self.match(msg(1, "@everyone", everyone=True), ids={"42"}))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.channels = [{"id": "111", "name": "⛓・dev-rel", "type": 0}]
        self.config = {
            "guild_id": GUILD,
            "keywords": ["bug", "stuck"],
            "watch_mention_ids": [],
            "channels": {"dev-rel": {"mode": "keywords"}},
        }
        self.sent = Sent()
        self._real_send = watcher.send_telegram
        watcher.send_telegram = self.sent.send
        # Real runs pace sends to respect Telegram's rate limit. Tests do not
        # need to sit through it.
        self._real_interval = watcher.SEND_INTERVAL_SECONDS
        watcher.SEND_INTERVAL_SECONDS = 0

    def tearDown(self):
        watcher.send_telegram = self._real_send
        watcher.SEND_INTERVAL_SECONDS = self._real_interval

    def go(self, messages, state):
        api = FakeDiscord(self.channels, {"111": messages})
        return watcher.run(self.config, state, api, "tg", "chat")

    def test_first_run_does_not_replay_backlog(self):
        backlog = [msg(i, "old bug report") for i in range(1, 6)]
        state, count = self.go(backlog, {})
        self.assertEqual(count, 0, "first run must not flood the chat with history")
        self.assertEqual(state["last_message_id"]["dev-rel"], "5")
        self.assertEqual(self.sent, [])

    def test_second_run_alerts_only_new_matches(self):
        messages = [msg(1, "gm"), msg(2, "found a bug"), msg(3, "unrelated chatter")]
        state = {"last_message_id": {"dev-rel": "1"}}
        state, count = self.go(messages, state)
        self.assertEqual(count, 1)
        self.assertIn("found a bug", self.sent[0])
        self.assertEqual(state["last_message_id"]["dev-rel"], "3")

    def test_no_duplicate_on_repeated_runs(self):
        messages = [msg(1, "gm"), msg(2, "found a bug")]
        state = {"last_message_id": {"dev-rel": "1"}}
        state, first = self.go(messages, state)
        self.sent.clear()
        state, second = self.go(messages, state)
        self.assertEqual((first, second), (1, 0), "same message must not alert twice")

    def test_flood_is_capped_and_deferred_not_dropped(self):
        """Over the cap, the cursor must stay put so nothing is lost."""
        state = {"last_message_id": {"dev-rel": "0"}}
        flood = [msg(i, "bug") for i in range(1, 40)]
        state, count = self.go(flood, state)
        self.assertEqual(count, watcher.MAX_ALERTS_PER_CHANNEL)
        cursor = int(state["last_message_id"]["dev-rel"])
        self.assertLess(cursor, 39, "cursor must not skip past deferred messages")
        self.assertEqual(cursor, watcher.MAX_ALERTS_PER_CHANNEL)

    def test_deferred_messages_arrive_on_the_next_run(self):
        state = {"last_message_id": {"dev-rel": "0"}}
        flood = [msg(i, "bug") for i in range(1, 40)]
        state, first = self.go(flood, state)
        self.sent.clear()
        state, second = self.go(flood, state)
        self.assertEqual(second, watcher.MAX_ALERTS_PER_CHANNEL)
        self.assertGreater(first + second, watcher.MAX_ALERTS_PER_CHANNEL)

    def test_alert_contains_jump_link(self):
        state = {"last_message_id": {"dev-rel": "1"}}
        self.go([msg(2, "bug here")], state)
        self.assertIn(f"https://discord.com/channels/{GUILD}/111/2", self.sent[0])

    def test_html_in_message_is_escaped(self):
        state = {"last_message_id": {"dev-rel": "1"}}
        self.go([msg(2, "bug <script>alert(1)</script>")], state)
        self.assertNotIn("<script>", self.sent[0])
        self.assertIn("&lt;script&gt;", self.sent[0])

    def test_long_message_is_truncated(self):
        state = {"last_message_id": {"dev-rel": "1"}}
        self.go([msg(2, "bug " + "x" * 3000)], state)
        self.assertLess(len(self.sent[0]), 4096)

    def test_escape_expansion_cannot_exceed_telegram_limit(self):
        """html.escape turns one apostrophe into six characters.

        Truncating before escaping is not enough: 900 apostrophes become a
        5535 character payload, Telegram answers 400, and without this clamp
        the same message kills every run forever.
        """
        for payload in ("'" * 3000, "&" * 3000, "<" * 3000, "\"" * 3000):
            state = {"last_message_id": {"dev-rel": "1"}}
            self.sent.clear()
            self.go([msg(2, "bug " + payload)], state)
            self.assertTrue(self.sent, "alert should still be sent")
            self.assertLessEqual(
                len(self.sent[0]), 4096, "escaped payload exceeded Telegram's cap"
            )

    def test_relayed_urls_are_not_auto_linked(self):
        """A hostile link must not arrive as a tappable link."""
        state = {"last_message_id": {"dev-rel": "1"}}
        self.go([msg(2, "bug at https://discord-support.example/verify")], state)
        body = self.sent[0]
        self.assertIn("<code>", body)
        start = body.index("<code>")
        end = body.index("</code>")
        self.assertIn("discord-support.example", body[start:end])
        self.assertNotIn("<a href", body[start:end])

    def test_missing_channel_does_not_crash(self):
        self.config["channels"]["nope"] = {"mode": "all"}
        state, count = self.go([msg(1, "bug")], {"last_message_id": {"dev-rel": "0"}})
        self.assertEqual(count, 1)

    def test_bot_not_in_guild_exits_clean(self):
        """403 on the guild means the bot is not added yet, not a crash."""
        api = FakeDiscord(self.channels, {})

        def denied(guild_id):
            raise watcher.HttpError(403, '{"message": "Missing Access", "code": 50001}')

        api.guild_channels = denied
        state, count = watcher.run(self.config, {}, api, "tg", "chat")
        self.assertEqual(count, 0)
        self.assertEqual(self.sent, [])

    def test_guild_not_found_exits_clean(self):
        api = FakeDiscord(self.channels, {})

        def missing(guild_id):
            raise watcher.HttpError(404, '{"message": "Unknown Guild"}')

        api.guild_channels = missing
        state, count = watcher.run(self.config, {}, api, "tg", "chat")
        self.assertEqual(count, 0)

    def test_bad_token_still_fails_loudly(self):
        """A revoked token must go red, not hide behind a green run."""
        api = FakeDiscord(self.channels, {})

        def unauthorized(guild_id):
            raise watcher.HttpError(401, '{"message": "401: Unauthorized"}')

        api.guild_channels = unauthorized
        with self.assertRaises(watcher.HttpError):
            watcher.run(self.config, {}, api, "tg", "chat")

    def test_unreadable_channel_is_skipped(self):
        api = FakeDiscord(self.channels, {})

        def boom(channel_id, after=None, limit=100):
            raise watcher.HttpError(403, "Missing Access")

        api.messages_after = boom
        state, count = watcher.run(
            self.config, {"last_message_id": {"dev-rel": "0"}}, api, "tg", "chat"
        )
        self.assertEqual(count, 0)


class StateFileTests(unittest.TestCase):
    def test_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            watcher.save_json(path, {"last_message_id": {"dev-rel": "42"}})
            self.assertEqual(
                watcher.load_json(path, {})["last_message_id"]["dev-rel"], "42"
            )

    def test_empty_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            open(path, "w").close()
            self.assertEqual(watcher.load_json(path, {}), {})

    def test_shipped_config_is_valid(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "config.json")) as handle:
            config = json.load(handle)
        self.assertIn("guild_id", config)
        for name, cfg in config["channels"].items():
            self.assertIn(cfg.get("mode"), ("all", "keywords", "mentions", "any"), name)




class DeliveryFailureTests(unittest.TestCase):
    """One bad message must never wedge the relay or lose the good ones."""

    def setUp(self):
        self.channels = [{"id": "111", "name": "⛓・dev-rel", "type": 0}]
        self.config = {
            "guild_id": GUILD,
            "keywords": ["bug"],
            "watch_mention_ids": [],
            "channels": {"dev-rel": {"mode": "keywords"}},
        }
        self._real = watcher.send_telegram
        self._real_interval = watcher.SEND_INTERVAL_SECONDS
        watcher.SEND_INTERVAL_SECONDS = 0

    def tearDown(self):
        watcher.send_telegram = self._real
        watcher.SEND_INTERVAL_SECONDS = self._real_interval

    def test_undeliverable_message_does_not_block_later_ones(self):
        delivered = []

        def flaky(token, chat_id, alert, dry_run=False):
            if "poison" in alert["plain"]:
                return False
            delivered.append(alert["plain"])
            return True

        watcher.send_telegram = flaky
        api = FakeDiscord(
            self.channels,
            {"111": [msg(2, "bug poison"), msg(3, "bug real one")]},
        )
        state, count = watcher.run(
            self.config, {"last_message_id": {"dev-rel": "1"}}, api, "tg", "chat"
        )
        self.assertEqual(count, 1)
        self.assertTrue(any("real one" in d for d in delivered))
        self.assertEqual(state["last_message_id"]["dev-rel"], "3")

    def test_cursor_does_not_skip_past_undelivered_on_fatal_error(self):
        sent = []

        def dies_on_second(token, chat_id, alert, dry_run=False):
            if len(sent) >= 1:
                raise watcher.FatalTelegramError("bot blocked")
            sent.append(alert)
            return True

        watcher.send_telegram = dies_on_second
        api = FakeDiscord(
            self.channels, {"111": [msg(2, "bug one"), msg(3, "bug two")]}
        )
        state = {"last_message_id": {"dev-rel": "1"}}
        with self.assertRaises(watcher.FatalTelegramError):
            watcher.run(self.config, state, api, "tg", "chat")
        self.assertEqual(
            state["last_message_id"]["dev-rel"], "2",
            "must keep the delivered one and retry the undelivered one",
        )


class RedactionTests(unittest.TestCase):
    def test_bot_token_is_stripped_from_urls(self):
        url = "https://api.telegram.org/bot8917233270:AAHsecretvalue/sendMessage"
        out = watcher.redact(f"rate limited on {url}, waiting 3s")
        self.assertNotIn("AAHsecretvalue", out)
        self.assertNotIn("8917233270", out)
        self.assertIn("/bot***/", out)


class ChannelAmbiguityTests(unittest.TestCase):
    def test_duplicate_normalised_names_are_refused(self):
        channels = [
            {"id": "111", "name": "🔒・general", "type": 0},
            {"id": "222", "name": "💬・general", "type": 0},
        ]
        resolved, missing, ambiguous = watcher.resolve_channels(channels, ["general"])
        self.assertEqual(ambiguous, ["general"])
        self.assertEqual(resolved, {})

    def test_fuzzy_match_does_not_bind_a_prefix_channel(self):
        channels = [{"id": "111", "name": "general-off-topic", "type": 0}]
        resolved, missing, ambiguous = watcher.resolve_channels(channels, ["general"])
        self.assertEqual(missing, ["general"])


class StateFileCorruptionTests(unittest.TestCase):
    def test_truncated_state_does_not_crash_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as h:
                h.write('{"last_message_id": {"dev-r')
            self.assertEqual(watcher.load_json(path, {}), {})

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            watcher.save_json(path, {"last_message_id": {"dev-rel": "42"}})
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(
                watcher.load_json(path, {})["last_message_id"]["dev-rel"], "42"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
