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
    def send(self, token, chat_id, text, dry_run=False):
        self.append(text)


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
        resolved, missing = watcher.resolve_channels(channels, ["dev-rel", "general"])
        self.assertEqual(resolved["dev-rel"]["id"], "111")
        self.assertEqual(resolved["general"]["id"], "222")
        self.assertEqual(missing, [])

    def test_reports_unknown_channel(self):
        resolved, missing = watcher.resolve_channels(
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

    def tearDown(self):
        watcher.send_telegram = self._real_send

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

    def test_overflow_is_summarised(self):
        state = {"last_message_id": {"dev-rel": "0"}}
        flood = [msg(i, "bug") for i in range(1, 40)]
        state, count = self.go(flood, state)
        self.assertEqual(count, watcher.MAX_ALERTS_PER_RUN)
        self.assertIn("more matching message", self.sent[-1])

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
        self.assertLess(len(self.sent[0]), 1200, "must stay under Telegram's 4096 cap")

    def test_missing_channel_does_not_crash(self):
        self.config["channels"]["nope"] = {"mode": "all"}
        state, count = self.go([msg(1, "bug")], {"last_message_id": {"dev-rel": "0"}})
        self.assertEqual(count, 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
