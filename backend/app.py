"""
════════════════════════════════════════════════════════════════
  SMRITIKANA BUSINESS SOLUTIONS — Python Flask Backend
  Stock data: Yahoo Finance (yfinance) — no API key needed
  Database:   MongoDB via pymongo
  Email:      smtplib (Gmail)
════════════════════════════════════════════════════════════════

  QUICK START:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your values
    python app.py

  ENDPOINTS:
    GET  /api/health                  — server health check
    GET  /api/stocks                  — all cached stock data
    GET  /api/stocks/<category>       — indices | india | crypto
    GET  /api/stocks/refresh          — force-refresh cache
    POST /api/leads                   — save consultation request
    POST /api/cibil                   — save CIBIL enquiry
    GET  /api/admin/leads             — list leads (x-admin-secret header)
    GET  /api/admin/cibil             — list CIBIL enquiries (admin)
    PATCH /api/admin/leads/<id>       — update lead status (admin)
════════════════════════════════════════════════════════════════
"""

import os
import json
import smtplib
import threading
import time
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError, InvalidURI
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# static_folder points to backend/public/ — where index.html lives
app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.getenv("ADMIN_SECRET", "dev-secret-key-12345") # Needed for session
CORS(app, origins=os.getenv("FRONTEND_URL", "*"), methods=["GET", "POST", "PATCH"])

# ─── CONFIG ──────────────────────────────────────────────────
MONGO_URI    = os.getenv("MONGODB_URI", "mongodb://localhost:27017/smritikana")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me")
EMAIL_USER   = os.getenv("EMAIL_USER", "")
EMAIL_PASS   = os.getenv("EMAIL_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", EMAIL_USER)
PORT         = int(os.getenv("PORT", 5000))
REFRESH_SECS = int(os.getenv("STOCK_REFRESH_SECS", 60))

# ─── MONGODB ─────────────────────────────────────────────────
db = None
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client.get_default_database() if "/" in MONGO_URI and MONGO_URI.count("/") >= 3 else client["smritikana"]
    log.info("✅  MongoDB connected")
except (ConnectionFailure, ConfigurationError, InvalidURI) as e:
    log.warning(f"⚠️  MongoDB unavailable — leads will NOT be saved: {e}")

# ─── STOCK SYMBOLS ───────────────────────────────────────────
SYMBOLS = {
    "indices": [
        {"symbol": "^NSEI",    "label": "NIFTY 50",   "exchange": "INDEX"},
        {"symbol": "^BSESN",   "label": "SENSEX",     "exchange": "INDEX"},
        {"symbol": "^NSEBANK", "label": "BANK NIFTY", "exchange": "INDEX"},
        {"symbol": "^CNXIT",   "label": "NIFTY IT",   "exchange": "INDEX"},
    ],
    "india": [
        {"symbol": "RELIANCE.NS",   "label": "Reliance",      "exchange": "NSE"},
        {"symbol": "TCS.NS",        "label": "TCS",            "exchange": "NSE"},
        {"symbol": "INFY.NS",       "label": "Infosys",        "exchange": "NSE"},
        {"symbol": "HDFCBANK.NS",   "label": "HDFC Bank",      "exchange": "NSE"},
        {"symbol": "ICICIBANK.NS",  "label": "ICICI Bank",     "exchange": "NSE"},
        {"symbol": "SBIN.NS",       "label": "SBI",            "exchange": "NSE"},
        {"symbol": "WIPRO.NS",      "label": "Wipro",          "exchange": "NSE"},
        {"symbol": "LT.NS",         "label": "L&T",            "exchange": "NSE"},
        {"symbol": "AXISBANK.NS",   "label": "Axis Bank",      "exchange": "NSE"},
        {"symbol": "BAJFINANCE.NS", "label": "Bajaj Finance",  "exchange": "NSE"},
        {"symbol": "HINDUNILVR.NS", "label": "HUL",            "exchange": "NSE"},
        {"symbol": "KOTAKBANK.NS",  "label": "Kotak Bank",     "exchange": "NSE"},
        {"symbol": "BHARTIARTL.NS", "label": "Airtel",         "exchange": "NSE"},
        {"symbol": "MARUTI.NS",     "label": "Maruti Suzuki",  "exchange": "NSE"},
        {"symbol": "TATAMOTORS.NS", "label": "Tata Motors",    "exchange": "NSE"},
    ],
    "crypto": [
        {"symbol": "BTC-USD", "label": "Bitcoin",  "exchange": "CRYPTO"},
        {"symbol": "ETH-USD", "label": "Ethereum", "exchange": "CRYPTO"},
    ],
}

ALL_SYMBOLS = [s for group in SYMBOLS.values() for s in group]

# ─── STOCK CACHE ─────────────────────────────────────────────
stock_cache = {"data": [], "updated_at": None}
cache_lock  = threading.Lock()


def fetch_one(sym_info: dict) -> dict | None:
    """Fetch a single ticker and return a normalised dict."""
    symbol = sym_info["symbol"]
    try:
        ticker = yf.Ticker(symbol)
        fi     = ticker.fast_info

        price     = fi.last_price         or 0.0
        prev      = fi.previous_close     or price
        change    = round(price - prev, 2)
        change_pct = round((change / prev * 100) if prev else 0, 2)

        return {
            "symbol":        symbol,
            "label":         sym_info["label"],
            "exchange":      sym_info["exchange"],
            "price":         round(float(price), 2),
            "change":        change,
            "change_percent": change_pct,
            "prev_close":    round(float(prev), 2),
            "volume":        int(fi.three_month_average_volume or 0),
            "currency":      fi.currency or ("INR" if ".NS" in symbol else "USD"),
            "market_state":  "OPEN",   # fast_info doesn't expose this directly
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "stale":         False,
        }
    except Exception as e:
        log.warning(f"  ⚠  {symbol}: {str(e)[:80]}")
        # Return stale cached entry if available
        with cache_lock:
            old = next((x for x in stock_cache["data"] if x["symbol"] == symbol), None)
        if old:
            return {**old, "stale": True}
        return None


def refresh_stocks():
    """Refresh all symbols in parallel using yfinance batch download."""
    log.info("🔄  Refreshing stock data…")
    results = []

    # Batch download is faster; fall back to single if needed
    all_syms = [s["symbol"] for s in ALL_SYMBOLS]
    try:
        df = yf.download(
            tickers=" ".join(all_syms),
            period="2d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        for sym_info in ALL_SYMBOLS:
            sym = sym_info["symbol"]
            try:
                if len(all_syms) == 1:
                    row = df.iloc[-1]
                    prev_row = df.iloc[-2] if len(df) >= 2 else row
                else:
                    row = df[sym].iloc[-1]
                    prev_row = df[sym].iloc[-2] if len(df[sym]) >= 2 else row

                price    = float(row["Close"])
                prev     = float(prev_row["Close"])
                change   = round(price - prev, 2)
                chg_pct  = round((change / prev * 100) if prev else 0, 2)
                currency = "INR" if ".NS" in sym or sym.startswith("^") else "USD"
                if sym in ("BTC-USD", "ETH-USD"):
                    currency = "USD"

                results.append({
                    "symbol":         sym,
                    "label":          sym_info["label"],
                    "exchange":       sym_info["exchange"],
                    "price":          round(price, 2),
                    "change":         change,
                    "change_percent": chg_pct,
                    "prev_close":     round(prev, 2),
                    "volume":         int(row.get("Volume", 0) or 0),
                    "currency":       currency,
                    "market_state":   "OPEN",
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                    "stale":          False,
                })
            except Exception as e:
                log.warning(f"  ⚠  {sym}: {str(e)[:80]}")
                with cache_lock:
                    old = next((x for x in stock_cache["data"] if x["symbol"] == sym), None)
                if old:
                    results.append({**old, "stale": True})

    except Exception as e:
        log.error(f"Batch download failed: {e} — falling back to individual fetches")
        for sym_info in ALL_SYMBOLS:
            r = fetch_one(sym_info)
            if r:
                results.append(r)

    if results:
        with cache_lock:
            stock_cache["data"]       = results
            stock_cache["updated_at"] = datetime.now(timezone.utc).isoformat()
        log.info(f"  ✅  {len(results)} symbols cached")


def stock_refresh_loop():
    """Background thread — refresh every REFRESH_SECS seconds."""
    while True:
        try:
            refresh_stocks()
        except Exception as e:
            log.error(f"Stock refresh error: {e}")
        time.sleep(REFRESH_SECS)


# Warm up cache immediately on startup (fixes cold-start empty response)
log.info("⏳  Initial stock fetch (startup warm-up)…")
try:
    refresh_stocks()
except Exception as e:
    log.warning(f"Startup stock fetch failed: {e}")

# Start background refresh thread ONLY if not serverless
IS_VERCEL = os.environ.get("VERCEL") == "1"
if not IS_VERCEL:
    threading.Thread(target=stock_refresh_loop, daemon=True).start()
else:
    log.info("⚡ Running on Vercel (Serverless) — Background ticker loop disabled.")

# ─── EMAIL ───────────────────────────────────────────────────
def send_notification_email(subject: str, html: str):
    """Generic helper — sends an HTML email to NOTIFY_EMAIL."""
    if not EMAIL_USER or not EMAIL_PASS:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_USER
        msg["To"]      = NOTIFY_EMAIL or EMAIL_USER
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, NOTIFY_EMAIL or EMAIL_USER, msg.as_string())
    except Exception as e:
        log.warning(f"Email failed: {e}")


def send_lead_email(lead: dict):
    if not EMAIL_USER or not EMAIL_PASS:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🆕 New Lead — {lead.get('service','General')} — {lead['name']}"
        msg["From"]    = EMAIL_USER
        msg["To"]      = NOTIFY_EMAIL or EMAIL_USER

        html = f"""
        <div style="font-family:sans-serif;max-width:520px">
          <h2 style="color:#B8972A;border-bottom:2px solid #B8972A;padding-bottom:8px">
            New Consultation Request
          </h2>
          <table style="width:100%;border-collapse:collapse;font-size:14px">
            {''.join(f'<tr><td style="padding:8px 12px;background:#f9f6ee;font-weight:600;width:110px">{k}</td>'
                     f'<td style="padding:8px 12px;border:1px solid #eee">{v}</td></tr>'
                     for k, v in [
                         ("Name",    lead.get("name","—")),
                         ("Mobile",  lead.get("mobile","—")),
                         ("Email",   lead.get("email","—")),
                         ("Service", lead.get("service","—")),
                         ("Message", lead.get("message","—")),
                         ("Time",    datetime.now().strftime("%d %b %Y %I:%M %p")),
                     ])}
          </table>
          <p style="font-size:11px;color:#999;margin-top:12px">Smritikana Business Solutions — Auto Notification</p>
        </div>"""
        send_notification_email(
            subject=f"🆕 New Lead — {lead.get('service','General')} — {lead['name']}",
            html=html,
        )
    except Exception as e:
        log.warning(f"Email failed: {e}")


def send_cibil_email(record: dict):
    """Send notification email when a CIBIL enquiry is submitted."""
    if not EMAIL_USER or not EMAIL_PASS:
        return
    try:
        html = f"""
        <div style="font-family:sans-serif;max-width:520px">
          <h2 style="color:#B8972A;border-bottom:2px solid #B8972A;padding-bottom:8px">
            New CIBIL Eligibility Request
          </h2>
          <table style="width:100%;border-collapse:collapse;font-size:14px">
            {''.join(f'<tr><td style="padding:8px 12px;background:#f9f6ee;font-weight:600;width:110px">{k}</td>'
                     f'<td style="padding:8px 12px;border:1px solid #eee">{v}</td></tr>'
                     for k, v in [
                         ("Name",    record.get("name", "—")),
                         ("Mobile",  record.get("mobile", "—")),
                         ("Purpose", record.get("purpose", "—")),
                         ("Time",    datetime.now().strftime("%d %b %Y %I:%M %p")),
                     ])}
          </table>
          <p style="font-size:11px;color:#999;margin-top:12px">Smritikana Business Solutions — Auto Notification</p>
        </div>"""
        send_notification_email(
            subject=f"🔍 CIBIL Enquiry — {record['name']}",
            html=html,
        )
    except Exception as e:
        log.warning(f"CIBIL email failed: {e}")


# ─── RATE LIMITING (simple in-memory) ────────────────────────
_form_calls: dict[str, list] = {}
FORM_LIMIT      = 10
FORM_WINDOW_SEC = 900   # 15 minutes

def rate_limit_form(ip: str) -> bool:
    now   = time.time()
    calls = [t for t in _form_calls.get(ip, []) if now - t < FORM_WINDOW_SEC]
    _form_calls[ip] = calls
    if len(calls) >= FORM_LIMIT:
        return False
    _form_calls[ip].append(now)
    return True


# ─── INPUT VALIDATION ────────────────────────────────────────
import re as _re
_MOBILE_RE = _re.compile(r'^[6-9]\d{9}$')        # Indian mobile: starts 6-9, 10 digits
_EMAIL_RE  = _re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def is_valid_mobile(mobile: str) -> bool:
    """Accepts 10-digit Indian mobile numbers (optionally prefixed with +91 or 0)."""
    cleaned = _re.sub(r'^(\+91|91|0)', '', mobile.replace(' ', '').replace('-', ''))
    return bool(_MOBILE_RE.match(cleaned))

def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) if email else True  # email is optional


# ─── HELPERS ─────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("x-admin-secret") != ADMIN_SECRET:
            abort(401)
        return f(*args, **kwargs)
    return decorated


def obj_to_dict(doc: dict) -> dict:
    """Convert MongoDB document to JSON-serialisable dict."""
    doc["_id"] = str(doc["_id"])
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


# ─── API ROUTES ───────────────────────────────────────────────

@app.route("/api/health")
def health():
    mongo_ok = db is not None
    with cache_lock:
        stock_count  = len(stock_cache["data"])
        stock_updated = stock_cache["updated_at"]
    return jsonify({
        "status":  "ok",
        "mongo":   "connected" if mongo_ok else "unavailable",
        "stocks":  stock_count,
        "updated": stock_updated,
    })


@app.route("/api/stocks")
def get_all_stocks():
    with cache_lock:
        data = list(stock_cache["data"])
        updated = stock_cache["updated_at"]
    return jsonify({"success": True, "updated_at": updated, "data": data})


@app.route("/api/stocks/refresh")
def force_refresh():
    threading.Thread(target=refresh_stocks, daemon=True).start()
    return jsonify({"success": True, "message": "Refresh triggered"})


@app.route("/api/stocks/<category>")
def get_stocks_by_category(category: str):
    if category not in SYMBOLS:
        return jsonify({"error": f"Unknown category. Use: {', '.join(SYMBOLS)}"}), 400
    syms = {s["symbol"] for s in SYMBOLS[category]}
    with cache_lock:
        data    = [s for s in stock_cache["data"] if s["symbol"] in syms]
        updated = stock_cache["updated_at"]
    return jsonify({"success": True, "updated_at": updated, "data": data})


@app.route("/api/leads", methods=["POST"])
def create_lead():
    ip = request.remote_addr
    if not rate_limit_form(ip):
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    body = request.get_json(silent=True) or {}
    name    = (body.get("name")    or "").strip()[:120]
    mobile  = (body.get("mobile")  or "").strip()[:20]
    email   = (body.get("email")   or "").strip()[:120]
    service = (body.get("service") or "").strip()[:80]
    message = (body.get("message") or "").strip()[:1000]

    if not name or not mobile:
        return jsonify({"error": "Name and mobile number are required."}), 400
    if not is_valid_mobile(mobile):
        return jsonify({"error": "Please enter a valid 10-digit Indian mobile number."}), 400
    if email and not is_valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    lead = {
        "name": name, "mobile": mobile, "email": email,
        "service": service, "message": message,
        "status": "new", "ip": ip,
        "created_at": datetime.now(timezone.utc),
    }

    lead_id = None
    if db is not None:
        result  = db.leads.insert_one(lead)
        lead_id = str(result.inserted_id)

    # Fire-and-forget email notification (Sync if serverless, Thread if local/VM)
    if IS_VERCEL:
        send_lead_email(lead)
    else:
        threading.Thread(target=send_lead_email, args=(lead,), daemon=True).start()

    return jsonify({
        "success": True,
        "id":      lead_id,
        "message": "Thank you! We will contact you within 24 hours.",
    })


@app.route("/api/cibil", methods=["POST"])
def create_cibil():
    ip = request.remote_addr
    if not rate_limit_form(ip):
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    body = request.get_json(silent=True) or {}
    name    = (body.get("name")    or "").strip()[:120]
    mobile  = (body.get("mobile")  or "").strip()[:20]
    purpose = (body.get("purpose") or "").strip()[:80]

    if not name or not mobile:
        return jsonify({"error": "Name and mobile are required."}), 400
    if not is_valid_mobile(mobile):
        return jsonify({"error": "Please enter a valid 10-digit Indian mobile number."}), 400

    record = {
        "name": name, "mobile": mobile, "purpose": purpose,
        "status": "pending", "ip": ip,
        "created_at": datetime.now(timezone.utc),
    }
    if db is not None:
        db.cibil_enquiries.insert_one(record)

    # Fire-and-forget email notification (Sync if serverless, Thread if local/VM)
    if IS_VERCEL:
        send_cibil_email(record)
    else:
        threading.Thread(target=send_cibil_email, args=(record,), daemon=True).start()

    return jsonify({
        "success": True,
        "message": "Received! Our team will call you within 24 hours.",
    })


@app.route("/api/admin/leads")
@admin_required
def admin_leads():
    if db is None:
        return jsonify({"error": "MongoDB unavailable"}), 503
    page    = int(request.args.get("page",  1))
    limit   = int(request.args.get("limit", 50))
    status  = request.args.get("status")
    filt    = {"status": status} if status else {}
    total   = db.leads.count_documents(filt)
    docs    = list(db.leads.find(filt).sort("created_at", -1)
                   .skip((page - 1) * limit).limit(limit))
    return jsonify({
        "success": True, "total": total, "page": page,
        "data": [obj_to_dict(d) for d in docs],
    })


@app.route("/api/admin/leads/<lead_id>", methods=["PATCH"])
@admin_required
def update_lead(lead_id: str):
    if db is None:
        return jsonify({"error": "MongoDB unavailable"}), 503
    body   = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("new", "contacted", "converted", "closed"):
        return jsonify({"error": "Invalid status"}), 400
    db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"status": status}})
    return jsonify({"success": True})


# ─── ADMIN DASHBOARD ───────────────────────────────────────────

_ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Admin Login - Smritikana</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; background: #111; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .box { background: #1a1a1a; padding: 40px; border-radius: 10px; border: 1px solid #333; width: 100%; max-width: 320px; text-align: center; }
        input { wbox-sizing: border-box; width: 100%; padding: 12px; margin: 10px 0 20px; background: #222; border: 1px solid #444; color: #fff; border-radius: 5px; }
        button { width: 100%; padding: 12px; background: #B8972A; color: #000; border: none; font-weight: bold; border-radius: 5px; cursor: pointer; }
        button:hover { background: #d4b036; }
        form { margin-top: 20px; }
    </style>
</head>
<body>
    <div class="box">
        <h2 style="color:#B8972A;margin-top:0;">Admin Access</h2>
        <form method="POST">
            <input type="password" name="secret" placeholder="Enter Admin Secret" required autofocus>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
"""

_ADMIN_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard - Smritikana</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; background: #f4f6f8; color: #333; margin: 0; padding: 0; }
        header { background: #111; color: #B8972A; padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; }
        header a { color: #fff; text-decoration: none; font-size: 14px; padding: 8px 12px; border: 1px solid #333; border-radius: 5px; margin-left:10px;}
        header a:hover { background: #222; }
        .container { max-width: 1200px; margin: 40px auto; padding: 0 20px; }
        .card { background: #fff; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); margin-bottom: 30px; overflow-x: auto; }
        h2 { border-bottom: 2px solid #eee; padding-bottom: 15px; margin-top: 0; }
        table { width: 100%; border-collapse: collapse; min-width: 700px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f9f9f9; font-weight: 600; color: #555; }
        tr:hover { background: #fdfdfd; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 20px; font-size: 11px; font-weight: bold; background: #eef2ff; color: #4f46e5; text-transform: uppercase; }
        .badge.pending { background: #fef3c7; color: #d97706; }
        .badge.contacted { background: #d1fae5; color: #059669; }
        .empty { text-align: center; padding: 40px; color: #999; font-style: italic; }
        select { padding: 6px; border: 1px solid #ddd; border-radius: 4px; font-size: 12px; }
    </style>
</head>
<body>
    <header>
        <h3 style="margin:0;">⚙️ Smritikana Admin</h3>
        <div>
            <a href="/">View Site</a>
            <a href="/admin/logout">Logout</a>
        </div>
    </header>
    <div class="container">
        
        <div class="card">
            <h2>📢 Recent Consultation Leads</h2>
            {% if leads %}
            <table>
                <tr>
                    <th>Date</th>
                    <th>Name</th>
                    <th>Mobile</th>
                    <th>Service Needed</th>
                    <th>Message</th>
                    <th>Status</th>
                </tr>
                {% for l in leads %}
                <tr>
                    <td style="white-space:nowrap;font-size:13px;color:#666;">{{ l.created_at.strftime('%d %b, %H:%M') if l.created_at else '—' }}</td>
                    <td style="font-weight:bold;">{{ l.name }}</td>
                    <td><a href="tel:{{ l.mobile }}">{{ l.mobile }}</a></td>
                    <td><span class="badge">{{ l.service or 'General' }}</span></td>
                    <td style="font-size:13px;max-width:300px;">{{ l.message or '—' }}</td>
                    <td>
                        <select onchange="updateStatus('leads', '{{ l._id }}', this.value)">
                            <option value="new" {% if l.status == 'new' %}selected{% endif %}>New</option>
                            <option value="contacted" {% if l.status == 'contacted' %}selected{% endif %}>Contacted</option>
                        </select>
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <div class="empty">No leads found yet.</div>
            {% endif %}
        </div>

        <div class="card">
            <h2>🔍 CIBIL Eligibility Enquiries</h2>
            {% if cibil %}
            <table>
                <tr>
                    <th>Date</th>
                    <th>Name</th>
                    <th>Mobile</th>
                    <th>Purpose</th>
                    <th>Status</th>
                </tr>
                {% for c in cibil %}
                <tr>
                    <td style="white-space:nowrap;font-size:13px;color:#666;">{{ c.created_at.strftime('%d %b, %H:%M') if c.created_at else '—' }}</td>
                    <td style="font-weight:bold;">{{ c.name }}</td>
                    <td><a href="tel:{{ c.mobile }}">{{ c.mobile }}</a></td>
                    <td>{{ c.purpose or '—' }}</td>
                    <td>
                        <select onchange="updateStatus('cibil', '{{ c._id }}', this.value)">
                            <option value="pending" {% if c.status == 'pending' %}selected{% endif %}>Pending</option>
                            <option value="contacted" {% if c.status == 'contacted' %}selected{% endif %}>Contacted</option>
                        </select>
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <div class="empty">No CIBIL enquiries found yet.</div>
            {% endif %}
        </div>

    </div>

    <script>
        async function updateStatus(collection, id, status) {
            try {
                const res = await fetch(`/api/admin/status`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ collection, id, status })
                });
                if (!res.ok) alert('Failed to update status');
                else location.reload();
            } catch (err) {
                alert('Network error');
            }
        }
    </script>
</body>
</html>
"""

from jinja2 import Template

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect("/admin/dashboard")
        
    if request.method == "POST":
        secret = request.form.get("secret", "")
        if ADMIN_SECRET and secret == ADMIN_SECRET:
            session["admin"] = True
            session.permanent = True
            return redirect("/admin/dashboard")
        return "Invalid Secret", 401
        
    return _ADMIN_LOGIN_HTML


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    if db is None:
        return "Database not connected. Please see environment config.", 503
        
    # Fetch latest 50 leads
    leads = list(db.leads.find().sort("created_at", -1).limit(50))
    # Fetch latest 50 CIBIL enquiries
    cibil = list(db.cibil_enquiries.find().sort("created_at", -1).limit(50))
    
    # Render using Jinja2 Template directly from string
    tmpl = Template(_ADMIN_DASHBOARD_HTML)
    return tmpl.render(leads=leads, cibil=cibil)


@app.route("/api/admin/status", methods=["PATCH"])
@admin_required
def admin_update_status():
    if db is None:
        return jsonify({"error": "No DB"}), 503
        
    body = request.get_json(silent=True) or {}
    collection = body.get("collection")
    record_id = body.get("id")
    status = body.get("status")
    
    if collection not in ["leads", "cibil"]:
        return jsonify({"error": "Invalid collection"}), 400
        
    from bson.objectid import ObjectId
    try:
        oid = ObjectId(record_id)
    except:
        return jsonify({"error": "Invalid ID"}), 400
        
    if collection == "leads":
        db.leads.update_one({"_id": oid}, {"$set": {"status": status}})
    else: # (These are no longer needed as we have a full UI dashboard now)
        db.cibil_enquiries.update_one({"_id": oid}, {"$set": {"status": status}})
        
    return jsonify({"success": True})


# ─── FRONTEND FALLBACK ────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    static_dir = app.static_folder
    full = os.path.join(static_dir, path)
    if path and os.path.exists(full):
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, "index.html")


# ─── MAIN ────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"\n🚀  Smritikana Flask Backend → http://0.0.0.0:{PORT}")
    log.info("     GET  /api/stocks")
    log.info("     POST /api/leads")
    log.info("     POST /api/cibil")
    log.info("     GET  /api/health\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
