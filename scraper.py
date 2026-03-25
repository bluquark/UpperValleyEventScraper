#!/usr/bin/env python3
"""
Upper Valley Events Scraper
Fetches 30 days of events from:
  - home.dartmouth.edu/events
  - nhhumanities.org/programs/upcoming
  - northernstage.org
  - shakerbridgetheatre.org
Produces a self-contained HTML page with color-coded, filterable events.
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, quote_plus

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except ImportError:
    EASTERN = timezone(timedelta(hours=-4))

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DARTMOUTH_BASE = "https://home.dartmouth.edu"
DARTMOUTH_AJAX_URL = f"{DARTMOUTH_BASE}/events/ajax/search"
DARTMOUTH_DETAIL_URL = f"{DARTMOUTH_BASE}/events/event"

NHH_BASE = "https://www.nhhumanities.org"
NHH_LIST_URL = f"{NHH_BASE}/programs/upcoming"

NORTHERNSTAGE_TICKET_SITE = "https://northernstage.my.salesforce-sites.com/ticket"
NORTHERNSTAGE_APEXREMOTE = f"{NORTHERNSTAGE_TICKET_SITE}/apexremote"
NS_SKIP_NAMES = re.compile(
    r'\bCAMPS\b|Education Classes|Membership|Subscription|Digital Download|Physical Album',
    re.IGNORECASE
)

SHBT_BASE = "https://www.shakerbridgetheatre.org"
SHBT_TICKETS_URL = "https://app.arts-people.com/index.php?ticketing=sbt"

NUGGET_BASE = "https://www.nugget-theaters.com"
NUGGET_TICKETS_URL = "http://28879.formovietickets.com:2235"

LEBANON6_BASE = "https://www.entertainmentcinemas.com"
LEBANON6_URL = f"{LEBANON6_BASE}/lebanon-6"

MOVIE_FETCH_DAYS = 7

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

UNIMPORTANT_KEYWORDS = re.compile(r'\b(seminar|colloquium|thesis|dissertation)\b', re.IGNORECASE)

# Towns within ~30 minutes drive of Lebanon NH (used to flag far-away NH Humanities events)
NHH_NEARBY_TOWNS = {
    "Lebanon", "West Lebanon", "Hanover", "Enfield", "Canaan", "Lyme",
    "Plainfield", "Meriden", "Cornish", "Grantham", "Orford",
    "White River Junction", "Hartford", "Wilder", "Norwich", "Thetford",
    "Sharon", "Quechee", "Hartland", "Windsor", "Claremont",
}

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

THEATER_SOURCES = ["northernstage", "shakerbridgetheatre"]
MOVIE_SOURCES = ["nugget", "lebanon6"]
ALL_SOURCES = ["dartmouth", "nhhumanities"] + THEATER_SOURCES + MOVIE_SOURCES
SOURCE_GROUPS = {
    "all": ALL_SOURCES,
    "theater": THEATER_SOURCES,
    "movies": MOVIE_SOURCES,
}

INTERMEDIATE_FILES = {s: os.path.join("output", f"scraped_{s}.json") for s in ALL_SOURCES}


# ---------------------------------------------------------------------------
# Dartmouth scraping
# ---------------------------------------------------------------------------

def fetch_event_list(start: date, end: date) -> str:
    params = {"offset": 0, "limit": 300, "begin": start.isoformat(), "end": end.isoformat()}
    print(f"Fetching Dartmouth event list ({start} to {end})...")
    resp = requests.get(DARTMOUTH_AJAX_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    for cmd in data:
        if cmd.get("command") == "eventsContent":
            return cmd["content"]
    raise ValueError("No eventsContent command found in API response")


def parse_event_list(html: str, ref_year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for teaser in soup.find_all("div", class_="event-teaser"):
        title_link = teaser.select_one(".event-teaser__title-link")
        if not title_link:
            continue
        href = title_link.get("href", "")
        m = re.search(r'event=(\d+)', href)
        if not m:
            continue
        event_id = m.group(1)
        day_el = teaser.select_one(".event-teaser__date-day")
        month_el = teaser.select_one(".event-teaser__date-month")
        day = int(day_el.get_text(strip=True)) if day_el else 0
        month_str = month_el.get_text(strip=True) if month_el else ""
        month = MONTH_MAP.get(month_str, 0)
        year = ref_year
        if month < date.today().month - 1:
            year = ref_year + 1
        time_el = teaser.select_one(".event-teaser__time")
        time_str = time_el.get_text(strip=True) if time_el else ""
        summary_el = teaser.select_one(".event-teaser__summary")
        summary = summary_el.get_text(strip=True) if summary_el else ""
        events.append({
            "id": event_id,
            "title": title_link.get_text(strip=True),
            "date": date(year, month, day) if month and day else None,
            "time_str": time_str,
            "summary": summary,
            "source": "dartmouth",
        })
    return events


def fetch_detail(event_id: str) -> tuple[str, str | None]:
    try:
        time.sleep(0.05)
        resp = requests.get(DARTMOUTH_DETAIL_URL, params={"event": event_id}, timeout=30)
        if resp.status_code == 200:
            return event_id, resp.text
    except Exception as e:
        print(f"  Warning: failed to fetch event {event_id}: {e}", file=sys.stderr)
    return event_id, None


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    ld_tag = soup.find("script", type="application/ld+json")
    ld = {}
    if ld_tag and ld_tag.string:
        try:
            ld = json.loads(ld_tag.string)
        except json.JSONDecodeError:
            pass
    meta_items = soup.select(".news-event--meta__item--text")
    time_str = ""
    if len(meta_items) >= 2:
        time_str = meta_items[1].get_text(strip=True)
        if time_str == "Add to Calendar":
            time_str = ""
    contact_el = soup.select_one(".news-event--details__group--contact .news-event--details__group-text")
    contact = contact_el.get_text(strip=True) if contact_el else ""
    body_el = soup.select_one(".news-event--body")
    body_html = ""
    if body_el:
        for a in body_el.find_all("a", href=True):
            if a["href"].startswith("/"):
                a["href"] = DARTMOUTH_BASE + a["href"]
        body_html = str(body_el.decode_contents())
    start_iso = ld.get("startDate", "")
    start_dt_utc = None
    if start_iso:
        try:
            clean = re.sub(r'\.\d+', '', start_iso)
            dt = datetime.fromisoformat(clean)
            start_dt_utc = dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
    return {
        "title": ld.get("name", ""),
        "about": ld.get("about", ""),
        "description": body_html or ld.get("description", ""),
        "start_iso": start_iso,
        "start_dt": start_dt_utc,
        "location": (ld.get("location") or {}).get("name", ""),
        "audience": ld.get("audience", ""),
        "sponsor": ld.get("funder", ""),
        "duration": ld.get("duration", ""),
        "image": (ld.get("image") or [None])[0],
        "url": ld.get("url", ""),
        "time_str": time_str,
        "contact": contact,
    }


# ---------------------------------------------------------------------------
# NH Humanities scraping
# ---------------------------------------------------------------------------

def _nhh_parse_page(soup: BeautifulSoup) -> list[dict]:
    events = []
    for ev_div in soup.find_all("div", class_="event"):
        title_link = ev_div.find("a", class_="title")
        if not title_link:
            continue
        title = title_link.get_text(strip=True)
        href = title_link.get("href", "")
        url = (NHH_BASE + href) if href.startswith("/") else href
        date_div = ev_div.find("div", class_="date")
        date_text = location = ""
        if date_div:
            parts = [p.strip() for p in date_div.get_text("\n").split("\n") if p.strip()]
            if parts:
                date_text = parts[0]
            if len(parts) >= 2:
                location = parts[1]
        img_el = ev_div.find("img", class_="thumb")
        image = img_el.get("src", "") if img_el else ""
        if image and image.startswith("/"):
            image = NHH_BASE + image
        virtual_el = ev_div.find("div", class_="virtual")
        virtual = virtual_el.get_text(strip=True) if virtual_el else ""
        ev_date = None
        if date_text:
            try:
                ev_date = datetime.strptime(date_text, "%A, %B %d, %Y").date()
            except ValueError:
                pass
        events.append({
            "id": url, "title": title, "url": url, "date": ev_date,
            "location": location, "image": image, "virtual": virtual,
            "source": "nhhumanities",
        })
    return events


def fetch_nhh_event_list(end: date) -> list[dict]:
    print("Fetching NH Humanities event list...")
    all_events: list[dict] = []
    seen_urls: set[str] = set()
    page = 0
    while True:
        url = NHH_LIST_URL if page == 0 else f"{NHH_LIST_URL}?page={page}"
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        page_events = _nhh_parse_page(soup)
        new_events = [e for e in page_events if e["url"] not in seen_urls]
        if not new_events:
            break
        for e in new_events:
            seen_urls.add(e["url"])
        all_events.extend(new_events)
        # Stop if all events on this page are past our end date
        dated = [e["date"] for e in new_events if e.get("date")]
        if dated and min(dated) > end:
            break
        page += 1
    return all_events


def fetch_nhh_detail(url: str) -> tuple[str, str | None]:
    try:
        time.sleep(0.05)
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        if resp.status_code == 200:
            return url, resp.text
    except Exception as e:
        print(f"  Warning: failed to fetch {url}: {e}", file=sys.stderr)
    return url, None


def parse_nhh_detail(html: str, event_date: date | None) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    cat_el = soup.find("div", class_="eventCategory")
    category = cat_el.get_text(strip=True) if cat_el else ""
    presenter = ""
    for p in soup.find_all("p"):
        txt = p.get_text()
        if "Presenter:" in txt:
            a = p.find("a")
            presenter = a.get_text(strip=True) if a else re.sub(r'Presenter:\s*', '', txt).strip()
            break
    desc_el = soup.find("div", class_="eventDescription")
    description = ""
    if desc_el:
        for a in desc_el.find_all("a", href=True):
            if a["href"].startswith("/"):
                a["href"] = NHH_BASE + a["href"]
        description = str(desc_el.decode_contents())
    time_str = when_full = address = hosted_by = contact = ""
    details_el = soup.find("div", class_="eventDetails")
    if details_el:
        for mb in details_el.find_all("div", class_="mb25"):
            strong = mb.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True).rstrip(":")
            paras = mb.find_all("p")
            if len(paras) < 2:
                continue
            val = paras[1].get_text(strip=True)
            if label == "When":
                when_full = val
            elif label == "Where":
                address = paras[1].get_text("\n", strip=True)
            elif label == "Hosted By":
                hosted_by = val
            elif label == "Contact Info":
                contact = paras[1].get_text(", ", strip=True)
    start_dt_utc = None
    if when_full:
        m = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', when_full, re.IGNORECASE)
        if m:
            time_str = m.group(1).strip()
            try:
                d = event_date or datetime.strptime(
                    when_full.split(time_str)[0].strip(), "%A, %B %d, %Y").date()
                tm = re.match(r'(\d{1,2}):(\d{2})\s*([ap]m)', time_str, re.IGNORECASE)
                if tm:
                    h, mn = int(tm.group(1)), int(tm.group(2))
                    if tm.group(3).lower() == "pm" and h != 12:
                        h += 12
                    if tm.group(3).lower() == "am" and h == 12:
                        h = 0
                    dt_local = datetime(d.year, d.month, d.day, h, mn, tzinfo=EASTERN)
                    start_dt_utc = dt_local.astimezone(timezone.utc)
            except (ValueError, AttributeError):
                pass
    og_img = soup.find("meta", property="og:image")
    image = og_img.get("content", "") if og_img else ""
    return {
        "title": title, "category": category, "presenter": presenter,
        "description": description, "time_str": time_str, "address": address,
        "hosted_by": hosted_by, "contact": contact, "image": image,
        "start_dt": start_dt_utc,
    }


# ---------------------------------------------------------------------------
# Northern Stage scraping
# ---------------------------------------------------------------------------

def _ns_fmt_date_range(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    return f"{start.strftime('%b %-d, %Y')} – {end.strftime('%b %-d, %Y')}"


def _theater_show_to_event(show: dict, today: date) -> dict:
    """Convert a theater production dict to a standard event dict."""
    start_date = show["start_date"]
    end_date = show["end_date"]
    run_days = (end_date - start_date).days + 1
    return {
        "id": show["url"],
        "title": show["title"],
        "source": show["source"],
        # Sort by opening night (or today if already running)
        "date": max(start_date, today),
        "dates": [max(start_date, today)],
        "time_str": show["date_range_str"],
        "location": show.get("location", ""),
        "description": show.get("description", ""),
        "image": show.get("image", ""),
        "url": show["url"],
        "start_dt": None,  # no specific time → all-day GCal event
        "duration": f"{run_days} days",
        "unimportant": False,
    }


# ---------------------------------------------------------------------------
# Shaker Bridge Theatre scraping
# ---------------------------------------------------------------------------

_SHBT_FULL_DATE_RE = re.compile(
    r'(\w+)\s+(\d{1,2})\s*[-\u2013\xa0]+\s*(?:(\w+)\s+)?(\d{1,2}),?\s+(\d{4})'
)


def _shbt_parse_date_range(text: str) -> tuple[date, date] | None:
    """Parse 'March 26 - April 12, 2026' or 'May 7 - 24, 2026'."""
    text = text.replace('\xa0', ' ').strip()
    m = _SHBT_FULL_DATE_RE.match(text)
    if not m:
        return None
    start_month_s, start_day_s, end_month_s, end_day_s, year_s = m.groups()
    end_month_s = end_month_s or start_month_s
    try:
        start_d = datetime.strptime(f"{start_month_s} {start_day_s} {year_s}", '%B %d %Y').date()
        end_d = datetime.strptime(f"{end_month_s} {end_day_s} {year_s}", '%B %d %Y').date()
        return start_d, end_d
    except ValueError:
        return None


def _shbt_parse_ticketing_page(html: str) -> list[dict]:
    """Parse the arts-people ticketing page for Shaker Bridge Theatre."""
    soup = BeautifulSoup(html, "html.parser")
    shows = []
    current: dict | None = None
    desc_lines: list[str] = []
    in_special = False

    ARTS_BASE = "https://app.arts-people.com"

    def flush():
        nonlocal current, desc_lines, in_special
        if current:
            current["description"] = "\n".join(f"<p>{l}</p>" for l in desc_lines if l)
            shows.append(current)
        current = None
        desc_lines = []
        in_special = False

    for tag in soup.body.descendants:
        if not hasattr(tag, 'name'):
            continue
        if tag.name == 'img':
            src = tag.get('src', '')
            if '/uploads/' in src and 'logo' not in src.lower():
                flush()
                current = {'image': ARTS_BASE + src if src.startswith('/') else src,
                           'url': SHBT_TICKETS_URL, 'location': 'Briggs Opera House, West Lebanon',
                           'source': 'shakerbridgetheatre'}
                in_special = False
        elif tag.name == 'strong' and current is not None:
            t = tag.get_text(strip=True)
            if not t:
                continue
            if 'title' not in current:
                current['title'] = t
            elif 'author' not in current and t.lower().startswith('by '):
                current['author'] = t
            elif 'start_date' not in current:
                dates = _shbt_parse_date_range(t)
                if dates:
                    current['start_date'], current['end_date'] = dates
                    current['date_range_str'] = _ns_fmt_date_range(*dates)
            elif t.upper() == 'SPECIAL EVENTS':
                in_special = True
            elif t.upper() in ('TICKET PRICING',):
                flush()
        elif tag.name == 'p' and current is not None and not in_special:
            t = tag.get_text(strip=True)
            # Only direct <p> children (not nested inside <strong>)
            if t and not tag.find('strong'):
                desc_lines.append(t)
        elif tag.name == 'a' and current is not None:
            href = tag.get('href', '')
            if 'show=' in href:
                url = ARTS_BASE + href if href.startswith('/') else href
                current['url'] = url

    flush()
    return [s for s in shows if 'title' in s and 'start_date' in s]


# ---------------------------------------------------------------------------
# Movie scraping — Nugget Theaters & Lebanon 6
# ---------------------------------------------------------------------------

def _movie_event(title: str, ev_date: date, time_str: str, image: str,
                 url: str, rating: str, runtime: str, description: str,
                 location: str, source: str) -> dict:
    desc_parts = []
    if rating:
        desc_parts.append(f"Rated {rating}")
    if runtime:
        desc_parts.append(runtime)
    full_desc = (f"<p class='ev-movie-meta'>{'  ·  '.join(desc_parts)}</p>\n" if desc_parts else "") + description
    return {
        "id": f"{source}_{ev_date.isoformat()}_{title[:30]}",
        "title": title,
        "source": source,
        "date": ev_date,
        "dates": [ev_date],
        "time_str": time_str,
        "location": location,
        "description": full_desc,
        "image": image,
        "url": url,
        "start_dt": None,
        "unimportant": False,
    }


def _nugget_fetch_day(day: date) -> list[dict]:
    if day == date.today():
        url = NUGGET_BASE + "/"
    else:
        url = f"{NUGGET_BASE}/?moviedate={day.isoformat()}"
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Nugget {day}: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    events = []
    for card in soup.find_all("div", class_="movie-now-single"):
        title_a = card.select_one(".movie-now-title a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        movie_url = title_a.get("href", "") or NUGGET_TICKETS_URL

        playing_el = card.select_one(".movie-now-playing-today")
        if not playing_el:
            continue  # not showing today
        playing_text = playing_el.get_text(strip=True)
        m = re.search(r'\bat\s+(.+)$', playing_text, re.IGNORECASE)
        time_str = m.group(1).replace("|", "·").strip() if m else playing_text

        img = card.select_one(".movie-now-thumb")
        image = img.get("src", "") if img else ""

        rating_el = card.select_one(".movie-now-rating")
        rating = rating_el.get_text(strip=True) if rating_el else ""

        runtime_el = card.select_one(".movie-now-time")
        runtime = runtime_el.get_text(strip=True).replace("Running time ", "") if runtime_el else ""

        desc_el = card.select_one(".movie-now-description")
        description = str(desc_el.decode_contents()).strip() if desc_el else ""

        events.append(_movie_event(title, day, time_str, image, movie_url,
                                   rating, runtime, description,
                                   "Nugget Theater, Hanover", "nugget"))
    return events


def _lebanon6_fetch_day(day: date) -> list[dict]:
    url = LEBANON6_URL if day == date.today() else f"{LEBANON6_URL}?date={day.isoformat()}"
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Lebanon 6 {day}: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    events = []
    for card in soup.find_all("div", class_="cin-movie-card"):
        h3 = card.find("h3")
        if not h3:
            continue
        title_a = h3.find("a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        movie_url = (LEBANON6_BASE + href) if href.startswith("/") else href

        # Poster image
        img = card.find("img")
        image = img.get("src", "") if img else ""

        # Rating (div with bg-black class) and runtime (text-xs uppercase sibling)
        body = card.find("div", class_="cin-showtimes-body-container")
        rating = runtime = ""
        if body:
            rating_div = body.find("div", class_=re.compile(r'\bbg-black\b'))
            if rating_div:
                rating = rating_div.get_text(strip=True)
            for div in body.find_all("div", class_=re.compile(r'text-xs')):
                t = div.get_text(strip=True)
                if re.match(r'\d+h', t):
                    runtime = t
                    break

        # Showtimes — collect all time button links
        times = []
        for btn_group in card.find_all("div", class_="cin-showtimes-buttons"):
            for a in btn_group.find_all("a", href=True):
                t = a.get_text(strip=True)
                if t:
                    times.append(t)
        if not times:
            continue
        time_str = " · ".join(times)

        events.append(_movie_event(title, day, time_str, image, movie_url,
                                   rating, runtime, "",
                                   "Lebanon 6 Cinema", "lebanon6"))
    return events


def _dedup_movies(events: list[dict]) -> list[dict]:
    """Merge per-day movie entries into one entry per film with all dates listed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        groups[ev["title"].lower().strip()].append(ev)

    result = []
    for group in groups.values():
        group.sort(key=lambda e: e["dates"][0] if e.get("dates") else date.max)
        primary = group[0].copy()
        primary["dates"] = [e["dates"][0] for e in group if e.get("dates")]
        # Build a per-day schedule block prepended to the description
        schedule_rows = "".join(
            f"<tr><td><strong>{e['dates'][0].strftime('%a %b %-d')}</strong></td>"
            f"<td>{e.get('time_str', '')}</td></tr>"
            for e in group if e.get("dates") and e.get("time_str")
        )
        schedule = f'<table class="movie-schedule">{schedule_rows}</table>' if schedule_rows else ""
        primary["description"] = schedule + (primary.get("description") or "")
        primary["time_str"] = ""  # times are now in the schedule table
        result.append(primary)
    return result


def run_scrape_nugget(today: date, end: date) -> list[dict]:
    print(f"Fetching Nugget Theater showtimes ({MOVIE_FETCH_DAYS} days)...")
    events = []
    days = [today + timedelta(days=i) for i in range(MOVIE_FETCH_DAYS)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for day_events in pool.map(_nugget_fetch_day, days):
            events.extend(day_events)
    events = _dedup_movies(events)
    print(f"Nugget: {len(events)} films")
    return events


def _lebanon6_fetch_synopsis(url: str) -> str:
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.select_one(".cin-movie-desc")
        return el.get_text(strip=True) if el else ""
    except Exception:
        return ""


def run_scrape_lebanon6(today: date, end: date) -> list[dict]:
    print(f"Fetching Lebanon 6 showtimes ({MOVIE_FETCH_DAYS} days)...")
    events = []
    days = [today + timedelta(days=i) for i in range(MOVIE_FETCH_DAYS)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for day_events in pool.map(_lebanon6_fetch_day, days):
            events.extend(day_events)
    events = _dedup_movies(events)
    print(f"Lebanon 6: fetching synopses for {len(events)} films...")
    urls = [ev.get("url", "") for ev in events]
    with ThreadPoolExecutor(max_workers=6) as pool:
        synopses = list(pool.map(_lebanon6_fetch_synopsis, urls))
    for ev, synopsis in zip(events, synopses):
        if synopsis:
            ev["description"] = ev.get("description", "") + f"<p>{synopsis}</p>"
    print(f"Lebanon 6: {len(events)} films")
    return events


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def canonical_title(title: str) -> str:
    t = re.sub(r'\s*[-–]\s*[A-Z][A-Za-z]*\.[\w.,\s]+$', '', title)
    t = re.sub(r'\s*[-–]\s*[A-Z][a-z]+\s+[A-Z][a-z]+$', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def is_unimportant(title: str, audience: str) -> bool:
    if UNIMPORTANT_KEYWORDS.search(title):
        return True
    if audience and audience.strip().lower() != "public":
        return True
    return False


def merge_recurring(events: list[dict]) -> list[dict]:
    groups: dict[tuple, list] = defaultdict(list)
    for ev in events:
        key = (
            canonical_title(ev.get("title") or "").lower(),
            (ev.get("time_str") or "").lower(),
            (ev.get("location") or "").lower(),
        )
        groups[key].append(ev)
    merged = []
    for group in groups.values():
        group.sort(key=lambda e: e.get("date") or date.max)
        primary = group[0].copy()
        primary["dates"] = [e["date"] for e in group if e.get("date")]
        merged.append(primary)
    return merged


def format_time(time_str: str) -> str:
    if not time_str or time_str.lower() in ("add to calendar",):
        return ""
    if time_str.lower() == "all day":
        return "All day"
    return time_str


def sort_key(ev: dict):
    first_date = ev["dates"][0] if ev.get("dates") else date.max
    t = ev.get("time_str", "") or ""
    m = re.match(r'(\d+)(?::(\d+))?\s*(am|pm)', t.lower())
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and h != 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        mins = h * 60 + mn
    else:
        mins = 0 if "all day" in t.lower() else 9999
    return (first_date, mins)


def parse_duration_hours(duration_str: str) -> float:
    if not duration_str:
        return 1.0
    m = re.match(r'(\d+(?:\.\d+)?)\s*(hour|day)', duration_str.lower())
    if not m:
        return 1.0
    n = float(m.group(1))
    return n * 24 if 'day' in m.group(2) else n


def event_datetimes(ev: dict, occurrence_date: date) -> tuple[datetime, datetime, bool]:
    start_dt: datetime | None = ev.get("start_dt")
    duration_str = ev.get("duration", "")
    if start_dt is None:
        start_utc = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day,
                             tzinfo=timezone.utc)
        hours = parse_duration_hours(duration_str)
        days = max(1, round(hours / 24))
        end_utc = start_utc + timedelta(days=days)
        return start_utc, end_utc, True
    time_str = ev.get("time_str", "") or ""
    is_all_day = (not time_str or time_str.lower() in ("all day", "")) and start_dt.hour < 6
    if is_all_day:
        start_utc = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day,
                             tzinfo=timezone.utc)
        hours = parse_duration_hours(duration_str)
        days = max(1, round(hours / 24))
        end_utc = start_utc + timedelta(days=days)
        return start_utc, end_utc, True
    start_utc = start_dt.replace(
        year=occurrence_date.year, month=occurrence_date.month, day=occurrence_date.day)
    hours = parse_duration_hours(duration_str)
    if hours >= 20:
        hours = 1.0
    return start_utc, start_utc + timedelta(hours=hours), False


def gcal_url(ev: dict, occurrence_date: date) -> str:
    start_utc, end_utc, is_all_day = event_datetimes(ev, occurrence_date)
    title = ev.get("title", "")
    location = ev.get("location", "") or ev.get("address", "")
    desc_html = ev.get("description") or ev.get("about") or ""
    _d = re.sub(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                lambda m: f'{m.group(2)} ({m.group(1)})', desc_html,
                flags=re.IGNORECASE | re.DOTALL)
    _d = re.sub(r'<(?:em|i)>(.*?)</(?:em|i)>', r'_\1_', _d, flags=re.IGNORECASE | re.DOTALL)
    _d = re.sub(r'<br\s*/?>', '\n', _d, flags=re.IGNORECASE)
    _d = re.sub(r'</p>', '\n\n', _d, flags=re.IGNORECASE)
    _d = re.sub(r'<[^>]+>', '', _d)
    desc = html_mod.unescape(_d).strip()
    if ev.get("url"):
        desc = (desc + "\n\n" if desc else "") + ev["url"]
    dates = (start_utc.strftime("%Y%m%d") + "/" + end_utc.strftime("%Y%m%d") if is_all_day
             else start_utc.strftime("%Y%m%dT%H%M%SZ") + "/" + end_utc.strftime("%Y%m%dT%H%M%SZ"))
    params = {"action": "TEMPLATE", "text": title, "dates": dates}
    if location:
        params["location"] = location
    if desc:
        params["details"] = desc
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

SOURCE_META = {
    "dartmouth":          {"label": "Dartmouth",       "group": "dartmouth"},
    "nhhumanities":       {"label": "NH Humanities",   "group": "nhhumanities"},
    "northernstage":      {"label": "Northern Stage",  "group": "theater"},
    "shakerbridgetheatre":{"label": "Shaker Bridge",   "group": "theater"},
    "nugget":             {"label": "Nugget Theater",   "group": "movies"},
    "lebanon6":           {"label": "Lebanon 6",        "group": "movies"},
}


def generate_html(events: list[dict], start: date, end: date) -> str:
    sources_present = {ev.get("source", "dartmouth") for ev in events}
    groups_present = {SOURCE_META[s]["group"] for s in sources_present if s in SOURCE_META}

    by_date: dict[date, list] = defaultdict(list)
    for ev in events:
        first = ev["dates"][0] if ev.get("dates") else None
        if first:
            by_date[first].append(ev)

    def fmt_date_header(d: date) -> str:
        return d.strftime("%A, %B %-d, %Y")

    def fmt_date_short(d: date) -> str:
        return d.strftime("%b %-d")

    def render_event(ev: dict) -> str:
        source = ev.get("source", "dartmouth")
        title = ev.get("title") or "Untitled"
        unimportant = ev.get("unimportant", False)

        far_away = False
        if source == "nhhumanities":
            loc = ev.get("location", "") or ev.get("address", "") or ""
            far_away = not any(town.lower() in loc.lower() for town in NHH_NEARBY_TOWNS)

        open_attr = "" if (unimportant or far_away) else " open"
        cls = f"event source-{source}"
        cls += " unimportant" if unimportant else " important"
        if far_away:
            cls += " far-away"

        time_display = format_time(ev.get("time_str", ""))
        location = ev.get("location", "") or ev.get("address", "")
        description = ev.get("description", "")
        about = ev.get("about", "")
        image = ev.get("image", "")
        url = ev.get("url", "")
        dates = ev.get("dates", [])
        virtual = ev.get("virtual", "")

        dates_html = ""
        if len(dates) > 1:
            date_strs = ", ".join(fmt_date_short(d) for d in dates)
            dates_html = f'<p class="ev-dates">All dates: {date_strs}</p>'

        desc_html = ""
        if description and description.strip():
            desc_html = f'<div class="ev-description">{description}</div>'
        elif about and about.strip():
            desc_html = f'<div class="ev-description"><p>{about}</p></div>'

        meta_parts = []
        if source == "dartmouth":
            audience = ev.get("audience", "")
            if audience and audience.lower() != "public":
                meta_parts.append(f'<span class="chip chip-audience">{audience}</span>')
            if ev.get("sponsor"):
                meta_parts.append(f'<span class="chip chip-sponsor">{ev["sponsor"]}</span>')
            if ev.get("contact"):
                meta_parts.append(f'<span class="chip chip-contact">{ev["contact"]}</span>')
        elif source == "nhhumanities":
            if ev.get("category"):
                meta_parts.append(f'<span class="chip chip-category">{ev["category"]}</span>')
            if ev.get("presenter"):
                meta_parts.append(f'<span class="chip chip-presenter">{ev["presenter"]}</span>')
            if ev.get("hosted_by"):
                meta_parts.append(f'<span class="chip chip-hostedby">{ev["hosted_by"]}</span>')
            if ev.get("contact"):
                meta_parts.append(f'<span class="chip chip-contact">{ev["contact"]}</span>')
        meta_html = f'<div class="ev-meta">{"".join(meta_parts)}</div>' if meta_parts else ""

        img_html = f'<img class="ev-image" src="{image}" alt="">' if image else ""
        link_html = f'<a class="ev-link" href="{url}" target="_blank">Full details ↗</a>' if url else ""

        trailer_html = ""
        if source in MOVIE_SOURCES:
            trailer_q = quote_plus(f"{title} official trailer")
            trailer_html = (f'<a class="ev-link trailer-link" '
                            f'href="https://www.youtube.com/results?search_query={trailer_q}" '
                            f'target="_blank">Trailer ▶</a>')

        gcal_parts = []
        for d in dates:
            label = fmt_date_short(d) if len(dates) > 1 else "Add to Google Calendar"
            gcal_parts.append(
                f'<a class="gcal-btn" href="{gcal_url(ev, d)}" target="_blank">'
                f'<svg viewBox="0 0 24 24" width="13" height="13"><path d="M19 4h-1V2h-2v2H8V2H6v2H5'
                f'C3.9 4 3 4.9 3 6v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V9h14'
                f'v11zM7 11h5v5H7z" fill="currentColor"/></svg> {label}</a>'
            )
        gcal_html = f'<div class="gcal-row">{"".join(gcal_parts)}</div>'

        badge = ""
        if unimportant and source == "dartmouth":
            audience = ev.get("audience", "")
            reasons = []
            if UNIMPORTANT_KEYWORDS.search(title):
                reasons.append("academic")
            if audience and audience.strip().lower() != "public":
                reasons.append(f"audience: {audience}")
            badge = f'<span class="badge">{" · ".join(reasons)}</span>' if reasons else ""

        virtual_badge = f'<span class="virtual-badge">{virtual}</span>' if virtual else ""
        source_label = SOURCE_META.get(source, {}).get("label", source)
        source_pip = f'<span class="source-pip source-pip-{source}" title="{source_label}"></span>'

        ev_id = re.sub(r'[^\w-]', '_', ev.get("id", ""))
        date_attr = f' data-date="{ev["date"].isoformat()}"' if ev.get("date") else ""
        return f'''
  <details{open_attr} class="{cls}" id="{ev_id}"{date_attr}>
    <summary>
      {source_pip}
      <span class="ev-time">{time_display}</span>
      <span class="ev-title">{title}</span>
      {badge}{virtual_badge}
      {f'<span class="ev-location">{location}</span>' if location else ''}
    </summary>
    <div class="ev-body">
      {img_html}
      {dates_html}
      {desc_html}
      {meta_html}
      <div class="ev-actions">{link_html}{trailer_html}{gcal_html}</div>
    </div>
  </details>'''

    sections = []
    total = important_count = 0
    for d in sorted(by_date.keys()):
        day_events = sorted(by_date[d], key=sort_key)
        sections.append(f'\n  <h2 class="date-header">{fmt_date_header(d)}</h2>')
        for ev in day_events:
            sections.append(render_event(ev))
            total += 1
            if not ev.get("unimportant"):
                important_count += 1
    body = "\n".join(sections)

    # Filter bar
    filter_btns = ['<button class="filter-btn active" data-group="all" onclick="filterEvents(\'all\', this)">All</button>']
    if "dartmouth" in groups_present:
        filter_btns.append(
            '<button class="filter-btn" data-group="dartmouth" onclick="filterEvents(\'dartmouth\', this)">Dartmouth</button>')
    if "nhhumanities" in groups_present:
        filter_btns.append(
            '<button class="filter-btn" data-group="nhhumanities" onclick="filterEvents(\'nhhumanities\', this)">NH Humanities</button>')
    if "theater" in groups_present:
        filter_btns.append(
            '<button class="filter-btn" data-group="theater" onclick="filterEvents(\'theater\', this)">Theater</button>')
    if "movies" in groups_present:
        filter_btns.append(
            '<button class="filter-btn" data-group="movies" onclick="filterEvents(\'movies\', this)">Movies</button>')
    filter_html = (
        f'<div class="filter-bar">{"".join(filter_btns)}</div>'
        f'<div class="date-range-row">'
        f'<label class="date-range-label">From'
        f'<input type="date" id="date-from" min="{start.isoformat()}" max="{end.isoformat()}" onchange="applyDateRange()">'
        f'</label>'
        f'<label class="date-range-label">To'
        f'<input type="date" id="date-to" min="{start.isoformat()}" max="{end.isoformat()}" onchange="applyDateRange()">'
        f'</label>'
        f'<button class="date-clear-btn" onclick="clearDateRange()">Clear</button>'
        f'</div>'
    )

    css = """
    :root {
      --bg:             #121212;
      --surface:        #1e1e1e;
      --surface-muted:  #181818;
      --border:         #333;
      --border-muted:   #2a2a2a;
      --border-body:    #2a2a2a;
      --text:           #e5e5e5;
      --text-muted:     #aaa;
      --text-dim:       #888;
      --text-desc:      #ccc;
      --green:          #4ade80;
      --green-bg:       #0d2a1a;
      --arrow:          #555;
      --shadow:         rgba(0,0,0,0.5);
      --dates-bg:       #2a2000;
      --dates-border:   #b45309;
      --badge-bg:       #2d1b4e;
      --badge-color:    #c084fc;
      --chip-aud-bg:    #1e3a5f; --chip-aud-fg: #93c5fd;
      --chip-spo-bg:    #14401f; --chip-spo-fg: #86efac;
      --chip-con-bg:    #3a2200; --chip-con-fg: #fcd34d;
      --chip-cat-bg:    #1a2a40; --chip-cat-fg: #7dd3fc;
      --chip-pre-bg:    #2a1a3a; --chip-pre-fg: #d8b4fe;
      --chip-hby-bg:    #1a3a2a; --chip-hby-fg: #6ee7b7;
      --gcal-bg:        #2a4a8a;
      --gcal-hover:     #1e3a6e;
      --title-important:   #ffffff;
      --title-unimportant: #777;
      --virtual-bg:     #1a3a1a; --virtual-fg: #86efac;
      --filter-bg:      #252525;
      --filter-active-bg: #2a4a8a;
      --filter-active-fg: #fff;

      /* Source surfaces */
      --surf-dartmouth:          #1e1e1e; --bord-dartmouth:          #333;
      --pip-dartmouth:           #4ade80;
      --surf-nhhumanities:       #131f2e; --bord-nhhumanities:       #1a3048;
      --pip-nhhumanities:        #38bdf8;
      --surf-northernstage:      #1f1700; --bord-northernstage:      #3d2e00;
      --pip-northernstage:       #fbbf24;
      --surf-shakerbridgetheatre:#200a00; --bord-shakerbridgetheatre:#3d1500;
      --pip-shakerbridgetheatre: #f87171;
      --surf-nugget:             #080d1a; --bord-nugget:             #1a2a40;
      --pip-nugget:              #60a5fa;
      --surf-lebanon6:           #0d0818; --bord-lebanon6:           #2a1a3a;
      --pip-lebanon6:            #c084fc;
    }
    body.light {
      --bg:             #f5f5f5;
      --surface:        #fff;
      --surface-muted:  #fafafa;
      --border:         #ddd;
      --border-muted:   #e8e8e8;
      --border-body:    #f0f0f0;
      --text:           #1a1a1a;
      --text-muted:     #555;
      --text-dim:       #777;
      --text-desc:      #333;
      --green:          #00693e;
      --green-bg:       #e8f4ee;
      --arrow:          #999;
      --shadow:         rgba(0,0,0,0.1);
      --dates-bg:       #fff8e1;
      --dates-border:   #f59e0b;
      --badge-bg:       #f0e8ff;
      --badge-color:    #6b21a8;
      --chip-aud-bg:    #dbeafe; --chip-aud-fg: #1e40af;
      --chip-spo-bg:    #dcfce7; --chip-spo-fg: #166534;
      --chip-con-bg:    #fef3c7; --chip-con-fg: #92400e;
      --chip-cat-bg:    #e0f2fe; --chip-cat-fg: #0369a1;
      --chip-pre-bg:    #f3e8ff; --chip-pre-fg: #7e22ce;
      --chip-hby-bg:    #d1fae5; --chip-hby-fg: #065f46;
      --gcal-bg:        #4285f4;
      --gcal-hover:     #3367d6;
      --title-important:   #111;
      --title-unimportant: #999;
      --virtual-bg:     #d1fae5; --virtual-fg: #065f46;
      --filter-bg:      #eee;
      --filter-active-bg: #4285f4;
      --filter-active-fg: #fff;

      --surf-dartmouth:          #fff;    --bord-dartmouth:          #ddd;
      --pip-dartmouth:           #00693e;
      --surf-nhhumanities:       #f0f8ff; --bord-nhhumanities:       #bfdbfe;
      --pip-nhhumanities:        #0284c7;
      --surf-northernstage:      #fffbeb; --bord-northernstage:      #fde68a;
      --pip-northernstage:       #d97706;
      --surf-shakerbridgetheatre:#fff5f5; --bord-shakerbridgetheatre:#fecaca;
      --pip-shakerbridgetheatre: #dc2626;
      --surf-nugget:             #eff6ff; --bord-nugget:             #bfdbfe;
      --pip-nugget:              #2563eb;
      --surf-lebanon6:           #faf5ff; --bord-lebanon6:           #e9d5ff;
      --pip-lebanon6:            #9333ea;
    }

    * { box-sizing: border-box; }
    html, body { overflow-x: hidden; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 860px; margin: 0 auto; padding: 1rem 1.5rem 4rem;
      background: var(--bg); color: var(--text); line-height: 1.5;
      transition: background 0.2s, color 0.2s;
    }
    .header-row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
    h1 { font-size: 1.8rem; margin-bottom: 0.25rem; color: var(--green); }
    .subtitle { color: var(--text-muted); margin-bottom: 0.75rem; font-size: 0.95rem; }
    .subtitle a { color: var(--green); }
    .stats { background: var(--green-bg); border-left: 4px solid var(--green);
             padding: 0.6rem 1rem; margin-bottom: 1rem;
             border-radius: 0 6px 6px 0; font-size: 0.9rem; }
    .filter-bar { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .filter-btn {
      padding: 0.35rem 0.9rem; border-radius: 20px; border: 1px solid var(--border);
      background: var(--filter-bg); color: var(--text-muted); cursor: pointer; font-size: 0.85rem;
    }
    .filter-btn:hover { border-color: var(--green); color: var(--green); }
    .filter-btn.active { background: var(--filter-active-bg); color: var(--filter-active-fg); border-color: transparent; }
    #theme-btn {
      font-size: 0.8rem; padding: 0.3rem 0.75rem; border-radius: 5px;
      border: 1px solid var(--border); background: var(--surface); color: var(--text);
      cursor: pointer; white-space: nowrap; flex-shrink: 0;
    }
    #theme-btn:hover { border-color: var(--green); color: var(--green); }
    h2.date-header {
      font-size: 1.1rem; font-weight: 700; color: var(--green);
      margin: 2rem 0 0.5rem; padding: 0.4rem 0;
      border-bottom: 2px solid var(--green);
      position: sticky; top: 0; background: var(--bg); z-index: 10;
    }
    details.event {
      margin: 0.35rem 0; border-radius: 6px; border: 1px solid var(--border);
      background: var(--surface); transition: box-shadow 0.15s;
    }
    details.event[open] { box-shadow: 0 2px 8px var(--shadow); }
    details.source-dartmouth           { background: var(--surf-dartmouth);           border-color: var(--bord-dartmouth); }
    details.source-nhhumanities        { background: var(--surf-nhhumanities);        border-color: var(--bord-nhhumanities); }
    details.source-northernstage       { background: var(--surf-northernstage);       border-color: var(--bord-northernstage); }
    details.source-shakerbridgetheatre { background: var(--surf-shakerbridgetheatre); border-color: var(--bord-shakerbridgetheatre); }
    details.source-nugget              { background: var(--surf-nugget);              border-color: var(--bord-nugget); }
    details.source-lebanon6            { background: var(--surf-lebanon6);            border-color: var(--bord-lebanon6); }
    details.event.unimportant { opacity: 0.85; }
    details.event.unimportant[open] { opacity: 1; }
    details.event.far-away { opacity: 0.6; }
    details.event.far-away[open] { opacity: 0.85; }
    .far-away .ev-title { color: var(--title-unimportant); }
    summary {
      padding: 0.6rem 1rem; cursor: pointer; display: flex; align-items: baseline;
      gap: 0.6rem; flex-wrap: wrap; list-style: none; user-select: text;
    }
    summary::-webkit-details-marker { display: none; }
    summary::before {
      content: "▶"; font-size: 0.6rem; color: var(--arrow);
      flex-shrink: 0; align-self: center; transition: transform 0.15s;
    }
    details[open] > summary::before { transform: rotate(90deg); }
    .source-pip {
      width: 8px; height: 8px; border-radius: 50%;
      flex-shrink: 0; align-self: center; display: inline-block;
    }
    .source-pip-dartmouth           { background: var(--pip-dartmouth); }
    .source-pip-nhhumanities        { background: var(--pip-nhhumanities); }
    .source-pip-northernstage       { background: var(--pip-northernstage); }
    .source-pip-shakerbridgetheatre { background: var(--pip-shakerbridgetheatre); }
    .source-pip-nugget              { background: var(--pip-nugget); }
    .source-pip-lebanon6            { background: var(--pip-lebanon6); }
    .ev-movie-meta { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.4rem; }
    .movie-schedule { font-size: 0.82rem; border-collapse: collapse; margin-bottom: 0.6rem; }
    .movie-schedule td { padding: 0.1rem 0.8rem 0.1rem 0; vertical-align: top; }
    .movie-schedule td:first-child { white-space: nowrap; color: var(--text-muted); }
    .ev-time { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); white-space: nowrap; min-width: 90px; }
    .ev-title { font-weight: 600; font-size: 0.95rem; flex: 1; }
    .important .ev-title   { color: var(--title-important); }
    .unimportant .ev-title { color: var(--title-unimportant); }
    .ev-location { font-size: 0.8rem; color: var(--text-dim); font-style: italic; }
    .badge, .virtual-badge { font-size: 0.7rem; border-radius: 3px; padding: 0.1rem 0.4rem; white-space: nowrap; }
    .badge         { background: var(--badge-bg); color: var(--badge-color); }
    .virtual-badge { background: var(--virtual-bg); color: var(--virtual-fg); }
    .ev-body {
      padding: 0.75rem 1rem 1rem 2.5rem;
      border-top: 1px solid var(--border-body);
      overflow: hidden;
    }
    .ev-image { float: right; max-width: 160px; border-radius: 4px; margin: 0 0 0.5rem 1rem; }
    .ev-dates {
      font-size: 0.82rem; background: var(--dates-bg);
      border-left: 3px solid var(--dates-border);
      padding: 0.3rem 0.6rem; margin-bottom: 0.75rem; border-radius: 0 4px 4px 0;
    }
    .ev-description { font-size: 0.88rem; color: var(--text-desc); margin-bottom: 0.75rem; clear: both; }
    .ev-description p { margin: 0.4rem 0; }
    .ev-description a { color: var(--green); }
    .ev-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }
    .chip { font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 12px; max-width: min(100%, 28ch); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chip-audience  { background: var(--chip-aud-bg); color: var(--chip-aud-fg); }
    .chip-sponsor   { background: var(--chip-spo-bg); color: var(--chip-spo-fg); }
    .chip-contact   { background: var(--chip-con-bg); color: var(--chip-con-fg); }
    .chip-category  { background: var(--chip-cat-bg); color: var(--chip-cat-fg); }
    .chip-presenter { background: var(--chip-pre-bg); color: var(--chip-pre-fg); }
    .chip-hostedby  { background: var(--chip-hby-bg); color: var(--chip-hby-fg); }
    .ev-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.5rem; }
    .ev-link { font-size: 0.8rem; color: var(--green); text-decoration: none; font-weight: 500; }
    .ev-link:hover { text-decoration: underline; }
    .gcal-row { display: flex; flex-wrap: wrap; gap: 0.4rem; }
    .gcal-btn {
      display: inline-flex; align-items: center; gap: 0.3rem;
      font-size: 0.75rem; padding: 0.25rem 0.55rem; border-radius: 4px;
      background: var(--gcal-bg); color: #fff; text-decoration: none; white-space: nowrap;
    }
    .gcal-btn:hover { background: var(--gcal-hover); }
    .date-range-row {
      display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1.5rem; flex-wrap: wrap;
    }
    .date-range-label {
      display: flex; align-items: center; gap: 0.4rem;
      font-size: 0.85rem; color: var(--text-muted);
    }
    .date-range-label input[type="date"] {
      padding: 0.3rem 0.5rem; border-radius: 6px;
      border: 1px solid var(--border); background: var(--filter-bg); color: var(--text);
      font-size: 0.85rem; cursor: pointer;
    }
    .date-range-label input[type="date"]:focus { outline: none; border-color: var(--green); }
    .date-clear-btn {
      padding: 0.3rem 0.7rem; border-radius: 20px; border: 1px solid var(--border);
      background: var(--filter-bg); color: var(--text-muted); cursor: pointer; font-size: 0.8rem;
    }
    .date-clear-btn:hover { border-color: var(--green); color: var(--green); }
    @media (max-width: 600px) {
      .ev-image { float: none; max-width: 100%; margin: 0 0 0.5rem; }
      summary { flex-direction: column; gap: 0.2rem; }
      .ev-time { min-width: auto; }
      .date-range-row { gap: 0.5rem; }
      .date-range-label input[type="date"] { width: 100%; }
    }
    """

    source_links = {
        "dartmouth":           '<a href="https://home.dartmouth.edu/events">home.dartmouth.edu/events</a>',
        "nhhumanities":        '<a href="https://www.nhhumanities.org/programs/upcoming">nhhumanities.org</a>',
        "northernstage":       '<a href="https://northernstage.org">northernstage.org</a>',
        "shakerbridgetheatre": '<a href="https://www.shakerbridgetheatre.org">shakerbridgetheatre.org</a>',
        "nugget":              '<a href="https://www.nugget-theaters.com">nugget-theaters.com</a>',
        "lebanon6":            '<a href="https://www.entertainmentcinemas.com/lebanon-6">entertainmentcinemas.com/lebanon-6</a>',
    }
    subtitle_parts = " · ".join(
        source_links[s] for s in ALL_SOURCES if s in sources_present and s in source_links)

    source_groups_js = json.dumps({k: v for k, v in SOURCE_GROUPS.items() if k != "all"})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Upper Valley Events – {start.strftime("%b %-d")} to {end.strftime("%b %-d, %Y")}</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%232d5a27'/><polygon points='16,3 28,22 4,22' fill='%231a3d16'/><polygon points='10,12 20,26 0,26' fill='%232d5a27'/><polygon points='22,14 30,26 14,26' fill='%23234820'/><rect x='14' y='22' width='4' height='6' fill='%235c3d1e'/></svg>">
  <style>{css}</style>
</head>
<body>
  <div class="header-row">
    <h1>Upper Valley Events</h1>
    <button id="theme-btn" onclick="toggleTheme()">☀</button>
  </div>
  <p class="subtitle">Scraped from {subtitle_parts}</p>
  <div class="stats">
    <span id="stats-counts"></span>
    from <strong>{start.strftime("%B %-d")}</strong> to <strong>{end.strftime("%B %-d, %Y")}</strong>.
  </div>
  {filter_html}
  {body}
<script>
  const SOURCE_GROUPS = {source_groups_js};

  // --- Hash helpers ---
  function parseHash() {{
    const params = {{}};
    location.hash.slice(1).split('&').forEach(part => {{
      const eq = part.indexOf('=');
      if (eq > 0) params[decodeURIComponent(part.slice(0, eq))] = decodeURIComponent(part.slice(eq + 1));
    }});
    return params;
  }}
  function updateHash(params) {{
    const h = Object.entries(params).filter(([,v]) => v)
      .map(([k,v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
    history.replaceState(null, '', h ? '#' + h : location.pathname + location.search);
  }}

  // --- Stats ---
  function updateStats() {{
    let total = 0, open = 0, collapsed = 0;
    document.querySelectorAll('details.event').forEach(el => {{
      if (el.style.display === 'none') return;
      total++;
      if (el.classList.contains('important') && !el.classList.contains('far-away')) open++;
      else collapsed++;
    }});
    document.getElementById('stats-counts').innerHTML =
      `Showing <strong>${{total}}</strong> events (${{open}} open, ${{collapsed}} collapsed) `;
  }}

  // --- Filter ---
  let currentGroup = 'all';

  function applyFilters(skipHash) {{
    const fromVal = document.getElementById('date-from').value;
    const toVal = document.getElementById('date-to').value;
    document.querySelectorAll('details.event').forEach(el => {{
      let show = currentGroup === 'all';
      if (!show && SOURCE_GROUPS[currentGroup]) {{
        show = SOURCE_GROUPS[currentGroup].some(s => el.classList.contains('source-' + s));
      }} else if (!show) {{
        show = el.classList.contains('source-' + currentGroup);
      }}
      if (show && (fromVal || toVal)) {{
        const d = el.dataset.date || '';
        if (d) {{
          if (fromVal && d < fromVal) show = false;
          if (toVal && d > toVal) show = false;
        }}
      }}
      el.style.display = show ? '' : 'none';
    }});
    document.querySelectorAll('h2.date-header').forEach(h => {{
      let sib = h.nextElementSibling, visible = false;
      while (sib && !sib.classList.contains('date-header')) {{
        if (sib.classList.contains('event') && sib.style.display !== 'none') {{ visible = true; break; }}
        sib = sib.nextElementSibling;
      }}
      h.style.display = visible ? '' : 'none';
    }});
    if (!skipHash) {{
      const p = parseHash();
      p.filter = currentGroup === 'all' ? '' : currentGroup;
      p.from = fromVal;
      p.to = toVal;
      updateHash(p);
    }}
    updateStats();
  }}

  function filterEvents(group, btn, skipHash) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentGroup = group;
    applyFilters(skipHash);
  }}

  function applyDateRange() {{
    applyFilters(false);
  }}

  function clearDateRange() {{
    document.getElementById('date-from').value = '';
    document.getElementById('date-to').value = '';
    applyFilters(false);
  }}

  // --- Scroll tracking ---
  function topVisibleEvent() {{
    for (const el of document.querySelectorAll('details.event')) {{
      if (el.style.display === 'none') continue;
      if (el.getBoundingClientRect().bottom > 10) return el;
    }}
    return null;
  }}
  let scrollTimer;
  window.addEventListener('scroll', () => {{
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {{
      const el = topVisibleEvent();
      if (el) {{
        const p = parseHash();
        p.ev = el.id;
        updateHash(p);
      }}
    }}, 150);
  }}, {{passive: true}});

  // --- Theme ---
  function toggleTheme() {{
    const light = document.body.classList.toggle('light');
    document.getElementById('theme-btn').textContent = light ? '🌙' : '☀';
    localStorage.setItem('theme', light ? 'light' : 'dark');
  }}
  if (localStorage.getItem('theme') === 'light') {{
    document.body.classList.add('light');
    document.getElementById('theme-btn').textContent = '🌙';
  }}

  // --- Restore from hash on load ---
  (function() {{
    const p = parseHash();
    if (p.from) document.getElementById('date-from').value = p.from;
    if (p.to) document.getElementById('date-to').value = p.to;
    if (p.filter) {{
      const btn = document.querySelector(`.filter-btn[data-group="${{p.filter}}"]`);
      if (btn) {{ currentGroup = p.filter; btn.classList.add('active'); document.querySelector('.filter-btn[data-group="all"]').classList.remove('active'); }}
    }}
    applyFilters(true);
    if (p.ev) {{
      const el = document.getElementById(p.ev);
      if (el) setTimeout(() => el.scrollIntoView({{block: 'start'}}), 50);
    }}
  }})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Intermediate file I/O
# ---------------------------------------------------------------------------

def _json_default(obj):
    if isinstance(obj, datetime):
        return {"__type__": "datetime", "value": obj.isoformat()}
    if isinstance(obj, date):
        return {"__type__": "date", "value": obj.isoformat()}
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _json_hook(d):
    t = d.get("__type__")
    if t == "date":
        return date.fromisoformat(d["value"])
    if t == "datetime":
        return datetime.fromisoformat(d["value"])
    return d


def save_scrape_results(source: str, events: list[dict], start: date, end: date) -> None:
    os.makedirs("output", exist_ok=True)
    path = INTERMEDIATE_FILES[source]
    payload = {"source": source, "start": start.isoformat(), "end": end.isoformat(), "events": events}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, default=_json_default, indent=2)
    print(f"Scrape results written to {path} ({os.path.getsize(path) / 1024:.1f} KB)")


def load_scrape_results(source: str) -> tuple[list[dict], date, date]:
    path = INTERMEDIATE_FILES[source]
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f, object_hook=_json_hook)
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    print(f"Loaded {len(payload['events'])} {source} events from {path} ({start} to {end})")
    return payload["events"], start, end


# ---------------------------------------------------------------------------
# Scrape stages
# ---------------------------------------------------------------------------

def run_scrape_dartmouth(today: date, end: date) -> list[dict]:
    list_html = fetch_event_list(today, end)
    raw_events = parse_event_list(list_html, today.year)
    print(f"Found {len(raw_events)} Dartmouth event entries in listing")
    seen_ids: dict[str, dict] = {}
    for ev in raw_events:
        if ev["id"] not in seen_ids:
            seen_ids[ev["id"]] = ev
    unique_ids = list(seen_ids.keys())
    print(f"Fetching details for {len(unique_ids)} unique Dartmouth events...")
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_detail, eid): eid for eid in unique_ids}
        done = 0
        for future in as_completed(futures):
            eid, detail_html = future.result()
            done += 1
            if detail_html:
                details[eid] = parse_detail(detail_html)
            if done % 20 == 0:
                print(f"  {done}/{len(unique_ids)} details fetched...")
    print(f"Successfully fetched {len(details)} Dartmouth detail pages")
    combined = []
    for ev in raw_events:
        det = details.get(ev["id"], {})
        merged = {**ev}
        if det:
            merged.update({
                "title":    det.get("title") or ev["title"],
                "time_str": det.get("time_str") or ev["time_str"],
                "location": det.get("location", ""),
                "audience": det.get("audience", ""),
                "sponsor":  det.get("sponsor", ""),
                "description": det.get("description", ""),
                "about":    det.get("about", ""),
                "image":    det.get("image", ""),
                "url":      det.get("url", ""),
                "contact":  det.get("contact", ""),
                "start_dt": det.get("start_dt"),
                "start_iso":det.get("start_iso", ""),
                "duration": det.get("duration", ""),
            })
        combined.append(merged)
    merged_events = merge_recurring(combined)
    print(f"After merging recurring: {len(merged_events)} distinct Dartmouth events")
    for ev in merged_events:
        ev["unimportant"] = is_unimportant(ev.get("title", ""), ev.get("audience", ""))
    uc = sum(1 for e in merged_events if e["unimportant"])
    print(f"Dartmouth — Important: {len(merged_events) - uc}, Unimportant: {uc}")
    return merged_events


def run_scrape_nhhumanities(today: date, end: date) -> list[dict]:
    raw_events = fetch_nhh_event_list(end)
    print(f"Found {len(raw_events)} NH Humanities events total")
    raw_events = [e for e in raw_events if e.get("date") and today <= e["date"] <= end]
    print(f"Filtered to {len(raw_events)} NH Humanities events in window")
    print(f"Fetching details for {len(raw_events)} NH Humanities events...")
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_nhh_detail, ev["url"]): ev for ev in raw_events}
        for future in as_completed(futures):
            ev = futures[future]
            url, detail_html = future.result()
            if detail_html:
                details[url] = parse_nhh_detail(detail_html, ev.get("date"))
    print(f"Successfully fetched {len(details)} NH Humanities detail pages")
    combined = []
    for ev in raw_events:
        det = details.get(ev["url"], {})
        merged = {**ev}
        if det:
            merged.update({
                "title":       det.get("title") or ev["title"],
                "description": det.get("description", ""),
                "time_str":    det.get("time_str", ""),
                "start_dt":    det.get("start_dt"),
                "presenter":   det.get("presenter", ""),
                "hosted_by":   det.get("hosted_by", ""),
                "contact":     det.get("contact", ""),
                "category":    det.get("category", ""),
            })
            if det.get("address"):
                merged["location"] = det["address"]
            if det.get("image"):
                merged["image"] = det["image"]
        merged["dates"] = [ev["date"]] if ev.get("date") else []
        merged["unimportant"] = False
        combined.append(merged)
    print(f"NH Humanities: {len(combined)} events")
    return combined


def _ns_absolutize(path: str) -> str:
    return NORTHERNSTAGE_TICKET_SITE + path if path.startswith("/") else path


def run_scrape_northernstage(today: date, end: date) -> list[dict]:
    print("Fetching Northern Stage events from ticketing API...")
    session = requests.Session()
    try:
        resp = session.get(NORTHERNSTAGE_TICKET_SITE, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
        m = re.search(r'\{"name":"fetchEvents"[^}]+\}', html)
        if not m:
            print("Warning: Northern Stage — could not find fetchEvents config", file=sys.stderr)
            return []
        config = json.loads(m.group(0))
        csrf = config["csrf"]
        auth = config["authorization"]
        m2 = re.search(r'"vid":"([^"]+)"', html)
        vid = m2.group(1) if m2 else ""

        payload = {
            "action": "PatronTicket.Controller_PublicTicketApp",
            "method": "fetchEvents",
            "data": ["", "", ""],
            "type": "rpc", "tid": 2,
            "ctx": {"csrf": csrf, "vid": vid, "ns": "PatronTicket", "ver": 51.0, "authorization": auth},
        }
        remote_resp = session.post(
            NORTHERNSTAGE_APEXREMOTE, json=payload, timeout=30,
            headers={**BROWSER_HEADERS, "Content-Type": "application/json",
                     "X-User-Agent": "Visualforce-Remoting",
                     "Referer": NORTHERNSTAGE_TICKET_SITE},
        )
        remote_resp.raise_for_status()
        data = remote_resp.json()
    except Exception as e:
        print(f"Warning: Northern Stage API error: {e}", file=sys.stderr)
        return []

    if not data or data[0].get("statusCode") != 200:
        print(f"Warning: Northern Stage fetchEvents: {data[0].get('message', 'unknown error')}", file=sys.stderr)
        return []

    events = []
    for ev in data[0]["result"]:
        name = ev.get("name", "")
        if not ev.get("category") or NS_SKIP_NAMES.search(name):
            continue
        instances = ev.get("instances", [])
        show_dates = []
        for inst in instances:
            yyyymmdd = inst.get("formattedDates", {}).get("YYYYMMDD", "")
            if yyyymmdd:
                try:
                    show_dates.append(date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])))
                except ValueError:
                    pass
        if not show_dates:
            continue
        start_d, end_d = min(show_dates), max(show_dates)
        if end_d < today or start_d > end:
            continue
        events.append(_theater_show_to_event({
            "title": name,
            "start_date": start_d,
            "end_date": end_d,
            "date_range_str": _ns_fmt_date_range(start_d, end_d),
            "description": ev.get("detail", "") or ev.get("description", "") or "",
            "image": _ns_absolutize(ev.get("largeImagePath") or ev.get("smallImagePath", "")),
            "url": _ns_absolutize(ev.get("purchaseUrl", "")) or NORTHERNSTAGE_TICKET_SITE,
            "source": "northernstage",
        }, today))

    print(f"Northern Stage: {len(events)} shows in window")
    return events


def run_scrape_shakerbridgetheatre(today: date, end: date) -> list[dict]:
    print("Fetching Shaker Bridge Theatre ticketing page...")
    try:
        resp = requests.get(SHBT_TICKETS_URL, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        shows = _shbt_parse_ticketing_page(resp.text)
    except Exception as e:
        print(f"  Warning: could not fetch Shaker Bridge ticketing page: {e}", file=sys.stderr)
        shows = []

    events = []
    for show in shows:
        if show["end_date"] < today or show["start_date"] > end:
            continue
        events.append(_theater_show_to_event(show, today))

    print(f"Shaker Bridge: {len(events)} shows in window")
    return events


def run_generate(sources: list[str], today: date, end: date) -> None:
    all_events = []
    starts, ends = [], []
    for source in sources:
        events, s, e = load_scrape_results(source)
        all_events.extend(events)
        starts.append(s)
        ends.append(e)
    os.makedirs("output", exist_ok=True)
    start, end = min(starts), max(ends)
    html_output = generate_html(all_events, start, end)
    now_str = datetime.now(EASTERN).strftime("%Y-%m-%dT%H-%M")
    html_path = os.path.join("output", f"events_{now_str}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_output)
    print(f"HTML written to {html_path} ({len(html_output) / 1024:.1f} KB)")
    html_filename = f"events_{now_str}.html"
    symlink = os.path.join("output", "events.html")
    if os.path.islink(symlink) or os.path.exists(symlink):
        os.remove(symlink)
    os.symlink(html_filename, symlink)
    print(f"Symlink updated: {symlink} -> {html_filename}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SCRAPE_FNS = {
    "dartmouth":           run_scrape_dartmouth,
    "nhhumanities":        run_scrape_nhhumanities,
    "northernstage":       run_scrape_northernstage,
    "shakerbridgetheatre": run_scrape_shakerbridgetheatre,
    "nugget":              run_scrape_nugget,
    "lebanon6":            run_scrape_lebanon6,
}


def parse_sources(value: str) -> list[str]:
    result, seen = [], set()
    for part in [s.strip().lower() for s in value.split(",")]:
        if part in SOURCE_GROUPS:
            for s in SOURCE_GROUPS[part]:
                if s not in seen:
                    result.append(s)
                    seen.add(s)
        elif part in set(ALL_SOURCES):
            if part not in seen:
                result.append(part)
                seen.add(part)
        else:
            raise argparse.ArgumentTypeError(
                f"Unknown source/group '{part}'. "
                f"Sources: {', '.join(ALL_SOURCES)}. "
                f"Groups: {', '.join(SOURCE_GROUPS)}"
            )
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Upper Valley Events Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Sources: {', '.join(ALL_SOURCES)}
Groups:  all, theater, movies  (theater = {', '.join(THEATER_SOURCES)}; movies = {', '.join(MOVIE_SOURCES)})

Examples:
  python scraper.py                        # generate HTML from existing data files
  python scraper.py --scrape=all           # scrape everything, then generate
  python scraper.py --scrape=theater       # scrape theater sources only, then generate
  python scraper.py --scrape=dartmouth     # scrape Dartmouth only, then generate
        """,
    )
    parser.add_argument("--scrape", type=parse_sources, metavar="SOURCES",
                        help="Comma-separated sources/groups to scrape before generating")
    parser.add_argument("--days", type=int, default=90, metavar="N",
                        help="Number of days in the date range (default: 30)")
    args = parser.parse_args()

    today = date.today()
    end = today + timedelta(days=args.days)

    if args.scrape:
        for source in args.scrape:
            events = SCRAPE_FNS[source](today, end)
            save_scrape_results(source, events, today, end)

    run_generate(ALL_SOURCES, today, end)


if __name__ == "__main__":
    main()
