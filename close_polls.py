#!/usr/bin/env python3
"""Preview or explicitly close all open polls older than --days."""
import argparse
import logging
import sys
from datetime import timedelta
import bot

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
            records=db.execute("SELECT * FROM polls ORDER BY opened_at").fetchall()
            matches=eligible(records,args.days,bot.now())
            print(f"{'EXECUTE' if args.execute else 'PREVIEW'}: {len(matches)} open polls at least {args.days:g} days old",flush=True)
            for row in matches:
                age=(bot.now()-bot.parse(row[5])).total_seconds()/86400
                print(f"collection={row[0]} title={row[2]!r} age={age:.2f}d",flush=True)
            if not args.execute:
                print("No changes. Add --execute to close and evaluate these polls.",flush=True)
                return 0
            failed=0
            for row in matches:
                try:
                    bot.close_due(db,row,min_age_days=args.days)
                    status=db.execute("SELECT state FROM polls WHERE collection_id=? AND media_id=?",(row[0],row[1])).fetchone()[0]
                    print(f"DONE: {row[2]!r} -> {status}",flush=True)
                except Exception:
                    failed+=1
                    logging.exception("FAILED: %s",row[2])
            print(f"Finished: {len(matches)-failed} processed, {failed} failed",flush=True)
            return 1 if failed else 0
        finally:
            db.close()

if __name__=="__main__":
    sys.exit(main())
