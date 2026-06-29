"""
Flask server for Rep City.
State storage: Vercel KV (Upstash Redis) in production, JSON file locally.
User identity: UUID stored in a browser cookie (rc_user).
"""

import json
import os
import uuid
from flask import Flask, jsonify, request, send_from_directory, make_response

app = Flask(__name__, static_folder="static", template_folder="templates")

DATA_FILE = os.path.join(os.path.dirname(__file__), "screenings.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "screening_state.json")

# ── KV client (Vercel/Upstash) — only loaded when env vars are present ────────

def _get_kv():
    # Upstash marketplace integration uses UPSTASH_REDIS_REST_URL / _TOKEN
    # Legacy Vercel KV used KV_REST_API_URL / _TOKEN — check both
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if not url or not token:
        return None
    from upstash_redis import Redis
    return Redis(url=url, token=token)


# ── Screenings (always from the committed JSON file) ──────────────────────────

def load_screenings():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def save_screenings(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Per-user state ─────────────────────────────────────────────────────────────

def _kv_key(user_id):
    return f"state:{user_id}"


def load_state(user_id):
    kv = _get_kv()
    if kv:
        raw = kv.hgetall(_kv_key(user_id))
        return raw if raw else {}
    # Local fallback: single shared file
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def set_state_item(user_id, sid, status):
    kv = _get_kv()
    if kv:
        if status == "available":
            kv.hdel(_kv_key(user_id), sid)
        else:
            kv.hset(_kv_key(user_id), sid, status)
        return
    # Local fallback
    state = load_state(user_id)
    if status == "available":
        state.pop(sid, None)
    else:
        state[sid] = status
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def bulk_set_state(user_id, updates):
    kv = _get_kv()
    if kv:
        key = _kv_key(user_id)
        to_set = {u["id"]: u["status"] for u in updates if u.get("status") != "available"}
        to_del = [u["id"] for u in updates if u.get("status") == "available"]
        if to_set:
            kv.hset(key, **to_set)
        if to_del:
            kv.hdel(key, *to_del)
        return
    # Local fallback
    state = load_state(user_id)
    for item in updates:
        sid, status = item.get("id"), item.get("status")
        if not sid:
            continue
        if status == "available":
            state.pop(sid, None)
        else:
            state[sid] = status
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def reset_state(user_id):
    kv = _get_kv()
    if kv:
        kv.delete(_kv_key(user_id))
        return
    with open(STATE_FILE, "w") as f:
        json.dump({}, f)


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def get_user_id():
    uid = request.cookies.get("rc_user")
    if not uid:
        uid = str(uuid.uuid4())
    return uid


def attach_user_cookie(response, user_id):
    if not request.cookies.get("rc_user"):
        response.set_cookie(
            "rc_user", user_id,
            max_age=60 * 60 * 24 * 365 * 5,  # 5 years
            samesite="Lax",
            httponly=True,
            secure=os.environ.get("VERCEL") is not None,
        )
    return response


# ── Screening ID ───────────────────────────────────────────────────────────────

def screening_id(s):
    return f"{s['venue_short']}|{s['title']}|{s['date']}|{s['time']}"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/screenings")
def get_screenings():
    user_id = get_user_id()
    screenings = load_screenings()
    state = load_state(user_id)
    result = []
    for s in screenings:
        sid = screening_id(s)
        s_copy = dict(s)
        s_copy["id"] = sid
        s_copy["status"] = state.get(sid, "available")
        result.append(s_copy)
    resp = make_response(jsonify(result))
    attach_user_cookie(resp, user_id)
    return resp


@app.route("/api/state", methods=["POST"])
def update_state():
    user_id = get_user_id()
    data = request.json
    sid = data.get("id")
    status = data.get("status")
    if not sid or status not in ("available", "want", "dismissed"):
        return jsonify({"error": "invalid"}), 400
    set_state_item(user_id, sid, status)
    resp = make_response(jsonify({"ok": True}))
    attach_user_cookie(resp, user_id)
    return resp


@app.route("/api/state/bulk", methods=["POST"])
def bulk_update_state():
    user_id = get_user_id()
    updates = request.json
    valid = [u for u in updates if u.get("id") and u.get("status") in ("available", "want", "dismissed")]
    bulk_set_state(user_id, valid)
    resp = make_response(jsonify({"ok": True}))
    attach_user_cookie(resp, user_id)
    return resp


@app.route("/api/refresh", methods=["POST"])
def refresh():
    if os.environ.get("VERCEL"):
        return jsonify({"error": "Refresh not available in hosted mode. Re-scrape locally and redeploy."}), 503
    try:
        from scraper import scrape_all
        data = scrape_all()
        save_screenings(data)
        return jsonify({"ok": True, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset_state", methods=["POST"])
def reset_state_route():
    user_id = get_user_id()
    reset_state(user_id)
    resp = make_response(jsonify({"ok": True}))
    attach_user_cookie(resp, user_id)
    return resp


if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        print("No data found. Running initial scrape...")
        from scraper import scrape_all
        save_screenings(scrape_all())
    app.run(debug=True, port=5050)
