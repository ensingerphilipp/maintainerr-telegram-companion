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

real_metadata_exists = bot.metadata_exists
real_pace_telegram = bot.pace_telegram


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
        self.metadata_patcher = patch.object(bot, "metadata_exists", return_value=True)
        self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)
        self.pace_patcher = patch.object(bot, "pace_telegram")
        self.pace_patcher.start()
        self.addCleanup(self.pace_patcher.stop)

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


    def test_unrelated_telegram_error_does_not_apply_outcome(self):
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
        recorded_at = self.db.execute("SELECT recorded_at FROM final_poll_results").fetchone()[0]
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
        self.assertEqual(self.db.execute("SELECT count(*) FROM final_poll_results").fetchone()[0], 0)


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
        self.assertEqual(self.db.execute("SELECT count(*) FROM final_poll_results").fetchone()[0], 0)

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


    def test_successful_stop_with_wrong_identity_or_open_flag_is_not_saved(self):
        rec = self.insert("1")
        for field, value in (("is_closed", False), ("id", "different-poll")):
            with self.subTest(field=field):
                poll = self.closed_poll()
                poll[field] = value
                with patch.object(bot, "tg", return_value=poll):
                    with self.assertRaisesRegex(RuntimeError, "identity or closed state"):
                        bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM final_poll_results").fetchone()[0], 0)

    def test_http_failure_logs_full_reason_context_headers_and_redacts_tokens(self):
        import io
        import json
        import urllib.error
        response = {"ok": False, "error_code": 400, "description": "Bad Request: SECRET",
                    "parameters": {"retry_after": 31},
                    "debug": "https://api.telegram.org/botOTHER-CREDENTIAL/stopPoll"}
        error = urllib.error.HTTPError(
            "https://api.telegram.org/botSECRET/stopPoll", 400, "Bad Request",
            {"X-Request-ID": "trace-42", "Date": "today"},
            io.BytesIO(json.dumps(response).encode()))
        with patch.object(bot, "TOKEN", "SECRET"), patch.object(bot, "api", side_effect=error), \
             self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(bot.TelegramAPIError):
                bot.tg("stopPoll", {"chat_id": -123, "message_id": 90,
                                    "unrelated_private_field": "DO-NOT-LOG-ME"})
        text = "\n".join(logs.output)
        for expected in ("event=telegram_request_failed", "request_id=", "method=stopPoll",
                         '"message_id": 90', "http_status=400", "error_code=400",
                         "retry_after=31", "will_retry=False", "trace-42",
                         "response=", "[redacted]"):
            self.assertIn(expected, text)
        for secret in ("SECRET", "OTHER-CREDENTIAL", "DO-NOT-LOG-ME"):
            self.assertNotIn(secret, text)

    def test_network_failure_logs_reason_without_leaking_token_in_traceback(self):
        import traceback
        import urllib.error
        error = urllib.error.URLError("connection failed https://api.telegram.org/botSECRET/sendPoll")
        with patch.object(bot, "TOKEN", "SECRET"), patch.object(bot, "api", side_effect=error), \
             self.assertLogs(level="ERROR") as logs:
            try:
                bot.tg("sendPoll", {"chat_id": -123})
                self.fail("Expected a transport error")
            except bot.TelegramTransportError as exc:
                trace = "".join(traceback.format_exception(exc))
        text = "\n".join(logs.output)
        self.assertIn("event=telegram_transport_failed", text)
        self.assertIn("connection failed", text)
        self.assertIn("outcome=unknown", text)
        self.assertNotIn("SECRET", text + trace)
        self.assertIn("[redacted]", trace)

    def test_retry_and_success_logs_share_request_correlation_id(self):
        import re
        response = {"ok": False, "error_code": 429, "description": "rate limited",
                    "parameters": {"retry_after": 2}}
        with patch.object(bot, "telegram", side_effect=[response, {"ok": True, "result": True}]), \
             patch.object(bot.time, "sleep"), self.assertLogs(level="INFO") as logs:
            self.assertTrue(bot.tg("stopPoll", {"chat_id": -123, "message_id": 90}))
        text = "\n".join(logs.output)
        ids = re.findall(r"request_id=([a-f0-9]+)", text)
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertIn("retry_in=3", text)
        self.assertIn("event=telegram_request_succeeded", text)
        self.assertIn("attempt=2", text)

    def test_non_json_failure_logs_sanitized_response_body(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("https://example.invalid", 502, "Bad Gateway",
                                       {}, io.BytesIO(b"<html>upstream failed SECRET</html>"))
        with patch.object(bot, "TOKEN", "SECRET"), patch.object(bot, "api", side_effect=error), \
             self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(bot.TelegramAPIError):
                bot.tg("stopPoll", {"message_id": 90})
        text = "\n".join(logs.output)
        self.assertIn("http_status=502", text)
        self.assertIn("upstream failed [redacted]", text)
        self.assertNotIn("SECRET", text)

    def test_missing_metadata_skips_telegram_and_marks_young_poll_stale(self):
        rec = self.insert("1")
        with patch.object(bot, "metadata_exists", return_value=False), \
             patch.object(bot, "tg") as telegram, patch.object(bot, "maintain") as maintain, \
             self.assertLogs(level="WARNING") as logs:
            bot.close_due(self.db, rec, min_age_days=0)
        telegram.assert_not_called()
        maintain.assert_not_called()
        state, previous, since = self.db.execute(
            "SELECT state,stale_previous_state,stale_since FROM polls").fetchone()
        self.assertEqual((state, previous), ("stale", "open"))
        self.assertIsNotNone(since)
        self.assertIn("event=stale_poll_marked", "\n".join(logs.output))

    def test_stale_poll_archives_at_29_days_with_votes_and_ownership_intact(self):
        import json
        fixed = bot.now()
        with patch.object(bot, "now", return_value=fixed):
            rec = self.insert("1", days_old=29)
            self.db.execute("UPDATE polls SET keep_votes=2,owned_exclusion_ids='[77]'")
            self.db.commit()
            with patch.object(bot, "metadata_exists", return_value=False), \
                 patch.object(bot, "tg") as telegram, patch.object(bot, "maintain") as maintain:
                self.assertEqual(bot.refresh_poll_metadata(self.db), set())
        telegram.assert_not_called()
        maintain.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM polls").fetchone()[0], 0)
        saved = self.db.execute("SELECT poll_id,payload,reason FROM stale_poll_archive").fetchone()
        self.assertEqual(saved[0], rec[4])
        payload = json.loads(saved[1])
        self.assertEqual(payload["keep_votes"], 2)
        self.assertEqual(payload["owned_exclusion_ids"], "[77]")
        self.assertEqual(payload["message_id"], 1)
        self.assertEqual(payload["stale_previous_state"], "open")
        self.assertIn("no metadata", saved[2])

    def test_stale_poll_is_retained_before_29_day_boundary(self):
        fixed = bot.now()
        with patch.object(bot, "now", return_value=fixed):
            self.insert("1", days_old=29 - 1/86400)
            with patch.object(bot, "metadata_exists", return_value=False):
                bot.refresh_poll_metadata(self.db)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "stale")
        self.assertEqual(self.db.execute("SELECT count(*) FROM stale_poll_archive").fetchone()[0], 0)

    def test_returned_metadata_restores_pending_state_even_after_age_boundary(self):
        self.insert("1")
        self.db.execute("UPDATE polls SET state='veto_pending'")
        self.db.commit()
        with patch.object(bot, "metadata_exists", return_value=False):
            bot.refresh_poll_metadata(self.db)
        self.db.execute("UPDATE polls SET opened_at=?", (bot.iso(bot.now()-timedelta(days=30)),))
        self.db.commit()
        with patch.object(bot, "metadata_exists", return_value=True):
            bot.refresh_poll_metadata(self.db)
        self.assertEqual(self.db.execute(
            "SELECT state,stale_since,stale_reason,stale_previous_state FROM polls").fetchone(),
            ("veto_pending", None, None, None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM stale_poll_archive").fetchone()[0], 0)

    def test_metadata_outage_does_not_archive_old_stale_poll(self):
        self.insert("1")
        with patch.object(bot, "metadata_exists", return_value=False):
            bot.refresh_poll_metadata(self.db)
        self.db.execute("UPDATE polls SET opened_at=?", (bot.iso(bot.now()-timedelta(days=30)),))
        self.db.commit()
        with patch.object(bot, "metadata_exists", side_effect=RuntimeError("HTTP 503")), \
             self.assertLogs(level="ERROR"):
            failed = bot.refresh_poll_metadata(self.db)
        self.assertEqual(failed, {(1, "1")})
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "stale")
        self.assertEqual(self.db.execute("SELECT count(*) FROM stale_poll_archive").fetchone()[0], 0)

    def test_metadata_shape_checks_accept_only_confirmed_missing_or_matching_item(self):
        for response, expected in ((None, False), ({}, False), ({"id": "1"}, True)):
            with self.subTest(response=response), patch.object(bot, "maintain", return_value=response):
                self.assertEqual(real_metadata_exists("1"), expected)
        for response in ([], {"message": "unavailable"}, {"id": "different"}):
            with self.subTest(response=response), patch.object(bot, "maintain", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "Invalid metadata response"):
                    real_metadata_exists("1")

    def test_metadata_http_failure_remains_an_error(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("https://example.invalid", 503, "Unavailable",
                                       {}, io.BytesIO(b"unavailable"))
        self.addCleanup(error.close)
        with patch.object(bot, "maintain", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                real_metadata_exists("1")


    def test_bulk_missing_metadata_error_requires_matching_media_and_exact_reason(self):
        response = {"results": [{"mediaId": "1", "code": 0, "message": "Failed - no metadata"}]}
        with self.assertRaises(bot.voting.MissingMetadataError):
            bot.voting.apply_exclusion(lambda *a, **k: response, 1, "1")
        for media_id, message in (("other", "Failed - no metadata"), ("1", "Metadata provider unavailable")):
            response = {"results": [{"mediaId": media_id, "code": 0, "message": message}]}
            try:
                bot.voting.apply_exclusion(lambda *a, **k: response, 1, "1")
            except RuntimeError as exc:
                self.assertNotIsInstance(exc, bot.voting.MissingMetadataError)
            else:
                self.fail("Expected a real exclusion error")

    def test_metadata_disappearing_during_veto_is_ignored_as_stale(self):
        rec = self.insert("1")
        with patch.object(bot, "tg", return_value=self.closed_poll(keep=2)), \
             patch.object(bot.voting, "permanent_veto",
                          side_effect=bot.voting.MissingMetadataError("no metadata")):
            bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute(
            "SELECT state,stale_previous_state,keep_votes FROM polls").fetchone(),
            ("stale", "veto_pending", 2))

    def test_other_exclusion_failures_are_not_silently_ignored(self):
        rec = self.insert("1")
        with patch.object(bot, "tg", return_value=self.closed_poll(keep=2)), \
             patch.object(bot.voting, "permanent_veto", side_effect=RuntimeError("permission denied")):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0], "veto_pending")

    def test_manual_cli_ignores_stale_without_counting_failure(self):
        from contextlib import nullcontext, redirect_stdout
        import io
        self.insert("1")
        output = io.StringIO()
        with patch.object(bot, "metadata_exists", return_value=False), \
             patch.object(bot, "tg") as telegram, \
             patch.object(bot, "TOKEN", "test-token"), patch.object(bot, "CHAT", "test-chat"), \
             patch.object(bot, "execution_lock", return_value=nullcontext()), redirect_stdout(output):
            code = close_polls.main(["--days", "0", "--execute"])
        self.assertEqual(code, 0)
        self.assertIn("1 ignored as stale, 0 failed", output.getvalue())
        telegram.assert_not_called()

    def test_archived_missing_item_is_not_republished_from_stale_collection_data(self):
        self.insert("1", days_old=30)
        item = {"mediaServerId": "1", "mediaData": {"title": "Sample 1", "type": "movie"}}
        with patch.object(bot, "metadata_exists", return_value=False), \
             patch.object(bot, "TOKEN", "test-token"), patch.object(bot, "CHAT", "test-chat"), \
             patch.object(bot, "ENABLED", True), patch.object(bot, "IDS", [1]), \
             patch.object(bot, "check_collections", return_value={}), \
             patch.object(bot.covers, "backfill"), patch.object(bot, "members", return_value=[item]), \
             patch.object(bot, "publish") as publish:
            bot.sync()
        publish.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM polls").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM stale_poll_archive").fetchone()[0], 1)

    def test_failed_close_stays_open_and_is_retried_automatically(self):
        rec = self.insert("1", days_old=1)
        error = bot.TelegramAPIError("stopPoll", 500, "Internal Server Error")
        with patch.object(bot, "tg", side_effect=error):
            with self.assertRaises(bot.TelegramAPIError):
                bot.close_due(self.db, rec, min_age_days=0)
        self.assertEqual(self.db.execute("SELECT state,keep_votes FROM polls").fetchone(), ("open", None))
        self.assertIsNotNone(self.db.execute("SELECT close_requested_at FROM polls").fetchone()[0])
        with patch.object(bot, "TOKEN", "test-token"), patch.object(bot, "CHAT", "test-chat"), \
             patch.object(bot, "check_collections", return_value={}), \
             patch.object(bot.covers, "backfill"), patch.object(bot, "members", return_value=[]), \
             patch.object(bot, "tg", return_value=self.closed_poll()) as telegram:
            bot.sync()
        telegram.assert_called_once_with("stopPoll", {"chat_id": "test-chat", "message_id": 1})
        self.assertEqual(self.db.execute("SELECT state,close_requested_at FROM polls").fetchone(),
                         ("released", None))

    def test_429_then_400_does_not_finalize_or_create_a_manual_hold(self):
        rec = self.insert("1")
        errors = [bot.TelegramAPIError("stopPoll",429,"limited",{"retry_after":1}),
                  bot.TelegramAPIError("stopPoll",400,"Bad Request: poll can't be stopped"),
                  bot.TelegramAPIError("stopPoll",400,"Bad Request: poll can't be stopped")]
        with patch.object(bot, "telegram", side_effect=errors), patch.object(bot.time, "sleep"), \
             patch.object(bot, "maintain") as maintain:
            with self.assertRaises(bot.recovery.Deferred):
                bot.close_due(self.db, rec, min_age_days=0)
        maintain.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state,keep_votes FROM polls").fetchone(), ("open", None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM final_poll_results").fetchone()[0], 0)

    def test_upgrade_removes_bad_recovery_data_and_preserves_acknowledged_results(self):
        import json
        self.insert("1")
        self.insert("2")
        self.db.execute("UPDATE polls SET state='closure_unverified',keep_votes=0,"
                        "postpone_votes=3,owned_exclusion_ids='[77]' WHERE media_id='1'")
        self.db.execute("UPDATE polls SET state='vetoed',keep_votes=2 WHERE media_id='2'")
        self.db.execute("CREATE TABLE closed_poll_results("
                        "poll_id TEXT PRIMARY KEY,payload TEXT,recorded_at TEXT,source TEXT)")
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?,'unverified')",
                        ("poll-1",json.dumps(self.closed_poll(snooze=3)),bot.iso(bot.now())))
        self.db.execute("INSERT INTO closed_poll_results VALUES (?,?,?,'stopPoll')",
                        ("poll-2",'{"id":"poll-2","is_closed":true}',bot.iso(bot.now())))
        self.db.execute("CREATE TABLE poll_closure_reviews(poll_id TEXT)")
        self.db.commit()
        upgraded=bot.database()
        upgraded.close()
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("closed_poll_results",tables)
        self.assertNotIn("poll_closure_reviews",tables)
        self.assertEqual(self.db.execute(
            "SELECT state,keep_votes,postpone_votes,owned_exclusion_ids FROM polls WHERE media_id='1'"
        ).fetchone(),("open",None,None,"[77]"))
        self.assertIsNotNone(self.db.execute(
            "SELECT close_requested_at FROM polls WHERE media_id='1'").fetchone()[0])
        self.assertEqual(self.db.execute(
            "SELECT state,keep_votes FROM polls WHERE media_id='2'").fetchone(),("vetoed",2))
        self.assertEqual(self.db.execute("SELECT poll_id,proof FROM final_poll_results").fetchall(),
                         [("poll-2","stopPoll")])

    def test_final_result_journal_cannot_store_unverified_snapshots(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO final_poll_results VALUES (?,?,?,'unverified')",
                            ("poll-1","{}",bot.iso(bot.now())))

    def test_cli_retry_uses_the_normal_execute_command(self):
        from contextlib import nullcontext,redirect_stdout
        import io
        self.insert("1")
        with patch.object(bot, "telegram", side_effect=bot.TelegramAPIError(
                "stopPoll",400,"Bad Request: poll can't be stopped")), \
             patch.object(bot,"TOKEN","test-token"),patch.object(bot,"CHAT","test-chat"), \
             patch.object(bot,"execution_lock",return_value=nullcontext()), \
             redirect_stdout(io.StringIO()),self.assertLogs(level="ERROR"):
            self.assertEqual(close_polls.main(["--days","0","--execute"]),1)
        self.assertEqual(self.db.execute("SELECT state FROM polls").fetchone()[0],"open")

    def test_telegram_pacing_is_persisted_and_scoped_by_chat(self):
        clock=[1000.0]
        def sleep(seconds):clock[0]+=seconds
        with patch.object(bot.time,"time",side_effect=lambda:clock[0]), \
             patch.object(bot.time,"sleep",side_effect=sleep) as waits:
            real_pace_telegram("stopPoll",{"chat_id":1})
            real_pace_telegram("stopPoll",{"chat_id":1})
            real_pace_telegram("sendPoll",{"chat_id":2})
            real_pace_telegram("editMessageReplyMarkup",{"chat_id":1})
        self.assertEqual(waits.call_count,2)
        self.assertAlmostEqual(sum(c.args[0] for c in waits.call_args_list),6.4)

    def test_429_cooldown_survives_into_the_next_request(self):
        clock=[1000.0]
        def sleep(seconds):clock[0]+=seconds
        with patch.object(bot.time,"time",side_effect=lambda:clock[0]), \
             patch.object(bot.time,"sleep",side_effect=sleep) as waits:
            bot.record_telegram_cooldown(42)
            real_pace_telegram("getMe",{})
        waits.assert_called_once_with(42)

    def test_cover_url_is_strict(self):
        self.assertEqual(
            covers.poster_url({"image_path": "https://image.tmdb.org/t/p/w500/abc.jpg"}),
            "https://image.tmdb.org/t/p/w185/abc.jpg")
        self.assertIsNone(
            covers.poster_url({"image_path": "https://evil.example/t/p/w500/abc.jpg"}))


if __name__ == "__main__":
    unittest.main()
