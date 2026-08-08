"""
Build the dashboard archive landing page.

Each run of the pages workflow drops a dated snapshot into runs/ and calls this
to regenerate index.html - a list of every snapshot, newest first, with the
newest marked "latest". Self-contained and theme-aware, no external assets - the
same constraints the dashboards themselves follow.

    python scripts/build_index.py <site_dir>

<site_dir> holds a runs/ directory of `YYYY-MM-DD-HHMM-<sha>.html` snapshots;
index.html is written at its root.
"""

from __future__ import annotations

import html
import os
import sys


def parse_name(fname: str) -> dict:
    """Pull the date, time and short sha back out of a snapshot filename."""
    stem = fname[:-5] if fname.endswith(".html") else fname
    parts = stem.split("-")
    date = "-".join(parts[:3]) if len(parts) >= 3 else stem
    hhmm = parts[3] if len(parts) >= 4 else ""
    sha = parts[4] if len(parts) >= 5 else ""
    when_time = f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm
    return {"file": fname, "date": date, "time": when_time, "sha": sha}


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
  p.sub {{ color:var(--muted); margin:0 0 28px; }}
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
  footer {{ color:var(--muted); font-size:13px; margin-top:24px; }}
</style>
</head>
<body>
<main>
  <h1>Market Movers - dashboard archive</h1>
  <p class="sub">A snapshot from every build of the dbt pipeline, newest first.
     Synthetic data, no live warehouse behind it.</p>
  <a class="cta" href="{latest_href}">View latest dashboard &rarr;</a>
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
