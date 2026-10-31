"""Offline regression tests: no Telegram messages, no Maintainerr mutations."""
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import bot
import close_polls
import covers


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dbpath = Path(self.tempdir.name) / "test.sqlite"
        self.db_patcher = patch.object(bot, "DB", self.dbpath)
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)
        self.db = bot.database()
        self.addCleanup(self.db.close)

    def insert(self, mid, days_old=3):
        self.db.execute(
            "INSERT INTO polls(collection_id,media_id,title,message_id,poll_id,opened_at,state)"
            " VALUES (1,?,?,?,?,?,?)",
            (mid, "Sample " + mid, int(mid), "poll-" + mid,
             bot.iso(bot.now() - timedelta(days=days_old)), "open"))
        self.db.commit()
        return self.db.execute("SELECT * FROM polls WHERE media_id=?", (mid,)).fetchone()

    def test_preview_age_filter(self):
        self.insert("1", days_old=4)
        self.insert("2", days_old=1)
        rows = self.db.execute("SELECT * FROM polls").fetchall()
        eligible = close_polls.eligible(rows, 3, bot.now())
        self.assertEqual([r[1] for r in eligible], ["1"])
        self.assertEqual(len(close_polls.eligible(rows, 0, bot.now())), 2)

    def test_veto_beats_postpone_and_delete(self):
        rec = self.insert("1", days_old=30)
        calls = []
        def telegram(method, data):
            calls.append((method, data))
            return {"options": [
                {"voter_count": 1},
                {"voter_count": 12},
                {"voter_count": 99}]}
        def maintain(path, body=None, method=None):
            if path == "/api/rules/exclusions/bulk":
                return {"results": [{"code": 1}]}
            if path.startswith("/api/rules/exclusion?"):
                return [{"id": 11, "mediaServerId": "1", "ruleGroupId": 1}]
            self.fail("Unexpected Maintainerr operation: " + path)
        with patch.object(bot, "tg", telegram), patch.object(bot, "maintain", maintain):
            bot.close_due(self.db, rec, min_age_days=3)
        state, snooze_until = self.db.execute(
            "SELECT state,snooze_until FROM polls WHERE media_id='1'").fetchone()
        self.assertEqual(state, "vetoed")
        self.assertIsNone(snooze_until)
        self.assertEqual([call[0] for call in calls], ["stopPoll"])

    def test_snooze_creates_only_temporary_owned_exclusion(self):
        rec = self.insert("1", days_old=30)
        created = []
        def maintain(path, body=None, method=None):
            if path == "/api/rules/exclusions/bulk":
                created.append(True)
                return {"results": [{"code": 1}]}
            if path.startswith("/api/rules/exclusion?"):
                return ([{"id": 21, "mediaServerId": "1", "ruleGroupId": 1}]
                        if created else [])
            self.fail("Unexpected Maintainerr operation: " + path)
        with patch.object(bot, "tg", return_value={"options": [
            {"voter_count": 0}, {"voter_count": 2}, {"voter_count": 10}]}), \
             patch.object(bot, "maintain", maintain):
            bot.close_due(self.db, rec, min_age_days=3)
        state, owned, due = self.db.execute(
            "SELECT state,owned_exclusion_ids,snooze_until FROM polls WHERE media_id='1'").fetchone()
        self.assertEqual(state, "snoozed")
        self.assertEqual(owned, "[21]")
        self.assertIsNotNone(due)

    def test_younger_poll_does_not_close(self):
        rec = self.insert("1", days_old=1)
        with patch.object(bot, "tg") as telegram:
            bot.close_due(self.db, rec, min_age_days=3)
        telegram.assert_not_called()
        self.assertEqual(
            self.db.execute("SELECT state FROM polls").fetchone()[0], "open")

    def test_cover_url_is_strict(self):
        self.assertEqual(
            covers.poster_url({"image_path": "https://image.tmdb.org/t/p/w500/abc.jpg"}),
            "https://image.tmdb.org/t/p/w185/abc.jpg")
        self.assertIsNone(
            covers.poster_url({"image_path": "https://evil.example/t/p/w500/abc.jpg"}))


if __name__ == "__main__":
    unittest.main()
