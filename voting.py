"""Poll decision and temporary exclusion handling for the Maintainerr companion."""
import json
import logging
from datetime import timedelta

def migrate(db):
    fields = {r[1] for r in db.execute("PRAGMA table_info(polls)")}
    for col, kind in (
        ("postpone_votes", "INTEGER"),
        ("snooze_until", "TEXT"),
        ("owned_exclusion_ids", "TEXT"),
        ("exclusion_before", "TEXT"),
        ("round", "INTEGER DEFAULT 1"),
    ):
        if col not in fields:
            db.execute(f"ALTER TABLE polls ADD COLUMN {col} {kind}")
    db.execute("""CREATE TABLE IF NOT EXISTS poll_history(
        collection_id INTEGER, media_id TEXT, title TEXT,
        message_id INTEGER, poll_id TEXT, opened_at TEXT, closed_at TEXT,
        keep_votes INTEGER, postpone_votes INTEGER, delete_votes INTEGER,
        state TEXT, round INTEGER, archived_at TEXT)""")
    db.commit()

def scoped_ids(maintain, cid):
    rows = maintain(f"/api/rules/exclusion?rulegroupId={cid}")
    if not isinstance(rows, list):
        raise RuntimeError("Exclusions unavailable")
    return {int(r["id"]): r for r in rows if r.get("ruleGroupId") == cid}

def relevant(rows, media_id):
    return {rid: row for rid, row in rows.items()
            if row.get("mediaServerId") == media_id or row.get("parent") == media_id}

def save_votes(db, row, keep, snooze, ok, now, iso):
    state = "veto_pending" if keep else "snooze_pending" if snooze else "released"
    at = iso(now())
    due = iso(now() + timedelta(days=60)) if snooze and not keep else None
    db.execute("""UPDATE polls SET state=?, closed_at=?, keep_votes=?,
        postpone_votes=?, delete_votes=?, snooze_until=?
        WHERE collection_id=? AND media_id=?""",
        (state, at, keep, snooze, ok, due, row[0], row[1]))
    db.commit()
    return state

class MissingMetadataError(RuntimeError):
    """Maintainerr explicitly confirms that this media ID has no metadata."""

def apply_exclusion(maintain, cid, media_id):
    result = maintain("/api/rules/exclusions/bulk", {
        "collectionId": cid, "mediaIds": [media_id], "action": 0})
    outcomes = result.get("results", [])
    if len(outcomes) == 1:
        item = outcomes[0]
        if (item.get("code") == 0 and str(item.get("mediaId")) == str(media_id)
                and str(item.get("message", "")).strip().lower() == "failed - no metadata"):
            raise MissingMetadataError(f"Maintainerr has no metadata for media_id={media_id}")
    if len(outcomes) != 1 or outcomes[0].get("code") != 1:
        raise RuntimeError("Exclusion failed: "+str(result))

def permanent_veto(db, row, maintain):
    cid, mid = row[0], row[1]
    apply_exclusion(maintain, cid, mid)
    rows = scoped_ids(maintain, cid)
    if not relevant(rows, mid):
        raise RuntimeError("Veto not confirmed in Maintainerr")
    db.execute("UPDATE polls SET state='vetoed' WHERE collection_id=? AND media_id=?",
               (cid, mid))
    db.commit()
    logging.info("Permanent veto confirmed for %s", row[2])

def start_snooze(db, row, maintain):
    cid, mid = row[0], row[1]
    before_raw = row[13]
    if before_raw is None:
        before = sorted(relevant(scoped_ids(maintain, cid), mid))
        db.execute("UPDATE polls SET exclusion_before=? WHERE collection_id=? AND media_id=?",
                   (json.dumps(before), cid, mid))
        db.commit()
    else:
        before = json.loads(before_raw)
    if before:
        db.execute("UPDATE polls SET state='externally_protected' WHERE collection_id=? AND media_id=?", (cid,mid))
        db.commit()
        return
    apply_exclusion(maintain, cid, mid)
    after = relevant(scoped_ids(maintain, cid), mid)
    owned = sorted(set(after) - set(before))
    if not owned:
        raise RuntimeError("No newly created exclusion to own")
    db.execute("UPDATE polls SET state='snoozed',owned_exclusion_ids=? WHERE collection_id=? AND media_id=?",
               (json.dumps(owned), cid, mid))
    db.commit()
    logging.info("Snoozed %s for 60 days",row[2])

def finish_snooze(db, row, maintain, now, parse):
    if not row[11] or now() < parse(row[11]):
        return
    cid, mid = row[0], row[1]
    owned = json.loads(row[12] or "[]")
    current = scoped_ids(maintain, cid)
    for exclusion_id in owned:
        if exclusion_id in current:
            item = current[exclusion_id]
            if item.get("mediaServerId") != mid and item.get("parent") != mid:
                raise RuntimeError("Exclusion ownership mismatch")
            response = maintain(f"/api/rules/exclusion/{exclusion_id}", method="DELETE")
            if response.get("code") != 1:
                raise RuntimeError("Cannot remove owned exclusion: "+str(response))
    db.execute("UPDATE polls SET state='awaiting_reentry' WHERE collection_id=? AND media_id=?", (cid,mid))
    db.commit()
    logging.info("Snooze expired for %s",row[2])
