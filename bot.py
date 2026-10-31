#!/usr/bin/env python3
"""Maintainerr-owned cleanup; Telegram voting companion (no delete endpoints)."""
import json, os, sqlite3, time, urllib.request, urllib.error, logging, fcntl
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import voting
import covers

ROOT=Path(__file__).resolve().parent
DB=ROOT/"polls.sqlite"
BASE=os.getenv("MAINTAINERR_URL","http://maintainerr:6246").rstrip("/")
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
IDS=[int(x.strip()) for x in os.getenv("COLLECTION_IDS","1,2").split(",") if x.strip()]
INTERVAL=int(os.getenv("POLL_INTERVAL_SECONDS","900"))
POLL_DAYS=int(os.getenv("POLL_DAYS","29"))
RETENTION_DAYS=int(os.getenv("RETENTION_DAYS","45"))
if not 1 <= POLL_DAYS < RETENTION_DAYS:
    raise ValueError("Require 1 <= POLL_DAYS < RETENTION_DAYS")
MAX_NEW_POLLS_PER_CYCLE=int(os.getenv("MAX_NEW_POLLS_PER_CYCLE","5"))
ENABLED=os.getenv("PUBLISH_POLLS","false").lower()=="true"
@contextmanager
def execution_lock():
    # Shared by the scheduler and manual CLI to prevent concurrent poll closures.
    with open(ROOT/"poll-execution.lock", "a+") as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
def now():return datetime.now(timezone.utc)
def iso(d):return d.isoformat()
def parse(s):return datetime.fromisoformat(s.replace("Z","+00:00"))
def api(url, body=None, method=None):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,method=method or ("POST" if data else "GET"),headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=40) as r:
        b=r.read()
        result=json.loads(b) if b else {}
        if not r.status in range(200,300):raise RuntimeError(str(result))
        return result
def maintain(path,body=None,method=None):return api(BASE+path,body,method)
def telegram(method,body):return api("https://api.telegram.org/bot"+TOKEN+"/"+method,body)
def tg(method,body):
    r=telegram(method,body)
    if not r.get("ok"):raise RuntimeError("Telegram "+method+": "+str(r.get("description")))
    return r["result"]
def database():
    c=sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS polls(
        collection_id INTEGER NOT NULL, media_id TEXT NOT NULL, title TEXT NOT NULL,
        message_id INTEGER NOT NULL, poll_id TEXT NOT NULL,
        opened_at TEXT NOT NULL, closed_at TEXT, keep_votes INTEGER, delete_votes INTEGER,
        state TEXT NOT NULL DEFAULT 'open',
        PRIMARY KEY(collection_id,media_id))""")
    c.commit()
    voting.migrate(c)
    covers.migrate(c)
    return c
def members(cid):
    page=1;result=[]
    while True:
        data=maintain(f"/api/collections/media/{cid}/content/{page}?size=100")
        batch=data.get("items",[])
        result+=batch
        if len(batch)<100:break
        page+=1
    return result
def check_collections():
    collections={c["id"]:c for c in maintain("/api/collections")}
    for cid in IDS:
        c=collections[cid]
        action=c.get("arrAction")
        delay=c.get("deleteAfterDays")
        logging.info("collection %s action=%s delay=%s count=%s",cid,action,delay,c.get("mediaCount"))
        if action is not None and delay!=RETENTION_DAYS:
            logging.warning("Collection %s is not at %s days; bot NEVER updates deletion settings",cid,RETENTION_DAYS)
    return collections
def publish(c,cid,m):
    media_id=m["mediaServerId"]
    d=m.get("mediaData") or {}
    title=d.get("title") or str(media_id)
    year=d.get("year")
    typ="Film" if (m.get("mediaData") or {}).get("type")=="movie" else "Serie"
    question=(f"{typ} zur Löschung vorgemerkt: {title}"+(f" ({year})" if year else "")+"?")[:300]
    cover_id=covers.first(m,title,tg,CHAT)
    payload={"chat_id":CHAT,"question":question,
       "options":[{"text":"🛡️ Löschen dauerhaft verhindern (Veto)"},{"text":"⏳ Löschung um 60 Tage verschieben"},{"text":"🗑️ Löschung ist in Ordnung"}],
       "is_anonymous":True,"allows_multiple_answers":False,
       "type":"regular"}
    if cover_id:
        payload["reply_parameters"]={"message_id":cover_id}
    result=tg("sendPoll",payload)
    opened=now()
    c.execute("INSERT INTO polls(collection_id,media_id,title,message_id,poll_id,opened_at,closed_at,keep_votes,delete_votes,state) VALUES (?,?,?,?,?,?,?,?,?,?)",
       (cid,media_id,title,result["message_id"],result["poll"]["id"],iso(opened),None,None,None,"timer_pending"))
    c.commit()
    reset_timer(c,cid,media_id,title)
    if cover_id:
        c.execute("UPDATE polls SET cover_status=?,cover_message_id=? WHERE collection_id=? AND media_id=?",("sent",cover_id,cid,media_id))
        c.commit()
    else:
        c.execute("UPDATE polls SET cover_status=? WHERE collection_id=? AND media_id=?",("missing",cid,media_id))
        c.commit()
    logging.info("Published %s (collection=%s)",title,cid)
def reset_timer(c,cid,media_id,title):
    result=maintain("/api/collections/media/postpone",{"collectionId":cid,"mediaId":media_id})
    if int(result.get("deleteAfterDays",-1))!=RETENTION_DAYS:
        raise RuntimeError("Timer reset did not confirm configured retention: "+str(result))
    c.execute("UPDATE polls SET state='open' WHERE collection_id=? AND media_id=?",(cid,media_id))
    c.commit()

def close_due(c,rec,min_age_days=POLL_DAYS):
    cid,media_id,title,msg,_,opened,_,_,_,state=rec[:10]
    if min_age_days < 0:
        raise ValueError("min_age_days must be nonnegative")
    if state!="open" or now()<parse(opened)+timedelta(days=min_age_days):return
    result=tg("stopPoll",{"chat_id":CHAT,"message_id":msg})
    options=result.get("options",[])
    if len(options) not in (2,3):raise RuntimeError("Unexpected poll choices: "+title)
    kept=int(options[0]["voter_count"])
    snooze=int(options[1]["voter_count"]) if len(options)==3 else 0
    deleted=int(options[-1]["voter_count"])
    outcome=voting.save_votes(c,rec,kept,snooze,deleted,now,iso)
    logging.info("Closed %s: veto=%d snooze=%d okay=%d => %s",title,kept,snooze,deleted,outcome)
    if kept:voting.permanent_veto(c,rec,maintain)
    elif snooze:voting.start_snooze(c,rec,maintain)
def exclude(c,cid,media_id,title):
    response=maintain("/api/rules/exclusions/bulk",{"collectionId":cid,"mediaIds":[media_id],"action":0})
    if not response.get("results") or any(x.get("code")!=1 for x in response["results"]):
        raise RuntimeError("Maintainerr exclusion not confirmed: "+str(response))
    c.execute("UPDATE polls SET state='vetoed' WHERE collection_id=? AND media_id=?",(cid,media_id))
    c.commit()
    logging.info("VETO protected %s",title)
def sync():
    if not TOKEN or not CHAT:
        logging.warning("Telegram credentials missing; not attempting polling")
        return
    c=database()
    collections=check_collections()
    existing={(r[0],r[1]):r for r in c.execute("SELECT * FROM polls").fetchall()}
    # Always reconcile historical results, even for media no longer in a collection.
    for rec in existing.values():
        try:
            if rec[9]=="open":close_due(c,rec)
            elif rec[9]=="veto_pending":voting.permanent_veto(c,rec,maintain)
            elif rec[9]=="snooze_pending":voting.start_snooze(c,rec,maintain)
            elif rec[9]=="snoozed":voting.finish_snooze(c,rec,maintain,now,parse)
            elif rec[9]=="timer_pending":reset_timer(c,rec[0],rec[1],rec[2])
        except Exception:
            logging.exception("Poll reconciliation failure for %s",rec[2])
    covers.backfill(c,IDS,members,tg,CHAT,limit=2)
    created=0
    for cid in IDS:
        for item in members(cid):
            key=(cid,item["mediaServerId"])
            previous=existing.get(key)
            if (previous is None or previous[9]=="awaiting_reentry") and ENABLED and created<MAX_NEW_POLLS_PER_CYCLE:
                try:
                    if previous is not None:
                        c.execute("INSERT INTO poll_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (previous[0],previous[1],previous[2],previous[3],previous[4],previous[5],
                             previous[6],previous[7],previous[10],previous[8],previous[9],
                             previous[14] or 1,iso(now())))
                        c.execute("DELETE FROM polls WHERE collection_id=? AND media_id=?",key)
                        c.commit()
                    publish(c,cid,item)
                    created+=1
                    time.sleep(1.5)
                except Exception:
                    logging.exception("Could not start poll for %s",key)
                    break
    c.close()
if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting companion: publish=%s collections=%s (never deletes media)",ENABLED,IDS)
    while True:
        try:
            with execution_lock():
                sync()
        except Exception:logging.exception("Reconciliation cycle failed")
        time.sleep(max(INTERVAL,60))
