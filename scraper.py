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


NEWBEV_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "newbev_meta_cache.json")


def _newbev_detail_metadata(html):
    """Pull {description, runtime, director, format} from a New Beverly program
    page. Credits live in a labeled block (Director / Writer / Starring / Format
    / Running Time); the synopsis is the first substantial content paragraph
    (skipping ticketing notices)."""
    import html as html_lib
    NOTICE = ("ticket", "sold out", "box office", "will be available",
              "doors open", "rsvp", "no late seating")
    meta = {"description": "", "runtime": "", "director": "", "format": ""}
    try:
        text = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))
        dr = re.search(
            r"Director\s+([A-Z][A-Za-z.\-'’,& ]{2,45}?)\s+"
            r"(?:Writer|Screenplay|Starring|Format|Running Time)", text)
        if dr:
            meta["director"] = dr.group(1).strip(" ,")
        rt = re.search(r"Running Time\s+(\d{1,3})\s*min", text, re.I)
        if rt:
            meta["runtime"] = f"{int(rt.group(1))} min"
        fm = re.search(r"Format\s+([0-9A-Za-z ]+?)\s+(?:Running Time|Director|Writer|Starring|Aspect)", text)
        if fm:
            meta["format"] = normalize_format(fm.group(1))

        m = re.search(r"movie__content(.*?)(?:Upcoming Showtimes|movie__details)", html, re.S | re.I)
        seg = m.group(1) if m else html
        for p in re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S | re.I):
            t = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p))).strip()
            low = t.lower()
            if len(t) > 80 and not any(k in low for k in NOTICE):
                meta["description"] = t
                break
    except Exception:
        pass
    return meta


def _newbev_meta_for_url(url, cache):
    empty = {"description": "", "runtime": "", "director": "", "format": ""}
    if not url or "/program/" not in url:
        return empty
    if url in cache:
        return cache[url]
    meta = empty
    try:
        rr = requests.get(url, headers=HEADERS, timeout=15)
        meta = _newbev_detail_metadata(rr.text)
    except Exception:
        pass
    cache[url] = meta
    return meta


def _load_newbev_cache():
    try:
        with open(NEWBEV_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_newbev_cache(cache):
    try:
        with open(NEWBEV_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def scrape_new_bev():
    """New Beverly Cinema — static HTML"""
    screenings = []
    cache = _load_newbev_cache()
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
                if link_el and link_el.get("href"):
                    href = link_el["href"]
                    # hrefs are now absolute; guard against the old double-prefix bug
                    url = href if href.startswith("http") else "https://thenewbev.com" + href
                else:
                    url = "https://thenewbev.com/schedule/"

                meta = _newbev_meta_for_url(url, cache)
                if not fmt:
                    fmt = meta.get("format", "")

                if title:
                    screenings.append({
                        "title": title,
                        "date": date_str,
                        "time": time_str,
                        "format": fmt,
                        "venue": "New Beverly Cinema",
                        "venue_short": "NewBev",
                        "url": url,
                        "description": meta["description"],
                        "runtime": meta["runtime"],
                        "director": meta["director"],
                    })
            except Exception:
                continue
    except Exception as e:
        print(f"[NewBev] Error: {e}")
    _save_newbev_cache(cache)
    return screenings


VISTA_SESSIONS_URL = "https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww"
VISTA_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "vista_meta_cache.json")


def _load_vista_cache():
    try:
        with open(VISTA_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vista_cache(cache):
    try:
        with open(VISTA_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def _vista_playwright_enrich(need, cache):
    """The Vista's runtime + synopsis live only on the Veezi purchase pages,
    which are Cloudflare-protected (plain requests get challenged). Load them
    with a headless browser. `need` maps title-key -> one purchase URL. Best
    effort: only successful results are cached (so failures retry next run)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"],
                                      viewport={"width": 1280, "height": 900})
            for tkey, url in need.items():
                meta = {"runtime": "", "description": ""}
                try:
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                    body = page.inner_text("body")
                    rm = re.search(r"\((\d{1,3})\s*minutes?\)", body)
                    if rm:
                        meta["runtime"] = f"{int(rm.group(1))} min"
                    try:
                        page.click("text=Show More", timeout=2500)
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    el = page.query_selector(".synopsis")
                    if el:
                        meta["description"] = re.sub(r"\s*Show (More|Less)\s*$", "",
                                                     el.inner_text()).strip()
                    page.close()
                except Exception:
                    pass
                if meta["runtime"] or meta["description"]:
                    cache[tkey] = meta
            browser.close()
    except Exception as e:
        print(f"[Vista/PW] Error: {e}")


def scrape_vista():
    """Vista Theater via Veezi ticketing. The listing is static HTML (title /
    date / times); film synopsis + runtime come from the Cloudflare-protected
    purchase pages via Playwright, cached by title. Format defaults to 35mm
    unless the title/description denotes otherwise (70mm, 16mm, VHS, DVD)."""
    screenings = []
    cache = _load_vista_cache()
    pending = []       # (screening dict, title_key)
    to_fetch = {}      # title_key -> one purchase URL
    try:
        r = requests.get(VISTA_SESSIONS_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        for film_div in soup.select("div.film"):
            try:
                title_el = film_div.select_one("h3.title")
                if not title_el:
                    continue
                title = title_el.text.strip()
                tkey = title.lower()

                # Vista is 35mm unless the title/description denotes otherwise
                desc_text = film_div.get_text(" ", strip=True)
                fmt = normalize_format(desc_text) or "35mm"

                for date_container in film_div.select("div.date-container"):
                    date_el = date_container.select_one("h4.date")
                    if not date_el:
                        continue
                    date_text = date_el.text.strip()  # e.g. "Sunday 28, June"

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
                        url = parent_a["href"] if parent_a and parent_a.get("href") else VISTA_SESSIONS_URL
                        if url.startswith("/"):
                            url = "https://ticketing.uswest.veezi.com" + url
                        if "/purchase/" in url:
                            to_fetch.setdefault(tkey, url)

                        screening = {
                            "title": title,
                            "date": date_str,
                            "time": time_str,
                            "format": fmt,
                            "venue": "Vista Theater",
                            "venue_short": "Vista",
                            "url": url,
                        }
                        pending.append((screening, tkey))
            except Exception:
                continue

        # Fetch metadata only for films not already cached
        need = {k: u for k, u in to_fetch.items() if k not in cache}
        if need:
            _vista_playwright_enrich(need, cache)

        for screening, tkey in pending:
            meta = cache.get(tkey) or {}
            screening["description"] = meta.get("description", "")
            screening["runtime"] = meta.get("runtime", "")
            screening["director"] = ""
            screenings.append(screening)
    except Exception as e:
        print(f"[Vista] Error: {e}")
    _save_vista_cache(cache)
    return screenings


SHOWTIME_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "showtime_meta_cache.json")


def _showtime_synopsis(movie_url, cache):
    """Fetch a film synopsis from a Vidiots/BrainDead movie page (first real
    paragraph, else og:description), cached by URL. Director/runtime already come
    from the listing's show-specs, so this is the only per-film fetch."""
    if not movie_url or "/movies/" not in movie_url:
        return ""
    if movie_url in cache:
        return cache[movie_url]
    desc = ""
    SKIP = ("select a showtime", "walk-up", "looking for more",
            "tickets for sale", "no online tickets")
    try:
        rr = requests.get(movie_url, headers=HEADERS, timeout=15)
        msoup = BeautifulSoup(rr.text, "html.parser")
        for p in msoup.find_all("p"):
            t = p.get_text(" ", strip=True)
            low = t.lower()
            if len(t) <= 80:
                continue
            if any(k in low for k in SKIP):
                continue
            # Skip credit/spec lines (Director:, Screenwriters :, Starring:, Run Time:, …)
            if re.match(r"[A-Za-z][A-Za-z .]{1,20}:\s", t):
                continue
            desc = t
            break
        if not desc:
            og = msoup.select_one('meta[property="og:description"]')
            if og:
                desc = (og.get("content") or "").strip()
    except Exception:
        pass
    cache[movie_url] = desc
    return desc


def _load_showtime_cache():
    try:
        with open(SHOWTIME_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_showtime_cache(cache):
    try:
        with open(SHOWTIME_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def _scrape_showtime_site(url, venue_name, venue_short):
    """Generic scraper for Vidiots/BrainDead (same CMS)."""
    screenings = []
    cache = _load_showtime_cache()
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        for show in soup.select("div.show-details"):
            try:
                title_el = show.select_one("h2.show-title a, h2.show-title")
                if not title_el:
                    continue
                title = title_el.text.strip()

                # Specs (format / director / runtime) are labeled spans in the listing
                specs = {}
                for spec in show.select("p.show-specs span"):
                    t = spec.get_text(" ", strip=True)
                    m = re.match(r"([\w ]+?):\s*(.+)", t)
                    if m and m.group(2).strip():
                        specs.setdefault(m.group(1).strip().lower(), m.group(2).strip())

                fmt = normalize_format(specs.get("format", ""))
                if not fmt:
                    fmt = normalize_format(show.get_text(" ", strip=True))
                director = specs.get("director", "")
                runtime = specs.get("run time", "")
                if runtime:
                    rm = re.search(r"\d{1,3}", runtime)
                    runtime = f"{int(rm.group())} min" if rm else ""

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

                synopsis = _showtime_synopsis(movie_url, cache)

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
                            "description": synopsis,
                            "runtime": runtime,
                            "director": director,
                        })
            except Exception:
                continue
    except Exception as e:
        print(f"[{venue_short}] Error: {e}")
    _save_showtime_cache(cache)
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


ACADEMY_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "academy_meta_cache.json")


def _contentful_text(field):
    """Flatten a Contentful rich-text field ({'json': {...}}) to plain text."""
    if not isinstance(field, dict):
        return ""
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("nodeType") == "text" and n.get("value"):
                out.append(n["value"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(field.get("json"))
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _academy_meta_for_url(url, cache):
    """The Academy ticketing API carries no film details. Pull format, runtime,
    director and synopsis from the event page's Contentful __NEXT_DATA__
    (filmFormat1 / filmMetadata1 credits / filmDescription1). Cached per event."""
    empty = {"description": "", "runtime": "", "director": "", "format": ""}
    if not url:
        return dict(empty)
    if url in cache:
        return cache[url]
    meta = dict(empty)
    try:
        rr = requests.get(url, headers=HEADERS, timeout=15)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', rr.text, re.S)
        if m:
            prog = (json.loads(m.group(1)).get("props", {})
                    .get("pageProps", {}).get("program") or {})
            meta["format"] = normalize_format(prog.get("filmFormat1") or "")
            credits = _contentful_text(prog.get("filmMetadata1"))
            rt = re.search(r"(\d{1,3})\s*min", credits, re.I)
            if rt:
                meta["runtime"] = f"{int(rt.group(1))} min"
            dr = re.search(r"DIRECTED BY:\s*(.+?)(?:\s{2,}|WRITTEN BY|WITH:|Print courtesy|$)", credits, re.I)
            if dr:
                meta["director"] = re.sub(r"\s+", " ", dr.group(1)).strip(" .,")
            meta["description"] = _contentful_text(prog.get("filmDescription1"))
        if not meta["format"]:
            tm = re.search(r"<title[^>]*>(.*?)</title>", rr.text, re.S | re.I)
            if tm:
                meta["format"] = normalize_format(tm.group(1))
    except Exception:
        pass
    cache[url] = meta
    return meta


def _load_academy_cache():
    try:
        with open(ACADEMY_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_academy_cache(cache):
    try:
        with open(ACADEMY_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def scrape_academy():
    """Academy Museum — tickets.academymuseum.org REST API (replaces slow Playwright scraper)."""
    FILM_CATS = {"Film Screening", "Film Screening: Matinee", "Film Screening: Double Feature"}
    # Academy Museum is in LA: PDT = UTC-7 (Mar–Nov), PST = UTC-8 (Nov–Mar)
    LA_UTC_OFFSET = -7  # PDT

    screenings = []
    cache = _load_academy_cache()
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

            # Event URL: /en/programs/detail/{slug}-{id}
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            event_id = tmpl.get("id", "")
            event_url = f"https://www.academymuseum.org/en/programs/detail/{slug}-{event_id}"

            # The ticketing API carries no film details, so enrich from the event
            # page's Contentful data (format / runtime / director / synopsis).
            meta = _academy_meta_for_url(event_url, cache)
            fmt = normalize_format(title)
            if not fmt:
                fmt = normalize_format(tmpl.get("subtitle", "") + " " + tmpl.get("summary", ""))
            if not fmt:
                fmt = meta.get("format", "")

            screenings.append({
                "title": title,
                "date": date_str,
                "time": time_str,
                "format": fmt,
                "venue": "Academy Museum",
                "venue_short": "Academy",
                "url": event_url,
                "description": meta["description"],
                "runtime": meta["runtime"],
                "director": meta["director"],
            })
    except Exception as e:
        print(f"[Academy] Error: {e}")
    _save_academy_cache(cache)
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

        # Prefer the "About the Film" section — anchoring there skips series
        # blurbs (e.g. Cinematic Void's mission statement) and breadcrumbs. Back
        # up to the enclosing <p> in case the label sits inside the synopsis
        # paragraph. Fall back to the first substantial paragraph after the
        # film's detail bar.
        about = re.search(r"ABOUT THE FILM", html, re.I)
        if about:
            p_before = html.rfind("<p", 0, about.start())
            start = p_before if p_before != -1 and about.start() - p_before < 400 else about.start()
        else:
            bar = re.search(r"eventDetailBar", html, re.I) or dr
            start = bar.end() if bar else 0
        SKIP = ("format:", "distributor:", "country:", "released in",
                "directed by", "home /", "now showing",
                "everything we do is rooted",
                "american cinematheque is supported")
        for p in re.findall(r"<p[^>]*>(.*?)</p>", html[start:], re.S | re.I):
            txt = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p))).strip()
            txt = re.sub(r"^ABOUT THE FILM:\s*", "", txt, flags=re.I)
            low = txt.lower()
            if len(txt) > 80 and not any(k in low for k in SKIP):
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
    """Scrape all venues and return combined, sorted list.

    Resilient by design: each venue is scraped in isolation. If a venue raises
    (or transiently returns nothing) while it had data on the previous run, that
    venue's last-known rows are retained. So one flaky venue can neither crash the
    whole scrape nor make itself disappear from the site — which is what a single
    transient failure used to do (it aborted the entire run, so nothing updated).
    """
    import os as _os
    from collections import Counter as _Counter, defaultdict as _defaultdict

    # Load the previous run so a failed/empty venue can fall back to last-known.
    prev_rows = []
    try:
        with open(_os.path.join(_os.path.dirname(__file__), "screenings.json")) as f:
            prev_rows = json.load(f)
    except Exception:
        prev_rows = []

    def _safe(label, fn, city=None):
        try:
            rows = fn()
            return _tag_city(rows, city) if city else rows
        except Exception as e:
            print(f"  [{label}] FAILED: {e} — falling back to last-known if available")
            return []

    all_screenings = []

    print("── Los Angeles ──")
    all_screenings += _safe("New Beverly", scrape_new_bev, "LA")
    all_screenings += _safe("Vista", scrape_vista, "LA")
    all_screenings += _safe("Vidiots", scrape_vidiots, "LA")
    all_screenings += _safe("Brain Dead", scrape_braindead, "LA")
    all_screenings += _safe("Academy Museum", scrape_academy, "LA")
    all_screenings += _safe("American Cinematheque", scrape_american_cinematheque, "LA")
    all_screenings += _safe("American Cinematheque runs", scrape_american_cinematheque_runs, "LA")

    print("\n── New York City ──")
    from scraper_nyc import scrape_all_nyc
    all_screenings += _safe("NYC", scrape_all_nyc)

    print("\n── London ──")
    from scraper_london import scrape_all_london
    all_screenings += _safe("London", scrape_all_london)

    # Sort by date then time
    def sort_key(s):
        t = s.get("time", "")
        try:
            parsed = datetime.strptime(t.upper().replace(".", "").replace(" ", ""), "%I:%M%p")
            t_norm = parsed.strftime("%H:%M")
        except Exception:
            t_norm = t
        return (s.get("date", ""), t_norm)

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

    # Decode any HTML entities that slipped through from source pages
    # (e.g. "Parts 13 &amp; 14" -> "Parts 13 & 14"), and default missing formats.
    # New Beverly and Vista exclusively screen film prints (never digital).
    import html as _html
    FILM_PRINT_VENUES = {"NewBev", "Vista"}
    for s in deduped:
        for key in ("title", "description", "director"):
            if s.get(key):
                s[key] = _html.unescape(s[key])
        if not s.get("format"):
            s["format"] = "35mm" if s.get("venue_short") in FILM_PRINT_VENUES else "DCP"

    # Resilience: retain last-known rows for any venue that produced nothing this
    # run but had data previously. A transient venue failure must not drop a venue
    # from the site — it freezes at its last-good data until the scraper recovers.
    fresh_counts = _Counter(s["venue_short"] for s in deduped)
    prev_by_venue = _defaultdict(list)
    for s in prev_rows:
        if s.get("venue_short"):
            prev_by_venue[s["venue_short"]].append(s)
    retained = 0
    for vs, rows in prev_by_venue.items():
        if fresh_counts.get(vs, 0) == 0 and rows:
            deduped += rows
            retained += len(rows)
            print(f"  [retain] {vs}: scraper returned 0 — kept {len(rows)} last-known rows")

    deduped.sort(key=sort_key)

    print(f"\nTotal: {len(deduped)} screenings across all venues"
          + (f" ({retained} retained from last run)" if retained else ""))
    return deduped


if __name__ == "__main__":
    data = scrape_all()
    with open("screenings.json", "w") as f:
        json.dump(data, f, indent=2)
    print("Saved to screenings.json")
