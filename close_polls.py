#!/usr/bin/env python3
"""Preview or explicitly close all open polls older than --days."""
import argparse
import logging
import sys
from datetime import timedelta
import bot
import recovery

def eligible(rows, days, timestamp):
    cutoff=timestamp-timedelta(days=days)
    return [r for r in rows if r[9]=="open" and bot.parse(r[5])<=cutoff]

def main(argv=None):
    parser=argparse.ArgumentParser(description="Close and evaluate polls after a configurable minimum age.")
    parser.add_argument("--days",type=float,required=True,help="Minimum poll age in days, e.g. 3 (0 means all open polls)")
    parser.add_argument("--execute",action="store_true",help="Actually stop and evaluate matching polls; otherwise preview only")
    args=parser.parse_args(argv)
    if not 0<=args.days<=bot.POLL_DAYS:
        parser.error(f"--days must be between 0 and {bot.POLL_DAYS}")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.execute and (not bot.TOKEN or not bot.CHAT):
        parser.error("Telegram credentials missing (run inside the Compose container)")
    with bot.execution_lock():
        db=bot.database()
        try:
            initial_count=db.execute("SELECT count(*) FROM polls").fetchone()[0]
            metadata_failures=bot.refresh_poll_metadata(db) if args.execute else set()
            records=db.execute("SELECT * FROM polls ORDER BY opened_at").fetchall()
            stale_count=sum(row[9]=="stale" for row in records)
            archived_count=initial_count-len(records)
            if stale_count or archived_count:
                print(f"Stale metadata: {stale_count} ignored, {archived_count} archived.",flush=True)
            matches=eligible(records,args.days,bot.now())
            print(f"{'EXECUTE' if args.execute else 'PREVIEW'}: {len(matches)} eligible polls at least {args.days:g} days old",flush=True)
            for row in matches:
                age=(bot.now()-bot.parse(row[5])).total_seconds()/86400
                print(f"collection={row[0]} title={row[2]!r} age={age:.2f}d state={row[9]}",flush=True)
            if not args.execute:
                print("No Telegram or Maintainerr changes. Add --execute to close and evaluate these polls.",flush=True)
                return 0
            failed=len(metadata_failures)
            ignored=stale_count+archived_count
            bot.resume_poll_recovery(db)
            # Also finish acknowledged outcomes interrupted by an earlier process.
            for row in db.execute("SELECT * FROM polls WHERE state IN ('veto_pending','snooze_pending')").fetchall():
                if (row[0],row[1]) in metadata_failures:
                    continue
                try:
                    bot.reconcile_outcome(db,row)
                except Exception:
                    failed+=1
                    logging.exception('Outcome reconciliation failed poll_id=%s title=%r',row[4],row[2])
            for row in matches:
                if (row[0],row[1]) in metadata_failures:
                    continue
                try:
                    current=db.execute('SELECT * FROM polls WHERE poll_id=?',(row[4],)).fetchone()
                    if current and current[9]=='open':
                        bot.close_due(db,current,min_age_days=args.days,metadata_checked=True)
                except recovery.Deferred as exc:
                    logging.info('event=poll_close_pending poll_id=%s reason=%r retry_at=%s',
                                 row[4],str(exc),exc.retry_at)
                except Exception:
                    failed+=1
                    logging.exception("FAILED title=%r collection_id=%s media_id=%s poll_id=%s "
                                      "chat_id=%s message_id=%s state_before=%s",
                                      row[2],row[0],row[1],row[4],bot.redact_diagnostic(bot.CHAT),
                                      row[3],row[9])
            bot.resume_poll_recovery(db)
            processed=pending=0
            if recovery.session(db)['phase']=='blocked':
                failed+=1
            for row in matches:
                if (row[0],row[1]) in metadata_failures:
                    continue
                found=db.execute('SELECT state FROM polls WHERE poll_id=?',(row[4],)).fetchone()
                status=found[0] if found else 'archived_stale'
                if status in ('stale','archived_stale'):
                    ignored+=1
                    label='IGNORED'
                elif status in ('released','vetoed','snoozed','externally_protected'):
                    processed+=1
                    label='DONE'
                else:
                    pending+=1
                    label='PENDING'
                print(f"{label}: {row[2]!r} -> {status}",flush=True)
            print(f"Finished: {processed} processed, {ignored} ignored as stale, {failed} failed, {pending} pending",flush=True)
            return 1 if failed else 2 if pending else 0
        finally:
            db.close()

if __name__=="__main__":
    sys.exit(main())
