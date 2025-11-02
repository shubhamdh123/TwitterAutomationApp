# app.py
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from threading import Lock

from flask import Flask, g, render_template, request, redirect, url_for, flash, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import tweepy

# --- Configuration via ENV ---
DB_PATH = os.environ.get("DATABASE_PATH", "tweets.db")
TW_API_KEY = os.environ.get("TWITTER_API_KEY")
TW_API_SECRET = os.environ.get("TWITTER_API_SECRET")
TW_ACCESS_TOKEN = os.environ.get("TWITTER_ACCESS_TOKEN")
TW_ACCESS_SECRET = os.environ.get("TWITTER_ACCESS_SECRET")

if not all([TW_API_KEY, TW_API_SECRET, TW_ACCESS_TOKEN, TW_ACCESS_SECRET]):
    print("WARNING: Twitter API credentials not fully set. Set TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-for-prod")
scheduler = BackgroundScheduler()
scheduler_lock = Lock()

# --- DB helpers ---
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            scheduled_utc TIMESTAMP NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | posted | failed
            posted_at TIMESTAMP,
            twitter_id TEXT,
            error TEXT
        );
        """)
        db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# --- Tweepy client (OAuth1) ---
def get_tweepy_api():
    auth = tweepy.OAuth1UserHandler(TW_API_KEY, TW_API_SECRET, TW_ACCESS_TOKEN, TW_ACCESS_SECRET)
    api = tweepy.API(auth)
    return api

# --- Posting job ---
def post_tweet_job(scheduled_id):
    """
    Called by APScheduler at the scheduled UTC time.
    Posts the tweet via Tweepy and updates DB.
    """
    with scheduler_lock:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        cur = db.cursor()
        cur.execute("SELECT * FROM scheduled_tweets WHERE id = ?", (scheduled_id,))
        row = cur.fetchone()
        if not row:
            db.close()
            return

        # don't re-post
        if row["status"] != "scheduled":
            db.close()
            return

        text = row["text"]
        try:
            api = get_tweepy_api()
            resp = api.update_status(status=text)
            twitter_id = getattr(resp, "id_str", None) or str(getattr(resp, "id", ""))
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            cur.execute("UPDATE scheduled_tweets SET status='posted', posted_at=?, twitter_id=? WHERE id=?",
                        (now.isoformat(), twitter_id, scheduled_id))
            db.commit()
            print(f"Posted tweet id={scheduled_id} twitter_id={twitter_id}")
        except Exception as e:
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            cur.execute("UPDATE scheduled_tweets SET status='failed', posted_at=?, error=? WHERE id=?",
                        (now.isoformat(), str(e), scheduled_id))
            db.commit()
            print(f"Failed posting tweet id={scheduled_id}: {e}")
        finally:
            db.close()

# --- Scheduling helpers ---
def schedule_job(scheduled_id, run_time_utc: datetime):
    # APScheduler expects naive datetimes in UTC or with tzinfo depending on config; we'll pass tz-aware UTC
    scheduler.add_job(func=lambda: post_tweet_job(scheduled_id),
                      trigger='date',
                      run_date=run_time_utc,
                      id=f"tweet-{scheduled_id}",
                      replace_existing=True)
    print(f"Scheduled job tweet-{scheduled_id} at {run_time_utc.isoformat()}")

def unschedule_job(scheduled_id):
    job_id = f"tweet-{scheduled_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

def load_and_schedule_all():
    """
    Load all tweets in 'scheduled' state and schedule them.
    Also reschedule any in future (useful on start).
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    cur.execute("SELECT * FROM scheduled_tweets WHERE status = 'scheduled' ORDER BY scheduled_utc")
    rows = cur.fetchall()
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    for row in rows:
        scheduled_time = datetime.fromisoformat(row["scheduled_utc"])
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        # if scheduled_time is in the past, schedule immediately (run ASAP)
        run_time = scheduled_time if scheduled_time > now else now + timedelta(seconds=5)
        try:
            schedule_job(row["id"], run_time)
        except Exception as e:
            print("Error scheduling:", e)
    db.close()

# --- Routes ---
@app.route("/")
def index():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM scheduled_tweets ORDER BY scheduled_utc DESC LIMIT 200")
    rows = cur.fetchall()
    # convert scheduled_utc string to ISO for display
    tweets = []
    for r in rows:
        tweets.append({
            "id": r["id"],
            "text": r["text"],
            "scheduled_utc": r["scheduled_utc"],
            "status": r["status"],
            "posted_at": r["posted_at"],
            "twitter_id": r["twitter_id"],
            "error": r["error"]
        })
    return render_template("index.html", tweets=tweets)

@app.route("/schedule", methods=["POST"])
def schedule():
    text = request.form.get("text", "").strip()
    # client will send datetime-local value and offset minutes
    local_dt = request.form.get("local_datetime")  # e.g. 2025-11-02T22:30
    tz_offset_min = int(request.form.get("tz_offset_min", "0"))  # minutes offset from UTC (client -> server)
    if not text or not local_dt:
        flash("Both tweet text and date/time required.", "danger")
        return redirect(url_for("index"))

    # parse local datetime (no timezone) and convert to UTC using offset
    try:
        dt_local = datetime.fromisoformat(local_dt)
    except Exception:
        flash("Invalid date/time format.", "danger")
        return redirect(url_for("index"))

    # client offset is minutes to add to local to get UTC: e.g. IST is +330 -> tz_offset_min = -330?
    # We'll assume client sends offset = minutes *from* UTC (i.e. new Date().getTimezoneOffset()) -> returns minutes behind UTC
    # getTimezoneOffset gives minutes to add to local to get UTC (UTC = local + offset); it's typically -330 for IST? Actually getTimezoneOffset returns minutes difference: UTC - local. For IST (UTC+5:30), getTimezoneOffset() returns -330.
    # So UTC = local + (-offset_min) minutes? Wait: If getTimezoneOffset = -330, UTC = local + (-(-330))? Simpler: compute UTC = local + timedelta(minutes = -tz_offset_min)
    utc_dt = dt_local + timedelta(minutes=-tz_offset_min)
    utc_dt = utc_dt.replace(tzinfo=timezone.utc)

    db = get_db()
    cur = db.cursor()
# Convert ISO format (with 'T' and timezone) to SQLite-safe format
safe_time = utc_dt.isoformat().replace("T", " ").split("+")[0]

cur.execute("INSERT INTO scheduled_tweets (text, scheduled_utc, status) VALUES (?, ?, 'scheduled')",
            (text, safe_time))


    db.commit()
    scheduled_id = cur.lastrowid
    # schedule job
    try:
        schedule_job(scheduled_id, utc_dt)
    except Exception as e:
        print("Error scheduling job:", e)
        flash("Scheduled but couldn't schedule background job; it will be scheduled on app restart.", "warning")
    else:
        flash("Tweet scheduled successfully!", "success")
    return redirect(url_for("index"))

@app.route("/cancel/<int:tid>", methods=["POST"])
def cancel(tid):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM scheduled_tweets WHERE id = ?", (tid,))
    row = cur.fetchone()
    if not row:
        flash("Not found", "danger")
        return redirect(url_for("index"))
    if row["status"] != "scheduled":
        flash("Cannot cancel - already posted or failed.", "warning")
        return redirect(url_for("index"))
    cur.execute("UPDATE scheduled_tweets SET status='cancelled' WHERE id = ?", (tid,))
    db.commit()
    unschedule_job(tid)
    flash("Cancelled scheduled tweet.", "info")
    return redirect(url_for("index"))

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

# --- Startup ---
if __name__ == "__main__":
    init_db()
    scheduler.start()
    load_and_schedule_all()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    # running under gunicorn on render
    init_db()
    scheduler.start()
    load_and_schedule_all()
