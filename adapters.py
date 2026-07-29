"""
Cross-market platform adapters and shared extractors.

These are reusable across cities/venues (not tied to LA or NYC) so that adding a
new market only requires wiring venues to the right adapter rather than writing a
bespoke scraper each time. Output schema matches scraper.py / scraper_nyc.py:
    {title, date, time, format, venue, venue_short, url, city,
     description, runtime, director}

Adapters here:
  - extract_jsonld_screenings(): generic schema.org ScreeningEvent extractor.
    Many bespoke cinema sites (and MoMA) embed <script type="application/ld+json">
    ScreeningEvent objects — one shared parser handles them all regardless of the
    underlying CMS/ticketing platform.
  - scrape_spektrix(): Spektrix ticketing API adapter (UK arthouses). See below.
"""

import re
import json
import html as _html
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _normalize_format(text):
    """Map free text to a canonical presentation format label ('' if none)."""
    t = (text or "").lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "nitrate" in t: return "Nitrate"
    if "4k dcp" in t or "4k restoration" in t: return "4K DCP"
    if "2k dcp" in t: return "2K DCP"
    if "dcp" in t: return "DCP"
    if "digital" in t: return "Digital"
    if "vhs" in t: return "VHS"
    if "dvd" in t: return "DVD"
    return ""


def _strip_html(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _fmt_time(dt):
    """3:00 PM style (no leading zero, platform-independent)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _iso8601_duration_to_min(dur):
    """'PT2H5M' -> '125 min' ('' if unparseable)."""
    if not dur or not isinstance(dur, str):
        return ""
    m = re.match(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", dur)
    if not m or not (m.group(1) or m.group(2)):
        return ""
    total = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return f"{total} min" if total else ""


def _parse_dt(value, tz):
    """Parse an ISO-8601 datetime string into the given tz. Returns aware dt or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # Fall back to date-only
        try:
            dt = datetime.fromisoformat(v[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _walk_jsonld(obj, out):
    """Yield every dict node from a JSON-LD payload (object, list, or @graph)."""
    if isinstance(obj, list):
        for x in obj:
            _walk_jsonld(x, out)
    elif isinstance(obj, dict):
        if "@graph" in obj and isinstance(obj["@graph"], list):
            _walk_jsonld(obj["@graph"], out)
        out.append(obj)
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _walk_jsonld(v, out)


def _type_matches(node, wanted):
    t = node.get("@type")
    if isinstance(t, list):
        return any(x in wanted for x in t)
    return t in wanted


def _directors_from_work(work):
    names = []
    for key in ("director", "author"):
        d = work.get(key)
        if isinstance(d, dict):
            d = [d]
        for person in (d or []):
            if isinstance(person, dict) and person.get("name"):
                names.append(person["name"])
            elif isinstance(person, str):
                names.append(person)
    return ", ".join(dict.fromkeys(names))  # dedupe, keep order


def extract_jsonld_screenings(html, venue, venue_short, city,
                              tz="America/New_York", default_url="",
                              title_hint=None):
    """Extract schema.org ScreeningEvent occurrences from a page's JSON-LD.

    Works for any site that embeds <script type="application/ld+json"> ScreeningEvent
    (or Event) objects. `tz` is the IANA zone the venue's local showtimes should be
    expressed in. Returns a list of screening dicts in the standard schema; empty
    list if the page has no usable JSON-LD.
    """
    zone = ZoneInfo(tz)
    nodes = []
    for blk in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                          html, re.S | re.I):
        blk = blk.strip()
        if not blk:
            continue
        try:
            payload = json.loads(blk)
        except Exception:
            # Some sites emit multiple concatenated objects or trailing commas;
            # try a lenient recovery on the largest {...} span.
            try:
                payload = json.loads(blk[blk.index("{"):blk.rindex("}") + 1])
            except Exception:
                continue
        _walk_jsonld(payload, nodes)

    screenings = []
    seen = set()
    for node in nodes:
        if not _type_matches(node, {"ScreeningEvent", "Event"}):
            continue
        start = _parse_dt(node.get("startDate"), zone)
        if not start:
            continue

        work = node.get("workPresented")
        if isinstance(work, list):
            work = work[0] if work else {}
        if not isinstance(work, dict):
            work = {}

        title = (node.get("name") or work.get("name") or title_hint or "").strip()
        title = _strip_html(title)
        if not title:
            continue

        desc = _strip_html(node.get("description") or work.get("description") or "")
        runtime = (_iso8601_duration_to_min(work.get("duration") or node.get("duration"))
                   or (lambda m: f"{m.group(1)} min" if m else "")(
                       re.search(r"(\d+)\s*min", desc, re.I)))
        director = _directors_from_work(work) or _directors_from_work(node)

        fmt = _normalize_format(desc) or _normalize_format(node.get("name", ""))

        url = default_url
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict) and offers.get("url"):
            url = offers["url"]
        elif node.get("url"):
            url = node["url"]

        key = (title.lower(), start.date().isoformat(), start.strftime("%H:%M"))
        if key in seen:
            continue
        seen.add(key)

        screenings.append({
            "title": title,
            "date": start.strftime("%Y-%m-%d"),
            "time": _fmt_time(start),
            "format": fmt,
            "venue": venue,
            "venue_short": venue_short,
            "url": url,
            "city": city,
            "description": desc,
            "runtime": runtime,
            "director": director,
        })
    return screenings


# ── Spektrix adapter ─────────────────────────────────────────────────────────
# Spektrix is a hosted box-office platform used by many UK arts venues. Its public
# JSON API needs no auth and is not Cloudflare-gated:
#     https://system.spektrix.com/{client}/api/v3/{events,instances}
# Showtimes + titles are reliable (and format when the programmer puts it in the
# title). Director/synopsis are generally NOT exposed via the API for future film
# screenings — they live on each venue's public website — and runtime is only
# populated by some clients (ICA yes, Barbican no). Metadata is therefore
# best-effort; enrich from the venue website later if richer detail is wanted.

_SPEKTRIX_BASE = "https://system.spektrix.com/{client}/api/v3/"
_SPEKTRIX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
}
_LONDON = ZoneInfo("Europe/London")

# Spektrix exposes each client's custom fields as attribute_* keys, so the "is this
# a film?" flag is client-specific. Any matching predicate => film. Inspect a new
# client's /events attribute keys once and add a rule; PrimaryArtForm and
# Category/TAAArtform are the two most common film attributes in the wild.
_FILM_RULES = {
    "ica": [("attribute_Category", {"films"}), ("attribute_TAAArtform", {"film"})],
    "barbicancentre": [("attribute_PrimaryArtForm", {"cinema"})],
}
_GENERIC_FILM_KEYS = ("attribute_PrimaryArtForm", "attribute_Category",
                      "attribute_TAAArtform", "attribute_EventType", "attribute_Genre")
_GENERIC_FILM_VALUES = {"film", "films", "cinema", "screening"}

_SPX_FMT_RE = re.compile(r"\b(70mm|35mm|16mm|DCP|IMAX|4K)\b", re.I)
_SPX_DIR_RE = re.compile(r"(?:Dir(?:ected by|ector)?\.?\s*[:\-]?\s+)"
                         r"([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,3})")


def _spx_clean(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _spx_is_film(event, client):
    rules = _FILM_RULES.get(client)
    if rules:
        matched = any(str(event.get(k) or "").strip().lower() in acc for k, acc in rules)
        if not matched:
            return False
        # Barbican tags cinema-bar food/drink and some workshops/talks as Cinema too.
        # Keep only real screenings.
        if client == "barbicancentre":
            if event.get("attribute_BasketAddOn") is True:
                return False
            et = str(event.get("attribute_EventType") or "").lower()
            if not ("screening" in et or "film" in et):
                return False
        return True
    for key in _GENERIC_FILM_KEYS:
        if str(event.get(key) or "").strip().lower() in _GENERIC_FILM_VALUES:
            return True
    return False


def _spx_format(*texts):
    for t in texts:
        m = _SPX_FMT_RE.search(t or "")
        if m:
            up = m.group(1).upper()
            return up if up in ("DCP", "IMAX", "4K") else m.group(1).lower()
    return ""


def _spx_runtime(event):
    dur = event.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return f"{int(dur)} min"
    m = re.search(r"\d+", str(event.get("attribute_Duration") or "").strip())
    return f"{m.group(0)} min" if m else ""


def _spx_director(event, description):
    for key in ("attribute_ConductorDirectorOrChoreographer",
                "attribute_Director", "attribute_CreatorComposerOrPlaywright"):
        v = str(event.get(key) or "").strip()
        if v:
            return v
    m = _SPX_DIR_RE.search(description or "")
    return m.group(1).strip() if m else ""


def _spx_to_london(iso_utc):
    if not iso_utc:
        return None
    dt = datetime.fromisoformat(iso_utc.strip().replace("Z", "")).replace(tzinfo=timezone.utc)
    return dt.astimezone(_LONDON)


def scrape_spektrix(client, venue, venue_short):
    """Return future film screenings for a Spektrix client, in the standard schema.
    `client` is the Spektrix account slug (e.g. 'ica', 'barbicancentre')."""
    base = _SPEKTRIX_BASE.format(client=client)
    sess = requests.Session()
    sess.headers.update(_SPEKTRIX_HEADERS)

    events = sess.get(base + "events", timeout=180).json()
    film_events = {e["id"]: e for e in events if e.get("id") and _spx_is_film(e, client)}

    # The bare /instances endpoint is unusable (Barbican 500s, ICA times out on a
    # ~10MB body); ?startFrom keeps it small and scopes to upcoming showtimes.
    today = datetime.now(_LONDON).strftime("%Y-%m-%d")
    instances = sess.get(base + "instances", params={"startFrom": today}, timeout=180).json()

    now_utc = datetime.now(timezone.utc)
    rows = []
    for inst in instances:
        ev = film_events.get((inst.get("event") or {}).get("id"))
        if ev is None or inst.get("cancelled"):
            continue
        local = _spx_to_london(inst.get("startUtc") or inst.get("start"))
        if local is None or local.astimezone(timezone.utc) < now_utc:
            continue

        title = (ev.get("name") or "").strip()
        description = _spx_clean(ev.get("description")) or _spx_clean(ev.get("htmlDescription"))
        rows.append({
            "title": title,
            "date": local.strftime("%Y-%m-%d"),
            "time": local.strftime("%I:%M %p").lstrip("0"),
            "format": _spx_format(title, description),
            "venue": venue,
            "venue_short": venue_short,
            "url": f"https://system.spektrix.com/{client}/website/EventDetails.aspx?EventId={ev['id']}",
            "city": "London",
            "description": description,
            "runtime": _spx_runtime(ev),
            "director": _spx_director(ev, description),
        })

    rows.sort(key=lambda r: (r["date"], r["time"], r["title"]))
    return rows
