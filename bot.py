#!/usr/bin/env python3
"""Maintainerr-owned cleanup; Telegram voting companion (no delete endpoints)."""
import json, os, re, sqlite3, time, urllib.request, urllib.error, urllib.parse, logging, fcntl
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
STALE_POLL_DAYS=29
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
def redact_diagnostic(value):
    text = str(value)
    if TOKEN:
        text = text.replace(TOKEN, "[redacted]")
    # Never expose bot credentials embedded in an exception or response URL.
    return re.sub(r"(https?://api\.telegram\.org/bot)[^/\s\"'<>]+",
                  r"\1[redacted]", text, flags=re.IGNORECASE)

def diagnostic_json(value):
    text = redact_diagnostic(json.dumps(value, ensure_ascii=False, default=str))
    return text if len(text) <= 8192 else text[:8192] + "...[truncated]"

def telegram_target(body):
    return diagnostic_json({key: body[key] for key in
                            ("chat_id", "message_id", "poll_id", "from_chat_id") if key in body})

class TelegramAPIError(RuntimeError):
    def __init__(self, method, code, description, parameters=None, *,
                 http_status=None, response=None, response_headers=None):
        self.code = code
        self.http_status = code if http_status is None else http_status
        self.parameters = parameters if isinstance(parameters, dict) else {}
        retry_after = self.parameters.get("retry_after")
        self.retry_after = retry_after if type(retry_after) is int and retry_after >= 0 else None
        self.description = redact_diagnostic(description)
        self.api_code = response.get("error_code", code) if isinstance(response, dict) else code
        self.response_text = diagnostic_json(response)
        self.headers_text = diagnostic_json(response_headers or {})
        super().__init__(f"Telegram {method}: HTTP {self.http_status}: {self.description}")

class TelegramTransportError(RuntimeError):
    pass

def telegram(method,body):
    try:
        return api("https://api.telegram.org/bot"+TOKEN+"/"+method,body)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        headers = {key: exc.headers[key] for key in
                   ("Date", "Content-Type", "Retry-After", "X-Request-ID", "X-Telegram-Request-ID")
                   if exc.headers is not None and key in exc.headers}
        try:
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Non-object JSON error")
            description = response.get("description", "No description")
            parameters = response.get("parameters")
        except (ValueError, UnicodeError, AttributeError):
            description, parameters, response = "Unreadable HTTP error response", None, raw
        finally:
            exc.close()
        raise TelegramAPIError(method, exc.code, description, parameters,
                               http_status=exc.code, response=response,
                               response_headers=headers) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TelegramTransportError(
            f"Telegram {method}: {type(exc).__name__}: {redact_diagnostic(exc)}"
        ) from None

TELEGRAM_MAX_RETRIES = 5

TELEGRAM_CHAT_INTERVAL = 3.2
TELEGRAM_MUTATIONS = {"sendMessage", "sendPhoto", "sendPoll", "stopPoll",
                      "editMessageReplyMarkup", "deleteMessage"}

def pace_telegram(method, body):
    rate = sqlite3.connect(DB, timeout=30)
    try:
        rate.execute("BEGIN IMMEDIATE")
        rate.execute("""CREATE TABLE IF NOT EXISTS telegram_rate_limits(
            scope TEXT PRIMARY KEY,next_allowed REAL NOT NULL)""")
        current = time.time()
        global_row = rate.execute("SELECT next_allowed FROM telegram_rate_limits WHERE scope='global'").fetchone()
        ready = max(current, global_row[0] if global_row else current)
        if method in TELEGRAM_MUTATIONS and "chat_id" in body:
            scope = "chat:" + str(body["chat_id"])
            chat_row = rate.execute("SELECT next_allowed FROM telegram_rate_limits WHERE scope=?", (scope,)).fetchone()
            ready = max(ready, chat_row[0] if chat_row else current)
            rate.execute("INSERT OR REPLACE INTO telegram_rate_limits VALUES (?,?)",
                         (scope, ready + TELEGRAM_CHAT_INTERVAL))
        rate.commit()
    finally:
        rate.close()
    delay = max(0, ready - current)
    if delay:
        logging.debug("event=telegram_paced method=%s target=%s delay=%.3f",
                      method, telegram_target(body), delay)
    while delay > 0:
        chunk = min(delay, 60)
        time.sleep(chunk)
        delay -= chunk

def record_telegram_cooldown(delay):
    rate = sqlite3.connect(DB, timeout=30)
    try:
        with rate:
            rate.execute("""CREATE TABLE IF NOT EXISTS telegram_rate_limits(
                scope TEXT PRIMARY KEY,next_allowed REAL NOT NULL)""")
            rate.execute("""INSERT INTO telegram_rate_limits VALUES ('global',?)
                ON CONFLICT(scope) DO UPDATE SET
                next_allowed=MAX(telegram_rate_limits.next_allowed,excluded.next_allowed)""",
                (time.time() + delay,))
    finally:
        rate.close()

def tg(method,body):
    request_id = f"{time.time_ns():x}"
    target = telegram_target(body)
    for attempt in range(TELEGRAM_MAX_RETRIES + 1):
        pace_telegram(method, body)
        started = time.monotonic()
        try:
            r = telegram(method, body)
            if not r.get("ok"):
                raise TelegramAPIError(method, r.get("error_code"),
                                       r.get("description"), r.get("parameters"),
                                       http_status=200, response=r)
            elapsed = (time.monotonic() - started) * 1000
            logging.log(logging.INFO if attempt or method == "stopPoll" else logging.DEBUG,
                        "event=telegram_request_succeeded request_id=%s method=%s target=%s "
                        "attempt=%s elapsed_ms=%.1f", request_id, method, target, attempt + 1, elapsed)
            return r["result"]
        except TelegramAPIError as exc:
            retry = exc.code == 429 and attempt < TELEGRAM_MAX_RETRIES
            delay = (exc.retry_after + 1 if exc.retry_after is not None
                     else min(5 * 2**attempt, 60)) if retry else None
            logging.log(logging.WARNING if retry else logging.ERROR,
                        "event=telegram_request_failed request_id=%s method=%s target=%s "
                        "attempt=%s/%s elapsed_ms=%.1f http_status=%s error_code=%s "
                        "description=%r parameters=%s retry_after=%s retry_in=%s "
                        "will_retry=%s headers=%s response=%s",
                        request_id, method, target, attempt + 1, TELEGRAM_MAX_RETRIES + 1,
                        (time.monotonic() - started) * 1000, exc.http_status, exc.api_code,
                        exc.description, diagnostic_json(exc.parameters), exc.retry_after,
                        delay, retry, exc.headers_text, exc.response_text)
            if exc.code == 429:
                cooldown = exc.retry_after + 1 if exc.retry_after is not None else min(5 * 2**attempt, 60)
                record_telegram_cooldown(cooldown)
            if not retry:
                raise
            # Persist flood waits for the next process as well as this retry.
            while delay > 0:
                chunk = min(delay, 60)
                time.sleep(chunk)
                delay -= chunk
        except (TelegramTransportError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            logging.error("event=telegram_transport_failed request_id=%s method=%s target=%s "
                          "attempt=%s elapsed_ms=%.1f exception_type=%s reason=%r "
                          "will_retry=False outcome=unknown",
                          request_id, method, target, attempt + 1,
                          (time.monotonic() - started) * 1000, type(exc).__name__,
                          redact_diagnostic(exc))
            raise

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
    fields = {r[1] for r in c.execute("PRAGMA table_info(polls)")}
    for name in ("stale_since", "stale_reason", "stale_previous_state", "close_requested_at"):
        if name not in fields:
            c.execute(f"ALTER TABLE polls ADD COLUMN {name} TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS stale_poll_archive(
        poll_id TEXT PRIMARY KEY, collection_id INTEGER NOT NULL, media_id TEXT NOT NULL,
        title TEXT NOT NULL, payload TEXT NOT NULL, stale_since TEXT NOT NULL,
        reason TEXT NOT NULL, archived_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS final_poll_results(
        poll_id TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_at TEXT NOT NULL,
        proof TEXT NOT NULL CHECK(proof='stopPoll'))""")
    # Remove the superseded recovery schema. Only acknowledged responses survive.
    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "closed_poll_results" in tables:
        old_fields = {row[1] for row in c.execute("PRAGMA table_info(closed_poll_results)")}
        if "source" in old_fields:
            c.execute("""INSERT OR IGNORE INTO final_poll_results
                SELECT poll_id,payload,recorded_at,'stopPoll' FROM closed_poll_results
                WHERE source='stopPoll'""")
            untrusted = "SELECT poll_id FROM closed_poll_results WHERE source!='stopPoll'"
        else:
            untrusted = "SELECT poll_id FROM closed_poll_results"
        c.execute("""UPDATE polls SET
            state=CASE WHEN state='closure_unverified' THEN 'open' ELSE state END,
            closed_at=NULL,keep_votes=NULL,postpone_votes=NULL,delete_votes=NULL,snooze_until=NULL,
            close_requested_at=COALESCE(close_requested_at,?)
            WHERE poll_id IN (""" + untrusted + ")", (iso(now()),))
        c.execute("DROP TABLE closed_poll_results")
    c.execute("""UPDATE polls SET state='open',closed_at=NULL,keep_votes=NULL,
        postpone_votes=NULL,delete_votes=NULL,snooze_until=NULL,
        close_requested_at=COALESCE(close_requested_at,?)
        WHERE state='closure_unverified'""",(iso(now()),))
    c.execute("UPDATE polls SET stale_previous_state='open' WHERE stale_previous_state='closure_unverified'")
    c.execute("DROP TABLE IF EXISTS poll_closure_reviews")
    c.commit()
    return c
def metadata_exists(media_id):
    path = "/api/media-server/meta/" + urllib.parse.quote(str(media_id), safe="")
    metadata = maintain(path)
    if metadata is None or metadata == {}:
        return False
    if not isinstance(metadata, dict) or str(metadata.get("id")) != str(media_id):
        raise RuntimeError(f"Invalid metadata response for media_id={media_id}: "
                           + diagnostic_json(metadata))
    return True

def mark_stale(c, rec, reason):
    current = c.execute("SELECT * FROM polls WHERE collection_id=? AND media_id=?",
                        (rec[0], rec[1])).fetchone()
    if current is None:
        return
    first = current[9] != "stale"
    c.execute("""UPDATE polls SET
        stale_previous_state=CASE WHEN state!='stale' THEN state ELSE stale_previous_state END,
        stale_since=COALESCE(stale_since,?),stale_reason=?,state='stale'
        WHERE collection_id=? AND media_id=?""",
        (iso(now()), reason, rec[0], rec[1]))
    c.commit()
    if first:
        logging.warning("event=stale_poll_marked title=%r collection_id=%s media_id=%s "
                        "poll_id=%s message_id=%s prior_state=%s reason=%r archive_after=%s",
                        rec[2],rec[0],rec[1],rec[4],rec[3],current[9],reason,
                        iso(parse(rec[5]) + timedelta(days=STALE_POLL_DAYS)))
    else:
        logging.info("event=stale_poll_ignored title=%r collection_id=%s media_id=%s "
                     "poll_id=%s reason=%r archive_after=%s",rec[2],rec[0],rec[1],rec[4],
                     reason,iso(parse(rec[5]) + timedelta(days=STALE_POLL_DAYS)))
    if now() < parse(rec[5]) + timedelta(days=STALE_POLL_DAYS):
        return
    cursor = c.execute("SELECT * FROM polls WHERE collection_id=? AND media_id=?",
                       (rec[0], rec[1]))
    row = cursor.fetchone()
    snapshot = dict(zip((column[0] for column in cursor.description), row))
    # Preserve all vote, message, and exclusion ownership data before removal.
    with c:
        c.execute("""INSERT OR IGNORE INTO stale_poll_archive
            (poll_id,collection_id,media_id,title,payload,stale_since,reason,archived_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (rec[4],rec[0],rec[1],rec[2],json.dumps(snapshot),
             snapshot["stale_since"],reason,iso(now())))
        c.execute("DELETE FROM polls WHERE collection_id=? AND media_id=? AND poll_id=?",
                  (rec[0],rec[1],rec[4]))
    logging.info("event=stale_poll_archived title=%r collection_id=%s media_id=%s "
                 "poll_id=%s age_days=%.2f",rec[2],rec[0],rec[1],rec[4],
                 (now()-parse(rec[5])).total_seconds()/86400)

def reconcile_poll_metadata(c, rec):
    if not metadata_exists(rec[1]):
        mark_stale(c,rec,"Maintainerr metadata endpoint returned no metadata")
        return None
    if rec[9] == "stale":
        previous = c.execute("SELECT stale_previous_state FROM polls "
                             "WHERE collection_id=? AND media_id=?",(rec[0],rec[1])).fetchone()[0]
        if not previous or previous == "stale":
            raise RuntimeError(f"Missing previous state for stale poll {rec[4]}")
        c.execute("""UPDATE polls SET state=?,stale_since=NULL,stale_reason=NULL,
            stale_previous_state=NULL WHERE collection_id=? AND media_id=?""",
            (previous,rec[0],rec[1]))
        c.commit()
        logging.info("event=stale_poll_restored title=%r collection_id=%s media_id=%s "
                     "poll_id=%s restored_state=%s",rec[2],rec[0],rec[1],rec[4],previous)
        return c.execute("SELECT * FROM polls WHERE collection_id=? AND media_id=?",
                         (rec[0],rec[1])).fetchone()
    return rec

def refresh_poll_metadata(c):
    failed = set()
    for rec in c.execute("SELECT * FROM polls").fetchall():
        try:
            reconcile_poll_metadata(c,rec)
        except Exception:
            failed.add((rec[0],rec[1]))
            logging.exception("Metadata reconciliation failed title=%r collection_id=%s "
                              "media_id=%s poll_id=%s state=%s",
                              rec[2],rec[0],rec[1],rec[4],rec[9])
    return failed

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

def final_poll(c, rec):
    cached = c.execute("SELECT payload,recorded_at FROM final_poll_results WHERE poll_id=?",
                       (rec[4],)).fetchone()
    acknowledged = cached is not None
    if acknowledged:
        result, closed_at = json.loads(cached[0]), parse(cached[1])
    else:
        result = tg("stopPoll", {"chat_id": CHAT, "message_id": rec[3]})
        closed_at = now()
    if result.get("id") != rec[4] or result.get("is_closed") is not True:
        raise RuntimeError(f"Final poll identity or closed state not confirmed: {rec[2]}")
    options = result.get("options", [])
    if len(options) not in (2, 3) or any(
            type(o.get("voter_count")) is not int or o["voter_count"] < 0 for o in options):
        raise RuntimeError("Unexpected poll choices or counts: " + rec[2])
    if not acknowledged:
        # Only an acknowledged stop, never a message edit, can produce this cache.
        c.execute("""INSERT INTO final_poll_results(poll_id,payload,recorded_at,proof)
            VALUES (?,?,?,'stopPoll') ON CONFLICT(poll_id) DO UPDATE SET
            payload=excluded.payload,recorded_at=excluded.recorded_at,proof=excluded.proof""",
            (rec[4], json.dumps(result), iso(closed_at)))
        c.commit()
    logging.info("event=poll_close_result collection_id=%s media_id=%s poll_id=%s "
                 "message_id=%s source=%s is_closed=%s counts=%s recorded_at=%s",
                 rec[0], rec[1], rec[4], rec[3], "acknowledged_cache" if acknowledged else "stopPoll",
                 result.get("is_closed"), [o["voter_count"] for o in options], iso(closed_at))
    return result, closed_at

def close_due(c,rec,min_age_days=POLL_DAYS,*,metadata_checked=False):
    cid,media_id,title,msg,_,opened,_,_,_,state=rec[:10]
    if min_age_days < 0:
        raise ValueError("min_age_days must be nonnegative")
    requested = c.execute("SELECT close_requested_at FROM polls WHERE collection_id=? AND media_id=?",
                          (cid,media_id)).fetchone()[0]
    if state != "open" or (not requested and now()<parse(opened)+timedelta(days=min_age_days)):
        return
    if not metadata_checked and reconcile_poll_metadata(c,rec) is None:
        return
    logging.info("event=poll_close_attempt title=%r collection_id=%s media_id=%s "
                 "poll_id=%s chat_id=%s message_id=%s state=%s opened_at=%s min_age_days=%s",
                 title, cid, media_id, rec[4], redact_diagnostic(CHAT), msg, state,
                 opened, min_age_days)
    c.execute("UPDATE polls SET close_requested_at=COALESCE(close_requested_at,?) "
              "WHERE collection_id=? AND media_id=?",(iso(now()),cid,media_id))
    c.commit()
    result, closed_at = final_poll(c, rec)
    options = result["options"]
    kept=int(options[0]["voter_count"])
    snooze=int(options[1]["voter_count"]) if len(options)==3 else 0
    deleted=int(options[-1]["voter_count"])
    outcome=voting.save_votes(c,rec,kept,snooze,deleted,lambda: closed_at,iso)
    c.execute("UPDATE polls SET close_requested_at=NULL WHERE collection_id=? AND media_id=?",
              (cid,media_id))
    c.commit()
    logging.info("Closed %s: veto=%d snooze=%d okay=%d => %s",title,kept,snooze,deleted,outcome)
    try:
        if kept:voting.permanent_veto(c,rec,maintain)
        elif snooze:voting.start_snooze(c,rec,maintain)
    except voting.MissingMetadataError as exc:
        mark_stale(c,rec,str(exc))
def sync():
    if not TOKEN or not CHAT:
        logging.warning("Telegram credentials missing; not attempting polling")
        return
    c=database()
    collections=check_collections()
    metadata_failures=refresh_poll_metadata(c)
    existing={(r[0],r[1]):r for r in c.execute("SELECT * FROM polls").fetchall()}
    # Always reconcile historical results, even for media no longer in a collection.
    for rec in existing.values():
        if (rec[0],rec[1]) in metadata_failures:
            continue
        try:
            if rec[9]=="open":close_due(c,rec,metadata_checked=True)
            elif rec[9]=="veto_pending":voting.permanent_veto(c,rec,maintain)
            elif rec[9]=="snooze_pending":voting.start_snooze(c,rec,maintain)
            elif rec[9]=="snoozed":voting.finish_snooze(c,rec,maintain,now,parse)
            elif rec[9]=="timer_pending":reset_timer(c,rec[0],rec[1],rec[2])
        except voting.MissingMetadataError as exc:
            mark_stale(c,rec,str(exc))
        except Exception:
            logging.exception("Poll reconciliation failure title=%r collection_id=%s media_id=%s "
                              "poll_id=%s message_id=%s state=%s",
                              rec[2],rec[0],rec[1],rec[4],rec[3],rec[9])
    covers.backfill(c,IDS,members,tg,CHAT,limit=2)
    created=0
    for cid in IDS:
        for item in members(cid):
            key=(cid,item["mediaServerId"])
            previous=existing.get(key)
            if (previous is None or previous[9]=="awaiting_reentry") and ENABLED and created<MAX_NEW_POLLS_PER_CYCLE:
                try:
                    if not metadata_exists(item["mediaServerId"]):
                        logging.warning("event=poll_publication_skipped collection_id=%s media_id=%s "
                                        "reason=missing_metadata",cid,item["mediaServerId"])
                        continue
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
