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
from pymongo.errors import ConnectionFailure
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="public", static_url_path="")
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
except ConnectionFailure as e:
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


# Start background refresh thread
threading.Thread(target=stock_refresh_loop, daemon=True).start()

# ─── EMAIL ───────────────────────────────────────────────────
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

        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, NOTIFY_EMAIL or EMAIL_USER, msg.as_string())
    except Exception as e:
        log.warning(f"Email failed: {e}")


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
    name    = (body.get("name")    or "").strip()
    mobile  = (body.get("mobile")  or "").strip()
    email   = (body.get("email")   or "").strip()
    service = (body.get("service") or "").strip()
    message = (body.get("message") or "").strip()

    if not name or not mobile:
        return jsonify({"error": "Name and mobile number are required."}), 400

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

    # Fire-and-forget email notification
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
    name    = (body.get("name")    or "").strip()
    mobile  = (body.get("mobile")  or "").strip()
    purpose = (body.get("purpose") or "").strip()

    if not name or not mobile:
        return jsonify({"error": "Name and mobile are required."}), 400

    record = {
        "name": name, "mobile": mobile, "purpose": purpose,
        "status": "pending", "ip": ip,
        "created_at": datetime.now(timezone.utc),
    }
    if db is not None:
        db.cibil_enquiries.insert_one(record)

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


@app.route("/api/admin/cibil")
@admin_required
def admin_cibil():
    if db is None:
        return jsonify({"error": "MongoDB unavailable"}), 503
    docs = list(db.cibil_enquiries.find().sort("created_at", -1).limit(100))
    return jsonify({"success": True, "count": len(docs), "data": [obj_to_dict(d) for d in docs]})


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
