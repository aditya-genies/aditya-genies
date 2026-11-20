#!/usr/bin/env python3
"""Make the profile stats cards in assets/ from GitHub activity, private repos included.

Needs GH_TOKEN: a token for the profile owner that can read the private repos to count.
Uses the Search API because contributionsCollection does not count the private org work.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

LOGIN = "aditya-genies"
TZ = ZoneInfo("America/Los_Angeles")
API = "https://api.github.com"
OUT = Path(__file__).resolve().parent.parent / "assets"
SEARCH_CAP = 1000  # GitHub search returns at most 1000 results for one query.

TOKEN = os.environ.get("GH_TOKEN") or sys.exit("GH_TOKEN is not set. Add the STATS_TOKEN secret to the repo.")

PERIOD_MONTHS = 4
PERIOD_LABEL = f"last {PERIOD_MONTHS} months"


def midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), TZ)


def months_before(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return day.replace(year=year, month=month + 1, day=min(day.day, monthrange(year, month + 1)[1]))


NOW = datetime.now(TZ)
TODAY = NOW.date()
# The main period: the last 4 months, in whole days, today included.
PERIOD_START = months_before(TODAY, PERIOD_MONTHS) + timedelta(days=1)
PERIOD_DAYS = (TODAY - PERIOD_START).days + 1
# Heatmap columns start on a Sunday, like the GitHub graph.
HEAT_START = PERIOD_START - timedelta(days=(PERIOD_START.weekday() + 1) % 7)
# Fetch one more period before the main period, for the comparison.
START = midnight(PERIOD_START - timedelta(days=PERIOD_DAYS))
END = midnight(TODAY + timedelta(days=1))


# --- GitHub API ---------------------------------------------------------------------------


def request(url: str, body: dict | None = None) -> dict:
    """Send one API request. Wait and retry on rate limits and server errors."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-stats",
    }
    for attempt in range(8):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            retry_after = err.headers.get("Retry-After")
            reset = err.headers.get("X-RateLimit-Reset") if err.headers.get("X-RateLimit-Remaining") == "0" else None
            # A secondary rate limit can come with no headers. GitHub then asks for a wait of at least 1 minute.
            secondary = err.code == 403 and b"secondary rate limit" in err.read().lower()
            rate_limited = err.code == 429 or (err.code == 403 and (retry_after or reset or secondary))
            if not (rate_limited or err.code >= 500) or attempt == 7:
                raise
            if retry_after:
                wait = float(retry_after)
            elif reset:
                wait = float(reset) - time.time() + 1
            elif secondary:
                wait = 60
            else:
                wait = 2**attempt
        except (urllib.error.URLError, TimeoutError):
            if attempt == 7:
                raise
            wait = 2**attempt
        time.sleep(min(max(wait, 1), 300))
    raise AssertionError("unreachable")


def graphql(query: str, **variables: object) -> dict:
    result = request(f"{API}/graphql", {"query": query, "variables": variables})
    if result.get("errors"):
        raise RuntimeError(f"GraphQL error: {result['errors']}")
    return result["data"]


def date_range(start: datetime, end: datetime) -> str:
    return f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}"


# A page fetcher takes (start, end, page token) and returns (total, items, next page token).
PageToken = int | str | None
PageFetcher = Callable[[datetime, datetime, PageToken], tuple[int, list, PageToken]]


def search_all(fetch: PageFetcher, start: datetime, end: datetime) -> Iterator:
    """Yield all results in [start, end]. Split the range while it holds more than the search cap."""
    total, items, token = fetch(start, end, None)
    if total > SEARCH_CAP and end - start > timedelta(hours=1):
        middle = start + (end - start) / 2
        yield from search_all(fetch, start, middle)
        yield from search_all(fetch, middle, end)
        return
    if total > SEARCH_CAP:
        print(f"Warning: {total} results in {date_range(start, end)}; only {SEARCH_CAP} are counted.", file=sys.stderr)
    yield from items
    while token is not None:
        _, items, token = fetch(start, end, token)
        yield from items


def commit_fetcher(start: datetime, end: datetime, token: PageToken) -> tuple[int, list, PageToken]:
    page = int(token or 1)
    query = f"author:{LOGIN} author-date:{date_range(start, end)}"
    params = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
    data = request(f"{API}/search/commits?{params}")
    has_more = data["items"] and page * 100 < min(data["total_count"], SEARCH_CAP)
    return data["total_count"], data["items"], page + 1 if has_more else None


REPO_FIELDS = "repository { nameWithOwner primaryLanguage { name color } }"
PR_QUERY = f"""
query($q: String!, $after: String) {{
  search(type: ISSUE, query: $q, first: 100, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ id createdAt mergedAt additions deletions {REPO_FIELDS} }} }}
  }}
}}"""
REVIEW_QUERY = f"""
query($q: String!, $after: String, $login: String!) {{
  search(type: ISSUE, query: $q, first: 100, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ id {REPO_FIELDS} reviews(author: $login, first: 100) {{ nodes {{ submittedAt }} }} }} }}
  }}
}}"""


def pr_fetcher(query: str, base: str, field: str, **extra: str) -> PageFetcher:
    """Make a fetcher for a GraphQL PR search that filters `field` by the date range."""

    def fetch(start: datetime, end: datetime, cursor: PageToken) -> tuple[int, list, PageToken]:
        q = f"{base} {field}:{date_range(start, end)}"
        search = graphql(query, q=q, after=cursor, **extra)["search"]
        page = search["pageInfo"]
        return search["issueCount"], search["nodes"], page["endCursor"] if page["hasNextPage"] else None

    return fetch


# --- Data ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    kind: str  # "commit", "pr" or "review"
    at: datetime  # Pacific time
    repo: str
    ref: str  # commit SHA or PR id


@dataclass(frozen=True)
class Merge:
    at: datetime
    additions: int
    deletions: int


def local(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(TZ)


def collect() -> tuple[list[Event], list[Merge], dict[str, dict | None]]:
    events: list[Event] = []
    merges: list[Merge] = []
    languages: dict[str, dict | None] = {}
    seen: set[str] = set()

    authored = f"author:{LOGIN} type:pr"
    pr_searches = [
        pr_fetcher(PR_QUERY, authored, "created"),
        # PRs created before the start that were merged after it.
        pr_fetcher(PR_QUERY, f"{authored} is:merged created:<{START.date().isoformat()}", "merged"),
    ]
    for fetch in pr_searches:
        for pr in search_all(fetch, START, NOW):
            if pr["id"] in seen:
                continue
            seen.add(pr["id"])
            repo = pr["repository"]["nameWithOwner"]
            languages[repo] = pr["repository"]["primaryLanguage"]
            events.append(Event("pr", local(pr["createdAt"]), repo, pr["id"]))
            if pr["mergedAt"]:
                merges.append(Merge(local(pr["mergedAt"]), pr["additions"], pr["deletions"]))
    print(f"PRs: {len(seen)}", file=sys.stderr)

    reviewed = pr_fetcher(REVIEW_QUERY, f"reviewed-by:{LOGIN} -author:{LOGIN} type:pr", "updated", login=LOGIN)
    review_prs: set[str] = set()
    for pr in search_all(reviewed, START, NOW):
        if pr["id"] in review_prs:
            continue
        review_prs.add(pr["id"])
        repo = pr["repository"]["nameWithOwner"]
        languages[repo] = pr["repository"]["primaryLanguage"]
        for review in pr["reviews"]["nodes"]:
            if review["submittedAt"]:  # A pending review has no submit time.
                events.append(Event("review", local(review["submittedAt"]), repo, pr["id"]))
    print(f"Reviewed PRs: {len(review_prs)}", file=sys.stderr)

    shas: set[str] = set()
    for commit in search_all(commit_fetcher, START, NOW):
        if commit["sha"] in shas:
            continue
        shas.add(commit["sha"])
        repo = commit["repository"]["full_name"]
        languages.setdefault(repo, None)
        events.append(Event("commit", local(commit["commit"]["author"]["date"]), repo, commit["sha"]))
    print(f"Commits: {len(shas)}", file=sys.stderr)

    events = [e for e in events if START <= e.at <= NOW]
    return events, merges, languages


def window_stats(events: list[Event], merges: list[Merge], first: date, days: int) -> dict[str, int]:
    """Count the activity in `days` whole days that start on `first`."""
    start, end = midnight(first), midnight(first + timedelta(days=days))
    inside = [e for e in events if start <= e.at < end]
    merged = [m for m in merges if start <= m.at < end]
    return {
        "commits": sum(e.kind == "commit" for e in inside),
        "opened": sum(e.kind == "pr" for e in inside),
        "merged": len(merged),
        "reviewed": len({e.ref for e in inside if e.kind == "review"}),
        "added": sum(m.additions for m in merged),
        "deleted": sum(m.deletions for m in merged),
        "days": len({e.at.date() for e in inside}),
        "repos": len({e.repo for e in inside}),
    }


def streaks(active: set[date]) -> tuple[int, int]:
    """Return (current, longest) runs of active days. The current run can end yesterday."""
    longest = run = 0
    previous = None
    for day in sorted(active):
        run = run + 1 if previous and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    day = TODAY if TODAY in active else TODAY - timedelta(days=1)
    current = 0
    while day in active:
        current += 1
        day -= timedelta(days=1)
    return current, longest


# --- SVG ----------------------------------------------------------------------------------

W = 840
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
# Reference palette from the dataviz skill: blue sequential ramp and categorical slots 1-3.
THEMES = {
    "light": {
        "card": "#ffffff", "border": "#d0d7de", "panel": "#f6f8fa", "text": "#1f2328", "muted": "#59636e",
        "faint": "#eaeef2", "series": ("#2a78d6", "#eb6834", "#1baf7a"), "up": "#1a7f37", "down": "#d03b3b",
        "heat": ("#eaeef2", "#b7d3f6", "#6da7ec", "#2a78d6", "#184f95"),
    },
    "dark": {
        "card": "#0d1117", "border": "#30363d", "panel": "#161b22", "text": "#e6edf3", "muted": "#8b949e",
        "faint": "#21262d", "series": ("#3987e5", "#d95926", "#199e70"), "up": "#0ca30c", "down": "#e66767",
        "heat": ("#1b222c", "#184f95", "#2a78d6", "#5598e7", "#9ec5f4"),
    },
}


def fmt(n: float) -> str:
    return f"{n:,}"


def compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def hour_label(hour: int) -> str:
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def card(theme: dict, height: int, title: str, subtitle: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" role="img" aria-label="{escape(title)}">
<style>
text {{ font-family: {FONT}; fill: {theme['text']}; }}
.m {{ fill: {theme['muted']}; }}
.up {{ fill: {theme['up']}; }}
.down {{ fill: {theme['down']}; }}
.n {{ font-variant-numeric: tabular-nums; }}
.in {{ animation: in .7s ease-out both; }}
@keyframes in {{ from {{ opacity: 0; }} }}
@media (prefers-reduced-motion: reduce) {{ .in {{ animation: none; }} }}
</style>
<rect x=".5" y=".5" width="{W - 1}" height="{height - 1}" rx="12" fill="{theme['card']}" stroke="{theme['border']}"/>
<text x="24" y="38" font-size="16" font-weight="600">{escape(title)}</text>
<text x="{W - 24}" y="38" font-size="12" class="m" text-anchor="end">{escape(subtitle)}</text>
{body}
</svg>
"""


def stat(x: float, y: float, value: str, label: str, delay: float = 0) -> str:
    return (
        f'<g class="in" style="animation-delay:{delay:.2f}s">'
        f'<text x="{x}" y="{y}" font-size="20" font-weight="600" class="n">{value}</text>'
        f'<text x="{x}" y="{y + 18}" font-size="11" class="m">{escape(label)}</text></g>'
    )


def header_card(theme: dict, total: int, weekly: list[int]) -> str:
    blue, _, aqua = theme["series"]
    # Sparkline of weekly contributions, right side.
    sx, sy, sw, sh = W - 228, 84, 200, 32
    peak = max(weekly) or 1
    step = sw / (len(weekly) - 1)
    points = [(sx + i * step, sy + sh - sh * v / peak) for i, v in enumerate(weekly)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
    area = f"{sx},{sy + sh} {line} {sx + sw},{sy + sh}"
    last_x, last_y = points[-1]

    body = f"""<defs>
<linearGradient id="accent" x1="0" x2="1"><stop offset="0" stop-color="{blue}"/><stop offset="1" stop-color="{aqua}"/></linearGradient>
<linearGradient id="fade" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="{blue}" stop-opacity=".28"/><stop offset="1" stop-color="{blue}" stop-opacity="0"/></linearGradient>
<clipPath id="top"><rect x="0" y="0" width="{W}" height="200" rx="12"/></clipPath>
</defs>
<rect x="0" y="0" width="{W}" height="4" fill="url(#accent)" clip-path="url(#top)"/>
<g class="in">
<text x="28" y="70" font-size="32" font-weight="700" letter-spacing="-.5">Aditya Mantri</text>
<text x="28" y="98" font-size="15" class="m">AI Backend Engineer at Genies</text>
</g>
<g class="in" style="animation-delay:.2s">
<text x="{W - 28}" y="52" font-size="28" font-weight="700" text-anchor="end" class="n">{fmt(total)}</text>
<text x="{W - 28}" y="72" font-size="12" class="m" text-anchor="end">contributions in the {PERIOD_LABEL}</text>
<polygon points="{area}" fill="url(#fade)"/>
<polyline points="{line}" fill="none" stroke="{blue}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{blue}" stroke="{theme['card']}" stroke-width="2"/>
</g>"""
    # The base card draws a title row; the header uses its own layout, so drop the empty title.
    return card(theme, 136, "", "", body)


def delta(current: int, previous: int) -> str:
    if previous == 0:
        return '<tspan class="m">new</tspan>' if current else '<tspan class="m">—</tspan>'
    change = round(100 * (current - previous) / previous)
    if change > 0:
        return f'<tspan class="up">▲</tspan><tspan class="m"> {change}%</tspan>'
    if change < 0:
        return f'<tspan class="down">▼</tspan><tspan class="m"> {-change}%</tspan>'
    return '<tspan class="m">— 0%</tspan>'


def pulse_card(theme: dict, windows: list[tuple[str, int, dict, dict]]) -> str:
    rows = [
        ("Commits", "commits"),
        ("PRs opened", "opened"),
        ("PRs merged", "merged"),
        ("PRs reviewed", "reviewed"),
        ("Lines shipped", "lines"),
        ("Active days", "days"),
        ("Repos touched", "repos"),
    ]
    top, row_h, col_w, col_x0 = 96, 32, 180, 270
    height = top + len(rows) * row_h + 24
    parts = []
    for r, (label, _) in enumerate(rows):
        y = top + r * row_h + 21
        parts.append(f'<text x="24" y="{y}" font-size="13" class="m">{label}</text>')
        if r:
            parts.append(f'<rect x="24" y="{top + r * row_h}" width="{W - 48}" height="1" fill="{theme["faint"]}"/>')
    for c, (name, days, cur, prev) in enumerate(windows):
        x = col_x0 + c * (col_w + 2)
        cells = [
            f'<rect x="{x}" y="58" width="{col_w}" height="{height - 70}" rx="8" fill="{theme["panel"]}"/>',
            f'<text x="{x + 14}" y="80" font-size="11" font-weight="600" letter-spacing=".6" class="m">{name.upper()}</text>',
        ]
        for r, (_, key) in enumerate(rows):
            y = top + r * row_h + 21
            if key == "lines":
                value = (
                    f'<tspan>+{compact(cur["added"])}</tspan><tspan class="m"> / −{compact(cur["deleted"])}</tspan>'
                )
                change = (cur["added"] + cur["deleted"], prev["added"] + prev["deleted"])
            elif key == "days":
                value = f'{cur["days"]}<tspan class="m" font-size="12" font-weight="400"> / {days}</tspan>'
                change = (cur["days"], prev["days"])
            else:
                value = fmt(cur[key])
                change = (cur[key], prev[key])
            cells.append(f'<text x="{x + 14}" y="{y}" font-size="15" font-weight="600" class="n">{value}</text>')
            cells.append(
                f'<text x="{x + col_w - 14}" y="{y}" font-size="11" text-anchor="end" class="n">{delta(*change)}</text>'
            )
        parts.append(f'<g class="in" style="animation-delay:{0.1 * c:.1f}s">{"".join(cells)}</g>')
    subtitle = f"vs. the previous period · updated {NOW:%b %-d, %Y}"
    return card(theme, height, "Pulse", subtitle, "".join(parts))


def heatmap_card(theme: dict, daily: Counter, current: int, longest: int) -> str:
    """Draw the period as a weekly grid, with the streak stats on the right."""
    total = sum(daily.values())
    best_day, best = max(daily.items(), key=lambda kv: kv[1], default=(TODAY, 0))
    nonzero = sorted(daily.values())
    cuts = [nonzero[int(len(nonzero) * q)] for q in (0.25, 0.5, 0.75)] if nonzero else [0, 0, 0]
    x0, y0, cell, step = 58, 84, 20, 24
    weeks = (TODAY - HEAT_START).days // 7 + 1
    cells, months = [], []
    for col in range(weeks):
        for row in range(7):
            day = HEAT_START + timedelta(weeks=col, days=row)
            if day < PERIOD_START or day > TODAY:
                continue
            count = daily.get(day, 0)
            level = 0 if not count else 1 + sum(count > cut for cut in cuts)
            cells.append(
                f'<rect x="{x0 + col * step}" y="{y0 + row * step}" width="{cell}" height="{cell}" rx="4" fill="{theme["heat"][level]}"/>'
            )
            if day.day == 1:
                months.append(f'<text x="{x0 + col * step}" y="{y0 - 10}" font-size="11" class="m">{day:%b}</text>')
    days = "".join(
        f'<text x="24" y="{y0 + row * step + 14}" font-size="10" class="m">{name}</text>'
        for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri"))
    )
    ly = y0 + 7 * step + 12
    legend = [f'<text x="{x0}" y="{ly + 9}" font-size="10" class="m">Less</text>']
    legend += [
        f'<rect x="{x0 + 30 + i * 14}" y="{ly}" width="11" height="11" rx="2.5" fill="{color}"/>'
        for i, color in enumerate(theme["heat"])
    ]
    legend.append(f'<text x="{x0 + 30 + 5 * 14 + 4}" y="{ly + 9}" font-size="10" class="m">More</text>')
    note = f'<text x="{W - 24}" y="{ly + 9}" font-size="10" class="m" text-anchor="end">Commits, PRs opened, and reviews · Pacific time</text>'

    side_x = x0 + weeks * step + 48
    stats = [
        (f"{current} days", "current streak"),
        (f"{longest} days", "longest streak"),
        (fmt(best), f"best day · {best_day:%b %-d}"),
        (f"{len(daily)} / {PERIOD_DAYS}", "active days"),
    ]
    parts = [
        stat(side_x + (i % 2) * 150, y0 + 24 + (i // 2) * 84, value, label, 0.08 * i)
        for i, (value, label) in enumerate(stats)
    ]
    grid = f'<g class="in">{"".join(months)}{days}{"".join(cells)}{"".join(legend)}{note}</g>'
    return card(theme, ly + 32, "Activity", f"{fmt(total)} contributions in the {PERIOD_LABEL}", grid + "".join(parts))


def repos_card(
    theme: dict, by_repo: dict[str, Counter], recent: Counter, languages: dict[str, dict | None]
) -> str:
    kinds = (("commit", "Commits"), ("pr", "PRs opened"), ("review", "Reviews"))
    ranked = sorted(by_repo.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:8]
    grand = sum(sum(c.values()) for c in by_repo.values()) or 1
    peak = max((sum(c.values()) for _, c in ranked), default=1)
    bar_x, bar_w, top, row_h = 300, 360, 104, 42

    legend, lx = [], 24
    for (_, label), color in zip(kinds, theme["series"]):
        legend.append(f'<rect x="{lx}" y="60" width="10" height="10" rx="2" fill="{color}"/>')
        legend.append(f'<text x="{lx + 16}" y="69" font-size="11" class="m">{label}</text>')
        lx += 16 + 7 * len(label) + 18
    heads = "".join(
        f'<text x="{x}" y="{top - 12}" font-size="10" font-weight="600" letter-spacing=".6" class="m" text-anchor="end">{h}</text>'
        for x, h in ((W - 112, f"{PERIOD_MONTHS} MO"), (W - 66, "SHARE"), (W - 24, "30 D"))
    )
    rows = []
    for i, (repo, counts) in enumerate(ranked):
        y = top + i * row_h
        owner, name = repo.split("/", 1)
        language = languages.get(repo)
        lang = ""
        if language:
            lang = (
                f'<circle cx="28" cy="{y + 29}" r="4" fill="{language["color"] or theme["muted"]}"/>'
                f'<text x="38" y="{y + 33}" font-size="11" class="m">{escape(language["name"])}</text>'
            )
        total = sum(counts.values())
        width = bar_w * total / peak
        segments, sx = [], bar_x
        for (kind, _), color in zip(kinds, theme["series"]):
            seg = width * counts[kind] / total
            if seg >= 1:
                segments.append(f'<rect x="{sx:.1f}" y="{y + 10}" width="{max(seg - 2, 1):.1f}" height="12" fill="{color}"/>')
                sx += seg
        rows.append(
            f'<g class="in" style="animation-delay:{0.05 * i:.2f}s">'
            f'<clipPath id="bar{i}"><rect x="{bar_x}" y="{y + 10}" width="{width:.1f}" height="12" rx="4"/></clipPath>'
            f'<text x="24" y="{y + 19}" font-size="13"><tspan class="m">{escape(owner)}/</tspan>'
            f'<tspan font-weight="600">{escape(name)}</tspan></text>{lang}'
            f'<g clip-path="url(#bar{i})">{"".join(segments)}</g>'
            f'<text x="{W - 112}" y="{y + 21}" font-size="13" font-weight="600" text-anchor="end" class="n">{fmt(total)}</text>'
            f'<text x="{W - 66}" y="{y + 21}" font-size="12" text-anchor="end" class="m n">{100 * total / grand:.0f}%</text>'
            f'<text x="{W - 24}" y="{y + 21}" font-size="12" text-anchor="end" class="n">{fmt(recent[repo])}</text></g>'
        )
    height = top + len(ranked) * row_h + 12
    body = "".join(legend) + heads + "".join(rows)
    return card(theme, height, "Where the work goes", f"Top repos · {len(by_repo)} repos in the {PERIOD_LABEL}", body)


def rhythm_card(theme: dict, punch: Counter, weekend_active: int, weekend_days: int) -> str:
    blue = theme["series"][0]
    peak = max(punch.values(), default=1)
    x0, y0, col, row = 70, 78, 24, 26
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    dots = []
    for wd in range(7):
        dots.append(f'<text x="24" y="{y0 + wd * row + 4}" font-size="11" class="m">{names[wd]}</text>')
        for hour in range(24):
            count = punch[wd, hour]
            cx, cy = x0 + hour * col, y0 + wd * row
            if count:
                radius = 2 + 9 * math.sqrt(count / peak)
                dots.append(f'<circle cx="{cx}" cy="{cy}" r="{radius:.1f}" fill="{blue}" fill-opacity=".9"/>')
            else:
                dots.append(f'<circle cx="{cx}" cy="{cy}" r="1.5" fill="{theme["faint"]}"/>')
    hours = "".join(
        f'<text x="{x0 + h * col}" y="{y0 + 7 * row}" font-size="10" class="m" text-anchor="middle">{hour_label(h)}</text>'
        for h in (0, 6, 12, 18)
    )
    by_day = Counter()
    by_hour = Counter()
    for (wd, hour), count in punch.items():
        by_day[wd] += count
        by_hour[hour] += count
    busiest = max(range(7), key=lambda d: by_day[d])
    peak_hour = max(range(24), key=lambda h: by_hour[h])
    side_x = x0 + 24 * col + 24
    side = [
        stat(side_x, 92, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[busiest], "busiest day", 0.2),
        stat(side_x, 148, hour_label(peak_hour), "peak hour", 0.28),
        stat(side_x, 204, f"{weekend_active} / {weekend_days}", "weekend days active", 0.36),
    ]
    body = f'<g class="in">{"".join(dots)}{hours}</g>' + "".join(side)
    return card(theme, y0 + 7 * row + 20, "When I ship", f"By weekday and hour · Pacific time · {PERIOD_LABEL}", body)


# --- Main ---------------------------------------------------------------------------------


def main() -> None:
    events, merges, languages = collect()

    windows = []
    for name, days in (("Last 7 days", 7), ("Last 30 days", 30), (PERIOD_LABEL.capitalize(), PERIOD_DAYS)):
        first = TODAY - timedelta(days=days - 1)
        current = window_stats(events, merges, first, days)
        previous = window_stats(events, merges, first - timedelta(days=days), days)
        windows.append((name, days, current, previous))

    period = [e for e in events if e.at >= midnight(PERIOD_START)]
    daily = Counter(e.at.date() for e in period)
    current_streak, longest_streak = streaks(set(daily))
    weeks = (TODAY - HEAT_START).days // 7 + 1
    weekly = [sum(daily.get(HEAT_START + timedelta(weeks=w, days=d), 0) for d in range(7)) for w in range(weeks)]

    by_repo: dict[str, Counter] = defaultdict(Counter)
    for e in period:
        by_repo[e.repo][e.kind] += 1
    recent = Counter(e.repo for e in events if e.at >= midnight(TODAY - timedelta(days=29)))
    punch = Counter((e.at.weekday(), e.at.hour) for e in period)
    period_days = (PERIOD_START + timedelta(days=d) for d in range(PERIOD_DAYS))
    weekend = [day for day in period_days if day.weekday() >= 5]

    OUT.mkdir(exist_ok=True)
    for mode, theme in THEMES.items():
        cards = {
            "header": header_card(theme, len(period), weekly),
            "pulse": pulse_card(theme, windows),
            "activity": heatmap_card(theme, daily, current_streak, longest_streak),
            "repos": repos_card(theme, by_repo, recent, languages),
            "rhythm": rhythm_card(theme, punch, sum(day in daily for day in weekend), len(weekend)),
        }
        for name, svg in cards.items():
            (OUT / f"{name}-{mode}.svg").write_text(svg)
    print(f"Wrote {len(THEMES) * 5} cards to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
