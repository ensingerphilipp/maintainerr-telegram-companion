#!/usr/bin/env python3
"""Preview or explicitly close all open polls older than --days."""
import argparse
import logging
import sys
from datetime import timedelta
import bot

def eligible(rows, days, timestamp, include_unverified=False):
    cutoff=timestamp-timedelta(days=days)
    states = {"open", "closure_unverified"} if include_unverified else {"open"}
    return [r for r in rows if r[9] in states and bot.parse(r[5])<=cutoff]

def main(argv=None):
    parser=argparse.ArgumentParser(description="Close and evaluate polls after a configurable minimum age.")
    parser.add_argument("--days",type=float,required=True,help="Minimum poll age in days, e.g. 3 (0 means all open polls)")
    parser.add_argument("--execute",action="store_true",help="Actually stop and evaluate matching polls; otherwise preview only")
    parser.add_argument("--retry-unverified", action="store_true",
                        help="Also attempt a real stop for polls held for closure review; may close live polls")
    args=parser.parse_args(argv)
    if not 0<=args.days<=bot.POLL_DAYS:
        parser.error(f"--days must be between 0 and {bot.POLL_DAYS}")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.execute and (not bot.TOKEN or not bot.CHAT):
        parser.error("Telegram credentials missing (run inside the Compose container)")
    with bot.execution_lock():
        db=bot.database()
        try:
            records=db.execute("SELECT * FROM polls ORDER BY opened_at").fetchall()
            held = sum(row[9] == "closure_unverified" for row in records)
            if held:
                print(f"Closure review: {held} unverified polls. A real close retry requires --retry-unverified --execute.",flush=True)
            matches=eligible(records,args.days,bot.now(),include_unverified=args.retry_unverified)
            print(f"{'EXECUTE' if args.execute else 'PREVIEW'}: {len(matches)} eligible polls at least {args.days:g} days old",flush=True)
            for row in matches:
                age=(bot.now()-bot.parse(row[5])).total_seconds()/86400
                print(f"collection={row[0]} title={row[2]!r} age={age:.2f}d state={row[9]}",flush=True)
            if not args.execute:
                print("No Telegram or Maintainerr changes. Add --execute to close and evaluate these polls.",flush=True)
                return 0
            failed=0
            for row in matches:
                try:
                    bot.close_due(db,row,min_age_days=args.days,retry_unverified=args.retry_unverified)
                    status=db.execute("SELECT state FROM polls WHERE collection_id=? AND media_id=?",(row[0],row[1])).fetchone()[0]
                    print(f"DONE: {row[2]!r} -> {status}",flush=True)
                except Exception:
                    failed+=1
                    logging.exception("FAILED title=%r collection_id=%s media_id=%s poll_id=%s "
                                      "chat_id=%s message_id=%s state_before=%s",
                                      row[2],row[0],row[1],row[4],bot.redact_diagnostic(bot.CHAT),
                                      row[3],row[9])
            print(f"Finished: {len(matches)-failed} processed, {failed} failed",flush=True)
            return 1 if failed else 0
        finally:
            db.close()

if __name__=="__main__":
    sys.exit(main())
