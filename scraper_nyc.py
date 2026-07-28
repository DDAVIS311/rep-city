"""
NYC rep theatre scrapers.
Same output schema as scraper.py: {title, date, time, format, venue, venue_short, url, city}
"""

import re
import os
import json
import time
import html as html_lib
import requests
from bs4 import BeautifulSoup, Comment
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


def _normalize_format(text):
    t = text.lower()
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


def _make(title, date, time_str, fmt, venue, venue_short, url, meta=None):
    d = {
        "title": title, "date": date, "time": time_str, "format": fmt,
        "venue": venue, "venue_short": venue_short, "url": url, "city": "NYC",
    }
    if meta:
        d["description"] = meta.get("description", "")
        d["runtime"] = meta.get("runtime", "")
        d["director"] = meta.get("director", "")
    return d


# ── Film-metadata enrichment (director / runtime / synopsis) ──────────────────
# Each venue's detail page is fetched once and cached by URL, so daily runs only
# hit pages for films they haven't seen. Extractors are per-venue (each site has
# its own markup) and return {"description","runtime","director"}.

NYC_META_CACHE_FILE = os.path.join(os.path.dirname(__file__), "nyc_meta_cache.json")
_EMPTY_META = {"description": "", "runtime": "", "director": ""}


def _load_nyc_cache():
    try:
        with open(NYC_META_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_nyc_cache(cache):
    try:
        with open(NYC_META_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=0, sort_keys=True)
    except Exception:
        pass


def _nyc_meta(url, cache, extractor):
    """Cache-aware fetch + extract for a film detail page."""
    if not url or not url.startswith("http"):
        return dict(_EMPTY_META)
    if url in cache:
        return cache[url]
    meta = dict(_EMPTY_META)
    try:
        rr = requests.get(url, headers=HEADERS, timeout=15)
        meta = extractor(rr.text)
    except Exception:
        pass
    cache[url] = meta
    return meta


def _ifc_extract(html):
    """IFC Center — structured ul.film-details rows + prose paragraphs."""
    meta = dict(_EMPTY_META)
    try:
        soup = BeautifulSoup(html, "html.parser")
        fd = soup.select_one("ul.film-details")
        details = {}
        if fd:
            for li in fd.find_all("li"):
                strong = li.find("strong")
                if not strong:
                    continue
                label = re.sub(r"\s+", " ", strong.get_text()).strip()
                value = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
                if value.lower().startswith(label.lower()):
                    value = value[len(label):].strip()
                details[label.lower().rstrip(":")] = value
        meta["director"] = re.sub(r"^(directed by|director)\s*[:\-]?\s*", "",
                                  details.get("director", ""), flags=re.I).strip()
        rt = re.search(r"\d+", details.get("running time", "") or details.get("runtime", ""))
        if rt:
            meta["runtime"] = f"{rt.group(0)} min"
        meta["format"] = _normalize_format(details.get("format", ""))

        drop = re.compile(r"^(screening as part|previously screened|also screening|part of|"
                          r"ifc center does not|buy tickets|get tickets|showtimes|sign up|"
                          r"newsletter|watch the trailer|view the trailer|see all|share this)", re.I)
        event = re.compile(r"^(mon|tues|wednes|thurs|fri|satur|sun)day\b.*\bat\b\s*\d{1,2}", re.I)
        paras = []
        if fd is not None:
            for child in fd.parent.find_all("p", recursive=False):
                if fd in child.find_all_previous("ul") or child.get("class"):
                    continue
                txt = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
                if not txt or drop.match(txt) or event.match(txt):
                    continue
                if len(txt) <= 70 and txt.endswith("!"):
                    continue
                paras.append(txt)
        meta["description"] = " ".join(paras).strip()
    except Exception:
        pass
    return meta


def _ff_extract(html):
    """Film Forum — first <p> inside div.copy (credits-first or synopsis-first)."""
    meta = dict(_EMPTY_META)
    try:
        cm = re.search(r'<div class="copy">', html)
        if not cm:
            return meta
        pm = re.search(r"<p\b[^>]*>(.*?)</p>", html[cm.end():], re.S)
        if not pm:
            return meta
        para_html = pm.group(1)
        para_text = re.sub(r"[ \t]+", " ", html_lib.unescape(
            re.sub(r"<[^>]+>", "", para_html)).replace("\xa0", " ")).strip()

        dm = re.search(r"(?:written\s+and\s+|co-)?directed\s+by\s+(.+)", para_text, re.I)
        if dm:
            d = re.split(r"\s+from\s+|\s*[.(]|,\s|\s+&\s|\s+based\s+on\b", dm.group(1), 1, flags=re.I)[0]
            meta["director"] = d.strip().strip(" .;")
        rm = re.search(r"(\d{1,3})\s*\.?\s*min\b", para_text, re.I)
        if rm:
            meta["runtime"] = f"{rm.group(1)} min"

        # Format lives in the credits <strong> block (e.g. "104 min. 35mm.").
        # Scope to <strong> so synopsis prose can't leak a false match, and
        # default to DCP when unspecified (Film Forum's convention).
        strong_text = re.sub(r"<[^>]+>", " ", " ".join(
            re.findall(r"<strong\b[^>]*>(.*?)</strong>", para_html, re.S | re.I)))
        meta["format"] = _normalize_format(strong_text) or "DCP"

        body = re.sub(r"<strong\b[^>]*>.*?</strong>", "\n", para_html, flags=re.S | re.I)
        body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
        drop = re.compile(r"^(presented\b|restored\b|a\b.*\brestoration\b|restoration\b"
                          r"|in\s+[a-z].*subtitles\b|(mon|tues|wednes|thurs|fri|satur|sun)day\b"
                          r"|open\s+caption\b|a\s+[a-z].*\brelease\b)", re.I)

        def shouty(ln):
            letters = [c for c in ln if c.isalpha()]
            return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6

        prose = []
        for ln in body.split("\n"):
            ln = re.sub(r"[ \t]+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", ln)).replace("\xa0", " ")).strip()
            if len(ln) >= 50 and not drop.match(ln) and not shouty(ln):
                prose.append(ln)
        meta["description"] = " ".join(prose).strip()
    except Exception:
        pass
    return meta


def _low_extract(html):
    """Low Cinema — div.movie-description: "Dir. X, YEAR, COUNTRY, NN min." + prose."""
    meta = dict(_EMPTY_META)
    try:
        m = re.search(r'<div class="movie-description">(.*?)</div>', html, re.S)
        block = m.group(1) if m else ""

        def clean(s):
            return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", s))).strip()

        paras = [p for p in (clean(x) for x in re.findall(r"<p\b[^>]*>(.*?)</p>", block, re.S)) if p]
        credit = re.compile(r"^Dir\.\s*(?P<director>.+?),\s*(?P<year>\d{4})\s*,\s*"
                            r"(?P<country>[^,]+?)\s*,\s*(?P<runtime>\d+)\s*min\b", re.I)
        for p in paras:
            cm = credit.match(p)
            if cm:
                meta["director"] = cm.group("director").strip()
                meta["runtime"] = f'{cm.group("runtime")} min'
                break
        if not meta["runtime"]:
            rm = re.search(r"(\d+)\s*min\b", block)
            if rm:
                meta["runtime"] = f"{rm.group(1)} min"
        for p in paras:
            if credit.match(p) or p.lower().startswith("all sales are final") or len(p) < 60:
                continue
            meta["description"] = p
            break
    except Exception:
        pass
    return meta


def _metrograph_extract(html):
    """Metrograph — div.movie-info: <h5>Director: X</h5>, <h5>YEAR / NNNmin / FMT</h5>, prose <p>."""
    meta = dict(_EMPTY_META)
    try:
        soup = BeautifulSoup(html, "html.parser")
        mi = soup.find("div", class_="movie-info")
        if not mi:
            return meta
        for h5 in mi.find_all("h5"):
            t = re.sub(r"\s+", " ", h5.get_text(" ", strip=True)).strip()
            dm = re.match(r"Directors?\s*:\s*(.+)$", t, re.I)
            if dm and not meta["director"]:
                meta["director"] = re.sub(r"\s+", " ", dm.group(1)).strip()
                continue
            if not t.lower().startswith("director"):
                if not meta["runtime"]:
                    rm = re.search(r"(\d{1,3})\s*min", t, re.I)
                    if rm:
                        meta["runtime"] = f"{rm.group(1)} min"
                if not meta.get("format"):
                    f = _normalize_format(t)
                    if f:
                        meta["format"] = f
        sh = mi.find("div", class_="showtimes")
        if sh:
            sh.decompose()
        paras = []
        for p in mi.find_all("p"):
            if p.find("p") or p.find("a", class_="back-link"):
                continue
            txt = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            if txt and not re.match(r"^(Distributor|Distributed by|Print courtesy)\s*:", txt, re.I):
                paras.append(txt)
        meta["description"] = " ".join(paras).strip()
    except Exception:
        pass
    return meta


_AFA_BLOCK_RE = re.compile(
    r'<div class="film-showing clearfix">.*?'
    r'(?=<div class="film-showing clearfix">|<div class="rule">|$)', re.S)
_AFA_NAME_RE = re.compile(r'name="showing-(\d+)"')


def _afa_blocks(list_html):
    """Map {showing_id: block_html} for one Anthology list-view month page."""
    out = {}
    for m in _AFA_BLOCK_RE.finditer(list_html):
        blk = m.group(0)
        idm = _AFA_NAME_RE.search(blk)
        if idm:
            out[idm.group(1)] = blk
    return out


def _anthology_extract(block_html):
    """One Anthology film-showing block -> {director, runtime, description}."""
    meta = dict(_EMPTY_META)
    try:
        soup = BeautifulSoup(block_html, "html.parser")
        details = soup.find("div", class_="showing-details")
        notes = soup.find("div", class_="film-notes")

        head_text = ""
        if details:
            head = details.decode_contents().split('<span class="share-toggle"')[0].split('<div class="film-notes"')[0]
            hs = BeautifulSoup(head, "html.parser")
            t = hs.find("span", class_="film-title")
            if t:
                t.extract()
            head_text = hs.get_text("\n", strip=True)

        for line in head_text.split("\n"):
            line = line.strip()
            if line.lower().startswith("by "):
                meta["director"] = line[3:].strip(" ,")
                break

        # Specs line carries the format, e.g. "..., 1930, 73 min, 35mm, b&w"
        meta["format"] = _normalize_format(head_text)

        rt = re.search(r"(\d{1,3})\s*min\b", head_text)
        if not rt and notes:
            ntext = notes.get_text(" ", strip=True)
            rt = (re.search(r"[Tt]otal running time:\s*(?:ca\.?\s*)?(\d{1,3})\s*min", ntext)
                  or re.search(r"(\d{1,3})\s*min\b", ntext))
        if rt:
            meta["runtime"] = f"{int(rt.group(1))} min"

        if notes:
            for a in notes.find_all("a"):
                if "buy tickets" in a.get_text(strip=True).lower():
                    a.extract()
            for img in notes.find_all("img"):
                img.extract()
            desc = re.sub(r"\s+", " ", notes.get_text(" ", strip=True)).strip()
            meta["description"] = re.sub(r"\s*CLICK HERE TO BUY TICKETS NOW!?\s*$", "", desc, flags=re.I).strip()
    except Exception:
        pass
    return meta


# ─────────────────────────────────────────────────────────────
# IFC Center
# ─────────────────────────────────────────────────────────────
def scrape_ifc():
    screenings = []
    cache = _load_nyc_cache()
    try:
        r = requests.get("https://www.ifccenter.com/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        for day_key in DAY_ORDER:
            day_div = soup.select_one(f"div.daily-schedule.{day_key}")
            if not day_div:
                continue

            # Header like "Mon Jun 29"
            header = day_div.select_one("h3")
            if not header:
                continue
            date_str = _parse_ifc_date(header.text.strip())
            if not date_str:
                continue

            for film_li in day_div.select("li"):
                title_el = film_li.select_one("div.details h3 a")
                if not title_el:
                    continue
                title = title_el.text.strip()
                film_url = title_el.get("href", "https://www.ifccenter.com/")
                meta = _nyc_meta(film_url, cache, _ifc_extract)

                for time_li in film_li.select("ul.times li a"):
                    time_str = time_li.text.strip()
                    ticket_url = time_li.get("href", film_url)
                    if title and time_str:
                        screenings.append(_make(title, date_str, time_str, meta.get("format", ""), "IFC Center", "IFC", ticket_url, meta))

    except Exception as e:
        print(f"[IFC] Error: {e}")
    _save_nyc_cache(cache)
    return screenings


def _parse_ifc_date(text):
    """Parse 'Mon Jun 29' or 'Fri Jul 4' into YYYY-MM-DD."""
    m = re.search(r"(\w+)\s+(\d{1,2})", text)
    if not m:
        return None
    month_str = m.group(1).lower()[:3]
    day = int(m.group(2))
    month = MONTH_MAP.get(month_str, 0)
    if not month:
        return None
    year = datetime.now().year
    # Handle year rollover
    try:
        dt = datetime(year, month, day)
        if dt < datetime.now() - timedelta(days=1):
            dt = datetime(year + 1, month, day)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
# Film Forum
# ─────────────────────────────────────────────────────────────
def scrape_film_forum():
    screenings = []
    cache = _load_nyc_cache()
    try:
        r = requests.get("https://filmforum.org/films", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Build format lookup from image alt text on the page
        format_by_title = {}
        for img in soup.find_all("img", alt=True):
            alt = img["alt"]
            fmt = _normalize_format(alt)
            if fmt:
                # Try to find the nearest film title
                parent = img.parent
                for _ in range(5):
                    if not parent:
                        break
                    link = parent.find("a", href=re.compile(r"/film/"))
                    if link:
                        format_by_title[link.text.strip().upper()] = fmt
                        break
                    parent = parent.parent

        # Tab → day offset from Monday (tabs-0=Mon, tabs-1=Tue, ... tabs-6=Sun)
        TAB_DAY_OFFSET = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}  # Mon=0...Sun=6
        # Find Monday date from the week shown (look for comment <!-- N --> in tabs-0)
        monday_date = None
        tabs_container = soup.select_one(".showtimes-container")
        if tabs_container:
            tab0 = tabs_container.select_one("#tabs-0")
            if tab0:
                # HTML comment contains the day number
                for item in tab0.contents:
                    if isinstance(item, Comment):
                        try:
                            day_num = int(str(item).strip())
                            monday_date = _resolve_film_forum_date(soup, day_num, 0)
                            break
                        except ValueError:
                            pass

        if not monday_date:
            # Fallback: find next Monday from today
            today = datetime.now()
            days_until_monday = (7 - today.weekday()) % 7
            if days_until_monday == 0 and today.weekday() != 0:
                days_until_monday = 7
            monday_date = (today - timedelta(days=today.weekday())).date()

        if tabs_container:
            for tab_idx in range(7):
                tab = tabs_container.select_one(f"#tabs-{tab_idx}")
                if not tab:
                    continue

                # Calculate the actual date for this tab
                # tabs-0=Mon, offset 0; tabs-6=Sun, offset 6
                tab_date = datetime.combine(monday_date, datetime.min.time()) + timedelta(days=tab_idx)
                date_str = tab_date.strftime("%Y-%m-%d")

                # Each film is a <p> with <strong><a>Title</a></strong><br/><span>HH:MM</span>...
                for p in tab.select("p"):
                    title_el = p.select_one("strong a")
                    if not title_el:
                        continue
                    title = title_el.get_text(" ", strip=True)
                    film_url = title_el.get("href", "https://filmforum.org/films")
                    if not film_url.startswith("http"):
                        film_url = "https://filmforum.org" + film_url

                    fmt = format_by_title.get(title.upper(), _normalize_format(p.get_text()))
                    meta = _nyc_meta(film_url, cache, _ff_extract)

                    for span in p.select("span"):
                        raw_time = span.text.strip()  # "12:20" or "7:00"
                        time_str = _ff_time_to_ampm(raw_time)
                        if time_str:
                            screenings.append(_make(title, date_str, time_str, meta.get("format") or fmt, "Film Forum", "FilmForum", film_url, meta))

    except Exception as e:
        print(f"[FilmForum] Error: {e}")
    _save_nyc_cache(cache)
    return screenings


def _resolve_film_forum_date(soup, day_num, tab_idx):
    """Given the day number from a tab's HTML comment, find the full date."""
    # Look for week range like "Friday, June 26 – Thursday, July 2"
    text = soup.get_text(" ", strip=True)
    # Find something like "June 26" near this day_num
    year = datetime.now().year
    # Try to find the month by looking at surrounding context
    # tabs-0 = Mon, so find a Monday with day_num in the current or next week
    today = datetime.now()
    # Start from last Monday
    last_monday = today - timedelta(days=today.weekday())
    for week_offset in range(-1, 3):
        candidate = last_monday + timedelta(weeks=week_offset, days=tab_idx)
        if candidate.day == day_num:
            return candidate.date()
    return last_monday.date()


def _ff_time_to_ampm(raw):
    """Convert Film Forum time "12:20" or "7:00" to "12:20 PM" / "7:00 PM"."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if not m:
        return ""
    hour, minute = int(m.group(1)), m.group(2)
    # Film Forum convention: 10 and 11 = AM; everything else = PM
    if hour in (10, 11):
        ampm = "AM"
    else:
        ampm = "PM"
    return f"{hour}:{minute} {ampm}"


# ─────────────────────────────────────────────────────────────
# Anthology Film Archives
# ─────────────────────────────────────────────────────────────
def scrape_anthology():
    screenings = []
    try:
        today = datetime.now()
        months_to_scrape = [(today.year, today.month)]
        # Add next 2 months
        for i in range(1, 3):
            m = today.month + i
            y = today.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            months_to_scrape.append((y, m))

        for year, month in months_to_scrape:
            url = f"https://www.anthologyfilmarchives.org/film_screenings/calendar?month={month:02d}&year={year}"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if not r.ok:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            # The list view carries the metadata blocks (synopsis/credits/runtime),
            # keyed by the same showing id as the grid's <li>. Fetch once per month.
            blocks = {}
            try:
                lv = requests.get(
                    f"https://www.anthologyfilmarchives.org/film_screenings/calendar?view=list&month={month:02d}&year={year}",
                    headers=HEADERS, timeout=15)
                if lv.ok:
                    blocks = _afa_blocks(lv.text)
            except Exception:
                pass

            for day_td in soup.select("td.calendar_day[name]"):
                day_num = int(day_td["name"])
                try:
                    date_obj = datetime(year, month, day_num)
                except ValueError:
                    continue
                if date_obj.date() < today.date():
                    continue
                date_str = date_obj.strftime("%Y-%m-%d")

                for ev in day_td.select("li.calendar_event"):
                    ev_text = ev.get_text(" ", strip=True)
                    # Format: "7:00 PM TITLE OF FILM"
                    time_match = re.match(r"(\d{1,2}:\d{2}\s*[APap][Mm])\s+(.+)", ev_text)
                    if not time_match:
                        continue
                    time_str = time_match.group(1).strip()
                    title = time_match.group(2).strip()
                    # Clean up title (remove trailing punctuation)
                    title = re.sub(r"\s+", " ", title).strip()

                    link_el = ev.select_one("a[href]")
                    ev_url = "https://www.anthologyfilmarchives.org" + link_el["href"] \
                        if link_el and link_el["href"].startswith("/") \
                        else "https://www.anthologyfilmarchives.org/film_screenings"

                    sid = ev.get("id")
                    meta = _anthology_extract(blocks[sid]) if sid and sid in blocks else None
                    screenings.append(_make(title, date_str, time_str, (meta.get("format", "") if meta else ""), "Anthology Film Archives", "Anthology", ev_url, meta))

            time.sleep(0.5)

    except Exception as e:
        print(f"[Anthology] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Low Cinema
# ─────────────────────────────────────────────────────────────
def scrape_low_cinema():
    screenings = []
    cache = _load_nyc_cache()
    try:
        r = requests.get("https://lowcinema.com/", headers=HEADERS, timeout=15)
        m = re.search(r"window\.showingsData\s*=\s*(\{.*?\});", r.text, re.DOTALL)
        if not m:
            return screenings

        data = json.loads(m.group(1))
        today_str = datetime.now().strftime("%Y-%m-%d")

        for date_str, shows in data.items():
            if date_str < today_str:
                continue
            for show in shows:
                title = show.get("movie", "").strip()
                time_str = show.get("time", "").strip()
                ticket_url = show.get("url", "")
                if ticket_url and not ticket_url.startswith("http"):
                    ticket_url = "https://lowcinema.com" + ticket_url
                movie_url = show.get("movie_url", "")
                if movie_url and not movie_url.startswith("http"):
                    movie_url = "https://lowcinema.com" + movie_url
                url = ticket_url or movie_url or "https://lowcinema.com/"

                if title:
                    meta = _nyc_meta(movie_url, cache, _low_extract)
                    screenings.append(_make(title, date_str, time_str, "", "Low Cinema", "LowCinema", url, meta))

    except Exception as e:
        print(f"[LowCinema] Error: {e}")
    _save_nyc_cache(cache)
    return screenings


# ─────────────────────────────────────────────────────────────
# Metrograph — homepage calendar (all dates/times in static HTML)
# ─────────────────────────────────────────────────────────────
def scrape_metrograph():
    screenings = []
    cache = _load_nyc_cache()
    try:
        r = requests.get("https://metrograph.com/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        today = datetime.now().date()

        for day_div in soup.select("div.calendar-list-day[id]"):
            day_id = day_div.get("id", "")
            # id="calendar-list-day-2026-07-01"
            date_m = re.search(r"(\d{4}-\d{2}-\d{2})$", day_id)
            if not date_m:
                continue
            date_str = date_m.group(1)
            if date_str < str(today):
                continue

            for item in day_div.select("div.item.film-thumbnail"):
                title_a = item.select_one("h4 a.title")
                if not title_a:
                    continue
                title = title_a.get_text(" ", strip=True)
                film_href = title_a.get("href", "")
                if film_href and not film_href.startswith("http"):
                    film_href = "https://metrograph.com" + film_href

                # format sometimes listed in a subtitle/h5 below the title
                fmt = ""
                for sub in item.select("h5, .subtitle, .format, .note"):
                    f = _normalize_format(sub.get_text())
                    if f:
                        fmt = f
                        break

                meta = _nyc_meta(film_href, cache, _metrograph_extract)

                for time_a in item.select("div.showtimes a"):
                    raw_time = time_a.get_text(strip=True)  # e.g. "3:15pm"
                    if not raw_time:
                        continue
                    time_str = _normalize_12h(raw_time)
                    ticket_url = time_a.get("href") or film_href or "https://metrograph.com/"
                    if ticket_url.startswith("/"):
                        ticket_url = "https://metrograph.com" + ticket_url
                    screenings.append(_make(title, date_str, time_str, meta.get("format") or fmt, "Metrograph", "Metrograph", ticket_url, meta))

    except Exception as e:
        print(f"[Metrograph] Error: {e}")
    _save_nyc_cache(cache)
    return screenings


def _normalize_12h(raw):
    """Convert '3:15pm' / '10:00am' to '3:15 PM' / '10:00 AM'."""
    m = re.match(r"(\d{1,2}:\d{2})\s*([aApP][mM])", raw.strip())
    if m:
        return f"{m.group(1)} {m.group(2).upper()}"
    return raw.strip()


# ─────────────────────────────────────────────────────────────
# Film at Lincoln Center — Playwright + API interception
# ─────────────────────────────────────────────────────────────
def scrape_film_linc():
    screenings = []
    try:
        api_data = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()

            def handle_response(resp):
                if "filmlinc" in resp.url and "json" in resp.headers.get("content-type", ""):
                    try:
                        api_data.append({"url": resp.url, "body": resp.json()})
                    except Exception:
                        pass

            page.on("response", handle_response)
            page.goto("https://www.filmlinc.org/calendar/", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        # Try to extract from intercepted API calls first
        for call in api_data:
            extracted = _parse_filmlinc_api(call["body"])
            screenings.extend(extracted)

        if not screenings:
            # Fall back to parsing rendered HTML
            soup = BeautifulSoup(html, "html.parser")
            screenings = _parse_filmlinc_html(soup)

    except Exception as e:
        print(f"[FilmLinc] Error: {e}")
    return screenings


def _parse_filmlinc_api(data):
    screenings = []
    try:
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            for k in ("events", "data", "results", "items"):
                if isinstance(data.get(k), list):
                    events = data[k]
                    break

        today = datetime.now().date()
        for ev in events:
            title = ev.get("title") or ev.get("name") or ""
            if not title:
                continue
            date_str = ""
            for key in ("start_date", "start", "date", "startDate"):
                if ev.get(key):
                    raw = str(ev[key])[:10]
                    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                        date_str = raw
                        break
            if not date_str:
                continue
            if date_str < str(today):
                continue

            time_str = ev.get("start_time") or ev.get("time") or ""
            fmt = _normalize_format(str(ev))
            url = ev.get("url") or ev.get("link") or "https://www.filmlinc.org/calendar/"

            screenings.append(_make(title, date_str, time_str, fmt, "Film at Lincoln Center", "FilmLinc", url))
    except Exception:
        pass
    return screenings


def _parse_filmlinc_html(soup):
    screenings = []
    today = datetime.now().date()
    year = datetime.now().year

    # Try various event card selectors
    for sel in ["article", "[class*='event']", "[class*='Event']", ".program-item", "li.event"]:
        cards = soup.select(sel)
        if len(cards) > 3:
            for card in cards:
                try:
                    text = card.get_text(" ", strip=True)
                    if len(text) < 15:
                        continue
                    title_el = card.select_one("h1,h2,h3,h4,[class*='title'],[class*='Title']")
                    if not title_el:
                        continue
                    title = title_el.text.strip()
                    if len(title) < 3:
                        continue

                    date_m = re.search(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?",
                        text, re.I
                    )
                    if not date_m:
                        continue
                    month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                    day = int(date_m.group(2))
                    yr = int(date_m.group(3)) if date_m.group(3) else year
                    dt = datetime(yr, month, day)
                    if dt.date() < today:
                        continue

                    time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                    time_str = time_m.group(0).strip() if time_m else ""
                    fmt = _normalize_format(text)
                    link = card.select_one("a[href]")
                    url = link["href"] if link else "https://www.filmlinc.org/calendar/"
                    if url.startswith("/"):
                        url = "https://www.filmlinc.org" + url

                    screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt,
                                            "Film at Lincoln Center", "FilmLinc", url))
                except Exception:
                    continue
            break

    return screenings


# ─────────────────────────────────────────────────────────────
# MoMA — try their API, fall back to Playwright
# ─────────────────────────────────────────────────────────────
def scrape_moma():
    screenings = []
    try:
        # MoMA has an events API
        today = datetime.now()
        from_str = today.strftime("%Y-%m-%d")
        to_str = (today + timedelta(days=90)).strftime("%Y-%m-%d")

        for api_url in [
            f"https://www.moma.org/api/calendar/events?type=film&from={from_str}&to={to_str}&per_page=200",
            f"https://www.moma.org/calendar/events.json?type=film&start_date={from_str}&end_date={to_str}",
            "https://www.moma.org/api/v1/events?category=film&limit=200",
        ]:
            r = requests.get(api_url, headers=HEADERS, timeout=12)
            if r.ok and "json" in r.headers.get("content-type", ""):
                data = r.json()
                if data:
                    events = data if isinstance(data, list) else data.get("events") or data.get("data") or []
                    screenings = _parse_moma_events(events)
                    if screenings:
                        break

        if not screenings:
            # Try Playwright
            screenings = _scrape_moma_playwright()

    except Exception as e:
        print(f"[MoMA] Error: {e}")
    return screenings


def _parse_moma_events(events):
    screenings = []
    today = datetime.now().date()
    for ev in events:
        try:
            title = ev.get("title") or ev.get("name") or ""
            if not title:
                continue
            date_str = ""
            for key in ("start_date", "date", "start", "startDate", "event_date"):
                v = ev.get(key, "")
                if v and re.match(r"\d{4}-\d{2}-\d{2}", str(v)[:10]):
                    date_str = str(v)[:10]
                    break
            if not date_str or date_str < str(today):
                continue
            time_str = ev.get("start_time") or ev.get("time") or ""
            fmt = _normalize_format(str(ev))
            url = ev.get("url") or ev.get("link") or "https://www.moma.org/calendar/?filters[0][value]=Film"
            screenings.append(_make(title, date_str, time_str, fmt, "MoMA", "MoMA", url))
        except Exception:
            continue
    return screenings


def _scrape_moma_playwright():
    screenings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(
                "https://www.moma.org/calendar/?filters[0][value]=Film",
                wait_until="networkidle", timeout=30000
            )
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        today = datetime.now().date()
        year = today.year

        for card in soup.select("article, [class*='event'], [class*='Event'], li.grid-item"):
            try:
                text = card.get_text(" ", strip=True)
                title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='Title']")
                if not title_el:
                    continue
                title = title_el.text.strip()
                if len(title) < 3:
                    continue
                date_m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?", text, re.I)
                if not date_m:
                    continue
                month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                day = int(date_m.group(2))
                yr = int(date_m.group(3)) if date_m.group(3) else year
                dt = datetime(yr, month, day)
                if dt.date() < today:
                    continue
                time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                time_str = time_m.group(0).strip() if time_m else ""
                fmt = _normalize_format(text)
                link = card.select_one("a[href]")
                url = link["href"] if link else "https://www.moma.org/calendar/?filters[0][value]=Film"
                if url.startswith("/"):
                    url = "https://www.moma.org" + url
                screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt, "MoMA", "MoMA", url))
            except Exception:
                continue
    except Exception as e:
        print(f"[MoMA/Playwright] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Museum of the Moving Image — Playwright
# ─────────────────────────────────────────────────────────────
def scrape_momi():
    screenings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto("https://movingimage.us/program/", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        today = datetime.now().date()
        year = today.year

        for card in soup.select("article, [class*='event'], [class*='program'], [class*='film']"):
            try:
                text = card.get_text(" ", strip=True)
                title_el = card.select_one("h1,h2,h3,h4,[class*='title']")
                if not title_el:
                    continue
                title = title_el.text.strip()
                if len(title) < 3:
                    continue
                date_m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?", text, re.I)
                if not date_m:
                    continue
                month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                day = int(date_m.group(2))
                yr = int(date_m.group(3)) if date_m.group(3) else year
                dt = datetime(yr, month, day)
                if dt.date() < today:
                    continue
                time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                time_str = time_m.group(0).strip() if time_m else ""
                fmt = _normalize_format(text)
                link = card.select_one("a[href]")
                url = link["href"] if link else "https://movingimage.us/program/"
                if url.startswith("/"):
                    url = "https://movingimage.us" + url
                screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt,
                                        "Museum of Moving Image", "MoMI", url))
            except Exception:
                continue
    except Exception as e:
        print(f"[MoMI] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def scrape_all_nyc():
    results = {}

    print("  Scraping IFC Center...")
    results["IFC"] = scrape_ifc()
    print(f"    → {len(results['IFC'])} screenings")

    print("  Scraping Film Forum...")
    results["FilmForum"] = scrape_film_forum()
    print(f"    → {len(results['FilmForum'])} screenings")

    print("  Scraping Anthology Film Archives...")
    results["Anthology"] = scrape_anthology()
    print(f"    → {len(results['Anthology'])} screenings")

    print("  Scraping Low Cinema...")
    results["LowCinema"] = scrape_low_cinema()
    print(f"    → {len(results['LowCinema'])} screenings")

    print("  Scraping Metrograph (Playwright)...")
    results["Metrograph"] = scrape_metrograph()
    print(f"    → {len(results['Metrograph'])} screenings")

    print("  Scraping Film at Lincoln Center (Playwright)...")
    results["FilmLinc"] = scrape_film_linc()
    print(f"    → {len(results['FilmLinc'])} screenings")

    print("  Scraping MoMA...")
    results["MoMA"] = scrape_moma()
    print(f"    → {len(results['MoMA'])} screenings")

    print("  Scraping Museum of Moving Image (Playwright)...")
    results["MoMI"] = scrape_momi()
    print(f"    → {len(results['MoMI'])} screenings")

    all_screenings = []
    for v in results.values():
        all_screenings.extend(v)

    return all_screenings


if __name__ == "__main__":
    screenings = scrape_all_nyc()
    print(f"\nNYC total: {len(screenings)}")
    from collections import Counter
    vc = Counter(s["venue_short"] for s in screenings)
    for v, c in sorted(vc.items()):
        print(f"  {v}: {c}")
