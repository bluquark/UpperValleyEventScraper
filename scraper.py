#!/usr/bin/env python3
"""
Upper Valley Events Scraper
Fetches 30 days of events from:
  - home.dartmouth.edu/events
  - nhhumanities.org/programs/upcoming
Produces a self-contained HTML page with color-coded, filterable events.
Recurring Dartmouth events are merged; academic/non-public ones are collapsed.
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
from urllib.parse import urlencode

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except ImportError:
    EASTERN = timezone(timedelta(hours=-4))

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Dartmouth constants
# ---------------------------------------------------------------------------
DARTMOUTH_BASE = "https://home.dartmouth.edu"
DARTMOUTH_AJAX_URL = f"{DARTMOUTH_BASE}/events/ajax/search"
DARTMOUTH_DETAIL_URL = f"{DARTMOUTH_BASE}/events/event"

# ---------------------------------------------------------------------------
# NH Humanities constants
# ---------------------------------------------------------------------------
NHH_BASE = "https://www.nhhumanities.org"
NHH_LIST_URL = f"{NHH_BASE}/programs/upcoming"

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
UNIMPORTANT_KEYWORDS = re.compile(r'\b(seminar|colloquium|thesis|dissertation)\b', re.IGNORECASE)

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

ALL_SOURCES = ["dartmouth", "nhhumanities"]

INTERMEDIATE_FILES = {
    "dartmouth": os.path.join("output", "scraped_dartmouth.json"),
    "nhhumanities": os.path.join("output", "scraped_nhhumanities.json"),
}


# ---------------------------------------------------------------------------
# Dartmouth scraping
# ---------------------------------------------------------------------------

def fetch_event_list(start: date, end: date) -> str:
    """Fetch all events in date range via AJAX API. Returns HTML string of teasers."""
    params = {
        "offset": 0,
        "limit": 300,
        "begin": start.isoformat(),
        "end": end.isoformat(),
    }
    print(f"Fetching Dartmouth event list ({start} to {end})...")
    resp = requests.get(DARTMOUTH_AJAX_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    for cmd in data:
        if cmd.get("command") == "eventsContent":
            return cmd["content"]
    raise ValueError("No eventsContent command found in API response")


def parse_event_list(html: str, ref_year: int) -> list[dict]:
    """Parse the event teaser HTML and return list of raw event dicts."""
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
        title = title_link.get_text(strip=True)

        events.append({
            "id": event_id,
            "title": title,
            "date": date(year, month, day) if month and day else None,
            "time_str": time_str,
            "summary": summary,
            "source": "dartmouth",
        })
    return events


def fetch_detail(event_id: str) -> tuple[str, str | None]:
    """Fetch detail page HTML for one Dartmouth event."""
    try:
        time.sleep(0.05)
        resp = requests.get(DARTMOUTH_DETAIL_URL, params={"event": event_id}, timeout=30)
        if resp.status_code == 200:
            return event_id, resp.text
    except Exception as e:
        print(f"  Warning: failed to fetch event {event_id}: {e}", file=sys.stderr)
    return event_id, None


def parse_detail(html: str) -> dict:
    """Parse Dartmouth event detail page and return structured data dict."""
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

NHH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def fetch_nhh_event_list() -> list[dict]:
    """Fetch all upcoming NH Humanities events. Returns list of partial event dicts."""
    print(f"Fetching NH Humanities event list...")
    resp = requests.get(NHH_LIST_URL, headers=NHH_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []
    for ev_div in soup.find_all("div", class_="event"):
        title_link = ev_div.find("a", class_="title")
        if not title_link:
            continue
        title = title_link.get_text(strip=True)
        href = title_link.get("href", "")
        url = (NHH_BASE + href) if href.startswith("/") else href

        date_div = ev_div.find("div", class_="date")
        date_text = ""
        location = ""
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
            "id": url,  # use URL as stable ID
            "title": title,
            "url": url,
            "date": ev_date,
            "location": location,
            "image": image,
            "virtual": virtual,
            "source": "nhhumanities",
        })
    return events


def fetch_nhh_detail(url: str) -> tuple[str, str | None]:
    """Fetch detail page HTML for one NH Humanities event."""
    try:
        time.sleep(0.05)
        resp = requests.get(url, headers=NHH_HEADERS, timeout=30)
        if resp.status_code == 200:
            return url, resp.text
    except Exception as e:
        print(f"  Warning: failed to fetch {url}: {e}", file=sys.stderr)
    return url, None


def parse_nhh_detail(html: str, event_date: date | None) -> dict:
    """Parse NH Humanities event detail page."""
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    cat_el = soup.find("div", class_="eventCategory")
    category = cat_el.get_text(strip=True) if cat_el else ""

    # Presenter (appears as a <p> containing "Presenter:")
    presenter = ""
    for p in soup.find_all("p"):
        txt = p.get_text()
        if "Presenter:" in txt:
            a = p.find("a")
            presenter = a.get_text(strip=True) if a else re.sub(r'Presenter:\s*', '', txt).strip()
            break

    # Description
    desc_el = soup.find("div", class_="eventDescription")
    description = ""
    if desc_el:
        for a in desc_el.find_all("a", href=True):
            if a["href"].startswith("/"):
                a["href"] = NHH_BASE + a["href"]
        description = str(desc_el.decode_contents())

    # Event details block
    time_str = ""
    when_full = ""
    address = ""
    hosted_by = ""
    contact = ""
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

    # Extract time portion from "Tuesday, March 24, 2026 2:00pm"
    start_dt_utc = None
    if when_full:
        m = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', when_full, re.IGNORECASE)
        if m:
            time_str = m.group(1).strip()
            # Build datetime for gcal
            try:
                d = event_date or datetime.strptime(when_full.split(time_str)[0].strip(),
                                                    "%A, %B %d, %Y").date()
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

    # OG image as fallback
    og_img = soup.find("meta", property="og:image")
    image = og_img.get("content", "") if og_img else ""

    return {
        "title": title,
        "category": category,
        "presenter": presenter,
        "description": description,
        "time_str": time_str,
        "address": address,
        "hosted_by": hosted_by,
        "contact": contact,
        "image": image,
        "start_dt": start_dt_utc,
    }


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def canonical_title(title: str) -> str:
    t = re.sub(r'\s*[-–]\s*[A-Z][A-Za-z]*\.[\w.,\s]+$', '', title)
    t = re.sub(r'\s*[-–]\s*[A-Z][a-z]+\s+[A-Z][a-z]+$', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


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
            canonical_title(ev.get("title") or ev.get("teaser_title", "")).lower(),
            (ev.get("time_str") or "").lower(),
            (ev.get("location") or "").lower(),
        )
        groups[key].append(ev)

    merged = []
    for key, group in groups.items():
        group.sort(key=lambda e: e.get("date") or date.max)
        primary = group[0].copy()
        primary["dates"] = [e["date"] for e in group if e.get("date")]
        merged.append(primary)
    return merged


def format_time(time_str: str) -> str:
    if not time_str or time_str.lower() in ("all day", "add to calendar"):
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
        end_utc = start_utc + timedelta(days=1)
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
    else:
        start_utc = start_dt.replace(
            year=occurrence_date.year,
            month=occurrence_date.month,
            day=occurrence_date.day,
        )
        hours = parse_duration_hours(duration_str)
        if hours >= 20:
            hours = 1.0
        end_utc = start_utc + timedelta(hours=hours)
        return start_utc, end_utc, False


def gcal_url(ev: dict, occurrence_date: date) -> str:
    start_utc, end_utc, is_all_day = event_datetimes(ev, occurrence_date)
    title = ev.get("title", "")
    location = ev.get("location", "") or ev.get("address", "")
    desc_html = ev.get("description") or ev.get("about") or ""
    _d = re.sub(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                lambda m: f'{m.group(2)} ({m.group(1)})', desc_html, flags=re.IGNORECASE | re.DOTALL)
    _d = re.sub(r'<(?:em|i)>(.*?)</(?:em|i)>', r'_\1_', _d, flags=re.IGNORECASE | re.DOTALL)
    _d = re.sub(r'<br\s*/?>', '\n', _d, flags=re.IGNORECASE)
    _d = re.sub(r'</p>', '\n\n', _d, flags=re.IGNORECASE)
    _d = re.sub(r'<[^>]+>', '', _d)
    desc = html_mod.unescape(_d).strip()
    if ev.get("url"):
        desc = (desc + "\n\n" if desc else "") + ev["url"]

    if is_all_day:
        dates = start_utc.strftime("%Y%m%d") + "/" + end_utc.strftime("%Y%m%d")
    else:
        dates = start_utc.strftime("%Y%m%dT%H%M%SZ") + "/" + end_utc.strftime("%Y%m%dT%H%M%SZ")

    params = {"action": "TEMPLATE", "text": title, "dates": dates}
    if location:
        params["location"] = location
    if desc:
        params["details"] = desc
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(events: list[dict], start: date, end: date) -> str:
    sources_present = sorted({ev.get("source", "dartmouth") for ev in events})

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
        title = ev.get("title") or ev.get("teaser_title", "Untitled")
        unimportant = ev.get("unimportant", False)
        open_attr = "" if unimportant else " open"
        cls = f"event source-{source}"
        if unimportant:
            cls += " unimportant"
        else:
            cls += " important"

        time_display = format_time(ev.get("time_str", ""))
        location = ev.get("location", "") or ev.get("address", "")
        audience = ev.get("audience", "")
        sponsor = ev.get("sponsor", "")
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
            if audience and audience.lower() != "public":
                meta_parts.append(f'<span class="chip chip-audience">{audience}</span>')
            if sponsor:
                meta_parts.append(f'<span class="chip chip-sponsor">{sponsor}</span>')
            if ev.get("contact"):
                meta_parts.append(f'<span class="chip chip-contact">{ev["contact"]}</span>')
        else:  # nhhumanities
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

        gcal_parts = []
        for d in dates:
            label = fmt_date_short(d) if len(dates) > 1 else "Add to Google Calendar"
            gcal_parts.append(
                f'<a class="gcal-btn" href="{gcal_url(ev, d)}" target="_blank">'
                f'<svg viewBox="0 0 24 24" width="13" height="13"><path d="M19 4h-1V2h-2v2H8V2H6v2H5C3.9 4 3 4.9 3 6v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V9h14v11zM7 11h5v5H7z" fill="currentColor"/></svg>'
                f' {label}</a>'
            )
        gcal_html = f'<div class="gcal-row">{"".join(gcal_parts)}</div>'

        badge = ""
        if unimportant and source == "dartmouth":
            reasons = []
            if UNIMPORTANT_KEYWORDS.search(title):
                reasons.append("academic")
            if audience and audience.strip().lower() != "public":
                reasons.append(f"audience: {audience}")
            badge = f'<span class="badge">{" · ".join(reasons)}</span>' if reasons else ""

        virtual_badge = f'<span class="virtual-badge">{virtual}</span>' if virtual else ""

        source_label = "Dartmouth" if source == "dartmouth" else "NH Humanities"
        source_pip = f'<span class="source-pip source-pip-{source}" title="{source_label}"></span>'

        return f'''
  <details{open_attr} class="{cls}">
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
      <div class="ev-actions">{link_html}{gcal_html}</div>
    </div>
  </details>'''

    sections = []
    total = 0
    important_count = 0
    for d in sorted(by_date.keys()):
        day_events = sorted(by_date[d], key=sort_key)
        sections.append(f'\n  <h2 class="date-header">{fmt_date_header(d)}</h2>')
        for ev in day_events:
            sections.append(render_event(ev))
            total += 1
            if not ev.get("unimportant"):
                important_count += 1

    body = "\n".join(sections)

    # Build filter bar (only if multiple sources)
    filter_html = ""
    if len(sources_present) > 1:
        btns = ['<button class="filter-btn active" onclick="filterSource(\'all\', this)">All</button>']
        if "dartmouth" in sources_present:
            btns.append('<button class="filter-btn" onclick="filterSource(\'dartmouth\', this)">Dartmouth</button>')
        if "nhhumanities" in sources_present:
            btns.append('<button class="filter-btn" onclick="filterSource(\'nhhumanities\', this)">NH Humanities</button>')
        filter_html = f'<div class="filter-bar">{"".join(btns)}</div>'

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
      --pip-dartmouth:  #4ade80;
      --pip-nhh:        #38bdf8;
      --surf-dartmouth: #1e1e1e;
      --surf-nhh:       #131f2e;
      --bord-dartmouth: #333;
      --bord-nhh:       #1a3048;
      --filter-bg:      #252525;
      --filter-active-bg: #2a4a8a;
      --filter-active-fg: #fff;
      --virtual-bg:     #1a3a1a; --virtual-fg: #86efac;
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
      --pip-dartmouth:  #00693e;
      --pip-nhh:        #0284c7;
      --surf-dartmouth: #fff;
      --surf-nhh:       #f0f8ff;
      --bord-dartmouth: #ddd;
      --bord-nhh:       #bfdbfe;
      --filter-bg:      #eee;
      --filter-active-bg: #4285f4;
      --filter-active-fg: #fff;
      --virtual-bg:     #d1fae5; --virtual-fg: #065f46;
    }

    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 860px;
      margin: 0 auto;
      padding: 1rem 1.5rem 4rem;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      transition: background 0.2s, color 0.2s;
    }
    .header-row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
    h1 { font-size: 1.8rem; margin-bottom: 0.25rem; color: var(--green); }
    .subtitle { color: var(--text-muted); margin-bottom: 0.75rem; font-size: 0.95rem; }
    .subtitle a { color: var(--green); }
    .stats { background: var(--green-bg); border-left: 4px solid var(--green); padding: 0.6rem 1rem;
             margin-bottom: 1rem; border-radius: 0 6px 6px 0; font-size: 0.9rem; }

    .filter-bar {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
    }
    .filter-btn {
      padding: 0.35rem 0.9rem;
      border-radius: 20px;
      border: 1px solid var(--border);
      background: var(--filter-bg);
      color: var(--text-muted);
      cursor: pointer;
      font-size: 0.85rem;
    }
    .filter-btn:hover { border-color: var(--green); color: var(--green); }
    .filter-btn.active { background: var(--filter-active-bg); color: var(--filter-active-fg); border-color: transparent; }

    #theme-btn {
      font-size: 0.8rem;
      padding: 0.3rem 0.75rem;
      border-radius: 5px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
    }
    #theme-btn:hover { border-color: var(--green); color: var(--green); }

    h2.date-header {
      font-size: 1.1rem;
      font-weight: 700;
      color: var(--green);
      margin: 2rem 0 0.5rem;
      padding: 0.4rem 0;
      border-bottom: 2px solid var(--green);
      position: sticky;
      top: 0;
      background: var(--bg);
      z-index: 10;
    }

    details.event {
      margin: 0.35rem 0;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface);
      transition: box-shadow 0.15s;
    }
    details.event[open] { box-shadow: 0 2px 8px var(--shadow); }

    details.source-dartmouth {
      background: var(--surf-dartmouth);
      border-color: var(--bord-dartmouth);
    }
    details.source-nhhumanities {
      background: var(--surf-nhh);
      border-color: var(--bord-nhh);
    }
    details.event.unimportant { opacity: 0.85; }
    details.event.unimportant[open] { opacity: 1; }

    summary {
      padding: 0.6rem 1rem;
      cursor: pointer;
      display: flex;
      align-items: baseline;
      gap: 0.6rem;
      flex-wrap: wrap;
      list-style: none;
      user-select: text;
    }
    summary::-webkit-details-marker { display: none; }
    summary::before {
      content: "▶";
      font-size: 0.6rem;
      color: var(--arrow);
      flex-shrink: 0;
      align-self: center;
      transition: transform 0.15s;
    }
    details[open] > summary::before { transform: rotate(90deg); }

    .source-pip {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
      align-self: center;
      display: inline-block;
    }
    .source-pip-dartmouth { background: var(--pip-dartmouth); }
    .source-pip-nhhumanities { background: var(--pip-nhh); }

    .ev-time {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
      white-space: nowrap;
      min-width: 90px;
    }
    .ev-title {
      font-weight: 600;
      font-size: 0.95rem;
      flex: 1;
    }
    .important .ev-title { color: var(--title-important); }
    .unimportant .ev-title { color: var(--title-unimportant); }
    .ev-location {
      font-size: 0.8rem;
      color: var(--text-dim);
      font-style: italic;
    }
    .badge, .virtual-badge {
      font-size: 0.7rem;
      border-radius: 3px;
      padding: 0.1rem 0.4rem;
      white-space: nowrap;
    }
    .badge { background: var(--badge-bg); color: var(--badge-color); }
    .virtual-badge { background: var(--virtual-bg); color: var(--virtual-fg); }

    .ev-body {
      padding: 0.75rem 1rem 1rem 2.5rem;
      border-top: 1px solid var(--border-body);
      overflow: hidden;
    }
    .ev-image {
      float: right;
      max-width: 160px;
      border-radius: 4px;
      margin: 0 0 0.5rem 1rem;
    }
    .ev-dates {
      font-size: 0.82rem;
      background: var(--dates-bg);
      border-left: 3px solid var(--dates-border);
      padding: 0.3rem 0.6rem;
      margin-bottom: 0.75rem;
      border-radius: 0 4px 4px 0;
    }
    .ev-description {
      font-size: 0.88rem;
      color: var(--text-desc);
      margin-bottom: 0.75rem;
      clear: both;
    }
    .ev-description p { margin: 0.4rem 0; }
    .ev-description a { color: var(--green); }
    .ev-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }
    .chip {
      font-size: 0.75rem;
      padding: 0.2rem 0.5rem;
      border-radius: 12px;
    }
    .chip-audience  { background: var(--chip-aud-bg); color: var(--chip-aud-fg); }
    .chip-sponsor   { background: var(--chip-spo-bg); color: var(--chip-spo-fg); }
    .chip-contact   { background: var(--chip-con-bg); color: var(--chip-con-fg); }
    .chip-category  { background: var(--chip-cat-bg); color: var(--chip-cat-fg); }
    .chip-presenter { background: var(--chip-pre-bg); color: var(--chip-pre-fg); }
    .chip-hostedby  { background: var(--chip-hby-bg); color: var(--chip-hby-fg); }
    .ev-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.5rem; }
    .ev-link {
      font-size: 0.8rem;
      color: var(--green);
      text-decoration: none;
      font-weight: 500;
    }
    .ev-link:hover { text-decoration: underline; }
    .gcal-row { display: flex; flex-wrap: wrap; gap: 0.4rem; }
    .gcal-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.75rem;
      padding: 0.25rem 0.55rem;
      border-radius: 4px;
      background: var(--gcal-bg);
      color: #fff;
      text-decoration: none;
      white-space: nowrap;
    }
    .gcal-btn:hover { background: var(--gcal-hover); }
    @media (max-width: 600px) {
      .ev-image { float: none; max-width: 100%; margin: 0 0 0.5rem; }
      summary { flex-direction: column; gap: 0.2rem; }
      .ev-time { min-width: auto; }
    }
    """

    source_links = {
        "dartmouth": '<a href="https://home.dartmouth.edu/events">home.dartmouth.edu/events</a>',
        "nhhumanities": '<a href="https://www.nhhumanities.org/programs/upcoming">nhhumanities.org</a>',
    }
    subtitle_parts = " · ".join(source_links[s] for s in sources_present if s in source_links)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Upper Valley Events – {start.strftime("%b %-d")} to {end.strftime("%b %-d, %Y")}</title>
  <style>{css}</style>
</head>
<body>
  <div class="header-row">
    <h1>Upper Valley Events</h1>
    <button id="theme-btn" onclick="toggleTheme()">☀ Light mode</button>
  </div>
  <p class="subtitle">Scraped from {subtitle_parts}</p>
  <div class="stats">
    Showing <strong>{total}</strong> events ({important_count} open, {total - important_count} collapsed)
    from <strong>{start.strftime("%B %-d")}</strong> to <strong>{end.strftime("%B %-d, %Y")}</strong>.
  </div>
  {filter_html}
  {body}
<script>
  function filterSource(src, btn) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('details.event').forEach(el => {{
      el.style.display = (src === 'all' || el.classList.contains('source-' + src)) ? '' : 'none';
    }});
    document.querySelectorAll('h2.date-header').forEach(h => {{
      let sib = h.nextElementSibling, visible = false;
      while (sib && !sib.classList.contains('date-header')) {{
        if (sib.style.display !== 'none') {{ visible = true; break; }}
        sib = sib.nextElementSibling;
      }}
      h.style.display = visible ? '' : 'none';
    }});
  }}
  function toggleTheme() {{
    const light = document.body.classList.toggle('light');
    document.getElementById('theme-btn').textContent = light ? '🌙 Dark mode' : '☀ Light mode';
    localStorage.setItem('theme', light ? 'light' : 'dark');
  }}
  if (localStorage.getItem('theme') === 'light') {{
    document.body.classList.add('light');
    document.getElementById('theme-btn').textContent = '🌙 Dark mode';
  }}
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
        eid = ev["id"]
        det = details.get(eid, {})
        merged = {**ev}
        if det:
            merged["title"] = det.get("title") or ev["title"]
            merged["time_str"] = det.get("time_str") or ev["time_str"]
            merged["location"] = det.get("location", "")
            merged["audience"] = det.get("audience", "")
            merged["sponsor"] = det.get("sponsor", "")
            merged["description"] = det.get("description", "")
            merged["about"] = det.get("about", "")
            merged["image"] = det.get("image", "")
            merged["url"] = det.get("url", "")
            merged["contact"] = det.get("contact", "")
            merged["start_dt"] = det.get("start_dt")
            merged["start_iso"] = det.get("start_iso", "")
            merged["duration"] = det.get("duration", "")
        combined.append(merged)

    merged_events = merge_recurring(combined)
    print(f"After merging recurring: {len(merged_events)} distinct Dartmouth events")

    for ev in merged_events:
        ev["unimportant"] = is_unimportant(ev.get("title", ""), ev.get("audience", ""))

    unimportant_count = sum(1 for e in merged_events if e["unimportant"])
    print(f"Dartmouth — Important: {len(merged_events) - unimportant_count}, Unimportant: {unimportant_count}")
    return merged_events


def run_scrape_nhhumanities(today: date, end: date) -> list[dict]:
    raw_events = fetch_nhh_event_list()
    print(f"Found {len(raw_events)} NH Humanities events total")

    raw_events = [e for e in raw_events if e.get("date") and today <= e["date"] <= end]
    print(f"Filtered to {len(raw_events)} NH Humanities events in next 30 days")

    print(f"Fetching details for {len(raw_events)} NH Humanities events...")
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_nhh_detail, ev["url"]): ev for ev in raw_events}
        done = 0
        for future in as_completed(futures):
            ev = futures[future]
            url, detail_html = future.result()
            done += 1
            if detail_html:
                details[url] = parse_nhh_detail(detail_html, ev.get("date"))
    print(f"Successfully fetched {len(details)} NH Humanities detail pages")

    combined = []
    for ev in raw_events:
        det = details.get(ev["url"], {})
        merged = {**ev}
        if det:
            merged["title"] = det.get("title") or ev["title"]
            merged["description"] = det.get("description", "")
            merged["time_str"] = det.get("time_str", "")
            merged["start_dt"] = det.get("start_dt")
            if det.get("address"):
                merged["location"] = det["address"]
            merged["presenter"] = det.get("presenter", "")
            merged["hosted_by"] = det.get("hosted_by", "")
            merged["contact"] = det.get("contact", "")
            merged["category"] = det.get("category", "")
            if det.get("image"):
                merged["image"] = det["image"]
        merged["dates"] = [ev["date"]] if ev.get("date") else []
        merged["unimportant"] = False
        combined.append(merged)

    print(f"NH Humanities: {len(combined)} events")
    return combined


def run_generate(sources: list[str], today: date, end: date) -> None:
    all_events = []
    starts, ends = [], []
    for source in sources:
        events, s, e = load_scrape_results(source)
        all_events.extend(events)
        starts.append(s)
        ends.append(e)

    start = min(starts)
    end = max(ends)

    os.makedirs("output", exist_ok=True)
    stem = f"events_{start.isoformat()}_to_{end.isoformat()}"
    html_output = generate_html(all_events, start, end)
    html_path = os.path.join("output", f"{stem}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_output)
    print(f"HTML written to {html_path} ({len(html_output) / 1024:.1f} KB)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SCRAPE_FNS = {
    "dartmouth": run_scrape_dartmouth,
    "nhhumanities": run_scrape_nhhumanities,
}


def parse_sources(value: str) -> list[str]:
    if value.lower() == "all":
        return list(ALL_SOURCES)
    sources = [s.strip().lower() for s in value.split(",")]
    invalid = [s for s in sources if s not in ALL_SOURCES]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"Unknown source(s): {', '.join(invalid)}. Valid: {', '.join(ALL_SOURCES)}, all"
        )
    return sources


def main():
    parser = argparse.ArgumentParser(
        description="Upper Valley Events Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Sources: {', '.join(ALL_SOURCES)}, all

Examples:
  python scraper.py --scrape=all --generate=all
  python scraper.py --scrape=dartmouth
  python scraper.py --generate=dartmouth,nhhumanities
  python scraper.py --scrape=nhhumanities --generate=nhhumanities
        """,
    )
    parser.add_argument("--scrape", type=parse_sources, metavar="SOURCES",
                        help="Comma-separated sources to scrape (or 'all')")
    parser.add_argument("--generate", type=parse_sources, metavar="SOURCES",
                        help="Comma-separated sources to generate HTML from (or 'all')")
    args = parser.parse_args()

    if not args.scrape and not args.generate:
        parser.print_help()
        sys.exit(0)

    today = date.today()
    end = today + timedelta(days=30)

    if args.scrape:
        for source in args.scrape:
            events = SCRAPE_FNS[source](today, end)
            save_scrape_results(source, events, today, end)

    if args.generate:
        run_generate(args.generate, today, end)


if __name__ == "__main__":
    main()
