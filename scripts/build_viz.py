"""
Build the dashboard: one self-contained HTML page, rendered from the marts.

There is no model call anywhere in this file, and that is the point. Every
number on the page comes from a SELECT run here, aggregated here, and drawn
here, so the page is a function of the warehouse and nothing else. The written
narrative it embeds is the one artefact this script does not compute, and it is
only ever read from a report the publication gate already passed - see
_load_narrative below.

Two backends, the same split report.py uses:

  VIZ_TARGET=snowflake (default) - the real marts, via the standard
      SNOWFLAKE_* variables. Locally those resolve to the read-only REPORTER
      identity in agent/.env; in CI they are already exported by the dbt step,
      so the dashboard needs no secret of its own.
  VIZ_TARGET=duckdb - the repo's market.duckdb, opened read_only=True. This is
      what CI stage 1 runs: it proves the generator works before stage 2 spends
      anything on Snowflake.

Usage (from the project root, with the project venv):
    .venv/bin/python3 scripts/build_viz.py
    VIZ_TARGET=duckdb .venv/bin/python3 scripts/build_viz.py

This replaced an earlier CSV export whose output still had to be rendered by
hand afterwards. A dashboard that needs a human in the loop is not a layer.
"""

from __future__ import annotations

import glob
import html
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "exports")
REPORTS_DIR = os.path.join(ROOT, "reports")

# The agent's env, not the root one: dashboards read, they never write, so the
# local default is the REPORTER role rather than the transformer that dbt uses.
load_dotenv(os.path.join(ROOT, "agent", ".env"))


# ---------------------------------------------------------------------------
# Backend. One read-only QueryRunner, shared with the agent and the report.
# Rows come back as dicts with lowercased column names - Snowflake upper-cases
# identifiers and DuckDB does not, and that difference dies here. limit=None
# because the dashboard aggregates over the whole result, not its first page.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(ROOT, "agent"))
import query  # noqa: E402 - the shared read-only QueryRunner lives in agent/


class _Backend:
    def __init__(self, engine: str):
        self._runner = query.QueryRunner(engine)
        if engine == "snowflake":
            self.label = "Snowflake"
            self.detail = (
                f'{query.require("SNOWFLAKE_DATABASE")}.'
                f'{query.require("SNOWFLAKE_SCHEMA")}'
            )
        else:
            self.label = "DuckDB"
            path = os.environ.get(
                "MARKET_DUCKDB_PATH", os.path.join(ROOT, "market.duckdb"))
            self.detail = os.path.basename(path)

    def rows(self, sql: str) -> list[dict]:
        result = self._runner.run(sql, limit=None)
        if result.is_error:
            raise RuntimeError(result.error)
        cols = [c.lower() for c in result.columns]
        return [dict(zip(cols, r)) for r in result.rows]

    def close(self) -> None:
        self._runner.close()


def _backend():
    target = os.environ.get("VIZ_TARGET", "snowflake").lower()
    return _Backend("duckdb" if target == "duckdb" else "snowflake")


# ---------------------------------------------------------------------------
# Queries. Plain SELECTs against the marts, portable across both adapters.
# ---------------------------------------------------------------------------

Q_COVERAGE = """
select count(*) as n_rows,
       count(distinct ticker) as n_tickers,
       min(trade_date) as first_dt,
       max(trade_date) as last_dt
from fct_prices
"""

Q_ASSET_CLASS = """
select asset_class, count(*) as n_names, max(as_of_date) as latest
from mart_movers
group by asset_class
order by asset_class
"""

Q_MOVERS = """
select ticker, name, sector, asset_class, as_of_date,
       close_price, ret_1d, ret_5d, ret_1m
from mart_movers
order by ret_1d desc
"""

Q_MOMENTUM = """
select ticker, trend_signal, drawdown_from_peak, vol_daily, daily_return_z
from mart_momentum
"""

Q_SECTORS = """
select sector, n_names, avg_ret_1d, avg_ret_5d, avg_ret_1m, pct_up_1d,
       top_ticker, top_ret_1d, bottom_ticker, bottom_ret_1d
from mart_sector_overview
order by avg_ret_1d desc
"""

Q_PRICES = """
select w.sector, w.is_benchmark, p.ticker, p.trade_date, p.close_price
from fct_prices p
join stg_watchlist w on p.ticker = w.ticker
order by p.trade_date, p.ticker
"""


# ---------------------------------------------------------------------------
# Shaping. Rebasing and the sector index are the only arithmetic here, and both
# are done on whole series rather than per-row so a missing session cannot
# quietly become a zero.
# ---------------------------------------------------------------------------


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def sector_index(price_rows: list[dict]) -> tuple[dict[str, list], list[date]]:
    """Equal-weight index per sector, each ticker rebased to 100 at its own start.

    Rebasing per ticker before averaging is what makes the sectors comparable:
    an index built on raw prices would just track whichever name is most
    expensive. Averaging across the tickers actually present on each date -
    rather than forward-filling - is deliberate too. Crypto trades seven days a
    week and equities do not, so a forward fill would invent equity sessions
    that never happened and flatten exactly the calendar difference the header
    calls out.
    """
    by_ticker: dict[str, list[tuple[date, float]]] = defaultdict(list)
    sector_of: dict[str, str] = {}
    for r in price_rows:
        t = r["ticker"]
        by_ticker[t].append((_as_date(r["trade_date"]), float(r["close_price"])))
        sector_of[t] = "benchmark" if r["is_benchmark"] else r["sector"]

    # ticker -> {date: rebased}
    rebased: dict[str, dict[date, float]] = {}
    for t, series in by_ticker.items():
        series.sort()
        base = series[0][1]
        if not base:
            continue
        rebased[t] = {d: c / base * 100.0 for d, c in series}

    buckets: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t, points in rebased.items():
        bucket = sector_of.get(t, "unknown")
        for d, v in points.items():
            buckets[bucket][d].append(v)

    all_dates = sorted({d for p in rebased.values() for d in p})
    out: dict[str, list] = {}
    for bucket, per_date in buckets.items():
        out[bucket] = [(d, sum(vs) / len(vs)) for d, vs in sorted(per_date.items())]
    return out, all_dates


# ---------------------------------------------------------------------------
# The narrative. Read only, and only from a report that was already published.
#
# This is the seam that keeps the guarantee intact. report.py either publishes
# a markdown file or blocks and writes a gate-failure JSON; this function looks
# for the former and never the latter. So a draft that failed fact-checking has
# no path onto the page: not a degraded path, no path. The charts do not depend
# on any of it - when nothing has been published, they simply render alone.
# ---------------------------------------------------------------------------


_TARGET_MARKER = re.compile(r"<!--\s*generated-from:\s*(\w+)\s*-->")


def _load_narrative(target: str) -> tuple[str, str] | None:
    """(iso_date, markdown_body) of the newest published report, or None.

    The target has to match. A report written against Snowflake describes real
    market data; the public page is built from the synthetic seed. Pasting one
    onto the other would produce a page whose prose and whose charts disagree
    about what happened, which is worse than a page with no prose at all.

    A report with no marker is skipped rather than guessed at - fail closed, the
    same rule the publication gate itself uses.
    """
    published = sorted(glob.glob(os.path.join(REPORTS_DIR, "*-weekly-report.md")))
    if not published:
        return None
    path = published[-1]
    stamp = os.path.basename(path)[:10]
    with open(path, encoding="utf-8") as f:
        body = f.read()
    marker = _TARGET_MARKER.search(body)
    if not marker or marker.group(1).lower() != target.lower():
        found = marker.group(1) if marker else "unmarked"
        print(f"  note: skipping narrative from {os.path.basename(path)} "
              f"(written against {found}, page is {target})", file=sys.stderr)
        return None
    # Drop the file's own H1 and the evidence appendix; the page supplies its
    # own heading, and the appendix is a wall of SQL that belongs in the repo
    # rather than on a dashboard.
    body = re.sub(r"\A#\s+.*?\n", "", body)
    body = _TARGET_MARKER.sub("", body)
    # Cut at the horizontal rule, which is where the report's own footer and
    # query appendix begin. The card states the provenance in its own words, so
    # repeating the file's footer here would just say it twice.
    body = re.split(r"\n-{3,}\s*\n", body)[0]
    body = body.split("## Evidence: queries run")[0]
    return stamp, body.strip()


def _mini_markdown(md: str) -> str:
    """Just enough markdown for the report body: paragraphs, bold, italics, code.

    Deliberately not a markdown library. The writer's output is prose with the
    occasional bold ticker, the shapes are known, and a dependency that renders
    arbitrary HTML into a page built from warehouse data is a bigger surface
    than this is worth.
    """
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        text = html.escape(" ".join(block.split()))
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        if text.startswith("#"):
            text = f"<h3>{text.lstrip('# ')}</h3>"
        else:
            text = f"<p>{text}</p>"
        out.append(text)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Formatting helpers. Plain hyphens for negatives - the project bans em dashes
# and Unicode minus signs, and a generator is the easiest place to guarantee it.
# ---------------------------------------------------------------------------


def pct(v, dp: int = 2) -> str:
    return "n/a" if v is None else f"{float(v) * 100:+.{dp}f}%"


def num(v, dp: int = 2) -> str:
    return "n/a" if v is None else f"{float(v):,.{dp}f}"


def esc(v) -> str:
    return html.escape(str(v))


# ---------------------------------------------------------------------------
# Charts. Hand-built SVG: no chart library, no CDN, no runtime fetch, so the
# file works offline, survives any CSP, and is one artifact to upload.
#
# Palette is the validated default (validate_palette.js, 4 categorical slots,
# both modes ALL CHECKS PASS). Light mode flags aqua and yellow below 3:1
# against the surface, which obliges relief: every line is direct-labelled at
# its end and every value also appears in the table view below.
# ---------------------------------------------------------------------------

SECTOR_SLOT = {}          # filled at render time, fixed order, never cycled
SLOT_ORDER = ["s1", "s2", "s3", "s4"]


def _nice_step(span: float, target: int = 4) -> float:
    """A round-number gridline step, so ticks read 2% and 4% rather than 2.8%.

    Axis labels are read, not just looked at. Deriving them from the data's own
    maximum produces arbitrary numbers that make a reader do arithmetic.
    """
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _declutter(labels: list[tuple[float, str, str]],
               gap: float = 13.0) -> list[tuple[float, str, str]]:
    """Nudge end labels apart so none sits on top of another.

    Line ends land wherever the data puts them, which on a converged chart is
    all in the same few pixels. Direct labels are what carries identity here
    (colour alone must never), so two of them overlapping is not cosmetic - it
    is the accessibility affordance failing.
    """
    ordered = sorted(labels, key=lambda t: t[0])
    for i in range(1, len(ordered)):
        y, text, cls = ordered[i]
        floor = ordered[i - 1][0] + gap
        if y < floor:
            ordered[i] = (floor, text, cls)
    return ordered


def _bar_path(x0: float, x1: float, y: float, h: float, r: float = 4.0) -> str:
    """Rect with only the data end rounded, so the mark stays anchored to zero."""
    r = min(r, abs(x1 - x0))
    if x1 >= x0:
        return (f"M{x0},{y} H{x1 - r} Q{x1},{y} {x1},{y + r} V{y + h - r} "
                f"Q{x1},{y + h} {x1 - r},{y + h} H{x0} Z")
    return (f"M{x0},{y} H{x1 + r} Q{x1},{y} {x1},{y + r} V{y + h - r} "
            f"Q{x1},{y + h} {x1 + r},{y + h} H{x0} Z")


def chart_movers(rows: list[dict]) -> str:
    """Diverging horizontal bars, one per ticker, sorted by 1-day return.

    Diverging because the data has a true zero and the sign is the story. Blue
    up / red down is the documented diverging pair; red/green would be the
    obvious finance convention and the wrong call, since it is the one pairing
    a red-green colourblind reader cannot separate.
    """
    rows = [r for r in rows if r["sector"] != "benchmark"]
    if not rows:
        return "<p class='empty'>No non-benchmark rows in mart_movers.</p>"

    row_h, bar_h = 24, 11
    # The left gutter has to clear the widest value label as well as the ticker,
    # because on a diverging chart the longest bar and its label grow towards
    # that gutter. Sized for "-99.99%" against the longest symbol.
    pad_l, pad_r, pad_t = 96, 84, 28
    plot_w = 380
    height = pad_t + row_h * len(rows) + 26
    width = pad_l + plot_w + pad_r

    # 1.35 rather than a tight fit: the headroom is what keeps the end label of
    # the largest mover from running into the ticker column.
    peak = (max(abs(float(r["ret_1d"] or 0)) for r in rows) or 0.01) * 1.35
    mid = pad_l + plot_w / 2

    def x_of(v: float) -> float:
        return mid + (v / peak) * (plot_w / 2)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="One-day return by ticker, diverging bar chart" '
             f'preserveAspectRatio="xMidYMid meet">']

    # Recessive grid: solid hairlines, one shade off the surface, on round steps.
    step = _nice_step(peak, 3)
    ticks, k = [], 1
    while step * k < peak:
        ticks.extend([-step * k, step * k])
        k += 1
    for t in ticks:
        gx = x_of(t)
        parts.append(f'<line class="grid" x1="{gx:.1f}" y1="{pad_t - 8}" '
                     f'x2="{gx:.1f}" y2="{pad_t + row_h * len(rows)}"/>')
        parts.append(f'<text class="tick" x="{gx:.1f}" y="{pad_t - 14}" '
                     f'text-anchor="middle">{t * 100:+g}%</text>')

    parts.append(f'<line class="zero" x1="{mid}" y1="{pad_t - 8}" x2="{mid}" '
                 f'y2="{pad_t + row_h * len(rows)}"/>')

    for i, r in enumerate(rows):
        v = float(r["ret_1d"] or 0)
        y = pad_t + i * row_h + (row_h - bar_h) / 2
        x1 = x_of(v)
        cls = "pos" if v >= 0 else "neg"
        parts.append(
            f'<g class="bar-row" tabindex="0" role="listitem" '
            f'data-t="{esc(r["ticker"])}" data-n="{esc(r["name"])}" '
            f'data-s="{esc(r["sector"])}" data-v="{pct(v)}" '
            f'data-d="{esc(r["as_of_date"])}" data-c="{num(r["close_price"])}">'
            f'<rect class="hit" x="{pad_l}" y="{pad_t + i * row_h}" '
            f'width="{plot_w}" height="{row_h}"/>'
            f'<text class="tickerlab" x="{pad_l - 10}" y="{y + bar_h - 1}" '
            f'text-anchor="end">{esc(r["ticker"])}</text>'
            f'<path class="{cls}" d="{_bar_path(mid, x1, y, bar_h)}"/>'
            # Direct label on every bar: this is the relief the contrast WARN
            # obliges, and with 18 rows it is one label per mark, not clutter.
            f'<text class="vallab" x="{x1 + (7 if v >= 0 else -7):.1f}" '
            f'y="{y + bar_h - 1}" text-anchor="{"start" if v >= 0 else "end"}">'
            f'{pct(v)}</text></g>')

    parts.append("</svg>")
    return "".join(parts)


def chart_sector_index(index: dict[str, list], all_dates: list[date]) -> str:
    """Sector equal-weight indices rebased to 100, plus the benchmark.

    Four sectors is four categorical slots, assigned in fixed order and held by
    the sector rather than by rank, so a sector that moves in the table never
    changes colour here. The benchmark deliberately takes muted ink instead of
    a fifth hue: it is context, not a peer.
    """
    series = {k: v for k, v in index.items() if k not in ("benchmark", "unknown")}
    if not series or len(all_dates) < 2:
        return "<p class='empty'>Not enough price history to plot an index.</p>"

    for i, name in enumerate(sorted(series)):
        SECTOR_SLOT[name] = SLOT_ORDER[i % len(SLOT_ORDER)]

    bench = index.get("benchmark", [])
    width, height = 780, 300
    pad_l, pad_r, pad_t, pad_b = 46, 96, 18, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    d0, d1 = all_dates[0].toordinal(), all_dates[-1].toordinal()
    span = max(d1 - d0, 1)
    every = [v for pts in list(series.values()) + [bench] for _, v in pts]
    lo, hi = min(every), max(every)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad

    def x_of(d: date) -> float:
        return pad_l + (d.toordinal() - d0) / span * plot_w

    def y_of(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Sector indices rebased to 100" '
             f'preserveAspectRatio="xMidYMid meet">']

    step = _nice_step(hi - lo, 4)
    t = math.ceil(lo / step) * step
    while t <= hi:
        y = y_of(t)
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" '
                     f'x2="{pad_l + plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 8}" y="{y + 3:.1f}" '
                     f'text-anchor="end">{t:g}</text>')
        t += step

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        d = date.fromordinal(int(d0 + span * frac))
        parts.append(f'<text class="tick" x="{x_of(d):.1f}" y="{height - 12}" '
                     f'text-anchor="middle">{d.isoformat()}</text>')

    parts.append(f'<line class="axis" x1="{pad_l}" y1="{pad_t + plot_h}" '
                 f'x2="{pad_l + plot_w}" y2="{pad_t + plot_h}"/>')

    def path_of(points) -> str:
        return "M" + " L".join(f"{x_of(d):.1f},{y_of(v):.1f}" for d, v in points)

    end_labels: list[tuple[float, str, str]] = []

    if bench:
        parts.append(f'<path class="line bench" d="{path_of(bench)}"/>')
        bd, bv = bench[-1]
        end_labels.append((y_of(bv), "benchmarks", "bench"))

    for name in sorted(series):
        pts = series[name]
        slot = SECTOR_SLOT[name]
        parts.append(f'<path class="line {slot}" d="{path_of(pts)}"/>')
        ld, lv = pts[-1]
        end_labels.append((y_of(lv), name, slot))

    # Direct label at every line end, nudged apart where the lines converge:
    # identity never rests on colour alone, so these have to stay readable.
    label_x = pad_l + plot_w + 7
    for y, text, cls in _declutter(end_labels):
        parts.append(f'<text class="endlab {cls}" x="{label_x:.1f}" '
                     f'y="{y + 3:.1f}">{esc(text)}</text>')

    parts.append(f'<line id="xhair" class="xhair" y1="{pad_t}" '
                 f'y2="{pad_t + plot_h}" x1="0" x2="0" style="display:none"/>')
    parts.append(f'<rect id="lineHit" x="{pad_l}" y="{pad_t}" width="{plot_w}" '
                 f'height="{plot_h}" fill="transparent"/>')
    parts.append("</svg>")
    return "".join(parts)


def chart_sector_bars(sectors: list[dict]) -> str:
    """Average 1-day return by sector, with breadth as text beside it.

    Uses the same blue-up / red-down diverging pair as the movers chart, not
    the sector hues. Colouring these bars by sector would mean blue reads as
    "up" in one chart and "crypto" in the next, and one page cannot hold two
    meanings for the same colour. Sector identity is carried by the row label
    here and by the legend on the index chart above.
    """
    if not sectors:
        return "<p class='empty'>mart_sector_overview is empty.</p>"
    row_h, bar_h = 34, 11
    pad_l, pad_t = 92, 14
    plot_w = 300
    width, height = pad_l + plot_w + 150, pad_t + row_h * len(sectors) + 10
    peak = max(abs(float(s["avg_ret_1d"] or 0)) for s in sectors) * 1.3 or 0.01
    mid = pad_l + plot_w / 2

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Average one-day return by sector" '
             f'preserveAspectRatio="xMidYMid meet">']
    parts.append(f'<line class="zero" x1="{mid}" y1="{pad_t - 6}" x2="{mid}" '
                 f'y2="{pad_t + row_h * len(sectors) - 8}"/>')

    for i, s in enumerate(sectors):
        v = float(s["avg_ret_1d"] or 0)
        y = pad_t + i * row_h
        x1 = mid + (v / peak) * (plot_w / 2)
        slot = "pos" if v >= 0 else "neg"
        parts.append(
            f'<g class="bar-row" tabindex="0" '
            f'data-t="{esc(s["sector"])}" data-n="{s["n_names"]} names" '
            f'data-s="best {esc(s["top_ticker"])} {pct(s["top_ret_1d"])} / '
            f'worst {esc(s["bottom_ticker"])} {pct(s["bottom_ret_1d"])}" '
            f'data-v="{pct(v)}" data-d="breadth {float(s["pct_up_1d"]) * 100:.0f}% up" '
            f'data-c="{num(s["avg_ret_1m"] * 100)}% over ~1m">'
            f'<rect class="hit" x="{pad_l}" y="{y - 4}" width="{plot_w}" '
            f'height="{row_h - 6}"/>'
            f'<text class="tickerlab" x="{pad_l - 10}" y="{y + bar_h - 1}" '
            f'text-anchor="end">{esc(s["sector"])}</text>'
            f'<path class="{slot}" d="{_bar_path(mid, x1, y, bar_h)}"/>'
            f'<text class="vallab" x="{pad_l + plot_w + 12}" y="{y + bar_h - 1}">'
            f'{pct(v)}</text>'
            f'<text class="sub" x="{pad_l + plot_w + 78}" y="{y + bar_h - 1}">'
            f'{float(s["pct_up_1d"]) * 100:.0f}% up</text></g>')

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page assembly.
# ---------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;
--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;
--text-muted:#898781;--grid:#e1e0d9;--baseline:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--pos:#2a78d6;--neg:#e34948;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){color-scheme:dark;
--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--pos:#3987e5;--neg:#e66767;}}
:root[data-theme=dark]{color-scheme:dark;
--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--pos:#3987e5;--neg:#e66767;}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);line-height:1.55;
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px;}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 64px;}
h1{font-size:24px;font-weight:600;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;font-weight:600;margin:0 0 2px}
h3{font-size:15px;font-weight:600;margin:18px 0 4px}
.sub{fill:var(--text-muted);color:var(--text-muted);font-size:13px}
.lede{color:var(--text-secondary);font-size:14px;margin:0 0 24px}
.card{background:var(--surface-1);border:.5px solid var(--border);border-radius:10px;
padding:18px 20px;margin:0 0 20px;}
.card>.sub{margin:0 0 14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:0 0 20px}
.kpi{background:var(--surface-1);border:.5px solid var(--border);border-radius:10px;padding:12px 14px}
.kpi .v{font-size:21px;font-weight:600;line-height:1.2}
.kpi .k{font-size:12px;color:var(--text-muted);margin-top:2px}
svg{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}
.axis,.zero{stroke:var(--baseline);stroke-width:1}
.tick{fill:var(--text-muted);font-size:10.5px;font-variant-numeric:tabular-nums}
.tickerlab{fill:var(--text-secondary);font-size:11.5px;font-weight:500}
.vallab{fill:var(--text-secondary);font-size:11px;font-variant-numeric:tabular-nums}
.endlab{font-size:11.5px;font-weight:600}
.pos{fill:var(--pos)}.neg{fill:var(--neg)}
.s1fill{fill:var(--s1)}.s2fill{fill:var(--s2)}.s3fill{fill:var(--s3)}.s4fill{fill:var(--s4)}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.line.s1{stroke:var(--s1)}.line.s2{stroke:var(--s2)}
.line.s3{stroke:var(--s3)}.line.s4{stroke:var(--s4)}
.endlab.s1{fill:var(--s1)}.endlab.s2{fill:var(--s2)}
.endlab.s3{fill:var(--s3)}.endlab.s4{fill:var(--s4)}
.line.bench{stroke:var(--text-muted);stroke-width:1.5;opacity:.75}
.endlab.bench{fill:var(--text-muted);font-weight:500}
.xhair{stroke:var(--baseline);stroke-width:1}
.hit{fill:transparent}
.bar-row{cursor:default}
.bar-row:hover .hit,.bar-row:focus .hit{fill:var(--grid);opacity:.5}
.bar-row:focus{outline:none}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:12px 0 0;font-size:12.5px;
color:var(--text-secondary)}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block;
margin-right:5px;vertical-align:-1px}
table{border-collapse:collapse;width:100%;font-size:13px;
font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:.5px solid var(--border);
white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--text-muted);font-weight:500;font-size:11.5px;text-transform:uppercase;
letter-spacing:.04em}
td.up{color:var(--pos)}td.down{color:var(--neg)}
.scroll{overflow-x:auto}
.narrative p{margin:0 0 10px;font-size:14.5px}
.narrative code{background:var(--page);padding:1px 4px;border-radius:3px;font-size:.9em}
.note{color:var(--text-muted);font-size:13px;margin:0}
.banner{background:var(--surface-1);border:.5px solid var(--border);
border-left:3px solid var(--s4);border-radius:8px;padding:11px 14px;margin:0 0 20px;
font-size:13.5px;color:var(--text-secondary)}
.banner.prod{border-left-color:var(--s3)}
.banner strong{color:var(--text-primary)}
.empty{color:var(--text-muted);font-size:13px}
.badge{display:inline-block;font-size:11.5px;color:var(--text-secondary);
border:.5px solid var(--border);border-radius:999px;padding:2px 9px;margin-left:6px}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;
background:var(--surface-1);border:.5px solid var(--baseline);border-radius:8px;
padding:8px 10px;font-size:12.5px;box-shadow:0 4px 14px rgba(0,0,0,.13);z-index:9;
max-width:270px}
#tip b{display:block;font-size:13px;margin-bottom:2px}
#tip .r{color:var(--text-secondary);font-variant-numeric:tabular-nums}
footer{color:var(--text-muted);font-size:12.5px;margin-top:30px;
border-top:.5px solid var(--border);padding-top:14px}
"""

JS = """
const tip=document.getElementById('tip');
function show(html,x,y){tip.innerHTML=html;tip.style.opacity='1';
 const r=tip.getBoundingClientRect();
 tip.style.left=Math.min(x+14,innerWidth-r.width-10)+'px';
 tip.style.top=Math.max(10,y-r.height-12)+'px';}
function hide(){tip.style.opacity='0';}
document.querySelectorAll('.bar-row').forEach(g=>{
 const d=g.dataset;
 const body=`<b>${d.t}</b><span class="r">${d.n}</span><br>
   <span class="r">${d.s}</span><br><span class="r">1d ${d.v}</span><br>
   <span class="r">${d.d}</span><br><span class="r">${d.c}</span>`;
 const on=e=>show(body,(e.clientX||0),(e.clientY||0));
 g.addEventListener('mousemove',on);
 g.addEventListener('mouseleave',hide);
 // Keyboard focus shows exactly what hover shows - a tooltip that only
 // answers to a mouse is a value some readers cannot reach at all.
 g.addEventListener('focus',()=>{const r=g.getBoundingClientRect();
   show(body,r.left+r.width/2,r.top+r.height);});
 g.addEventListener('blur',hide);
});
const SERIES=JSON.parse(document.getElementById('series-data').textContent);
const hit=document.getElementById('lineHit'),xh=document.getElementById('xhair');
if(hit&&SERIES.dates.length){
 const svg=hit.ownerSVGElement;
 hit.addEventListener('mousemove',e=>{
  const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;
  const loc=pt.matrixTransform(svg.getScreenCTM().inverse());
  const x0=+hit.getAttribute('x'),w=+hit.getAttribute('width');
  const f=Math.max(0,Math.min(1,(loc.x-x0)/w));
  const i=Math.round(f*(SERIES.dates.length-1));
  const gx=x0+(i/(SERIES.dates.length-1))*w;
  xh.setAttribute('x1',gx);xh.setAttribute('x2',gx);xh.style.display='';
  let rows='';
  for(const s of SERIES.series){const v=s.values[i];
   if(v===null||v===undefined)continue;
   rows+=`<span class="r" style="color:${s.color}">&#9632;</span>
    <span class="r"> ${s.name} ${v.toFixed(1)}</span><br>`;}
  show(`<b>${SERIES.dates[i]}</b>${rows}`,e.clientX,e.clientY);
 });
 hit.addEventListener('mouseleave',()=>{hide();xh.style.display='none';});
}
"""


def build_page(data: dict) -> str:
    cov = data["coverage"]
    movers, sectors, momentum = data["movers"], data["sectors"], data["momentum"]
    idx, all_dates = data["index"], data["dates"]
    mom_by_ticker = {m["ticker"]: m for m in momentum}

    charts = {
        "movers": chart_movers(movers),
        "index": chart_sector_index(idx, all_dates),
        "sectors": chart_sector_bars(sectors),
    }

    # KPI tiles. Proportional figures on purpose: tabular-nums makes a standalone
    # number look loose at this size, and nothing here has to align vertically.
    ac = {r["asset_class"]: r for r in data["asset_class"]}
    kpis = [
        (f'{cov["n_rows"]:,}', "rows in fct_prices"),
        (str(cov["n_tickers"]), "tickers"),
        (str(len(sectors)), "sectors"),
        (f'{_as_date(cov["first_dt"]).isoformat()}', "first session"),
        (f'{_as_date(cov["last_dt"]).isoformat()}', "latest session"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>'
        for v, k in kpis)

    # The calendar caveat, stated with the actual dates rather than in the
    # abstract. Crypto sits ahead of equities and a reader comparing the two
    # columns should be told so before they draw a conclusion from it.
    latest_line = " &middot; ".join(
        f'{esc(k)} to {_as_date(v["latest"]).isoformat()}'
        for k, v in sorted(ac.items()))

    legend = "".join(
        f'<span><i style="background:var(--{SECTOR_SLOT[s]})"></i>{esc(s)}</span>'
        for s in sorted(SECTOR_SLOT))
    legend += ('<span><i style="background:var(--text-muted)"></i>'
               'benchmarks (SPY, QQQ)</span>')

    # Table view: the WCAG-clean twin of every chart above, so no value on this
    # page is reachable only by hovering something.
    head = ("<tr><th>Ticker</th><th>Name</th><th>Sector</th><th>As of</th>"
            "<th>Close</th><th>1d</th><th>5d</th><th>~1m</th><th>Trend</th>"
            "<th>Z</th><th>Drawdown</th></tr>")
    body = ""
    for r in movers:
        m = mom_by_ticker.get(r["ticker"], {})
        v = float(r["ret_1d"] or 0)
        body += (
            f'<tr><td>{esc(r["ticker"])}</td><td>{esc(r["name"])}</td>'
            f'<td>{esc(r["sector"])}</td><td>{_as_date(r["as_of_date"]).isoformat()}</td>'
            f'<td>{num(r["close_price"])}</td>'
            f'<td class="{"up" if v >= 0 else "down"}">{pct(r["ret_1d"])}</td>'
            f'<td>{pct(r["ret_5d"])}</td><td>{pct(r["ret_1m"])}</td>'
            f'<td>{esc(m.get("trend_signal", "n/a"))}</td>'
            f'<td>{num(m.get("daily_return_z"), 2)}</td>'
            f'<td>{pct(m.get("drawdown_from_peak"))}</td></tr>')

    # Provenance, stated where a reader cannot miss it. The public page is
    # built from the synthetic seed on purpose - it needs no credentials and
    # puts no real position data on the open web - but a page of plausible
    # numbers that does not say so is just a lie with a chart on it.
    if data["target"] == "duckdb":
        provenance = (
            '<p class="banner"><strong>Synthetic data.</strong> These figures come '
            'from <code>scripts/seed_sample.py</code>, not from market data. The '
            'pipeline, models and tests are real and this page is built by a real '
            'dbt run; the prices are generated so the page needs no warehouse '
            'credentials and exposes no real holdings. Numbers here are not '
            'anyone\'s actual returns.</p>')
    else:
        provenance = (
            '<p class="banner prod"><strong>Production data</strong>, refreshed by '
            'the nightly build. Descriptive only, and not investment advice.</p>')

    narrative = _load_narrative(data["target"])
    if narrative:
        stamp, md = narrative
        narrative_html = (
            f'<div class="card"><h2>What happened'
            f'<span class="badge">published {esc(stamp)}</span></h2>'
            f'<p class="sub">Written by the report agent, then fact-checked: every '
            f'figure below was traced to a query result by code and every claim '
            f'reviewed by a second model before the gate allowed it to publish.</p>'
            f'<div class="narrative">{_mini_markdown(md)}</div></div>')
    else:
        narrative_html = (
            '<div class="card"><h2>What happened</h2>'
            '<p class="note">No verified narrative for this run. The report agent '
            'either has not run yet or its draft was blocked by the publication '
            'gate. The charts below are unaffected - they are computed directly '
            'from the marts and never depend on a model.</p></div>')

    # Line-chart hover data. Embedded as JSON rather than fetched, so the page
    # stays one file with no network dependency of any kind.
    slot_hex = {"s1": "var(--s1)", "s2": "var(--s2)", "s3": "var(--s3)", "s4": "var(--s4)"}
    date_list = [d.isoformat() for d in all_dates]
    series_json = {"dates": date_list, "series": []}
    for name in sorted(SECTOR_SLOT):
        lookup = dict(idx[name])
        series_json["series"].append({
            "name": name,
            "color": slot_hex[SECTOR_SLOT[name]],
            "values": [round(lookup[d], 2) if d in lookup else None for d in all_dates],
        })
    if idx.get("benchmark"):
        lookup = dict(idx["benchmark"])
        series_json["series"].append({
            "name": "benchmarks",
            "color": "var(--text-muted)",
            "values": [round(lookup[d], 2) if d in lookup else None for d in all_dates],
        })

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market movers - build dashboard</title>
<style>{CSS}</style></head>
<body>
<div class="wrap">
<h1>Market movers</h1>
<p class="lede">Built from the marts by the last dbt run &middot;
source <strong>{esc(data["source_label"])}</strong>
({esc(data["source_detail"])}) &middot; {latest_line}.
Crypto trades every day and equities do not, so those dates differ by design;
each figure is as of the latest session for its own ticker.</p>
{provenance}

<div class="kpis">{kpi_html}</div>

{narrative_html}

<div class="card">
<h2>One-day move by ticker</h2>
<p class="sub">mart_movers, benchmarks excluded. Blue up, red down, against a
true zero. Blue and red rather than the usual green and red because red/green
is the one pairing a colourblind reader cannot separate.</p>
{charts["movers"]}
</div>

<div class="card">
<h2>Sector indices</h2>
<p class="sub">Each ticker rebased to 100 at its first session, then averaged
equal-weight within its sector. Levels are relative to each sector's own start,
so lines are comparable to one another and not to any index you would trade.</p>
{charts["index"]}
<div class="legend">{legend}</div>
</div>

<div class="card">
<h2>Sector breadth</h2>
<p class="sub">mart_sector_overview. The bar is the average 1-day return; the
figure beside it is the share of names in that sector that finished up, which
separates a broad move from one name carrying the sector.</p>
{charts["sectors"]}
</div>

<div class="card">
<h2>All tickers</h2>
<p class="sub">The table view: every value plotted above, readable without
hovering anything.</p>
<div class="scroll"><table>
<thead>{head}</thead><tbody>{body}</tbody></table></div>
</div>

<footer>
Generated by <code>scripts/build_viz.py</code>. Deterministic: no model call
produced any number on this page. Descriptive only, and not investment advice.
</footer>
</div>
<div id="tip" role="status" aria-live="polite"></div>
<script type="application/json" id="series-data">{json.dumps(series_json)}</script>
<script>{JS}</script>
</body></html>
"""


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "dashboard.html"),
                    help="output path, or a directory (written as index.html)")
    args = ap.parse_args()

    target = os.environ.get("VIZ_TARGET", "snowflake").lower()
    backend = _backend()
    try:
        price_rows = backend.rows(Q_PRICES)
        idx, all_dates = sector_index(price_rows)
        data = {
            "coverage": backend.rows(Q_COVERAGE)[0],
            "asset_class": backend.rows(Q_ASSET_CLASS),
            "movers": backend.rows(Q_MOVERS),
            "momentum": backend.rows(Q_MOMENTUM),
            "sectors": backend.rows(Q_SECTORS),
            "index": idx,
            "dates": all_dates,
            "target": target,
            "source_label": backend.label,
            "source_detail": backend.detail,
        }
    finally:
        backend.close()

    out = args.out
    if os.path.isdir(out) or not out.endswith(".html"):
        out = os.path.join(out, "index.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    page = build_page(data)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    print(f"wrote {os.path.relpath(out, ROOT)}  "
          f"({len(page):,} bytes, {data['coverage']['n_rows']:,} price rows, "
          f"{len(data['movers'])} tickers, source={backend.label})")


if __name__ == "__main__":
    main()
