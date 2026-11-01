"""Durable, bounded recovery of ambiguous Telegram poll closure failures.

Callers hold bot.execution_lock(). No message edits or cached poll updates are
accepted as closure evidence. All deadlines are UTC epoch seconds, not uptime.
"""
import logging
import sqlite3

RESET_INTERVAL = 3600
UNRESOLVED_INTERVAL = 3600


class Deferred(RuntimeError):
    def __init__(self, reason, retry_at=None):
        self.retry_at = retry_at
        super().__init__(reason)


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS poll_close_incidents(
            poll_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            phase TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
            first_error_at REAL NOT NULL, last_error_at REAL NOT NULL,
            description TEXT NOT NULL, reset_generation INTEGER,
            next_attempt REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS telegram_session_recovery(
            id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL DEFAULT 'idle',
            generation INTEGER NOT NULL DEFAULT 0, expected_bot_id INTEGER,
            next_attempt REAL NOT NULL DEFAULT 0, reset_not_before REAL NOT NULL DEFAULT 0,
            last_reset_at REAL, failures INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO telegram_session_recovery(id) VALUES (1);
    """)


def session(db):
    row = db.execute("SELECT * FROM telegram_session_recovery WHERE id=1")
    return dict(zip((c[0] for c in row.description), row.fetchone()))


def guard(path, method):
    """Pause normal API traffic while a reset/reconnect is incomplete."""
    db=sqlite3.connect(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_session_recovery'").fetchone():
            return
        state = session(db)
    finally:
        db.close()
    if state['phase'] in ('preflight','closing','close_sent','reconnecting'):
        raise Deferred(f"Telegram {method} paused for session recovery: {state['phase']}", state['next_attempt'])


def suspect(error):
    description = error.description.strip().lower()
    if description.startswith('bad request:'):
        description = description[len('bad request:'):].strip()
    return error.code == 400 and description in (
        "poll can't be stopped", "poll has already been closed")


def incident(db, poll_id):
    cursor = db.execute("SELECT * FROM poll_close_incidents WHERE poll_id=?", (poll_id,))
    row = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), row)) if row else None


def stop_poll(db, rec, chat, send, clock, error_type):
    """Confirm the exact failure with one paced retry, then queue one reset."""
    saved = incident(db, rec[4])
    current = clock()
    if saved:
        if saved['chat_id'] != str(chat) or saved['message_id'] != rec[3]:
            raise RuntimeError('Poll recovery target does not match the original message')
        if saved['phase'] == 'reset':
            raise Deferred('Poll closure awaits session recovery', saved['next_attempt'])
        if saved['next_attempt'] > current:
            raise Deferred('Poll closure retry is scheduled', saved['next_attempt'])
    for _ in range(2):
        try:
            return send('stopPoll', {'chat_id': chat, 'message_id': rec[3]})
        except error_type as error:
            if not suspect(error):
                raise
            current = clock()
            saved = incident(db, rec[4])
            failures = (saved['failures'] if saved else 0) + 1
            already_reset = saved and saved['reset_generation'] is not None
            phase = 'unresolved' if already_reset else 'reset' if failures >= 2 else 'confirm'
            next_at = current + UNRESOLVED_INTERVAL if already_reset else current
            with db:
                db.execute("""INSERT INTO poll_close_incidents
                    (poll_id,chat_id,message_id,phase,failures,first_error_at,last_error_at,
                     description,next_attempt) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(poll_id) DO UPDATE SET phase=excluded.phase,
                    failures=excluded.failures,last_error_at=excluded.last_error_at,
                    description=excluded.description,next_attempt=excluded.next_attempt""",
                    (rec[4],str(chat),rec[3],phase,failures,current,current,error.description,next_at))
            logging.log(logging.ERROR if already_reset else logging.WARNING,
                        'event=poll_close_incident poll_id=%s message_id=%s phase=%s '
                        'failures=%s reset_generation=%s reason=%r retry_at=%s',
                        rec[4],rec[3],phase,failures,
                        saved['reset_generation'] if saved else None,error.description,next_at)
            if phase != 'confirm':
                raise Deferred('Poll closure needs recovery; no votes applied', next_at) from None


def resolved(db, poll_id):
    previous = incident(db,poll_id)
    with db:
        db.execute("UPDATE poll_close_incidents SET phase='resolved',next_attempt=0 WHERE poll_id=?",
                   (poll_id,))
    if previous and previous['phase'] != 'resolved':
        logging.info('event=poll_close_incident_resolved poll_id=%s reset_generation=%s',
                     poll_id,previous['reset_generation'])


def candidates(db):
    # Stale, archived and already finalized polls cannot cause a session reset.
    return db.execute("""SELECT i.poll_id FROM poll_close_incidents i JOIN polls p
        ON p.poll_id=i.poll_id WHERE i.phase='reset' AND p.state='open'
        AND p.close_requested_at IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM final_poll_results f WHERE f.poll_id=i.poll_id)""").fetchall()


def resume(db, send, clock, error_type, transport_type, token_bot_id):
    """Advance one durable bot-wide reset, without sleeping through cooldowns.

send is a single-attempt, nonblocking request with the normal diagnostics and
pacing. Normal requests are guarded until reconnect finishes. Caller retries
closures only after this function returns with phase idle.
"""
    state = session(db)
    current = clock()
    if state['phase'] == 'blocked':
        raise Deferred('Telegram session recovery blocked; check ERROR logs')
    if state['phase'] == 'idle':
        targets = candidates(db)
        if not targets:
            return
        if current < state['reset_not_before']:
            with db:
                db.execute("UPDATE poll_close_incidents SET next_attempt=? WHERE phase='reset'",
                           (state['reset_not_before'],))
            raise Deferred('Session reset cooldown', state['reset_not_before'])
        with db:
            # Reserve the budget before the external call, including crash cases.
            db.execute("""UPDATE telegram_session_recovery SET phase='preflight',
                generation=generation+1,expected_bot_id=NULL,next_attempt=0,failures=0,
                reset_not_before=? WHERE id=1""", (current+RESET_INTERVAL,))
        state = session(db)
        logging.warning('event=telegram_session_recovery_started generation=%s polls=%s',
                        state['generation'], [r[0] for r in targets])
    if state['next_attempt'] > current:
        raise Deferred('Telegram session recovery retry scheduled', state['next_attempt'])
    stage = state['phase']
    try:
        if stage != 'preflight' and state['expected_bot_id'] != token_bot_id:
            raise ValueError('Configured bot identity changed during session recovery')
        if stage == 'preflight':
            me = send('getMe', {})
            bot_id = me.get('id')
            if (type(bot_id) is not int or bot_id <= 0 or me.get('is_bot') is not True
                    or token_bot_id is None or bot_id != token_bot_id):
                raise ValueError('getMe identity does not match configured bot token')
            webhook = send('getWebhookInfo', {})
            if not isinstance(webhook, dict) or webhook.get('url') != '':
                raise ValueError('Automatic reset requires no webhook; no webhook was changed')
            with db:
                db.execute("UPDATE telegram_session_recovery SET phase='closing',expected_bot_id=? WHERE id=1",
                           (bot_id,))
            stage = 'closing'
        if stage == 'closing':
            with db:
                db.execute("UPDATE telegram_session_recovery SET phase='close_sent' WHERE id=1")
            try:
                answer = send('close', {})
                if answer is not True:
                    raise ValueError('Telegram close did not acknowledge success')
            except error_type as error:
                if not (error.code == 400 and error.description.lower().removeprefix('bad request:').strip()
                        == 'the bot has already been closed'):
                    with db:
                        db.execute("UPDATE telegram_session_recovery SET phase='closing' WHERE id=1")
                    raise
                # A crash may have occurred after close succeeded but before commit.
            except transport_type:
                # close may have succeeded: reconnect/probe instead of blindly resetting again.
                logging.warning('event=telegram_session_close_unknown generation=%s',state['generation'])
            with db:
                db.execute("UPDATE telegram_session_recovery SET phase='reconnecting',next_attempt=0 WHERE id=1")
            stage = 'reconnecting'
        if stage == 'close_sent':
            # The process died around close. Its outcome is unknown; never resend
            # a potentially successful reset simply because the PC restarted.
            logging.warning('event=telegram_session_close_unknown generation=%s reason=process_restart',
                            state['generation'])
            with db:
                db.execute("UPDATE telegram_session_recovery SET phase='reconnecting' WHERE id=1")
            stage = 'reconnecting'
        if stage == 'reconnecting':
            expected = session(db)['expected_bot_id']
            me = send('getMe', {})
            if me.get('id') != expected or me.get('is_bot') is not True:
                raise ValueError('Reconnected bot identity mismatch')
            current = clock()
            with db:
                db.execute("""UPDATE poll_close_incidents SET phase='retry',reset_generation=?,
                    next_attempt=0 WHERE phase='reset'""", (state['generation'],))
                db.execute("""UPDATE telegram_session_recovery SET phase='idle',last_reset_at=?,
                    next_attempt=0,failures=0,reset_not_before=MAX(reset_not_before,?) WHERE id=1""",
                    (current,current+RESET_INTERVAL))
            logging.info('event=telegram_session_reconnected generation=%s bot_id=%s',
                         state['generation'],expected)
    except Deferred as error:
        with db:
            db.execute('UPDATE telegram_session_recovery SET next_attempt=? WHERE id=1',
                       (error.retry_at if error.retry_at is not None else clock()+30,))
        logging.info('event=telegram_session_recovery_paced generation=%s stage=%s retry_at=%s',
                     state['generation'],stage,error.retry_at)
        raise
    except (error_type, transport_type) as error:
        failures = state['failures'] + 1
        delay = (error.retry_after + 1 if isinstance(error,error_type) and error.retry_after is not None
                 else min(5 * 2**min(failures-1,6),300))
        if isinstance(error,error_type) and error.code not in (429,500,502,503,504):
            with db:
                db.execute("UPDATE telegram_session_recovery SET phase='blocked' WHERE id=1")
            logging.error('event=telegram_session_recovery_blocked generation=%s stage=%s reason=%r',
                          state['generation'],stage,str(error))
            raise Deferred('Telegram session recovery blocked; check ERROR logs') from None
        next_at = clock()+delay
        with db:
            db.execute("UPDATE telegram_session_recovery SET next_attempt=?,failures=? WHERE id=1",
                       (next_at,failures))
        logging.warning('event=telegram_session_recovery_deferred generation=%s stage=%s '
                        'reason=%r retry_at=%s',state['generation'],stage,str(error),next_at)
        raise Deferred('Telegram session recovery retry scheduled', next_at) from None
    except (ValueError, AttributeError) as error:
        with db:
            db.execute("UPDATE telegram_session_recovery SET phase='blocked' WHERE id=1")
        logging.error('event=telegram_session_recovery_blocked generation=%s stage=%s reason=%r',
                      state['generation'],stage,str(error))
        raise Deferred('Telegram session recovery blocked; check ERROR logs') from None


def next_wakeup(db, current, interval):
    state = session(db)
    if state['phase'] == 'blocked':
        return interval
    if state['phase'] not in ('idle','blocked'):
        return min(interval, max(1, state['next_attempt']-current))
    if candidates(db):
        return min(interval, max(1, state['reset_not_before']-current))
    return interval
