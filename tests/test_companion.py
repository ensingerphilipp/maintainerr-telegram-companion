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


    def test_http_429_keeps_retry_after_parameters(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Too Many Requests", {},
            io.BytesIO(b'{"description":"Too Many Requests","parameters":{"retry_after":17}}'))
        with patch.object(bot, "api", side_effect=error):
            with self.assertRaises(bot.TelegramAPIError) as caught:
                bot.telegram("stopPoll", {})
        self.assertEqual(caught.exception.retry_after, 17)

    def test_429_response_waits_then_repeats_identical_request(self):
        body = {"chat_id": "chat", "message_id": 123}
        responses = [
            {"ok": False, "error_code": 429, "description": "Too Many Requests",
             "parameters": {"retry_after": 12}},
            {"ok": True, "result": {"done": True}},
        ]
        with patch.object(bot, "telegram", side_effect=responses) as telegram, \
             patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot.tg("stopPoll", body), {"done": True})
        sleep.assert_called_once_with(13)
        self.assertEqual(telegram.call_args_list, [
            unittest.mock.call("stopPoll", body), unittest.mock.call("stopPoll", body)])

    def test_http_429_wait_is_not_shortened_and_long_wait_is_chunked(self):
        error = bot.TelegramAPIError("stopPoll", 429, "rate limited", {"retry_after": 125})
        with patch.object(bot, "telegram", side_effect=[error, {"ok": True, "result": True}]), \
             patch.object(bot.time, "sleep") as sleep:
            self.assertTrue(bot.tg("stopPoll", {}))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [60, 60, 6])

    def test_repeated_429_uses_bounded_backoff_without_losing_open_state(self):
        rec = self.insert("1")
        error = bot.TelegramAPIError("stopPoll", 429, "rate limited")
        with patch.object(bot, "telegram", side_effect=error) as telegram, \
             patch.object(bot.time, "sleep") as sleep:
            with self.assertRaises(bot.TelegramAPIError):
                bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(telegram.call_count, 6)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10, 20, 40, 60])
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "open")
        self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 0)

    def test_non_429_and_network_timeouts_are_not_retried(self):
        for error in (bot.TelegramAPIError("sendPoll", 400, "bad request"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(bot, "telegram", side_effect=error) as telegram, \
                     patch.object(bot.time, "sleep") as sleep:
                    with self.assertRaises(type(error)):
                        bot.tg("sendPoll", {})
                telegram.assert_called_once()
                sleep.assert_not_called()

    def test_scheduler_closure_succeeds_after_429(self):
        rec = self.insert("1", days_old=30)
        error = bot.TelegramAPIError("stopPoll", 429, "rate limited", {"retry_after": 1})
        with patch.object(bot, "telegram", side_effect=[
                error, {"ok": True, "result": self.closed_poll()}]), \
             patch.object(bot.time, "sleep"):
            bot.close_due(self.db, rec)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "released")

    def test_manual_cli_succeeds_after_429(self):
        from contextlib import nullcontext, redirect_stdout
        import io
        self.insert("1")
        error = bot.TelegramAPIError("stopPoll", 429, "rate limited", {"retry_after": 1})
        with patch.object(bot, "telegram", side_effect=[
                error, {"ok": True, "result": self.closed_poll()}]), \
             patch.object(bot.time, "sleep"), \
             patch.object(bot, "TOKEN", "test-token"), patch.object(bot, "CHAT", "test-chat"), \
             patch.object(bot, "execution_lock", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(close_polls.main(["--days", "0", "--execute"]), 0)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "released")

    def test_cached_closed_error_holds_poll_without_reading_message_or_applying_votes(self):
        rec = self.insert("1")
        error = bot.TelegramAPIError("stopPoll", 400, "Bad Request: poll can't be stopped")
        with patch.object(bot, "tg", side_effect=error) as telegram, \
             patch.object(bot, "maintain") as maintain:
            with self.assertRaisesRegex(bot.PollClosureUnverified, "no successful stopPoll"):
                bot.close_due(self.db, rec, min_age_days=0)
        telegram.assert_called_once_with("stopPoll", {"chat_id": bot.CHAT, "message_id": 1})
        maintain.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state,keep_votes FROM polls").fetchone(),
                         ("closure_unverified", None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 0)

    def test_429_followed_by_cached_closed_error_never_finalizes_votes(self):
        rec = self.insert("1")
        errors = [
            bot.TelegramAPIError("stopPoll", 429, "rate limited", {"retry_after": 1}),
            bot.TelegramAPIError("stopPoll", 400, "Bad Request: poll can't be stopped"),
        ]
        with patch.object(bot, "telegram", side_effect=errors) as telegram, \
             patch.object(bot.time, "sleep") as sleep, patch.object(bot, "maintain") as maintain:
            with self.assertRaises(bot.PollClosureUnverified):
                bot.close_due(self.db, rec, min_age_days=0)
        sleep.assert_called_once_with(2)
        self.assertEqual(telegram.call_count, 2)
        maintain.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "closure_unverified")

    def test_migration_holds_legacy_cache_and_preserves_votes_and_protection_ownership(self):
        self.insert("1")
        self.db.execute("UPDATE polls SET state='snoozed',keep_votes=0,postpone_votes=2,"
                        "owned_exclusion_ids='[77]',snooze_until='2027-01-01T00:00:00+00:00'")
        self.db.execute("DROP TABLE closed_poll_results")
        self.db.execute("CREATE TABLE closed_poll_results("
                        "poll_id TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_at TEXT NOT NULL)")
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?)",
                        ("poll-1", '{"is_closed":true}', bot.iso(bot.now())))
        self.db.commit()
        with patch.object(bot, "maintain") as maintain, patch.object(bot, "tg") as telegram:
            upgraded = bot.database()
            upgraded.close()
            upgraded = bot.database()
            upgraded.close()
        maintain.assert_not_called()
        telegram.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state,postpone_votes,owned_exclusion_ids,snooze_until "
                                         "FROM polls").fetchone(),
                         ("closure_unverified", 2, "[77]", "2027-01-01T00:00:00+00:00"))
        self.assertEqual(self.db.execute("SELECT source FROM closed_poll_results").fetchone()[0], "unverified")
        self.assertEqual(self.db.execute("SELECT prior_state FROM poll_closure_reviews").fetchall(), [("snoozed",)])

    def test_migration_keeps_acknowledged_cache_trusted(self):
        self.insert("1")
        self.db.execute("UPDATE polls SET state='released'")
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?,'stopPoll')",
                        ("poll-1", "{}", bot.iso(bot.now())))
        self.db.commit()
        upgraded = bot.database()
        upgraded.close()
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "released")
        self.assertEqual(self.db.execute("SELECT count(*) FROM poll_closure_reviews").fetchone()[0], 0)

    def test_default_paths_do_not_close_held_polls(self):
        rec = self.insert("1", days_old=40)
        self.db.execute("UPDATE polls SET state='closure_unverified'")
        self.db.commit()
        rec = self.db.execute("SELECT * FROM polls").fetchone()
        with patch.object(bot, "tg") as telegram:
            bot.close_due(self.db, rec)
        telegram.assert_not_called()
        self.assertEqual(close_polls.eligible([rec], 0, bot.now()), [])
        self.assertEqual(close_polls.eligible([rec], 0, bot.now(), include_unverified=True), [rec])

    def test_explicit_retry_replaces_untrusted_counts_only_after_successful_stop(self):
        import json
        self.insert("1")
        self.db.execute("UPDATE polls SET state='closure_unverified',keep_votes=0,delete_votes=7")
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?,'unverified')",
                        ("poll-1", json.dumps(self.closed_poll(delete=7)), bot.iso(bot.now())))
        self.db.commit()
        rec = self.db.execute("SELECT * FROM polls").fetchone()
        with patch.object(bot, "tg", return_value=self.closed_poll(keep=1, delete=7)) as telegram, \
             patch.object(bot.voting, "permanent_veto") as veto:
            bot.close_due(self.db, rec, min_age_days=0, retry_unverified=True)
        telegram.assert_called_once()
        veto.assert_called_once()
        self.assertEqual(self.db.execute("SELECT state,keep_votes FROM polls").fetchone(), ("veto_pending", 1))
        self.assertEqual(self.db.execute("SELECT source FROM closed_poll_results").fetchone()[0], "stopPoll")

    def test_explicit_retry_cannot_reuse_untrusted_cached_zero_veto(self):
        import json
        self.insert("1")
        self.db.execute("UPDATE polls SET state='closure_unverified',keep_votes=0,delete_votes=7")
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?,'unverified')",
                        ("poll-1", json.dumps(self.closed_poll(delete=7)), bot.iso(bot.now())))
        self.db.commit()
        rec = self.db.execute("SELECT * FROM polls").fetchone()
        with patch.object(bot, "tg", side_effect=bot.TelegramAPIError(
                "stopPoll", 400, "Bad Request: poll has already been closed")), \
             patch.object(bot, "maintain") as maintain:
            with self.assertRaises(bot.PollClosureUnverified):
                bot.close_due(self.db, rec, min_age_days=0, retry_unverified=True)
        maintain.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "closure_unverified")
        self.assertEqual(self.db.execute("SELECT source FROM closed_poll_results").fetchone()[0], "unverified")

    def test_manual_cli_explicit_retry_reports_unverified_failure(self):
        from contextlib import nullcontext, redirect_stdout
        import io
        self.insert("1")
        self.db.execute("UPDATE polls SET state='closure_unverified'")
        self.db.commit()
        with patch.object(bot, "telegram", side_effect=bot.TelegramAPIError(
                "stopPoll", 400, "Bad Request: poll can't be stopped")), \
             patch.object(bot, "TOKEN", "test-token"), patch.object(bot, "CHAT", "test-chat"), \
             patch.object(bot, "execution_lock", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()), self.assertLogs(level="ERROR"):
            code = close_polls.main(["--days", "0", "--retry-unverified", "--execute"])
        self.assertEqual(code, 1)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "closure_unverified")

    def test_successful_stop_with_wrong_identity_or_open_flag_is_not_saved(self):
        rec = self.insert("1")
        for field, value in (("is_closed", False), ("id", "different-poll")):
            with self.subTest(field=field):
                poll = self.closed_poll()
                poll[field] = value
                with patch.object(bot, "tg", return_value=poll):
                    with self.assertRaisesRegex(RuntimeError, "identity or closed state"):
                        bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM closed_poll_results").fetchone()[0], 0)

    def test_cover_url_is_strict(self):
        self.assertEqual(
            covers.poster_url({"image_path": "https://image.tmdb.org/t/p/w500/abc.jpg"}),
            "https://image.tmdb.org/t/p/w185/abc.jpg")
        self.assertIsNone(
            covers.poster_url({"image_path": "https://evil.example/t/p/w500/abc.jpg"}))


if __name__ == "__main__":
    unittest.main()
