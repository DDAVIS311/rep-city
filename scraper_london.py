"""
London rep-cinema scrapers.
Same output schema as scraper.py / scraper_nyc.py:
    {title, date, time, format, venue, venue_short, url, city="London",
     description, runtime, director}

Venues:
  - ICA, Barbican -> Spektrix public JSON API (adapters.scrape_spektrix) for
    showtimes, then enriched from each venue's own front-end for a working
    per-event URL + director/synopsis/runtime (the Spektrix API exposes a broken
    embed URL and no director/synopsis). Front-end detail pages are cached by URL.
  - Prince Charles Cinema -> WordPress/Jacro what's-on page (all showtimes and
    metadata server-rendered into static HTML; one request covers ~4-5 months).
  - Garden, Castle, Rio, Nickel, Peckhamplex, Electric -> each in its own
    venues_*.py module (self-contained requests/bs4 scrapers), imported below.

Not yet included (need a real browser that won't run in the headless daily CI):
  - Curzon Soho (Vista OCAPI behind Cloudflare; token fetch needs a browser)
  - Close-Up (concrete5 behind Cloudflare; only a *headed* browser passes)
"""

import os
import re
import json
import time
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from adapters import scrape_spektrix
from venues_garden import scrape_garden
from venues_castle import scrape_castle
from venues_rio import scrape_rio
from venues_nickel import scrape_nickel
from venues_peckhamplex import scrape_peckhamplex
from venues_electric import scrape_electric

LONDON = ZoneInfo("Europe/London")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HEADERS = {"User-Agent": UA}
_HERE = os.path.dirname(__file__)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ── Title normalization for joining Spektrix titles to front-end pages ───────
# Spektrix titles carry certificate + format + strand noise, e.g.
#   "Off-Circuit: UK PREMIERE The Day She Returns", "The Odyssey (15) 35mm".
# Front-end titles are cleaner ("The Day She Returns", "The Odyssey [35mm]").
# Reduce both to a (base_title, format) key; format disambiguates same-title
# variants (e.g. Barbican runs a standard and a [35mm] "The Odyssey").
_FMT_KEY_RE = re.compile(r"\b(70mm|35mm|16mm|4k dcp|2k dcp|dcp|4k|imax|digital|nitrate|vhs|dvd)\b", re.I)
_BANNER_RE = re.compile(
    r"^(?:(?:UK|US|EUROPEAN|WORLD|LONDON|INTERNATIONAL)\s+)?(?:PREMIERE|PREVIEW)\s+", re.I)


def _norm_fmt(text):
    t = (text or "").lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "4k dcp" in t or "4k restoration" in t: return "4K DCP"
    if "2k dcp" in t: return "2K DCP"
    if "dcp" in t: return "DCP"
    if re.search(r"\b4k\b", t): return "4K"
    if "digital" in t: return "Digital"
    return ""


def _match_key(title):
    t = unicodedata.normalize("NFKC", title or "")
    if ": " in t:                       # drop strand prefix (Off-Circuit:, In Focus:)
        t = t.split(": ")[-1]
    fmt = _norm_fmt(t)
    base = re.sub(r"[\(\[].*?[\)\]]", " ", t)      # drop (15), [35mm], (4K Restoration)
    base = _FMT_KEY_RE.sub(" ", base)               # drop trailing bare format tokens
    base = _BANNER_RE.sub("", base.strip())         # drop leading premiere banner
    base = re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()
    base = re.sub(r"\s+", " ", base)
    return (base, fmt)


def _enrich_from_frontend(screenings, listing_fn, detail_fn, cache_file, fallback_url):
    """Fill each Spektrix screening's url + director/synopsis/runtime from the
    venue front-end. Detail pages are cached by URL so daily runs only fetch new
    films. Screenings that can't be matched fall back to the venue listing URL
    (never a broken link)."""
    cache_path = os.path.join(_HERE, cache_file)
    try:
        listing = listing_fn()
    except Exception as e:
        print(f"    front-end listing failed ({e}); using fallback URLs")
        for s in screenings:
            s["url"] = fallback_url
        return

    index = {}
    for e in listing:
        k = _match_key(e["title"])
        index.setdefault(k, e["url"])
        index.setdefault((k[0], ""), e["url"])      # base-only fallback (ignores format)

    cache = _load_json(cache_path)
    changed = False
    matched = 0
    for s in screenings:
        k = _match_key(s["title"])
        url = index.get(k) or index.get((k[0], ""))
        if not url:
            s["url"] = fallback_url
            continue
        matched += 1
        s["url"] = url
        if url not in cache:
            try:
                cache[url] = detail_fn(url)
            except Exception:
                cache[url] = {}
            changed = True
            time.sleep(0.3)
        meta = cache.get(url) or {}
        for field in ("director", "description", "runtime"):
            if not s.get(field) and meta.get(field):
                s[field] = meta[field]
        if not s.get("format") and meta.get("format"):
            s["format"] = meta["format"]
    if changed:
        _save_json(cache_path, cache)
    print(f"    enriched {matched}/{len(screenings)} showtimes from front-end")


# ── ICA front-end (https://www.ica.art/films) ────────────────────────────────
_ICA_BASE = "https://www.ica.art"
_ICA_NAV_SLUGS = {"today", "tomorrow", "everything"}


def _ica_clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _ica_strip_label(title):
    return re.sub(r"^(?:[A-Z0-9]{2,}\s+)+", "", title).strip() or title


def ica_listing():
    r = requests.get(_ICA_BASE + "/films", headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/films/"):
            continue
        if "year-btn" in (a.get("class") or []):
            continue
        slug = href.rstrip("/").split("/")[-1]
        if slug.isdigit() or slug in _ICA_NAV_SLUGS:
            continue
        if href in seen:
            continue
        film_titles = [t for t in a.find_all("div", class_="title")
                       if "season-item" not in (t.get("class") or [])]
        title = ""
        if film_titles:
            title = _ica_strip_label(film_titles[0].get_text(" ", strip=True))
        if not title:
            si = a.find("div", class_="season-item")
            if si:
                title = si.get_text(" ", strip=True)
        title = _ica_clean(title)
        if not title:
            continue
        seen.add(href)
        out.append({"title": title, "url": _ICA_BASE + href})
    return out


def ica_detail(url):
    result = {"director": "", "description": "", "runtime": "", "format": ""}
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return result
    soup = BeautifulSoup(r.text, "html.parser")
    colophon = soup.find(id="colophon")
    col_text = colophon.get_text(" ", strip=True) if colophon else ""
    if col_text:
        m = re.search(r"\bdir\.\s*(.+?)\s*,", col_text)
        if m:
            result["director"] = _ica_clean(m.group(1))
        m = re.search(r"(\d+)\s*min", col_text, re.I)
        if m:
            result["runtime"] = "%s min" % m.group(1)
    title_text = soup.title.get_text() if soup.title else ""
    result["format"] = _norm_fmt(col_text + " " + title_text)
    body = soup.find(id="detail-body")
    if body and colophon:
        chunks = []
        for sib in colophon.find_all_next():
            if sib is body or getattr(sib, "name", None) is None:
                continue
            if body not in sib.parents or sib.name != "div":
                continue
            cls = sib.get("class") or []
            if any(c in cls for c in ("row", "select", "subhead", "title",
                                      "season-item", "caption")):
                continue
            txt = sib.get_text(" ", strip=True)
            if not txt or txt.lower() == "book tickets":
                continue
            if sib.find("div"):
                continue
            chunks.append(txt)
        seen, uniq = set(), []
        for c in chunks:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        result["description"] = _ica_clean(" ".join(uniq))
    return result


# ── Barbican front-end ───────────────────────────────────────────────────────
# The plain /whats-on/cinema page only features a handful of films; the faceted
# whats-on listing (event_type:cinema) exposes the full upcoming cinema slate,
# paginated by &page=N. Note the Barbican models whole seasons as a single event
# page (e.g. a Pan-Africa strand), while Spektrix lists each sub-film separately,
# so season sub-films may have no individual page to match — those fall back to
# the cinema listing URL.
_BAR_BASE = "https://www.barbican.org.uk"
_BAR_LISTING = _BAR_BASE + "/whats-on?f%5B0%5D=event_type%3Acinema"
_BAR_EVENT_HREF_RE = re.compile(r"^/whats-on/\d{4}/event/")
_BAR_TITLE_SEL = re.compile(r"title", re.I)


def barbican_listing():
    out, seen, page = [], set(), 0
    while True:
        url = _BAR_LISTING if page == 0 else "%s&page=%d" % (_BAR_LISTING, page)
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for a in soup.find_all("a", href=_BAR_EVENT_HREF_RE):
            detail = _BAR_BASE + a["href"].split("?")[0]
            if detail in seen:
                continue
            card = a.find_parent(class_=re.compile(r"card|listing|search-result", re.I))
            title = ""
            if card:
                te = card.find(class_=_BAR_TITLE_SEL)
                if te:
                    title = te.get_text(" ", strip=True)
            if not title:
                title = a.get_text(" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            seen.add(detail)
            out.append({"title": title, "url": detail})
            new += 1
        if new == 0 or page > 15:
            break
        page += 1
        time.sleep(0.3)
    return out


def _bar_runtime(raw):
    if not raw:
        return ""
    h = re.search(r"(\d+)\s*h(?:r|ou)", raw, re.I)
    m = re.search(r"(\d+)\s*m(?:in)", raw, re.I)
    total = 0
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    if not h and not m:
        n = re.search(r"(\d+)", raw)
        if not n:
            return ""
        total = int(n.group(1))
    return "%d min" % total if total else ""


def barbican_detail(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    labels = {}
    for li in soup.select(".label-value-list li"):
        lab = li.select_one(".label-value-list__label")
        val = li.select_one(".label-value-list__value")
        if lab and val:
            labels[lab.get_text(strip=True).lower()] = val.get_text(" ", strip=True)
    director = labels.get("director", "")
    runtime = _bar_runtime(labels.get("runtime", ""))
    lead = soup.select_one(".lead-text")
    if lead:
        description = lead.get_text(" ", strip=True)
    else:
        og = (soup.find("meta", attrs={"property": "og:description"})
              or soup.find("meta", attrs={"name": "description"}))
        description = og["content"].strip() if og and og.get("content") else ""
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    fmt = _norm_fmt(" ".join([h1_text, slug.replace("-", " "), description]))
    return {"director": director, "description": description, "runtime": runtime, "format": fmt}


# ── Prince Charles Cinema (WordPress + Jacro plugin) ─────────────────────────
PCC_WHATS_ON_URL = "https://princecharlescinema.com/whats-on/"
_PCC_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def _pcc_format(tokens):
    t = " ".join(tokens).lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "4k" in t:   return "4K DCP"
    return ""


def _pcc_time(raw):
    raw = re.sub(r"\s+", " ", raw).strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap]m)", raw, re.I)
    return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}" if m else raw


def _pcc_heading_date(heading, today):
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)", heading)
    if not m:
        return None
    day = int(m.group(1))
    month = _PCC_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    for year in (today.year, today.year + 1, today.year + 2):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today:
            return d.isoformat()
    return None


def scrape_prince_charles(session=None, today=None):
    today = today or datetime.now(LONDON).date()
    sess = session or requests.Session()
    html = sess.get(PCC_WHATS_ON_URL, headers=HEADERS, timeout=45).text
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for ev in soup.select("div.jacro-event"):
        title_el = ev.select_one("a.liveeventtitle")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        detail_url = title_el.get("href", "")
        runtime = ""
        for sp in ev.select("div.running-time span"):
            m = re.match(r"(\d+)\s*mins?", sp.get_text(strip=True), re.I)
            if m:
                runtime = f"{m.group(1)} min"
                break
        director = ""
        for sp in ev.select("div.film-info span"):
            txt = sp.get_text(" ", strip=True)
            if txt.lower().startswith("directed by"):
                director = re.sub(r"\s*\|\s*", ", ", txt[len("directed by"):].strip())
                break
        desc_el = ev.select_one("div.jacro-formatted-text")
        description = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True)).strip() if desc_el else ""
        cur_date = None
        for node in ev.select("ul.performance-list-items > *"):
            classes = node.get("class", [])
            if "heading" in classes:
                cur_date = _pcc_heading_date(node.get_text(strip=True), today)
                continue
            if node.name != "li":
                continue
            time_el = node.select_one("span.time")
            book_el = node.select_one("a.film_book_button")
            if not time_el or cur_date is None:
                continue
            tags = [t.get_text(strip=True) for t in node.select("div.movietag span.tag")]
            rows.append({
                "title": title, "date": cur_date, "time": _pcc_time(time_el.get_text(strip=True)),
                "format": _pcc_format(list(classes) + tags),
                "venue": "Prince Charles Cinema", "venue_short": "PCC",
                "url": (book_el.get("href", "") if book_el else "") or detail_url,
                "city": "London", "description": description, "runtime": runtime, "director": director,
            })
    rows.sort(key=lambda r: (r["date"], r["time"], r["title"]))
    return rows


# ── Orchestration ────────────────────────────────────────────────────────────
# (spektrix_client, venue, venue_short, listing_fn, detail_fn, cache_file, fallback_url)
SPEKTRIX_VENUES = [
    ("ica", "ICA", "ICA", ica_listing, ica_detail,
     "ica_meta_cache.json", "https://www.ica.art/films"),
    ("barbicancentre", "Barbican", "Barbican", barbican_listing, barbican_detail,
     "barbican_meta_cache.json", "https://www.barbican.org.uk/whats-on/cinema"),
]


def scrape_all_london():
    screenings = []
    for client, venue, short, listing_fn, detail_fn, cache_file, fallback in SPEKTRIX_VENUES:
        try:
            rows = scrape_spektrix(client, venue, short)
            _enrich_from_frontend(rows, listing_fn, detail_fn, cache_file, fallback)
            print(f"  {venue}: {len(rows)} screenings")
            screenings += rows
        except Exception as e:
            print(f"  {venue}: FAILED ({e})")

    # Self-contained single-venue scrapers (each in its own venues_*.py module).
    OTHER_VENUES = [
        ("Prince Charles Cinema", scrape_prince_charles),
        ("The Garden Cinema", scrape_garden),
        ("The Castle Cinema", scrape_castle),
        ("Rio Cinema", scrape_rio),
        ("The Nickel Cinema", scrape_nickel),
        ("Peckhamplex", scrape_peckhamplex),
        ("Electric Cinema", scrape_electric),
    ]
    for label, fn in OTHER_VENUES:
        try:
            rows = fn()
            print(f"  {label}: {len(rows)} screenings")
            screenings += rows
        except Exception as e:
            print(f"  {label}: FAILED ({e})")

    # Normalize any bare-number runtime (e.g. "172") to "172 min" for consistency.
    for s in screenings:
        rt = str(s.get("runtime", "")).strip()
        if rt.isdigit():
            s["runtime"] = f"{rt} min"

    return screenings


if __name__ == "__main__":
    rows = scrape_all_london()
    print(f"\nLondon total: {len(rows)}")
