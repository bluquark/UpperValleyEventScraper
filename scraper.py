#!/usr/bin/env python3
"""
Dartmouth Events Scraper
Fetches 30 days of events from home.dartmouth.edu/events and produces a
self-contained HTML page. Recurring events are merged; unimportant events
(seminar/colloquium/thesis in title, or non-public audience) are collapsed.
"""

import html as html_mod
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://home.dartmouth.edu"
AJAX_URL = f"{BASE_URL}/events/ajax/search"
DETAIL_URL = f"{BASE_URL}/events/event"

UNIMPORTANT_KEYWORDS = re.compile(r'\b(seminar|colloquium|thesis|dissertation)\b', re.IGNORECASE)

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}


def fetch_event_list(start: date, end: date) -> str:
    """Fetch all events in date range via AJAX API. Returns HTML string of teasers."""
    params = {
        "offset": 0,
        "limit": 300,
        "begin": start.isoformat(),
        "end": end.isoformat(),
    }
    print(f"Fetching event list ({start} to {end})...")
    resp = requests.get(AJAX_URL, params=params, timeout=30)
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
        # Extract event ID and URL from title link
        title_link = teaser.select_one(".event-teaser__title-link")
        if not title_link:
            continue
        href = title_link.get("href", "")
        m = re.search(r'event=(\d+)', href)
        if not m:
            continue
        event_id = m.group(1)

        # Date
        day_el = teaser.select_one(".event-teaser__date-day")
        month_el = teaser.select_one(".event-teaser__date-month")
        day = int(day_el.get_text(strip=True)) if day_el else 0
        month_str = month_el.get_text(strip=True) if month_el else ""
        month = MONTH_MAP.get(month_str, 0)
        # Determine year (handle year boundary)
        year = ref_year
        if month < date.today().month - 1:
            year = ref_year + 1

        # Time and summary from teaser
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
        })
    return events


def fetch_detail(event_id: str) -> tuple[str, str | None]:
    """Fetch detail page HTML for one event. Returns (event_id, html)."""
    try:
        time.sleep(0.05)
        resp = requests.get(DETAIL_URL, params={"event": event_id}, timeout=30)
        if resp.status_code == 200:
            return event_id, resp.text
    except Exception as e:
        print(f"  Warning: failed to fetch event {event_id}: {e}", file=sys.stderr)
    return event_id, None


def parse_detail(html: str) -> dict:
    """Parse event detail page and return structured data dict."""
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD structured data
    ld_tag = soup.find("script", type="application/ld+json")
    ld = {}
    if ld_tag and ld_tag.string:
        try:
            ld = json.loads(ld_tag.string)
        except json.JSONDecodeError:
            pass

    # Human-readable time range from meta section
    meta_items = soup.select(".news-event--meta__item--text")
    time_str = ""
    if len(meta_items) >= 2:
        time_str = meta_items[1].get_text(strip=True)
        if time_str == "Add to Calendar":
            time_str = ""

    # Contact info
    contact_el = soup.select_one(".news-event--details__group--contact .news-event--details__group-text")
    contact = contact_el.get_text(strip=True) if contact_el else ""

    # Parse startDate — keep timezone for UTC conversion
    start_iso = ld.get("startDate", "")
    start_dt_utc = None
    if start_iso:
        try:
            clean = re.sub(r'\.\d+', '', start_iso)  # remove milliseconds
            dt = datetime.fromisoformat(clean)
            start_dt_utc = dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass

    return {
        "title": ld.get("name", ""),
        "about": ld.get("about", ""),
        "description": ld.get("description", ""),
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


def canonical_title(title: str) -> str:
    """Normalize title for recurring-event grouping by removing speaker suffixes."""
    # Remove trailing "- A.Name" or "- A.Name, B.Name" (initials with dots)
    t = re.sub(r'\s*[-–]\s*[A-Z][A-Za-z]*\.[\w.,\s]+$', '', title)
    # Remove trailing "- Firstname Lastname" (two capitalized words)
    t = re.sub(r'\s*[-–]\s*[A-Z][a-z]+\s+[A-Z][a-z]+$', '', t)
    # Normalize whitespace and ampersands
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def is_unimportant(title: str, audience: str) -> bool:
    """Return True if event should be collapsed by default."""
    if UNIMPORTANT_KEYWORDS.search(title):
        return True
    if audience and audience.strip().lower() != "public":
        return True
    return False


def merge_recurring(events: list[dict]) -> list[dict]:
    """
    Group events with the same canonical title + time + location into single entries.
    Each merged event has a 'dates' list sorted chronologically.
    """
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
        # Sort by date
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
    """Sort key: first date, then start time."""
    first_date = ev["dates"][0] if ev.get("dates") else date.max
    # Parse time for secondary sort
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
    """Parse '1 hours', '4 hours', '1 day', '17 days' → hours as float."""
    if not duration_str:
        return 1.0
    m = re.match(r'(\d+(?:\.\d+)?)\s*(hour|day)', duration_str.lower())
    if not m:
        return 1.0
    n = float(m.group(1))
    return n * 24 if 'day' in m.group(2) else n


def event_datetimes(ev: dict, occurrence_date: date) -> tuple[datetime, datetime, bool]:
    """
    Return (start_utc, end_utc, is_all_day) for a specific occurrence date.
    Uses the time-of-day from ev['start_dt'] (UTC) combined with occurrence_date.
    """
    start_dt: datetime | None = ev.get("start_dt")
    duration_str = ev.get("duration", "")

    if start_dt is None:
        # Fallback: midnight UTC all-day
        start_utc = datetime(occurrence_date.year, occurrence_date.month, occurrence_date.day,
                             tzinfo=timezone.utc)
        end_utc = start_utc + timedelta(days=1)
        return start_utc, end_utc, True

    # All-day: original time is midnight UTC (was midnight local → ~4am UTC, but
    # if hour < 5 and no time_str, treat as all-day)
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
        # Same time-of-day (UTC) but on occurrence_date
        start_utc = start_dt.replace(
            year=occurrence_date.year,
            month=occurrence_date.month,
            day=occurrence_date.day,
        )
        hours = parse_duration_hours(duration_str)
        if hours >= 20:
            hours = 1.0  # duration "1 day" for a timed event → treat as 1h
        end_utc = start_utc + timedelta(hours=hours)
        return start_utc, end_utc, False


def gcal_url(ev: dict, occurrence_date: date) -> str:
    """Build a Google Calendar 'add event' URL for one occurrence."""
    start_utc, end_utc, is_all_day = event_datetimes(ev, occurrence_date)
    title = ev.get("title", "")
    location = ev.get("location", "")
    # Plain-text description
    desc_html = ev.get("description") or ev.get("about") or ""
    desc = html_mod.unescape(re.sub(r'<[^>]+>', '', desc_html)).strip()
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
        params["details"] = desc[:1500]  # GCal URL limit
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------

def ics_escape(s: str) -> str:
    """Escape special characters for ICS text values."""
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def ics_fold(line: str) -> str:
    """Fold long ICS lines at 75 characters (close enough to RFC 5545)."""
    if len(line) <= 75:
        return line + "\r\n"
    result = []
    while len(line) > 75:
        result.append(line[:75])
        line = " " + line[75:]
    result.append(line)
    return "\r\n".join(result) + "\r\n"


def generate_ics(events: list[dict], start: date, end: date) -> str:
    """Generate a single VCALENDAR ICS string with one VEVENT per occurrence."""
    lines = [
        "BEGIN:VCALENDAR\r\n",
        "VERSION:2.0\r\n",
        "PRODID:-//Dartmouth Events Scraper//EN\r\n",
        "CALSCALE:GREGORIAN\r\n",
        f"X-WR-CALNAME:Dartmouth Events {start.isoformat()} to {end.isoformat()}\r\n",
        "X-WR-TIMEZONE:America/New_York\r\n",
    ]

    for ev in events:
        dates = ev.get("dates") or []
        title = ev.get("title", "Untitled")
        location = ev.get("location", "")
        desc_html = ev.get("description") or ev.get("about") or ev.get("summary") or ""
        desc_plain = html_mod.unescape(re.sub(r'<[^>]+>', '', desc_html)).strip()
        if ev.get("url"):
            desc_plain = (desc_plain + "\n\n" if desc_plain else "") + ev["url"]
        url = ev.get("url", "")
        event_id = ev.get("id", "unknown")

        for occ_date in dates:
            start_utc, end_utc, is_all_day = event_datetimes(ev, occ_date)
            uid = f"dartmouth-{event_id}-{occ_date.isoformat()}@home.dartmouth.edu"

            lines.append("BEGIN:VEVENT\r\n")
            lines.append(ics_fold(f"UID:{uid}"))
            lines.append(ics_fold(f"SUMMARY:{ics_escape(title)}"))

            if is_all_day:
                lines.append(ics_fold(f"DTSTART;VALUE=DATE:{start_utc.strftime('%Y%m%d')}"))
                lines.append(ics_fold(f"DTEND;VALUE=DATE:{end_utc.strftime('%Y%m%d')}"))
            else:
                lines.append(ics_fold(f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}"))
                lines.append(ics_fold(f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}"))

            if location:
                lines.append(ics_fold(f"LOCATION:{ics_escape(location)}"))
            if desc_plain:
                lines.append(ics_fold(f"DESCRIPTION:{ics_escape(desc_plain)}"))
            if url:
                lines.append(ics_fold(f"URL:{url}"))
            lines.append("END:VEVENT\r\n")

    lines.append("END:VCALENDAR\r\n")
    return "".join(lines)


def generate_html(events: list[dict], start: date, end: date, ics_filename: str = "") -> str:
    """Generate the full self-contained HTML page."""

    # Group events by first date
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
        title = ev.get("title") or ev.get("teaser_title", "Untitled")
        unimportant = ev.get("unimportant", False)
        open_attr = "" if unimportant else " open"
        cls = "event unimportant" if unimportant else "event important"

        time_display = format_time(ev.get("time_str", ""))
        location = ev.get("location", "")
        audience = ev.get("audience", "")
        sponsor = ev.get("sponsor", "")
        description = ev.get("description", "")
        about = ev.get("about", "")
        image = ev.get("image", "")
        url = ev.get("url", "")
        contact = ev.get("contact", "")
        dates = ev.get("dates", [])

        # Dates line (only show if recurring — more than 1 date)
        dates_html = ""
        if len(dates) > 1:
            date_strs = ", ".join(fmt_date_short(d) for d in dates)
            dates_html = f'<p class="ev-dates">All dates: {date_strs}</p>'

        # Description: prefer full HTML description, else about text
        desc_html = ""
        if description and description.strip():
            desc_html = f'<div class="ev-description">{description}</div>'
        elif about and about.strip():
            desc_html = f'<div class="ev-description"><p>{about}</p></div>'

        # Meta chips
        meta_parts = []
        if audience and audience.lower() != "public":
            meta_parts.append(f'<span class="chip chip-audience">{audience}</span>')
        if sponsor:
            meta_parts.append(f'<span class="chip chip-sponsor">{sponsor}</span>')
        if contact:
            meta_parts.append(f'<span class="chip chip-contact">{contact}</span>')
        meta_html = f'<div class="ev-meta">{"".join(meta_parts)}</div>' if meta_parts else ""

        img_html = f'<img class="ev-image" src="{image}" alt="">' if image else ""
        link_html = f'<a class="ev-link" href="{url}" target="_blank">Full details ↗</a>' if url else ""

        # Google Calendar buttons — one per occurrence date
        gcal_parts = []
        for d in dates:
            label = fmt_date_short(d) if len(dates) > 1 else "Add to Google Calendar"
            gcal_parts.append(
                f'<a class="gcal-btn" href="{gcal_url(ev, d)}" target="_blank">'
                f'<svg viewBox="0 0 24 24" width="13" height="13"><path d="M19 4h-1V2h-2v2H8V2H6v2H5C3.9 4 3 4.9 3 6v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V9h14v11zM7 11h5v5H7z" fill="currentColor"/></svg>'
                f' {label}</a>'
            )
        gcal_html = f'<div class="gcal-row">{"".join(gcal_parts)}</div>'

        # Unimportant badge
        badge = ""
        if unimportant:
            reasons = []
            if UNIMPORTANT_KEYWORDS.search(title):
                reasons.append("academic")
            if audience and audience.strip().lower() != "public":
                reasons.append(f"audience: {audience}")
            badge = f'<span class="badge">{" · ".join(reasons)}</span>' if reasons else ""

        return f'''
  <details{open_attr} class="{cls}">
    <summary>
      <span class="ev-time">{time_display}</span>
      <span class="ev-title">{title}</span>
      {badge}
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

    # Build body sections
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

    css = """
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 860px;
      margin: 0 auto;
      padding: 1rem 1.5rem 4rem;
      background: #f5f5f5;
      color: #1a1a1a;
      line-height: 1.5;
    }
    h1 { font-size: 1.8rem; margin-bottom: 0.25rem; color: #00693e; }
    .subtitle { color: #555; margin-bottom: 1.5rem; font-size: 0.95rem; }
    .stats { background: #e8f4ee; border-left: 4px solid #00693e; padding: 0.6rem 1rem;
             margin-bottom: 2rem; border-radius: 0 6px 6px 0; font-size: 0.9rem; }

    h2.date-header {
      font-size: 1.1rem;
      font-weight: 700;
      color: #00693e;
      margin: 2rem 0 0.5rem;
      padding: 0.4rem 0;
      border-bottom: 2px solid #00693e;
      position: sticky;
      top: 0;
      background: #f5f5f5;
      z-index: 10;
    }

    details.event {
      margin: 0.35rem 0;
      border-radius: 6px;
      border: 1px solid #ddd;
      background: #fff;
      transition: box-shadow 0.15s;
    }
    details.event[open] {
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }
    details.event.unimportant {
      background: #fafafa;
      border-color: #e8e8e8;
      opacity: 0.85;
    }
    details.event.unimportant[open] {
      opacity: 1;
    }

    summary {
      padding: 0.6rem 1rem;
      cursor: pointer;
      display: flex;
      align-items: baseline;
      gap: 0.6rem;
      flex-wrap: wrap;
      list-style: none;
      user-select: none;
    }
    summary::-webkit-details-marker { display: none; }
    summary::before {
      content: "▶";
      font-size: 0.6rem;
      color: #999;
      flex-shrink: 0;
      align-self: center;
      transition: transform 0.15s;
    }
    details[open] > summary::before {
      transform: rotate(90deg);
    }

    .ev-time {
      font-size: 0.8rem;
      font-weight: 600;
      color: #666;
      white-space: nowrap;
      min-width: 90px;
    }
    .ev-title {
      font-weight: 600;
      font-size: 0.95rem;
      flex: 1;
    }
    .ev-location {
      font-size: 0.8rem;
      color: #777;
      font-style: italic;
    }
    .badge {
      font-size: 0.7rem;
      background: #f0e8ff;
      color: #6b21a8;
      border-radius: 3px;
      padding: 0.1rem 0.4rem;
      white-space: nowrap;
    }

    .ev-body {
      padding: 0.75rem 1rem 1rem 2.5rem;
      border-top: 1px solid #f0f0f0;
    }
    .ev-image {
      float: right;
      max-width: 160px;
      border-radius: 4px;
      margin: 0 0 0.5rem 1rem;
    }
    .ev-dates {
      font-size: 0.82rem;
      background: #fff8e1;
      border-left: 3px solid #f59e0b;
      padding: 0.3rem 0.6rem;
      margin-bottom: 0.75rem;
      border-radius: 0 4px 4px 0;
    }
    .ev-description {
      font-size: 0.88rem;
      color: #333;
      margin-bottom: 0.75rem;
      clear: both;
    }
    .ev-description p { margin: 0.4rem 0; }
    .ev-description a { color: #00693e; }
    .ev-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }
    .chip {
      font-size: 0.75rem;
      padding: 0.2rem 0.5rem;
      border-radius: 12px;
    }
    .chip-audience { background: #dbeafe; color: #1e40af; }
    .chip-sponsor  { background: #dcfce7; color: #166534; }
    .chip-contact  { background: #fef3c7; color: #92400e; }
    .ev-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.5rem; }
    .ev-link {
      font-size: 0.8rem;
      color: #00693e;
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
      background: #4285f4;
      color: #fff;
      text-decoration: none;
      white-space: nowrap;
    }
    .gcal-btn:hover { background: #3367d6; }
    .ics-link {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.85rem;
      font-weight: 500;
      color: #00693e;
      text-decoration: none;
      padding: 0.3rem 0.7rem;
      border: 1px solid #00693e;
      border-radius: 5px;
      margin-top: 0.5rem;
    }
    .ics-link:hover { background: #e8f4ee; }

    @media (max-width: 600px) {
      .ev-image { float: none; max-width: 100%; margin: 0 0 0.5rem; }
      summary { flex-direction: column; gap: 0.2rem; }
      .ev-time { min-width: auto; }
    }
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dartmouth Events – {start.strftime("%b %-d")} to {end.strftime("%b %-d, %Y")}</title>
  <style>{css}</style>
</head>
<body>
  <h1>Dartmouth Events</h1>
  <p class="subtitle">Scraped from <a href="https://home.dartmouth.edu/events">home.dartmouth.edu/events</a></p>
  {f'<a class="ics-link" href="{ics_filename}" download>📅 Download all events (.ics for Google/Apple Calendar)</a>' if ics_filename else ''}
  <div class="stats">
    Showing <strong>{total}</strong> events ({important_count} open, {total - important_count} collapsed)
    from <strong>{start.strftime("%B %-d")}</strong> to <strong>{end.strftime("%B %-d, %Y")}</strong>.
    Collapsed events contain "seminar", "colloquium", or "thesis" in the title, or are not open to the general public.
    Recurring events are merged into a single entry.
  </div>
  {body}
</body>
</html>"""


def main():
    today = date.today()
    end = today + timedelta(days=30)

    # Step 1: Fetch event list
    list_html = fetch_event_list(today, end)

    # Step 2: Parse teasers
    raw_events = parse_event_list(list_html, today.year)
    print(f"Found {len(raw_events)} event entries in listing")

    # Deduplicate by event ID (same event may appear multiple days in listing
    # e.g. multi-day exhibits)
    seen_ids: dict[str, dict] = {}
    for ev in raw_events:
        eid = ev["id"]
        if eid not in seen_ids:
            seen_ids[eid] = ev
        else:
            # Keep track of additional occurrence dates
            pass
    unique_ids = list(seen_ids.keys())
    print(f"Fetching details for {len(unique_ids)} unique events...")

    # Step 3: Fetch detail pages in parallel
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_detail, eid): eid for eid in unique_ids}
        done = 0
        for future in as_completed(futures):
            eid, html = future.result()
            done += 1
            if html:
                details[eid] = parse_detail(html)
            if done % 20 == 0:
                print(f"  {done}/{len(unique_ids)} details fetched...")

    print(f"Successfully fetched {len(details)} detail pages")

    # Step 4: Combine teaser + detail data
    combined = []
    for ev in raw_events:
        eid = ev["id"]
        det = details.get(eid, {})
        merged = {**ev}
        if det:
            # Prefer detail data for title, time, location, etc.
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
        combined.append(merged)

    # Step 5: Merge recurring events
    merged_events = merge_recurring(combined)
    print(f"After merging recurring: {len(merged_events)} distinct events")

    # Step 6: Classify unimportant
    for ev in merged_events:
        ev["unimportant"] = is_unimportant(
            ev.get("title", ""),
            ev.get("audience", "")
        )

    unimportant_count = sum(1 for e in merged_events if e["unimportant"])
    print(f"Important: {len(merged_events) - unimportant_count}, Unimportant: {unimportant_count}")

    # Step 7: Generate ICS
    stem = f"events_{today.isoformat()}_to_{end.isoformat()}"
    ics_path = f"{stem}.ics"
    ics_output = generate_ics(merged_events, today, end)
    with open(ics_path, "w", encoding="utf-8", newline="") as f:
        f.write(ics_output)
    ics_event_count = ics_output.count("BEGIN:VEVENT")
    print(f"ICS written to {ics_path} ({ics_event_count} occurrences, {len(ics_output) / 1024:.1f} KB)")

    # Step 8: Generate HTML
    html_output = generate_html(merged_events, today, end, ics_filename=ics_path)
    html_path = f"{stem}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_output)
    print(f"HTML written to {html_path} ({len(html_output) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
