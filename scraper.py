"""
Scrapers for LA rep theatre schedules.
Returns list of dicts with: title, date (YYYY-MM-DD), time, format, venue, url
"""

import re
import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def normalize_format(text):
    """Extract projection format from any text."""
    text_lower = text.lower()
    if "70mm" in text_lower:
        return "70mm"
    if "35mm" in text_lower:
        return "35mm"
    if "16mm" in text_lower:
        return "16mm"
    if "vhs" in text_lower:
        return "VHS"
    if "dvd" in text_lower:
        return "DVD"
    if "dcp" in text_lower:
        return "DCP"
    if "digital" in text_lower:
        return "Digital"
    return ""


def scrape_new_bev():
    """New Beverly Cinema — static HTML"""
    screenings = []
    try:
        r = requests.get("https://thenewbev.com/schedule/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        year = datetime.now().year

        for card in soup.select("article.event-card"):
            try:
                month_el = card.select_one(".event-card__month")
                day_el = card.select_one(".event-card__numb")
                if not month_el or not day_el:
                    continue

                month_str = month_el.text.strip().lower()[:3]
                month = MONTH_MAP.get(month_str, 0)
                day = int(day_el.text.strip())
                if not month:
                    continue

                # Handle year rollover
                date_obj = datetime(year, month, day)
                if date_obj < datetime.now() - timedelta(days=1):
                    date_obj = datetime(year + 1, month, day)
                date_str = date_obj.strftime("%Y-%m-%d")

                title_el = card.select_one(".event-card__title")
                title = title_el.text.strip() if title_el else ""

                times = [t.text.strip() for t in card.select("time.event-card__time")]
                time_str = " / ".join(times) if times else ""

                # Format from label classes
                label_el = card.select_one("[class*='label-']")
                fmt = ""
                if label_el:
                    classes = " ".join(label_el.get("class", []))
                    fmt = normalize_format(classes)

                # Also check full card text for format hints
                full_text = card.get_text(" ", strip=True)
                if not fmt:
                    fmt = normalize_format(full_text)

                link_el = card.select_one("a[href]")
                url = "https://thenewbev.com" + link_el["href"] if link_el else "https://thenewbev.com/schedule/"

                if title:
                    screenings.append({
                        "title": title,
                        "date": date_str,
                        "time": time_str,
                        "format": fmt,
                        "venue": "New Beverly Cinema",
                        "venue_short": "NewBev",
                        "url": url,
                    })
            except Exception:
                continue
    except Exception as e:
        print(f"[NewBev] Error: {e}")
    return screenings


def scrape_vista():
    """Vista Theater via Veezi ticketing — static HTML"""
    screenings = []
    try:
        r = requests.get(
            "https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")

        for film_div in soup.select("div.film"):
            try:
                title_el = film_div.select_one("h3.title")
                if not title_el:
                    continue
                title = title_el.text.strip()

                # Format from description text
                desc_text = film_div.get_text(" ", strip=True)
                fmt = normalize_format(desc_text)

                for date_container in film_div.select("div.date-container"):
                    date_el = date_container.select_one("h4.date")
                    if not date_el:
                        continue
                    date_text = date_el.text.strip()  # e.g. "Sunday 28, June"

                    # Parse "Sunday 28, June" or "Sunday 28, June 2025"
                    date_match = re.search(r"(\d{1,2}),?\s+(\w+)(?:\s+(\d{4}))?", date_text)
                    if not date_match:
                        continue
                    day = int(date_match.group(1))
                    month_str = date_match.group(2).lower()[:3]
                    year = int(date_match.group(3)) if date_match.group(3) else datetime.now().year
                    month = MONTH_MAP.get(month_str, 0)
                    if not month:
                        continue

                    date_obj = datetime(year, month, day)
                    if date_obj < datetime.now() - timedelta(days=1):
                        date_obj = datetime(year + 1, month, day)
                    date_str = date_obj.strftime("%Y-%m-%d")

                    for time_el in date_container.select("time"):
                        time_str = time_el.text.strip()
                        parent_a = time_el.find_parent("a")
                        url = parent_a["href"] if parent_a and parent_a.get("href") else \
                            "https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww"
                        if url.startswith("/"):
                            url = "https://ticketing.uswest.veezi.com" + url

                        screenings.append({
                            "title": title,
                            "date": date_str,
                            "time": time_str,
                            "format": fmt,
                            "venue": "Vista Theater",
                            "venue_short": "Vista",
                            "url": url,
                        })
            except Exception:
                continue
    except Exception as e:
        print(f"[Vista] Error: {e}")
    return screenings


def _scrape_showtime_site(url, venue_name, venue_short):
    """Generic scraper for Vidiots/BrainDead (same CMS)."""
    screenings = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        for show in soup.select("div.show-details"):
            try:
                title_el = show.select_one("h2.show-title a, h2.show-title")
                if not title_el:
                    continue
                title = title_el.text.strip()

                # Format from specs
                fmt = ""
                for spec in show.select("p.show-specs span"):
                    text = spec.get_text(" ", strip=True)
                    if "Format:" in text or normalize_format(text):
                        fmt = normalize_format(text)
                        if fmt:
                            break
                if not fmt:
                    fmt = normalize_format(show.get_text(" ", strip=True))

                # Movie page URL
                link_el = title_el if title_el.name == "a" else title_el.find("a")
                movie_url = url
                if link_el and link_el.get("href"):
                    href = link_el["href"]
                    if href.startswith("http"):
                        movie_url = href
                    else:
                        from urllib.parse import urljoin
                        movie_url = urljoin(url, href)

                # Dates from data-date timestamps
                for date_li in show.select("li.show-date"):
                    ts = date_li.get("data-date")
                    if not ts:
                        continue
                    date_obj = datetime.fromtimestamp(int(ts))
                    date_str = date_obj.strftime("%Y-%m-%d")

                    # Showtimes for this date
                    for showtime_li in show.select(f"ol.showtimes li[data-date='{ts}']"):
                        time_el = showtime_li.select_one("a.showtime")
                        if not time_el:
                            continue
                        time_str = time_el.text.strip()
                        ticket_url = time_el.get("href", movie_url)
                        if ticket_url.startswith("/"):
                            from urllib.parse import urljoin
                            ticket_url = urljoin(url, ticket_url)

                        screenings.append({
                            "title": title,
                            "date": date_str,
                            "time": time_str,
                            "format": fmt,
                            "venue": venue_name,
                            "venue_short": venue_short,
                            "url": ticket_url,
                        })
            except Exception:
                continue
    except Exception as e:
        print(f"[{venue_short}] Error: {e}")
    return screenings


def scrape_vidiots():
    return _scrape_showtime_site(
        "https://vidiotsfoundation.org/coming-soon/",
        "Vidiots Foundation",
        "Vidiots"
    )


def scrape_braindead():
    return _scrape_showtime_site(
        "https://studios.wearebraindead.com/coming-soon/",
        "Brain Dead Studios",
        "BrainDead"
    )


def scrape_academy():
    """Academy Museum — tickets.academymuseum.org REST API (replaces slow Playwright scraper)."""
    FILM_CATS = {"Film Screening", "Film Screening: Matinee", "Film Screening: Double Feature"}
    # Academy Museum is in LA: PDT = UTC-7 (Mar–Nov), PST = UTC-8 (Nov–Mar)
    LA_UTC_OFFSET = -7  # PDT

    screenings = []
    try:
        from_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        from_str = from_dt.strftime("%Y-%m-%dT07:00:00.000Z")  # midnight LA as UTC

        url = (
            "https://tickets.academymuseum.org/cached_api/events/available"
            f"?event_session.start_datetime._gte={from_str}"
            "&_withmemberevents"
            "&category._in=Film%20Screening,Film%20Screening:%20Matinee,Film%20Screening:%20Double%20Feature"
            "&_embed=event_session,ticket_group,ticket_type,venue"
            "&_sort=event_session.start_datetime"
        )
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()

        templates = {t["id"]: t for t in data.get("event_template", {}).get("_data", [])}
        sessions = data.get("event_session", {}).get("_data", [])

        for sess in sessions:
            tmpl = templates.get(sess.get("event_template_id"))
            if not tmpl:
                continue
            if tmpl.get("category") not in FILM_CATS:
                continue

            title = tmpl.get("name", "").strip()
            if not title:
                continue

            # Convert UTC start_datetime to LA local time
            utc_str = sess.get("start_datetime", "")  # "2026-07-23T02:30:00Z"
            if not utc_str:
                continue
            utc_dt = datetime.strptime(utc_str[:19], "%Y-%m-%dT%H:%M:%S")
            la_dt = utc_dt + timedelta(hours=LA_UTC_OFFSET)
            date_str = la_dt.strftime("%Y-%m-%d")
            hour = la_dt.hour
            minute = la_dt.minute
            ampm = "AM" if hour < 12 else "PM"
            h12 = hour % 12 or 12
            time_str = f"{h12}:{minute:02d} {ampm}"

            # Format: try to extract from title or subtitle
            fmt = normalize_format(title)
            if not fmt:
                fmt = normalize_format(tmpl.get("subtitle", "") + " " + tmpl.get("summary", ""))

            # Event URL: /en/programs/detail/{slug}-{id}
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            event_id = tmpl.get("id", "")
            event_url = f"https://www.academymuseum.org/en/programs/detail/{slug}-{event_id}"

            screenings.append({
                "title": title,
                "date": date_str,
                "time": time_str,
                "format": fmt,
                "venue": "Academy Museum",
                "venue_short": "Academy",
                "url": event_url,
            })
    except Exception as e:
        print(f"[Academy] Error: {e}")
    return screenings


# American Cinematheque venue id → (full name, short name). Shared by the
# events feed and the runs feed below.
AMCIN_LOCATION_MAP = {
    54: ("Aero Theatre", "Aero"),
    55: ("Egyptian Theatre", "Egyptian"),
    102: ("Los Feliz Theatre", "LosFeliz"),
    181: ("Directors Village", "DirsVillage"),
}

# Cache of scraped detail-page metadata keyed by event URL, so daily runs only
# fetch pages for events they haven't seen before. Committed alongside the data.
AMCIN_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "amcin_meta_cache.json")


def _amcin_detail_metadata(html):
    """Pull {description, runtime, director} from an American Cinematheque event
    or run detail page. Film details live in an 'eventDetailBar'
    (RELEASED IN / NNN MINUTES / DIRECTED BY:); the synopsis is the first
    substantial paragraph on the page."""
    import html as html_lib
    meta = {"description": "", "runtime": "", "director": ""}
    try:
        rt = re.search(r"(\d{1,3})\s*MINUTES", html, re.I)
        if rt:
            meta["runtime"] = f"{int(rt.group(1))} min"

        dr = re.search(r"DIRECTED BY:\s*([^<\n|]{2,70})", html, re.I)
        if dr:
            meta["director"] = re.sub(r"\s+", " ", html_lib.unescape(dr.group(1))).strip(" .")

        # The synopsis sits AFTER the film's detail bar. Anchor there so we skip
        # the page-header boilerplate ("Now Showing … 35mm, 70mm, nitrate …").
        anchor = re.search(r"eventDetailBar", html, re.I) or dr
        tail = html[anchor.end():] if anchor else html
        for p in re.findall(r"<p[^>]*>(.*?)</p>", tail, re.S | re.I):
            txt = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p))).strip()
            if len(txt) > 60 and "American Cinematheque is supported" not in txt:
                meta["description"] = txt
                break
    except Exception:
        pass
    return meta


def _amcin_meta_for_url(url, cache):
    """Cache-aware fetch of detail-page metadata for one event URL."""
    empty = {"description": "", "runtime": "", "director": ""}
    if not url:
        return empty
    if url in cache:
        return cache[url]
    meta = empty
    try:
        rr = requests.get(url, headers=HEADERS, timeout=15)
        meta = _amcin_detail_metadata(rr.text)
    except Exception:
        pass
    cache[url] = meta
    return meta


def _load_amcin_cache():
    try:
        with open(AMCIN_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_amcin_cache(cache):
    try:
        with open(AMCIN_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def scrape_american_cinematheque():
    """American Cinematheque (Aero, Egyptian, Los Feliz, Directors Village) — direct WP JSON API."""
    FORMAT_MAP = {
        74: "35mm", 79: "70mm", 83: "DCP", 80: "Nitrate", 334: "Nitrate",
        315: "4K DCP", 333: "2K DCP", 336: "3-D DCP", 96: "4K", 97: "2K",
        103: "Digital", 104: "4K",
    }
    LOCATION_MAP = AMCIN_LOCATION_MAP
    TARGET_LOCATIONS = set(LOCATION_MAP.keys())

    screenings = []
    cache = _load_amcin_cache()
    try:
        from_ts = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
        to_ts = from_ts + 90 * 86400
        url = (
            f"https://www.americancinematheque.com/wp-json/wp/v2/algolia_get_events"
            f"?environment=production_2026&startDate={from_ts}&endDate={to_ts}"
        )
        r = requests.get(url, headers=HEADERS, timeout=15)
        hits = r.json().get("hits", [])

        for h in hits:
            locs = [l for l in h.get("event_location", []) if l in TARGET_LOCATIONS]
            if not locs:
                continue

            title = h.get("title", "").strip()
            if not title:
                continue

            date_raw = str(h.get("event_start_date", ""))  # "20260726"
            if len(date_raw) != 8:
                continue
            date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"

            time_str = h.get("event_start_time", "").strip()
            event_url = h.get("url", "https://www.americancinematheque.com/now-showing/")

            fmts = h.get("event_format", [])
            fmt = next((FORMAT_MAP[f] for f in fmts if f in FORMAT_MAP), "")

            meta = _amcin_meta_for_url(event_url, cache)

            for loc_id in locs:
                venue_name, venue_short = LOCATION_MAP[loc_id]
                screenings.append({
                    "title": title,
                    "date": date_str,
                    "time": time_str,
                    "format": fmt,
                    "venue": venue_name,
                    "venue_short": venue_short,
                    "url": event_url,
                    "description": meta["description"],
                    "runtime": meta["runtime"],
                    "director": meta["director"],
                })
    except Exception as e:
        print(f"[AmCin] Error: {e}")
    _save_amcin_cache(cache)
    return screenings


def scrape_american_cinematheque_runs():
    """American Cinematheque multi-week *runs* (e.g. 70mm engagements like
    "THE ODYSSEY in 70mm"). These are NOT in the algolia events feed — the site
    loads them from a separate active_runs endpoint, and each run's per-showtime
    schedule is embedded in the run page as JSON on #runShowtimesApp
    (data-showtimes). Without this, most Aero / Directors Village dates go missing.
    """
    import html as html_lib

    screenings = []
    try:
        from_str = datetime.now().strftime("%Y-%m-%d")
        to_str = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
        url = (
            "https://www.americancinematheque.com/wp-json/wp/v2/active_runs"
            f"?start={from_str}&end={to_str}"
        )
        r = requests.get(url, headers=HEADERS, timeout=15)
        runs = r.json().get("runs", [])

        for run in runs:
            run_url = run.get("url")
            if not run_url:
                continue
            try:
                rr = requests.get(run_url, headers=HEADERS, timeout=15)
                m = re.search(r'data-showtimes="(.*?)"', rr.text, re.S)
                if not m:
                    continue
                data = json.loads(html_lib.unescape(m.group(1)))
                run_meta = _amcin_detail_metadata(rr.text)
            except Exception:
                continue

            for ev in data.get("events", []):
                venue = ev.get("venue") or {}
                vid = venue.get("id")
                if vid not in AMCIN_LOCATION_MAP:
                    continue
                venue_name, venue_short = AMCIN_LOCATION_MAP[vid]

                title = (ev.get("title") or "").strip()
                date_str = (ev.get("start_date") or "").strip()
                time_str = (ev.get("time") or "").strip()
                if not title or not re.match(r"\d{4}-\d{2}-\d{2}$", date_str):
                    continue

                fmt = normalize_format(title)
                event_url = ev.get("url") or run_url

                screenings.append({
                    "title": title,
                    "date": date_str,
                    "time": time_str,
                    "format": fmt,
                    "venue": venue_name,
                    "venue_short": venue_short,
                    "url": event_url,
                    "description": run_meta.get("description", ""),
                    "runtime": run_meta.get("runtime", ""),
                    "director": run_meta.get("director", ""),
                })
    except Exception as e:
        print(f"[AmCin/Runs] Error: {e}")
    return screenings


def _tag_city(screenings, city):
    for s in screenings:
        s.setdefault("city", city)
    return screenings


def scrape_all():
    """Scrape all venues and return combined, sorted list."""
    print("── Los Angeles ──")
    print("Scraping New Beverly Cinema...")
    all_screenings = _tag_city(scrape_new_bev(), "LA")
    print(f"  → {len(all_screenings)} screenings")

    print("Scraping Vista Theater...")
    vista = _tag_city(scrape_vista(), "LA")
    print(f"  → {len(vista)} screenings")
    all_screenings += vista

    print("Scraping Vidiots Foundation...")
    vidiots = _tag_city(scrape_vidiots(), "LA")
    print(f"  → {len(vidiots)} screenings")
    all_screenings += vidiots

    print("Scraping Brain Dead Studios...")
    bd = _tag_city(scrape_braindead(), "LA")
    print(f"  → {len(bd)} screenings")
    all_screenings += bd

    print("Scraping Academy Museum...")
    academy = _tag_city(scrape_academy(), "LA")
    print(f"  → {len(academy)} screenings")
    all_screenings += academy

    print("Scraping American Cinematheque (Aero/Egyptian/Los Feliz/Directors Village)...")
    amcin = _tag_city(scrape_american_cinematheque(), "LA")
    print(f"  → {len(amcin)} screenings (events)")
    all_screenings += amcin

    print("Scraping American Cinematheque runs (70mm engagements at Aero/Directors Village)...")
    amcin_runs = _tag_city(scrape_american_cinematheque_runs(), "LA")
    print(f"  → {len(amcin_runs)} screenings (runs)")
    all_screenings += amcin_runs

    print("\n── New York City ──")
    from scraper_nyc import scrape_all_nyc
    nyc = scrape_all_nyc()
    print(f"  NYC subtotal: {len(nyc)} screenings")
    all_screenings += nyc

    # Sort by date then time
    def sort_key(s):
        t = s.get("time", "")
        # Normalize time for sorting
        try:
            parsed = datetime.strptime(t.upper().replace(".", "").replace(" ", ""), "%I:%M%p")
            t_norm = parsed.strftime("%H:%M")
        except Exception:
            t_norm = t
        return (s.get("date", ""), t_norm)

    all_screenings.sort(key=sort_key)

    # Deduplicate: same venue + title + date + time is the same screening
    seen = set()
    deduped = []
    for s in all_screenings:
        key = (s["venue_short"], s["title"].lower(), s["date"], s["time"].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    removed = len(all_screenings) - len(deduped)
    if removed:
        print(f"  Removed {removed} duplicate screenings")

    print(f"\nTotal: {len(deduped)} screenings across all venues")
    return deduped


if __name__ == "__main__":
    data = scrape_all()
    with open("screenings.json", "w") as f:
        json.dump(data, f, indent=2)
    print("Saved to screenings.json")
