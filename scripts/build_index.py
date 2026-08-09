"""
Build the site landing page: the dashboard archive, plus any explainers.

Each run of the pages workflow drops a dated snapshot into runs/ and calls this
to regenerate index.html - a list of every snapshot, newest first, with the
newest marked "latest". Self-contained and theme-aware, no external assets - the
same constraints the dashboards themselves follow.

    python scripts/build_index.py <site_dir>

<site_dir> holds a runs/ directory of `YYYY-MM-DD-HHMM-<sha>.html` snapshots;
index.html is written at its root.

It may also hold an explainers/ directory, copied there from docs/explainers by
the workflow. Those are prose pages about how the pipeline works, and they are
listed by reading each file's own <title> and <meta name="description"> rather
than from a hardcoded table here. The index is regenerated and force-pushed on
every build, so anything this function does not discover would be silently
dropped from the site on the next run - discovery is what keeps a new explainer
from needing a matching edit in this file.
"""

from __future__ import annotations

import html
import os
import re
import sys

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DESC_RE = re.compile(
    r"""<meta\s+name=["']description["']\s+content=["'](.*?)["']""", re.I | re.S
)


def parse_name(fname: str) -> dict:
    """Pull the date, time and short sha back out of a snapshot filename."""
    stem = fname[:-5] if fname.endswith(".html") else fname
    parts = stem.split("-")
    date = "-".join(parts[:3]) if len(parts) >= 3 else stem
    hhmm = parts[3] if len(parts) >= 4 else ""
    sha = parts[4] if len(parts) >= 5 else ""
    when_time = f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm
    return {"file": fname, "date": date, "time": when_time, "sha": sha}


def read_meta(path: str) -> dict:
    """Pull an explainer's own title and description out of its <head>.

    Only the head is read: these pages embed SQL and prose that can contain
    anything, and scanning the whole file risks matching a <title> that is
    being talked about rather than declared.
    """
    with open(path, encoding="utf-8") as f:
        head = f.read(4096)
    title = _TITLE_RE.search(head)
    desc = _DESC_RE.search(head)
    fallback = os.path.basename(path)[:-5].replace("-", " ")
    return {
        "file": os.path.basename(path),
        "title": " ".join(title.group(1).split()) if title else fallback,
        "desc": " ".join(desc.group(1).split()) if desc else "",
    }


def collect_explainers(site_dir: str) -> list:
    """Every explainer present on the site, alphabetical by filename."""
    d = os.path.join(site_dir, "explainers")
    if not os.path.isdir(d):
        return []
    return [
        read_meta(os.path.join(d, f))
        for f in sorted(os.listdir(d))
        if f.endswith(".html")
    ]


def render(site_dir: str) -> str:
    runs_dir = os.path.join(site_dir, "runs")
    files = (
        sorted((f for f in os.listdir(runs_dir) if f.endswith(".html")), reverse=True)
        if os.path.isdir(runs_dir)
        else []
    )
    runs = [parse_name(f) for f in files]

    items = []
    for i, r in enumerate(runs):
        tag = '<span class="latest">latest</span>' if i == 0 else ""
        when = html.escape(f"{r['date']} {r['time']}".strip())
        sha = html.escape(r["sha"])
        href = html.escape(f"runs/{r['file']}")
        items.append(
            f'      <li><a href="{href}"><span class="when">{when}</span>'
            f'<span class="sha">{sha}</span>{tag}</a></li>'
        )
    listing = "\n".join(items) or '      <li class="empty">No snapshots yet.</li>'
    count = len(runs)
    latest_href = html.escape(f"runs/{runs[0]['file']}") if runs else "#"
    plural = "" if count == 1 else "s"

    explainers = collect_explainers(site_dir)
    if explainers:
        rows = "\n".join(
            f'      <li><a href="{html.escape("explainers/" + e["file"])}">'
            f'<span class="etitle">{html.escape(e["title"])}</span>'
            f'<span class="edesc">{html.escape(e["desc"])}</span></a></li>'
            for e in explainers
        )
        explainer_section = f"""  <h2>Explainers</h2>
  <p class="sect">How the pipeline works, and what went wrong along the way.</p>
  <ul class="explainers">
{rows}
  </ul>

"""
    else:
        explainer_section = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Movers - dashboard archive</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f9f9f7; --card:#fff; --fg:#0b0b0b;
           --muted:#6b6a66; --border:rgba(0,0,0,.10); --accent:#2f6f4f; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0d0d0d; --card:#1a1a19; --fg:#fff; --muted:#a3a29d;
             --border:rgba(255,255,255,.12); --accent:#5bbf8a; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
          font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  main {{ max-width:720px; margin:0 auto; padding:48px 20px; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:36px 0 2px; }}
  p.sub {{ color:var(--muted); margin:0 0 28px; }}
  p.sect {{ color:var(--muted); font-size:13px; margin:0 0 12px; }}
  .cta {{ display:inline-block; background:var(--accent); color:#fff;
          text-decoration:none; padding:10px 16px; border-radius:8px;
          font-weight:600; margin-bottom:28px; }}
  ul {{ list-style:none; margin:0; padding:0; background:var(--card);
        border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  li + li {{ border-top:1px solid var(--border); }}
  li a {{ display:flex; align-items:center; gap:12px; padding:12px 16px;
          text-decoration:none; color:var(--fg); }}
  li a:hover {{ background:rgba(127,127,127,.08); }}
  .when {{ font-variant-numeric:tabular-nums; }}
  .sha {{ color:var(--muted); font-family:ui-monospace,Menlo,monospace;
          font-size:13px; margin-left:auto; }}
  .latest {{ background:var(--accent); color:#fff; font-size:11px;
             padding:2px 8px; border-radius:999px; margin-left:10px; }}
  .empty {{ padding:16px; color:var(--muted); }}
  ul.explainers li a {{ display:block; padding:14px 16px; }}
  .etitle {{ display:block; font-weight:600; }}
  .edesc {{ display:block; color:var(--muted); font-size:13px; margin-top:2px; }}
  footer {{ color:var(--muted); font-size:13px; margin-top:24px; }}
</style>
</head>
<body>
<main>
  <h1>Market Movers</h1>
  <p class="sub">A dbt analytics pipeline on Snowflake. Every build publishes a dashboard
     snapshot here, alongside the explainers. Synthetic data, no live warehouse behind it.</p>
  <a class="cta" href="{latest_href}">View latest dashboard &rarr;</a>

{explainer_section}  <h2>Build snapshots</h2>
  <p class="sect">One dashboard per build of the pipeline, newest first.</p>
  <ul>
{listing}
  </ul>
  <footer>{count} snapshot{plural} archived.</footer>
</main>
</body>
</html>
"""


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/build_index.py <site_dir>", file=sys.stderr)
        raise SystemExit(2)
    site_dir = sys.argv[1]
    os.makedirs(site_dir, exist_ok=True)
    out = os.path.join(site_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(site_dir))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
