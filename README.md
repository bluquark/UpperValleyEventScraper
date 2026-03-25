# Upper Valley Events Scraper

Scrapes upcoming events from multiple Upper Valley sources and produces
a self-contained HTML page: `output/events_YYYY-MM-DD.html`.

**Sources:**
- [home.dartmouth.edu/events](https://home.dartmouth.edu/events)
- [nhhumanities.org/programs/upcoming](https://www.nhhumanities.org/programs/upcoming)
- [northernstage.org](https://northernstage.org) (theater)
- [nlbarn.org](https://www.nlbarn.org) (National Theatre Live screenings)
- [shakerbridgetheatre.org](https://www.shakerbridgetheatre.org) (theater)
- [nugget-theaters.com](https://www.nugget-theaters.com) (Nugget Theater, Hanover)
- [entertainmentcinemas.com/lebanon-6](https://www.entertainmentcinemas.com/lebanon-6) (Lebanon 6)

![Screenshot](example_screenshot.png)

## Features

- Events color-coded and filterable by source group (All / Dartmouth / NH Humanities / Theater / Movies)
- Dartmouth: academic/non-public events collapsed by default; recurring events merged
- NH Humanities: events more than ~30 min drive from Lebanon NH rendered collapsed and dimmed
- Movies: per-day schedule table, YouTube trailer link, per-showing Google Calendar buttons
- Per-event "Add to Google Calendar" button(s)
- Dark/light theme toggle (persisted in `localStorage`)
- URL hash tracks active filter and current scroll position — shareable and restored on reload

## Requirements

```
pip install requests beautifulsoup4
```

Python 3.9+ required (uses `zoneinfo`).

## Usage

HTML is always generated. `--scrape` is optional — omitting it regenerates from existing data files without any network calls.

```
python scraper.py [--scrape=SOURCES] [--days=N] [--final_output=PATH]
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--scrape=SOURCES` | — | Fetch fresh data before generating (omit to use cached JSON) |
| `--days=N` | `30` | Number of days in the date range |
| `--final_output=PATH` | — | Also copy the generated HTML to this path |

`SOURCES` is a comma-separated list of source names or a named group.

| Name | Meaning |
|---|---|
| `all` | All sources |
| `theater` | `northernstage`, `nlbarn`, `shakerbridgetheatre` |
| `movies` | `nugget`, `lebanon6` |
| `dartmouth`, `nhhumanities`, `northernstage`, `nlbarn`, `shakerbridgetheatre`, `nugget`, `lebanon6` | Individual sources |

### Examples

Regenerate HTML from cached data (no network calls):
```
python scraper.py
```

Scrape everything and regenerate:
```
python scraper.py --scrape=all
```

Scrape only movies, then regenerate all:
```
python scraper.py --scrape=movies
```

60-day window, write output directly to a web root:
```
python scraper.py --scrape=all --days=60 --final_output=../LocalWebHost/events.html
```

## Intermediate files

Scraped data is stored as JSON in `output/` before HTML generation:

| Source | File |
|---|---|
| Dartmouth | `output/scraped_dartmouth.json` |
| NH Humanities | `output/scraped_nhhumanities.json` |
| Northern Stage | `output/scraped_northernstage.json` |
| NL Barn | `output/scraped_nlbarn.json` |
| Shaker Bridge | `output/scraped_shakerbridgetheatre.json` |
| Nugget Theater | `output/scraped_nugget.json` |
| Lebanon 6 | `output/scraped_lebanon6.json` |

This lets you re-generate the HTML (e.g. to tweak styling) without re-fetching all event pages.

## Notes

- Dartmouth: scraped via internal AJAX endpoint (`/events/ajax/search`), ~15s with 8 parallel requests
- NH Humanities: listing page parsed for event list; detail pages fetched in parallel
- Movies: showtime pages fetched day-by-day; per-film entries merged into a single card with a schedule table
