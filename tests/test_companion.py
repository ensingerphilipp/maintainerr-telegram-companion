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
            return {"id": "poll-1", "is_closed": True, "options": [
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
        with patch.object(bot, "tg", return_value={"id": "poll-1", "is_closed": True, "options": [
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

    def closed_poll(self, keep=0, snooze=0, delete=2):
        return {"id": "poll-1", "is_closed": True, "options": [
            {"voter_count": keep}, {"voter_count": snooze}, {"voter_count": delete}]}

    def test_telegram_http_error_preserves_description_and_redacts_token(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError(
            "https://api.telegram.org/botSECRET/stopPoll", 400, "Bad Request",
            {}, io.BytesIO(b'{"description":"Bad Request: poll cannot stop SECRET"}'))
        with patch.object(bot, "TOKEN", "SECRET"), \
             patch.object(bot, "api", side_effect=error):
            with self.assertRaises(bot.TelegramAPIError) as caught:
                bot.telegram("stopPoll", {})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("poll cannot stop [redacted]", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_non_json_telegram_http_error_is_readable(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("https://example.invalid", 502, "Bad Gateway",
                                       {}, io.BytesIO(b"<html>gateway</html>"))
        with patch.object(bot, "api", side_effect=error):
            with self.assertRaisesRegex(bot.TelegramAPIError, "HTTP 502"):
                bot.telegram("stopPoll", {})

    def test_already_closed_poll_recovers_real_veto_and_restores_markup(self):
        rec = self.insert("1")
        poll = self.closed_poll(keep=2, snooze=1, delete=6)
        calls = []
        def telegram(method, data):
            calls.append((method, data))
            if method == "stopPoll":
                raise bot.TelegramAPIError(method, 400, "Bad Request: poll can't be stopped")
            return {"poll": poll}
        with patch.object(bot, "tg", telegram), \
             patch.object(bot.voting, "permanent_veto") as veto, \
             patch.object(bot.voting, "start_snooze") as snooze:
            bot.close_due(self.db, rec, min_age_days=0)
        veto.assert_called_once()
        snooze.assert_not_called()
        self.assertEqual(self.db.execute(
            "SELECT keep_votes,postpone_votes,delete_votes,state FROM polls").fetchone(),
            (2, 1, 6, "veto_pending"))
        self.assertEqual([x[0] for x in calls],
                         ["stopPoll", "editMessageReplyMarkup", "editMessageReplyMarkup"])
        self.assertEqual(calls[-1][1]["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 1)

    def test_recovery_rejects_open_or_different_poll_without_applying_votes(self):
        for field, value in (("is_closed", False), ("id", "another-poll")):
            with self.subTest(field=field):
                rec = self.insert("1")
                poll = self.closed_poll()
                poll[field] = value
                def telegram(method, data):
                    if method == "stopPoll":
                        raise bot.TelegramAPIError(method, 400, "Bad Request: poll can't be stopped")
                    return {"poll": poll}
                with patch.object(bot, "tg", telegram), \
                     patch.object(bot, "maintain") as maintain:
                    with self.assertRaisesRegex(RuntimeError, "matching closed poll"):
                        bot.close_due(self.db, rec, min_age_days=0)
                maintain.assert_not_called()
                self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "open")
                self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 0)
                self.db.execute("DELETE FROM polls")
                self.db.commit()

    def test_unrelated_telegram_error_does_not_trigger_recovery(self):
        rec = self.insert("1")
        error = bot.TelegramAPIError("stopPoll", 400, "Bad Request: message not found")
        with patch.object(bot, "tg", side_effect=error) as telegram:
            with self.assertRaises(bot.TelegramAPIError):
                bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(telegram.call_count, 1)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "open")

    def test_retry_uses_saved_final_counts_after_database_write_failure(self):
        rec = self.insert("1")
        with patch.object(bot, "tg", return_value=self.closed_poll(snooze=2)), \
             patch.object(bot.voting, "save_votes", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                bot.close_due(self.db, rec, min_age_days=0)
        recorded_at = self.db.execute("SELECT recorded_at FROM closed_poll_results").fetchone()[0]
        with patch.object(bot, "tg") as telegram, \
             patch.object(bot.voting, "start_snooze") as snooze:
            bot.close_due(self.db, rec, min_age_days=0)
        telegram.assert_not_called()
        snooze.assert_called_once()
        due = self.db.execute("SELECT snooze_until FROM polls").fetchone()[0]
        self.assertEqual(bot.parse(due), bot.parse(recorded_at) + timedelta(days=60))

    def test_invalid_final_vote_counts_are_not_saved(self):
        rec = self.insert("1")
        poll = self.closed_poll()
        poll["options"][0]["voter_count"] = -1
        with patch.object(bot, "tg", return_value=poll):
            with self.assertRaisesRegex(RuntimeError, "counts"):
                bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 0)

    def test_restore_failure_does_not_discard_verified_results(self):
        rec = self.insert("1")
        calls = [
            bot.TelegramAPIError("stopPoll", 400, "Bad Request: poll has already been closed"),
            {"poll": self.closed_poll()},
            bot.TelegramAPIError("editMessageReplyMarkup", 429, "Too Many Requests"),
        ]
        with patch.object(bot, "tg", side_effect=calls), self.assertLogs(level="WARNING"):
            bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "released")

    def test_cover_url_is_strict(self):
        self.assertEqual(
            covers.poster_url({"image_path": "https://image.tmdb.org/t/p/w500/abc.jpg"}),
            "https://image.tmdb.org/t/p/w185/abc.jpg")
        self.assertIsNone(
            covers.poster_url({"image_path": "https://evil.example/t/p/w500/abc.jpg"}))


if __name__ == "__main__":
    unittest.main()
