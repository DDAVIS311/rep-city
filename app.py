"""
Flask server for Rep City.
State storage: Vercel KV (Upstash Redis) in production, JSON file locally.
User identity: UUID stored in a browser cookie (rc_user).
"""

import json
import os
import re
import uuid
import secrets
import unicodedata
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

    # Fallback: Vercel lets you set a custom env-var prefix when connecting a
    # database (e.g. STORAGE_KV_REST_API_URL). Auto-discover the REST
    # credentials under any prefix so a stray/renamed prefix can't silently
    # break persistence.
    if not (url and token):
        for name, value in os.environ.items():
            if not str(value).startswith("https://"):
                continue
            if name.endswith("REST_API_URL"):
                tok_name = name[: -len("REST_API_URL")] + "REST_API_TOKEN"
            elif name.endswith("REST_URL"):
                tok_name = name[: -len("REST_URL")] + "REST_TOKEN"
            else:
                continue
            tok = os.environ.get(tok_name)
            if tok:
                url, token = value, tok
                break

    if not url or not token:
        return None
    from upstash_redis import Redis
    return Redis(url=url, token=token)


def _kv_for_write():
    """Return the KV client for a write, or raise a clear error.

    On Vercel the filesystem is read-only, so the local-file fallback cannot
    persist. If KV isn't configured there, fail loudly instead of throwing an
    opaque OSError (which surfaced to users as a generic 500 and a schedule
    that silently reset on refresh)."""
    kv = _get_kv()
    if kv is None and os.environ.get("VERCEL"):
        raise RuntimeError(
            "State store not configured. Set UPSTASH_REDIS_REST_URL and "
            "UPSTASH_REDIS_REST_TOKEN (or KV_REST_API_URL / KV_REST_API_TOKEN) "
            "in the Vercel project, then redeploy."
        )
    return kv


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
    kv = _kv_for_write()
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
    kv = _kv_for_write()
    if kv:
        key = _kv_key(user_id)
        to_set = {u["id"]: u["status"] for u in updates if u.get("status") != "available"}
        to_del = [u["id"] for u in updates if u.get("status") == "available"]
        if to_set:
            kv.hset(key, values=to_set)
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
    kv = _kv_for_write()
    if kv:
        kv.delete(_kv_key(user_id))
        return
    with open(STATE_FILE, "w") as f:
        json.dump({}, f)


# ── Device sync (anonymous pairing codes) ──────────────────────────────────────
# A device mints a short, single-use code that maps to its rc_user id in the KV
# store with a short TTL. Another device redeems the code to adopt that id, so
# both devices then share the same state:{id} hash. No accounts / email.

# Unambiguous alphabet (no 0/O/1/I/L) so codes are easy to read and type.
_SYNC_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_SYNC_TTL = 900          # 15 minutes
_SYNC_CODE_LEN = 6


def _sync_key(code):
    return f"sync:{code.upper()}"


def _gen_sync_code():
    return "".join(secrets.choice(_SYNC_ALPHABET) for _ in range(_SYNC_CODE_LEN))


def force_user_cookie(response, user_id):
    """Set (overwriting) the rc_user cookie — used when a device adopts another
    device's id during sync."""
    response.set_cookie(
        "rc_user", user_id,
        max_age=60 * 60 * 24 * 365 * 5,
        samesite="Lax",
        httponly=True,
        secure=os.environ.get("VERCEL") is not None,
    )
    return response


# ── Aggregate demand signal (privacy-preserving; no per-user linkage) ──────────
# Each want/skip bumps a Redis sorted set keyed by a NORMALIZED film title, so the
# same film aggregates across venues and formats (e.g. "THE ODYSSEY in 70mm" and
# "The Odyssey (15) 35mm" both count toward "the odyssey"). This is a count of
# want/skip *actions* (intent events), not unique users — fine for relative demand.

_DEMAND_FMT_RE = re.compile(
    r"\b(?:(?:in|on)\s+)?(?:70mm|35mm|16mm|4k\s*dcp|2k\s*dcp|dcp|imax|4k|digital|nitrate|vhs|dvd)\b",
    re.I,
)


def _title_from_id(sid):
    # screening_id == "venue_short|title|date|time"; title may itself contain "|".
    try:
        return sid.split("|", 1)[1].rsplit("|", 2)[0]
    except Exception:
        return ""


def _normalize_title(raw):
    t = unicodedata.normalize("NFKC", raw or "")
    if ": " in t:
        t = t.split(": ")[-1]                          # drop strand/series prefix
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t)         # drop (15), [35mm], (4K Restoration)
    t = _DEMAND_FMT_RE.sub(" ", t)                      # drop format tokens / "in 70mm"
    t = re.sub(r"\b(?:UK|US|EUROPEAN|WORLD|LONDON|INTERNATIONAL)\s+PREMIERE\b", " ", t, re.I)
    t = re.sub(r"\bpremiere\b|\bpreview\b", " ", t, re.I)
    t = re.sub(r"[^0-9A-Za-z&'’.\- ]+", " ", t)         # strip punctuation noise
    t = re.sub(r"\s+", " ", t).strip()
    return t.lower()


def _bump_demand(status, sid):
    """Increment the aggregate demand tally. Never raises — a demand-store hiccup
    must not break the user's want/skip action."""
    try:
        kv = _get_kv()
        if not kv:
            return
        title = _normalize_title(_title_from_id(sid))
        if not title:
            return
        kv.zincrby("demand:want" if status == "want" else "demand:skip", 1, title)
    except Exception:
        pass


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
    # Base screening list — identical for every visitor. Deliberately sets NO
    # cookie and touches NO KV, so the response is cacheable at Vercel's edge and
    # a traffic spike is served from the CDN instead of this function. The data
    # only changes on deploy (each scrape commits + deploys, which purges the edge
    # cache). Per-user want/skip is fetched separately from GET /api/state and
    # merged in the browser.
    screenings = load_screenings()
    result = []
    for s in screenings:
        s_copy = dict(s)
        s_copy["id"] = screening_id(s)
        s_copy["status"] = "available"
        result.append(s_copy)
    resp = make_response(jsonify(result))
    resp.headers["Cache-Control"] = (
        "public, max-age=600, s-maxage=21600, stale-while-revalidate=86400"
    )
    return resp


@app.route("/api/state", methods=["GET"])
def get_state():
    # Brand-new visitors have no cookie and therefore no saved state — return an
    # empty map WITHOUT touching KV. This is what keeps anonymous browsing (the
    # bulk of a launch spike) off the Redis command budget entirely.
    if not request.cookies.get("rc_user"):
        resp = make_response(jsonify({}))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    try:
        state = load_state(get_user_id())
    except Exception:
        state = {}          # state store down/over-quota -> degrade, don't 500
    resp = make_response(jsonify(state))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/api/state", methods=["POST"])
def update_state():
    user_id = get_user_id()
    data = request.json
    sid = data.get("id")
    status = data.get("status")
    if not sid or status not in ("available", "want", "dismissed"):
        return jsonify({"error": "invalid"}), 400
    try:
        set_state_item(user_id, sid, status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if status in ("want", "dismissed"):
        _bump_demand(status, sid)
    resp = make_response(jsonify({"ok": True}))
    attach_user_cookie(resp, user_id)
    return resp


@app.route("/api/state/bulk", methods=["POST"])
def bulk_update_state():
    user_id = get_user_id()
    updates = request.json
    valid = [u for u in updates if u.get("id") and u.get("status") in ("available", "want", "dismissed")]
    try:
        bulk_set_state(user_id, valid)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
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
    try:
        reset_state(user_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    resp = make_response(jsonify({"ok": True}))
    attach_user_cookie(resp, user_id)
    return resp


@app.route("/api/sync/create", methods=["POST"])
def sync_create():
    """Mint a short single-use code that points at this device's rc_user id."""
    kv = _get_kv()
    if kv is None:
        return jsonify({"error": "Sync isn't available on this instance."}), 503
    user_id = get_user_id()
    # Retry a few times in the (vanishingly unlikely) event of a code collision.
    for _ in range(5):
        code = _gen_sync_code()
        key = _sync_key(code)
        if not kv.get(key):
            kv.set(key, user_id, ex=_SYNC_TTL)
            break
    else:
        return jsonify({"error": "Could not allocate a code, try again."}), 500
    resp = make_response(jsonify({"code": code, "expires_in": _SYNC_TTL}))
    attach_user_cookie(resp, user_id)   # ensure the minting device has a stable id
    return resp


@app.route("/api/sync/claim", methods=["POST"])
def sync_claim():
    """Redeem a code: adopt the source device's id, merging this device's list in."""
    kv = _get_kv()
    if kv is None:
        return jsonify({"error": "Sync isn't available on this instance."}), 503
    code = (request.json or {}).get("code", "").strip().upper()
    if not code:
        return jsonify({"error": "Enter a sync code."}), 400
    src_id = kv.get(_sync_key(code))
    if not src_id:
        return jsonify({"error": "That code is invalid or has expired."}), 404

    cur_id = get_user_id()
    merged = 0
    if cur_id != src_id:
        # Fold this device's existing wants/skips into the shared list (source
        # wins on conflict), then retire this device's now-orphaned hash.
        cur_state = load_state(cur_id)
        if cur_state:
            src_state = load_state(src_id)
            adds = [{"id": sid, "status": st} for sid, st in cur_state.items()
                    if sid not in src_state]
            if adds:
                bulk_set_state(src_id, adds)
                merged = len(adds)
            reset_state(cur_id)
    kv.delete(_sync_key(code))   # single-use

    count = len(load_state(src_id))
    resp = make_response(jsonify({"ok": True, "merged": merged, "count": count}))
    force_user_cookie(resp, src_id)
    return resp


def _demand_top(kv, key, n=150):
    """Return [(title, count), ...] highest-first from a demand sorted set,
    tolerating either flat [m, s, m, s] or [(m, s), ...] return shapes."""
    raw = kv.zrange(key, 0, n - 1, rev=True, withscores=True) or []
    pairs = []
    if raw and isinstance(raw[0], (list, tuple)):
        pairs = [(m, s) for m, s in raw]
    else:
        it = iter(raw)
        pairs = list(zip(it, it))
    out = []
    for m, s in pairs:
        try:
            out.append((str(m), int(float(s))))
        except (TypeError, ValueError):
            out.append((str(m), 0))
    return out


@app.route("/demand")
def demand_view():
    """Aggregate most-wanted / most-skipped films. Gated by a DEMAND_KEY env var
    so it isn't public; visit /demand?key=YOUR_KEY."""
    from markupsafe import escape
    secret = os.environ.get("DEMAND_KEY")
    if not secret:
        return ("Set a DEMAND_KEY environment variable in the Vercel project, "
                "then open /demand?key=YOUR_KEY", 503)
    if request.args.get("key") != secret:
        return ("Not found", 404)
    kv = _get_kv()
    if kv is None:
        return ("State store not configured.", 503)
    want = _demand_top(kv, "demand:want")
    skip = _demand_top(kv, "demand:skip")

    def table(title, rows):
        body = "".join(
            f"<tr><td class='n'>{i+1}</td><td>{escape(t.title())}</td>"
            f"<td class='c'>{c}</td></tr>"
            for i, (t, c) in enumerate(rows)
        ) or "<tr><td colspan='3' class='empty'>No data yet.</td></tr>"
        return (f"<section><h2>{title}</h2><table>"
                f"<tr><th>#</th><th>Film</th><th>Count</th></tr>{body}</table></section>")

    html = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Rep City — Demand</title><style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#0e0e10; color:#e8e8e8;
         margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#8a8a90; font-size:13px; margin-bottom:24px; }}
  .cols {{ display:flex; gap:32px; flex-wrap:wrap; align-items:flex-start; }}
  section {{ flex:1; min-width:280px; }}
  h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:#c8a04a; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #232327; }}
  th {{ color:#8a8a90; font-weight:600; font-size:12px; }}
  td.n {{ color:#6a6a70; width:28px; }}
  td.c {{ text-align:right; font-variant-numeric:tabular-nums; color:#c8a04a; font-weight:600; }}
  .empty {{ color:#6a6a70; }}
</style></head><body>
<h1>Rep City — Demand signal</h1>
<div class=sub>Aggregate want / skip actions by film (normalized across venues &amp; formats).</div>
<div class=cols>{table('Most wanted', want)}{table('Most skipped', skip)}</div>
</body></html>"""
    return html


if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        print("No data found. Running initial scrape...")
        from scraper import scrape_all
        save_screenings(scrape_all())
    app.run(debug=True, port=5050)
