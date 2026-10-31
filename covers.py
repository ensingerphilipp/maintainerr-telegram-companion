"""TMDb cover image helpers for Maintainerr Telegram polls."""
import logging
import re
import time
from urllib.parse import urlsplit

def migrate(db):
    cols={x[1] for x in db.execute("PRAGMA table_info(polls)")}
    if "cover_message_id" not in cols:
        db.execute("ALTER TABLE polls ADD COLUMN cover_message_id INTEGER")
    if "cover_status" not in cols:
        db.execute("ALTER TABLE polls ADD COLUMN cover_status TEXT")
    db.commit()

def poster_url(media):
    source=media.get("image_path") or ""
    url=urlsplit(source)
    if url.scheme!="https" or url.netloc!="image.tmdb.org":
        return None
    path=re.fullmatch(r"/t/p/(?:w[0-9]+|original)/([A-Za-z0-9_.-]+)",url.path)
    if not path:
        return None
    return "https://image.tmdb.org/t/p/w185/"+path.group(1)

def attach(db, row, media, telegram, chat):
    cid,mid,title,message_id=row
    url=poster_url(media)
    status="sending" if url else "missing"
    db.execute("UPDATE polls SET cover_status=? WHERE collection_id=? AND media_id=?",
               (status,cid,mid))
    db.commit()
    if not url:
        logging.info("No supported cover for %s",title)
        return
    try:
        result=telegram("sendPhoto",{
            "chat_id":chat,
            "photo":url,
            "caption":"🖼️ "+title,
            "reply_parameters":{"message_id":message_id},
            "disable_notification":True
        })
    except Exception:
        db.execute("UPDATE polls SET cover_status='failed' WHERE collection_id=? AND media_id=?",(cid,mid))
        db.commit()
        logging.exception("Could not attach cover for %s",title)
        return
    db.execute("UPDATE polls SET cover_status='sent',cover_message_id=? WHERE collection_id=? AND media_id=?",
               (result["message_id"],cid,mid))
    db.commit()
    logging.info("Cover attached: %s",title)

def backfill(db, ids, members, telegram, chat, limit=2):
    completed=0
    for cid in ids:
        if completed>=limit:
            break
        indexed={x["mediaServerId"]:x for x in members(cid)}
        pending=db.execute("""SELECT collection_id,media_id,title,message_id FROM polls
           WHERE collection_id=? AND state='open' AND cover_status IS NULL
           ORDER BY opened_at LIMIT ?""",(cid,limit-completed)).fetchall()
        for row in pending:
            media=indexed.get(row[1])
            if media is None:
                continue
            attach(db,row,media,telegram,chat)
            completed+=1
            time.sleep(.3)

def first(media, title, telegram, chat):
    """Send cover before poll, or None if absent/fails."""
    url=poster_url(media)
    if not url:
        return None
    try:
        result=telegram("sendPhoto",{
            "chat_id":chat,"photo":url,
            "caption":"🖼️ "+title,
            "disable_notification":True})
        return result["message_id"]
    except Exception:
        logging.exception("Small cover failed for %s; poll will still be posted",title)
        return None
