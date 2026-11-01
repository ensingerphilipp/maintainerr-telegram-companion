"""Failure/restart scenarios. Every Telegram and Maintainerr call is offline."""
import io
import sqlite3
import unittest
from contextlib import closing, nullcontext, redirect_stdout
from datetime import timedelta
from unittest.mock import patch

import bot
import close_polls
import recovery
import test_companion


class RecoveryTests(unittest.TestCase):
    setUp = test_companion.PollTests.setUp
    insert = test_companion.PollTests.insert
    closed_poll = test_companion.PollTests.closed_poll

    def setup_clock(self):
        self.clock = [1000000.0]
        for name, value in (('TOKEN','123:secret'),('CHAT','test-chat')):
            patcher = patch.object(bot,name,value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(bot.time,'time',side_effect=lambda:self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def problem(self, mid='1'):
        rec = self.insert(mid)
        error = bot.TelegramAPIError('stopPoll',400,"Bad Request: poll can't be stopped")
        with patch.object(bot,'tg',side_effect=error) as send:
            with self.assertRaises(recovery.Deferred):
                bot.close_due(self.db,rec,min_age_days=0)
        self.assertEqual(send.call_count,2)
        return rec

    def fake_api(self):
        self.calls = []
        self.reset = False
        def send(method,body):
            self.calls.append((method,body))
            if method == 'getMe':
                return {'ok':True,'result':{'id':123,'is_bot':True}}
            if method == 'getWebhookInfo':
                return {'ok':True,'result':{'url':''}}
            if method == 'close':
                self.reset = True
                return {'ok':True,'result':True}
            if method == 'stopPoll':
                if not self.reset:
                    raise bot.TelegramAPIError(method,400,"Bad Request: poll can't be stopped")
                result=self.closed_poll()
                result['id']='poll-'+str(body['message_id'])
                return {'ok':True,'result':result}
            self.fail('Unexpected Telegram operation '+method)
        return send

    def test_exact_failure_is_confirmed_then_one_reset_recovers_whole_batch(self):
        self.setup_clock()
        self.problem('1')
        self.problem('2')
        with patch.object(bot,'telegram',side_effect=self.fake_api()), patch.object(bot,'maintain') as maintain:
            bot.resume_poll_recovery(self.db)
        maintain.assert_not_called()
        self.assertEqual([m for m,b in self.calls].count('close'),1)
        self.assertEqual([m for m,b in self.calls].count('stopPoll'),2)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchall(),[('released',),('released',)])
        self.assertEqual(self.db.execute('SELECT phase FROM poll_close_incidents').fetchall(),
                         [('resolved',),('resolved',)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM final_poll_results').fetchone()[0],2)
        self.assertEqual(recovery.session(self.db)['phase'],'idle')
        self.assertIsNotNone(recovery.session(self.db)['last_reset_at'])

    def test_one_error_then_success_never_resets_session(self):
        rec=self.insert('1')
        error=bot.TelegramAPIError('stopPoll',400,'Bad Request: poll has already been closed')
        with patch.object(bot,'tg',side_effect=[error,self.closed_poll()]) as send:
            bot.close_due(self.db,rec,min_age_days=0)
            bot.resume_poll_recovery(self.db)
        self.assertEqual([c.args[0] for c in send.call_args_list],['stopPoll','stopPoll'])
        self.assertEqual(recovery.incident(self.db,rec[4])['phase'],'resolved')

    def test_other_errors_do_not_trigger_resets(self):
        rec=self.insert('1')
        for code,text in ((400,'Bad Request: message not found'),(403,'Forbidden'),(429,'rate limited')):
            with self.subTest(code=code):
                with patch.object(bot,'tg',side_effect=bot.TelegramAPIError('stopPoll',code,text)):
                    with self.assertRaises(bot.TelegramAPIError):
                        bot.close_due(self.db,rec,min_age_days=0)
                self.assertIsNone(recovery.incident(self.db,rec[4]))
        with patch.object(bot,'tg',side_effect=bot.TelegramTransportError('timeout')):
            with self.assertRaises(bot.TelegramTransportError):
                bot.close_due(self.db,rec,min_age_days=0)
        self.assertEqual(recovery.session(self.db)['generation'],0)

    def test_429_close_deadline_survives_offline_restart_and_is_not_slept(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        first=True
        def send(method,body):
            nonlocal first
            if method=='close' and first:
                first=False
                raise bot.TelegramAPIError(method,429,'wait',{'retry_after':600})
            return api(method,body)
        with patch.object(bot,'telegram',side_effect=send),patch.object(bot.time,'sleep') as sleep:
            bot.resume_poll_recovery(self.db)
            state=recovery.session(self.db)
            self.assertEqual(state['phase'],'closing')
            self.assertEqual(state['next_attempt'],self.clock[0]+601)
            before=len(self.calls)
            bot.resume_poll_recovery(self.db)
            self.assertEqual(len(self.calls),before)
            # Reopen the on-disk DB to simulate a different process after downtime.
            with closing(sqlite3.connect(bot.DB)) as reopened:
                self.assertEqual(recovery.session(reopened)['next_attempt'],state['next_attempt'])
            self.clock[0]+=7200
            bot.resume_poll_recovery(self.db)
        sleep.assert_not_called()
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'released')
        self.assertEqual([m for m,b in self.calls].count('close'),1)

    def test_reconnect_500_resumes_without_second_close(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        once=True
        def send(method,body):
            nonlocal once
            if method=='getMe' and self.reset and once:
                once=False
                raise bot.TelegramAPIError(method,500,'Internal Server Error')
            return api(method,body)
        with patch.object(bot,'telegram',side_effect=send):
            bot.resume_poll_recovery(self.db)
            self.assertEqual(recovery.session(self.db)['phase'],'reconnecting')
            with self.assertRaises(recovery.Deferred):
                bot.tg('sendPoll',{'chat_id':'test-chat'})
            self.clock[0]+=6
            bot.resume_poll_recovery(self.db)
        self.assertEqual([m for m,b in self.calls].count('close'),1)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'released')

    def test_crash_around_close_probes_connection_instead_of_resending_reset(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        def send(method,body):
            result=api(method,body)
            if method=='close':
                raise KeyboardInterrupt('power loss after close')
            return result
        with patch.object(bot,'telegram',side_effect=send):
            with self.assertRaises(KeyboardInterrupt):
                bot.resume_poll_recovery(self.db)
        self.assertEqual(recovery.session(self.db)['phase'],'close_sent')
        with patch.object(bot,'telegram',side_effect=api):
            bot.resume_poll_recovery(self.db)
        self.assertEqual([m for m,b in self.calls].count('close'),1)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'released')

    def test_ambiguous_close_timeout_probes_and_recovers(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        def send(method,body):
            result=api(method,body)
            if method=='close':
                raise bot.TelegramTransportError('response lost')
            return result
        with patch.object(bot,'telegram',side_effect=send):
            bot.resume_poll_recovery(self.db)
        self.assertEqual([m for m,b in self.calls].count('close'),1)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'released')

    def test_same_poll_failure_after_reset_cannot_cause_a_reset_loop(self):
        self.setup_clock()
        rec=self.problem()
        api=self.fake_api()
        def send(method,body):
            if method=='stopPoll':
                raise bot.TelegramAPIError(method,400,"Bad Request: poll can't be stopped")
            return api(method,body)
        with patch.object(bot,'telegram',side_effect=send),patch.object(bot,'maintain') as maintain:
            bot.resume_poll_recovery(self.db)
            self.assertEqual(recovery.incident(self.db,rec[4])['phase'],'unresolved')
            self.clock[0]+=86400
            with self.assertRaises(recovery.Deferred):
                bot.close_due(self.db,rec,min_age_days=0)
            bot.resume_poll_recovery(self.db)
        maintain.assert_not_called()
        self.assertEqual([m for m,b in self.calls].count('close'),1)
        self.assertEqual(self.db.execute('SELECT state,keep_votes FROM polls').fetchone(),('open',None))
        self.assertEqual(self.db.execute('SELECT count(*) FROM final_poll_results').fetchone()[0],0)

    def test_new_incident_respects_global_reset_budget_across_restart(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        with patch.object(bot,'telegram',side_effect=api):
            bot.resume_poll_recovery(self.db)
        self.problem('2')
        with patch.object(bot,'telegram',side_effect=api):
            bot.resume_poll_recovery(self.db)
            self.assertEqual([m for m,b in self.calls].count('close'),1)
            self.clock[0]+=3601
            bot.resume_poll_recovery(self.db)
        self.assertEqual([m for m,b in self.calls].count('close'),2)

    def test_preflight_checks_identity_and_webhook_without_changing_either(self):
        for me,webhook in (({'id':999,'is_bot':True},{'url':''}),
                           ({'id':123,'is_bot':True},{'url':'https://example.invalid'})):
            with self.subTest(me=me,webhook=webhook):
                self.setup_clock()
                rec=self.problem(str(me['id']))
                with self.db:
                    self.db.execute("UPDATE telegram_session_recovery SET phase='idle',reset_not_before=0")
                def send(method,body):
                    if method=='getMe': return {'ok':True,'result':me}
                    if method=='getWebhookInfo': return {'ok':True,'result':webhook}
                    self.fail('Unsafe preflight called '+method)
                with patch.object(bot,'telegram',side_effect=send):
                    bot.resume_poll_recovery(self.db)
                self.assertEqual(recovery.session(self.db)['phase'],'blocked')
                self.assertIsNone(self.db.execute('SELECT keep_votes FROM polls WHERE poll_id=?',(rec[4],)).fetchone()[0])

    def test_stale_or_archived_poll_cannot_trigger_reset(self):
        self.setup_clock()
        self.problem()
        with self.db:
            self.db.execute("UPDATE polls SET state='stale'")
        with patch.object(bot,'telegram') as send:
            bot.resume_poll_recovery(self.db)
        send.assert_not_called()
        self.assertEqual(recovery.session(self.db)['generation'],0)

    def test_changed_chat_cannot_retarget_recovery(self):
        self.setup_clock()
        rec=self.problem()
        with patch.object(bot,'CHAT','another-chat'),patch.object(bot,'tg') as send:
            with self.assertRaisesRegex(RuntimeError,'target'):
                recovery.stop_poll(self.db,rec,bot.CHAT,bot.tg,bot.time.time,bot.TelegramAPIError)
        send.assert_not_called()
        with patch.object(bot,'CHAT','another-chat'),patch.object(bot,'tg') as send:
            bot.resume_poll_recovery(self.db)
        send.assert_not_called()

    def test_persistent_global_flood_cooldown_defers_preflight_without_busy_loop(self):
        self.setup_clock()
        self.problem()
        bot.record_telegram_cooldown(720)
        with patch.object(bot,'pace_telegram',side_effect=test_companion.real_pace_telegram), \
             patch.object(bot,'telegram') as send,patch.object(bot.time,'sleep') as sleep:
            bot.resume_poll_recovery(self.db)
        send.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(recovery.session(self.db)['next_attempt'],self.clock[0]+720)
        self.assertEqual(bot.next_cycle_delay(),720)

    def test_nonblocking_chat_pacing_does_not_consume_another_slot_when_deferred(self):
        self.setup_clock()
        test_companion.real_pace_telegram('stopPoll',{'chat_id':'test-chat'},nonblocking=True)
        before=self.db.execute('SELECT next_allowed FROM telegram_rate_limits WHERE scope=?',
                               ('chat:test-chat',)).fetchone()[0]
        with self.assertRaises(recovery.Deferred) as caught:
            test_companion.real_pace_telegram('stopPoll',{'chat_id':'test-chat'},nonblocking=True)
        self.assertEqual(caught.exception.retry_at,before)
        self.assertEqual(self.db.execute('SELECT next_allowed FROM telegram_rate_limits WHERE scope=?',
                                        ('chat:test-chat',)).fetchone()[0],before)

    def test_blocked_recovery_does_not_spin_after_global_budget_expires(self):
        self.setup_clock()
        self.problem()
        with self.db:
            self.db.execute("UPDATE telegram_session_recovery SET phase='blocked'")
        self.clock[0]+=86400
        self.assertEqual(recovery.next_wakeup(self.db,self.clock[0],2400),2400)

    def test_cli_performs_automatic_recovery_and_reports_real_final_state(self):
        self.setup_clock()
        self.insert('1')
        output=io.StringIO()
        with patch.object(bot,'telegram',side_effect=self.fake_api()), \
             patch.object(bot,'execution_lock',return_value=nullcontext()),redirect_stdout(output):
            self.assertEqual(close_polls.main(['--days','0','--execute']),0)
        self.assertIn("DONE: 'Sample 1' -> released",output.getvalue())
        self.assertIn('0 pending',output.getvalue())
        self.assertEqual([m for m,b in self.calls].count('close'),1)

    def test_scheduler_automatically_recovers_an_early_manual_close(self):
        self.setup_clock()
        self.problem()
        with patch.object(bot,'telegram',side_effect=self.fake_api()), \
             patch.object(bot,'check_collections',return_value={}), \
             patch.object(bot.covers,'backfill'),patch.object(bot,'members',return_value=[]):
            bot.sync()
        self.assertEqual(self.db.execute('SELECT state,close_requested_at FROM polls').fetchone(),
                         ('released',None))
        self.assertEqual([m for m,b in self.calls].count('close'),1)

    def test_startup_without_matching_failures_and_preview_never_reset(self):
        self.setup_clock()
        self.insert('1',days_old=1)
        with patch.object(bot,'telegram') as send, \
             patch.object(bot,'check_collections',return_value={}), \
             patch.object(bot.covers,'backfill'),patch.object(bot,'members',return_value=[]):
            bot.sync()
        send.assert_not_called()
        self.problem('2')
        with patch.object(bot,'telegram') as send, \
             patch.object(bot,'execution_lock',return_value=nullcontext()),redirect_stdout(io.StringIO()):
            self.assertEqual(close_polls.main(['--days','0']),0)
        send.assert_not_called()
        self.assertEqual(recovery.session(self.db)['generation'],0)

    def test_cli_deferral_reports_pending_and_scheduler_deadline_is_shortened(self):
        self.setup_clock()
        self.problem()
        api=self.fake_api()
        def send(method,body):
            if method=='close':
                raise bot.TelegramAPIError(method,429,'wait',{'retry_after':120})
            return api(method,body)
        output=io.StringIO()
        with patch.object(bot,'telegram',side_effect=send), \
             patch.object(bot,'execution_lock',return_value=nullcontext()),redirect_stdout(output):
            self.assertEqual(close_polls.main(['--days','0','--execute']),2)
        self.assertIn('PENDING:',output.getvalue())
        self.assertNotIn('DONE:',output.getvalue())
        self.assertEqual(bot.next_cycle_delay(),121)

    def test_post_response_crash_replays_final_votes_without_telegram(self):
        self.setup_clock()
        rec=self.insert('1')
        with patch.object(bot,'tg',return_value=self.closed_poll(snooze=2)), \
             patch.object(bot.voting,'save_votes',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bot.close_due(self.db,rec,min_age_days=0)
        saved_at=self.db.execute('SELECT recorded_at FROM final_poll_results').fetchone()[0]
        with patch.object(bot,'tg') as send,patch.object(bot.voting,'start_snooze'):
            bot.close_due(self.db,rec,min_age_days=0)
        send.assert_not_called()
        due=self.db.execute('SELECT snooze_until FROM polls').fetchone()[0]
        self.assertEqual(bot.parse(due),bot.parse(saved_at)+timedelta(days=60))

    def test_veto_restart_verifies_existing_exclusion_without_duplicate_post(self):
        rec=self.insert('1')
        bot.voting.save_votes(self.db,rec,1,0,2,bot.now,bot.iso)
        current=self.db.execute('SELECT * FROM polls').fetchone()
        def maintain(path,body=None,method=None):
            if path.startswith('/api/rules/exclusion?'):
                return [{'id':55,'ruleGroupId':1,'mediaServerId':'1'}]
            self.fail('Duplicate or unexpected mutation '+path)
        with patch.object(bot,'maintain',side_effect=maintain):
            bot.reconcile_outcome(self.db,current)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'vetoed')

    def test_snooze_post_succeeded_response_lost_then_restart_reuses_exclusion(self):
        rec=self.insert('1')
        bot.voting.save_votes(self.db,rec,0,1,2,bot.now,bot.iso)
        current=self.db.execute('SELECT * FROM polls').fetchone()
        exclusions=[]
        def maintain(path,body=None,method=None):
            if path.startswith('/api/rules/exclusion?'): return list(exclusions)
            if path=='/api/rules/exclusions/bulk':
                exclusions.append({'id':55,'ruleGroupId':1,'mediaServerId':'1'})
                raise TimeoutError('Maintainerr accepted POST, response lost')
            self.fail('Unexpected operation '+path)
        with patch.object(bot,'maintain',side_effect=maintain) as calls:
            with self.assertRaises(TimeoutError): bot.reconcile_outcome(self.db,current)
            # Use the same stale tuple; baseline must be reloaded from SQLite.
            bot.reconcile_outcome(self.db,current)
        self.assertEqual(sum(c.args[0]=='/api/rules/exclusions/bulk' for c in calls.call_args_list),1)
        self.assertEqual(self.db.execute('SELECT state,owned_exclusion_ids FROM polls').fetchone(),
                         ('snoozed','[55]'))

    def test_snooze_does_not_claim_an_exclusion_added_before_its_post(self):
        rec=self.insert('1')
        bot.voting.save_votes(self.db,rec,0,1,2,bot.now,bot.iso)
        current=self.db.execute('SELECT * FROM polls').fetchone()
        reads=0
        def maintain(path,body=None,method=None):
            nonlocal reads
            if path.startswith('/api/rules/exclusion?'):
                reads+=1
                return [] if reads==1 else [{'id':55,'ruleGroupId':1,'mediaServerId':'1'}]
            self.fail('Should not create an exclusion '+path)
        with patch.object(bot,'maintain',side_effect=maintain):
            bot.reconcile_outcome(self.db,current)
        self.assertEqual(self.db.execute('SELECT state,owned_exclusion_ids FROM polls').fetchone(),
                         ('externally_protected',None))

    def test_untrusted_or_invalid_result_still_cannot_finalize_after_reset(self):
        self.setup_clock()
        rec=self.problem()
        api=self.fake_api()
        def send(method,body):
            result=api(method,body)
            if method=='stopPoll': result['result']['id']='wrong-poll'
            return result
        with patch.object(bot,'telegram',side_effect=send),patch.object(bot,'maintain') as maintain:
            bot.resume_poll_recovery(self.db)
        maintain.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM final_poll_results').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT state FROM polls').fetchone()[0],'open')


if __name__=='__main__':
    unittest.main()
