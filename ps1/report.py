#!/usr/bin/env python3

import collections
import datetime
import html
import math
import urllib.parse

import click

import crawler

RING = 78

SEED_RING = 54

MARGIN = 58

DEPTH_FILL = ("#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef")

DEPTH_FILL_DARK = ("#b7d3f6", "#86b6ef", "#5598e7", "#2a78d6", "#1c5cab")

STATUS_FILL = {
    "2": ("#0ca30c", "ok"),
    "3": ("#fab219", "redirected"),
    "4": ("#ec835a", "client error"),
    "5": ("#d03b3b", "server error"),
    "0": ("#d03b3b", "no response"),
}


def load(path: str) -> list[dict]:
    """Read the log into a list of dicts, in crawl order.

    Every value stays a string except the handful the charts do arithmetic on,
    which keeps the reader honest about what the log actually contains.
    """
    with open(path, encoding="utf-8") as handle:
        header = next(handle).rstrip("\n").split("\t")
        rows = [
            dict(zip(header, line.rstrip("\n").split("\t")))
            for line in handle
            if not line.startswith("#")
        ]
    if "parent" not in header:
        raise click.ClickException(
            f"{path} has no 'parent' column -- it was written by an older "
            "crawler.py, so the crawl graph cannot be reconstructed. Re-crawl."
        )
    for row in rows:
        row["depth"] = int(row["depth"])
        row["bytes"] = int(row["bytes"])
        row["secs"] = float(row["secs"])
        row["page_prio"] = float(row["page_prio"])
        row["domain_prio"] = float(row["domain_prio"])
        row["at"] = datetime.datetime.fromisoformat(row["time"])
        row["host"] = urllib.parse.urlsplit(row["url"]).hostname or ""
        row["site"] = crawler.superdomain(row["host"])
    return rows


def layout(rows: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Place every crawled page on a radial tree and return (nodes, edges).

    The crawler logs the page each URL was discovered on, and only the first
    discoverer becomes a parent, so those edges are a spanning tree of the
    crawl -- exactly the path the crawler took, not the whole link graph.

    Rings are crawl depth, so the picture is how far the frontier ran. Each
    subtree gets a slice of the circle proportional to how many leaves it has,
    which keeps a page that spawned four hundred links from squeezing its
    siblings into a sliver. Angles are assigned by walking the tree iteratively:
    a deep crawl would otherwise risk Python's recursion limit.
    """
    known = {row["url"]: row for row in rows}
    children: dict[str, list[str]] = collections.defaultdict(list)
    roots = []
    for row in rows:
        if row["parent"] and row["parent"] in known:
            children[row["parent"]].append(row["url"])
        else:
            roots.append(row["url"])

    leaves: dict[str, int] = {}
    for row in reversed(rows):
        kids = children.get(row["url"], ())
        leaves[row["url"]] = sum(leaves.get(kid, 1) for kid in kids) or 1

    nodes, edges = [], []
    span_of = {url: (index / len(roots), 1 / len(roots)) for index, url in enumerate(roots)}
    pending = list(roots)
    while pending:
        url = pending.pop()
        start, span = span_of[url]
        row = known[url]
        angle = (start + span / 2) * math.tau - math.pi / 2
        radius = SEED_RING + row["depth"] * RING
        row["x"] = radius * math.cos(angle)
        row["y"] = radius * math.sin(angle)
        nodes.append(row)
        cursor = start
        for kid in children.get(url, ()):
            share = span * leaves[kid] / leaves[url]
            span_of[kid] = (cursor, share)
            cursor += share
            edges.append((row["x"], row["y"], kid))
            pending.append(kid)

    placed = {row["url"]: row for row in nodes}
    edges = [(x, y, placed[kid]["x"], placed[kid]["y"]) for x, y, kid in edges]
    return nodes, edges


def svg_graph(rows: list[dict]) -> str:
    """The crawl tree: one ring per depth, one dot per page."""
    nodes, edges = layout(rows)
    deepest = max((row["depth"] for row in rows), default=0)
    extent = SEED_RING + deepest * RING + MARGIN
    size = 2 * extent
    parts = [
        f'<svg viewBox="{-extent} {-extent} {size} {size}" '
        'class="graph" role="img" aria-label="Radial tree of the crawl, '
        'one ring per link depth from the seed pages">'
    ]

    for depth in range(deepest + 1):
        radius = SEED_RING + depth * RING
        parts.append(
            f'<circle class="ring" cx="0" cy="0" r="{radius}"/>'
            f'<text class="ringlabel" x="7" y="{-radius - 6}">depth {depth}</text>'
        )
    parts.append('<g class="edges">')
    for x1, y1, x2, y2 in edges:
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    parts.append("</g>")

    for row in nodes:
        kids = max(0, row.get("kids", 0))
        radius = min(10.0, 3.0 + math.sqrt(kids) * 0.7)
        klass = f"n d{min(row['depth'], len(DEPTH_FILL) - 1)}"
        if not row["status"].startswith("2"):
            klass, radius = "n dead", 2.6
        parts.append(
            f'<circle class="{klass}" cx="{row["x"]:.1f}" cy="{row["y"]:.1f}" '
            f'r="{radius:.1f}" tabindex="0"'
            f' data-url="{html.escape(row["url"], quote=True)}"'
            f' data-meta="{row["status"]} &middot; depth {row["depth"]} &middot; '
            f'{row["bytes"] / 1024:.1f} KiB &middot; {row["secs"]:.3f}s &middot; '
            f'{html.escape(row["site"], quote=True)}'
            f'{" &middot; " + html.escape(row["note"], quote=True) if row["note"] else ""}"'
            "/>"
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_bars(pairs: list[tuple[str, float]], unit: str = "") -> str:
    """A ranked horizontal bar chart -- one series, so one colour."""
    if not pairs:
        return "<p class=empty>nothing to show</p>"
    top = max(value for _, value in pairs)
    rows = []
    for label, value in pairs:
        width = 100 * value / top
        rows.append(
            '<div class="bar">'
            f'<span class="barlabel" title="{html.escape(label, quote=True)}">'
            f"{html.escape(label)}</span>"
            f'<span class="bartrack"><span class="barfill" style="width:{width:.1f}%">'
            "</span></span>"
            f'<span class="barvalue">{value:,.0f}{unit}</span>'
            "</div>"
        )
    return "".join(rows)


def svg_lines(series: list[tuple[str, str, list[float]]], labels: list[str],
              height: int = 150, area: bool = False) -> str:
    """A line (or area) chart over an evenly spaced x axis.

    `series` is (name, css class, values); every series shares one y scale,
    because two y scales on one chart is the fastest way to mislead a reader.
    The plot starts inset from the left so the y labels get a gutter of their
    own rather than sitting on top of the data.
    """
    values = [v for _, _, vs in series for v in vs]
    if not values:
        return "<p class=empty>nothing to show</p>"
    left, width, top = 46, 760, max(values) or 1
    step = (width - left) / max(1, len(labels) - 1)

    def point(index, value):
        return (f"{left + index * step:.1f},"
                f"{height - (value / top) * (height - 18):.1f}")

    parts = [f'<svg viewBox="0 -6 {width} {height + 32}" class="chart" role="img">']
    for fraction in (0.5, 1.0):
        y = height - fraction * (height - 18)
        parts.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width}" y2="{y:.1f}"/>'
            f'<text class="tick end" x="{left - 8}" y="{y + 3.5:.1f}">'
            f"{top * fraction:,.2f}</text>"
        )
    parts.append(
        f'<line class="axis" x1="{left}" y1="{height}" x2="{width}" y2="{height}"/>'
    )

    for _, klass, vs in series:
        points = " ".join(point(i, v) for i, v in enumerate(vs))
        if area:
            parts.append(
                f'<polygon class="area {klass}" points="{left},{height} {points} '
                f'{left + (len(vs) - 1) * step:.1f},{height}"/>'
            )
        parts.append(f'<polyline class="line {klass}" points="{points}"/>')
    for index, label in enumerate(labels):
        if label:
            anchor = "start" if index == 0 else "end" if index == len(labels) - 1 else "mid"
            parts.append(
                f'<text class="tick {anchor}" x="{left + index * step:.1f}" '
                f'y="{height + 17}">{label}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def shorten(url: str, width: int = 62) -> str:
    """A URL trimmed to fit a table cell, scheme dropped, middle elided."""
    trimmed = url.split("://", 1)[-1]
    if len(trimmed) <= width:
        return trimmed
    return trimmed[:width - 22] + "\u2026" + trimmed[-21:]


def render(rows: list[dict], log_path: str) -> str:
    """Assemble the whole page."""
    outdegree = collections.Counter(row["parent"] for row in rows if row["parent"])
    for row in rows:
        row["kids"] = outdegree.get(row["url"], 0)

    span = (rows[-1]["at"] - rows[0]["at"]).total_seconds() or 1e-9
    total_bytes = sum(row["bytes"] for row in rows)
    sites = collections.Counter(row["site"] for row in rows)
    hosts = collections.Counter(row["host"] for row in rows)
    depths = collections.Counter(row["depth"] for row in rows)
    codes = collections.Counter(row["status"] for row in rows)
    notes = collections.Counter(
        part.strip().split(":")[0].split(" to ")[0]
        for row in rows for part in row["note"].split(";") if part.strip()
    )

    bucket = max(1, round(span / 60))
    timeline = collections.Counter(
        int((row["at"] - rows[0]["at"]).total_seconds()) // bucket for row in rows
    )
    slots = range(max(timeline) + 1) if timeline else range(1)
    rate = [timeline.get(slot, 0) / bucket for slot in slots]
    rate_labels = [
        f"{slot * bucket}s" if slot % max(1, len(rate) // 8) == 0 else ""
        for slot in slots
    ]

    window = max(1, len(rows) // 40)
    chunks = [rows[i:i + window] for i in range(0, len(rows), window)]
    page_prio = [sum(r["page_prio"] for r in c) / len(c) for c in chunks]
    site_prio = [sum(r["domain_prio"] for r in c) / len(c) for c in chunks]
    prio_labels = [
        f"{i * window}" if i % max(1, len(chunks) // 8) == 0 else ""
        for i in range(len(chunks))
    ]

    settled = rows[int(0.95 * len(rows))]["at"]
    sustained = 0.95 * len(rows) / max(1e-9, (settled - rows[0]["at"]).total_seconds())

    kept = sum(1 for row in rows if row["status"] == "200"
               and "not html" not in row["note"]
               and "already crawled" not in row["note"])
    tiles = [
        ("pages kept", f"{kept:,}",
         f"from {len(rows):,} requests &middot; {len(rows) - kept:,} empty-handed"),
        ("pages / second", f"{len(rows) / span:.2f}",
         f"{span:.1f}s end to end &middot; {sustained:.2f}/s for the first 95%"),
        ("distinct hosts", f"{len(hosts):,}", f"{len(sites):,} superdomains"),
        ("downloaded", f"{total_bytes / 1e6:,.2f} MB",
         f"{total_bytes / len(rows) / 1024:.1f} KiB per page"),
        ("deepest link", f"{max(depths)}", f"{depths[0]} seed pages"),
        ("biggest site", f"{100 * sites.most_common(1)[0][1] / len(rows):.2f}%",
         sites.most_common(1)[0][0]),
    ]

    depth_legend = "".join(
        f'<span class="key"><i class="d{min(d, len(DEPTH_FILL) - 1)}"></i>'
        f"depth {d} &middot; {depths[d]:,}</span>"
        for d in sorted(depths)
    ) + '<span class="key"><i class="dead"></i>not 200</span>'

    code_rows = "".join(
        f'<tr><td><span class="dot s{code[0]}"></span>{code}</td>'
        f"<td>{STATUS_FILL.get(code[0], ('', 'other'))[1]}</td>"
        f"<td class=num>{count:,}</td>"
        f"<td class=num>{100 * count / len(rows):.2f}%</td></tr>"
        for code, count in sorted(codes.items())
    )
    note_rows = "".join(
        f"<tr><td>{html.escape(note)}</td><td class=num>{count:,}</td></tr>"
        for note, count in notes.most_common(12)
    ) or '<tr><td colspan=2 class=empty>nothing unusual</td></tr>'

    busiest = sorted(rows, key=lambda row: -row["kids"])[:12]
    busy_rows = "".join(
        f'<tr><td class=url title="{html.escape(row["url"], quote=True)}">'
        f'{html.escape(shorten(row["url"]))}</td>'
        f'<td class=num>{row["depth"]}</td><td class=num>{row["kids"]:,}</td></tr>'
        for row in busiest
    )

    return TEMPLATE.format(
        log=html.escape(log_path),
        when=rows[0]["at"].strftime("%d %b %Y, %H:%M"),
        tiles="".join(
            f'<div class="tile"><span class="tilelabel">{label}</span>'
            f'<span class="tilevalue">{value}</span>'
            f'<span class="tilenote">{note}</span></div>'
            for label, value, note in tiles
        ),
        graph=svg_graph(rows),
        depth_legend=depth_legend,
        edges=f"{sum(1 for row in rows if row['parent']):,}",
        timeline=svg_lines([("pages/sec", "s1", rate)], rate_labels, area=True),
        bucket=bucket,
        priority=svg_lines(
            [("page priority", "s1", page_prio),
             ("domain priority", "s2", site_prio)],
            prio_labels, height=130),
        sites=svg_bars(sites.most_common(15)),
        hosts=svg_bars(hosts.most_common(10)),
        codes=code_rows,
        notes=note_rows,
        busy=busy_rows,
        depth_bars=svg_bars([(f"depth {d}", depths[d]) for d in sorted(depths)]),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crawl report</title>
<style>
:root {{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --edge:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --dead:#898781;
  --d0:#104281; --d1:#1c5cab; --d2:#2a78d6; --d3:#5598e7; --d4:#86b6ef;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --critical:#d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835; --edge:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926;
    --d0:#b7d3f6; --d1:#86b6ef; --d2:#5598e7; --d3:#2a78d6; --d4:#1c5cab;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --edge:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926;
  --d0:#b7d3f6; --d1:#86b6ef; --d2:#5598e7; --d3:#2a78d6; --d4:#1c5cab;
}}
* {{ box-sizing: border-box; }}
body {{
  margin:0; background:var(--plane); color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
  padding-block:40px; padding-inline:16px;
}}
main {{ max-width:960px; margin:0 auto; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.02em; }}
h2 {{ font-size:15px; margin:0 0 2px; letter-spacing:-.01em; }}
.sub {{ color:var(--ink2); margin:0 0 32px; }}
.lede {{ color:var(--ink2); margin:0 0 16px; max-width:66ch; }}
section {{
  background:var(--surface); border:1px solid var(--edge); border-radius:12px;
  padding:20px; margin-bottom:20px;
}}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }}
.tile {{ background:var(--surface); border:1px solid var(--edge); border-radius:12px; padding:14px 16px; display:flex; flex-direction:column; gap:2px; }}
.tilelabel {{ color:var(--ink2); font-size:12px; }}
.tilevalue {{ font-size:26px; letter-spacing:-.02em; line-height:1.15; }}
.tilenote {{ color:var(--muted); font-size:12px; }}
.graph {{ width:100%; height:auto; display:block; margin:4px 0 8px; }}
.graph .ring {{ fill:none; stroke:var(--grid); stroke-width:1; }}
.graph .ringlabel {{ fill:var(--muted); font-size:10px; }}
.graph .edges line {{ stroke:var(--axis); stroke-width:.55; opacity:.28; }}
.graph .n {{ stroke:var(--surface); stroke-width:1.2; cursor:crosshair; }}
.graph .n:hover, .graph .n:focus {{ stroke:var(--ink); stroke-width:2; outline:none; }}
.d0 {{ fill:var(--d0); }} .d1 {{ fill:var(--d1); }} .d2 {{ fill:var(--d2); }}
.d3 {{ fill:var(--d3); }} .d4 {{ fill:var(--d4); }} .dead {{ fill:var(--dead); }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; color:var(--ink2); font-size:12px; }}
.key {{ display:inline-flex; align-items:center; gap:6px; }}
.key i {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}
.key i.d0 {{ background:var(--d0); }} .key i.d1 {{ background:var(--d1); }}
.key i.d2 {{ background:var(--d2); }} .key i.d3 {{ background:var(--d3); }}
.key i.d4 {{ background:var(--d4); }} .key i.dead {{ background:var(--dead); }}
.key i.s1 {{ background:var(--s1); border-radius:2px; }}
.key i.s2 {{ background:var(--s2); border-radius:2px; }}
.chart {{ width:100%; height:auto; display:block; }}
.chart .grid {{ stroke:var(--grid); stroke-width:1; }}
.chart .axis {{ stroke:var(--axis); stroke-width:1; }}
.chart .tick {{ fill:var(--muted); font-size:11px; }}
.chart .tick.end {{ text-anchor:end; }}
.chart .tick.mid {{ text-anchor:middle; }}
.chart .line {{ fill:none; stroke-width:2; vector-effect:non-scaling-stroke; }}
.chart .line.s1 {{ stroke:var(--s1); }} .chart .line.s2 {{ stroke:var(--s2); }}
.chart .area.s1 {{ fill:var(--s1); opacity:.14; stroke:none; }}
.bar {{ display:grid; grid-template-columns:minmax(90px,26%) 1fr 64px; align-items:center; gap:10px; margin:5px 0; }}
.barlabel {{ color:var(--ink2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }}
.bartrack {{ background:var(--grid); border-radius:4px; height:11px; overflow:hidden; }}
.barfill {{ display:block; height:100%; background:var(--s1); border-radius:4px; }}
.barvalue {{ text-align:right; color:var(--ink2); font-variant-numeric:tabular-nums; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); }}
th {{ color:var(--muted); font-weight:600; font-size:12px; }}
th.num {{ text-align:right; }}
table.wide {{ table-layout:fixed; }}
table.wide th.num, table.wide td.num {{ width:96px; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.url {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:0; color:var(--ink2); }}
.dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:7px; }}
.dot.s2 {{ background:var(--good); }} .dot.s3 {{ background:var(--warn); }}
.dot.s4 {{ background:var(--serious); }} .dot.s5, .dot.s0 {{ background:var(--critical); }}
.two {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
@media (max-width:640px) {{ .two {{ grid-template-columns:1fr; }} }}
.empty {{ color:var(--muted); }}
#tip {{
  position:fixed; pointer-events:none; opacity:0; transition:opacity .1s;
  background:var(--ink); color:var(--surface); padding:7px 10px; border-radius:7px;
  font-size:12px; max-width:min(420px,80vw); z-index:9; line-height:1.45;
}}
#tip b {{ display:block; font-weight:600; word-break:break-all; }}
#tip span {{ opacity:.75; }}
</style></head><body>
<main>
<h1>Crawl report</h1>
<p class="sub">{log} &middot; {when}</p>

<div class="tiles">{tiles}</div>

<section>
  <h2>The crawl graph</h2>
  <p class="lede">Every page the crawler fetched, placed on the ring of its link
  depth from the seeds and joined to the page it was discovered on:
  {edges} edges, the spanning tree of the crawl. Dot size is how many new pages
  that page contributed. Hover any dot for its URL.</p>
  {graph}
  <div class="legend">{depth_legend}</div>
</section>

<section>
  <h2>Throughput</h2>
  <p class="lede">Pages finished per second, in {bucket}-second buckets. The
  ramp at the start is robots.txt. Early on nearly every page is a new
  host and costs an extra request before it can be fetched. A long thin tail at
  the end is the opposite problem: a few unreachable hosts each holding a thread
  until its timeout expires, which drags the end-to-end average well below the
  rate the crawler actually sustained.</p>
  {timeline}
</section>

<section>
  <h2>What the priority function was doing</h2>
  <p class="lede">Mean priority of the pages being picked, as the crawl runs.
  Both scores stay high because the frontier keeps offering fresh domains.
  If the crawler were sinking into one site, these would decay toward zero.</p>
  <div class="legend" style="margin-bottom:8px">
    <span class="key"><i class="s1"></i>page priority &middot; 1/lg(2+pages from this host)</span>
    <span class="key"><i class="s2"></i>domain priority &middot; superdomain footprint per subdomain</span>
  </div>
  {priority}
</section>

<section class="two">
  <div>
    <h2>Pages per superdomain</h2>
    <p class="lede">Top 15 of the sites reached.</p>
    {sites}
  </div>
  <div>
    <h2>Pages per host</h2>
    <p class="lede">Subdomains counted separately, as the scoring does.</p>
    {hosts}
  </div>
</section>

<section class="two">
  <div>
    <h2>What came back</h2>
    <table><thead><tr><th>code</th><th>meaning</th><th class=num>pages</th>
    <th class=num>share</th></tr></thead><tbody>{codes}</tbody></table>
  </div>
  <div>
    <h2>Anything unusual</h2>
    <table><thead><tr><th>note</th><th class=num>pages</th></tr></thead>
    <tbody>{notes}</tbody></table>
  </div>
</section>

<section>
  <h2>Pages per depth</h2>
  {depth_bars}
</section>

<section>
  <h2>Most productive pages</h2>
  <p class="lede">The pages that contributed the most previously unseen URLs to
  the frontier, the hubs of the crawl tree.</p>
  <table class="wide"><thead><tr><th>url</th><th class=num>depth</th>
  <th class=num>new pages</th></tr></thead><tbody>{busy}</tbody></table>
</section>
</main>
<div id="tip" role="status"></div>
<script>
const tip = document.getElementById('tip');
for (const dot of document.querySelectorAll('.graph .n')) {{
  const show = event => {{
    tip.innerHTML = '<b>' + dot.dataset.url + '</b><span>' + dot.dataset.meta + '</span>';
    const box = dot.getBoundingClientRect();
    const x = (event.clientX ?? box.x) + 14, y = (event.clientY ?? box.y) + 14;
    tip.style.opacity = 1;
    tip.style.left = Math.min(x, innerWidth - tip.offsetWidth - 12) + 'px';
    tip.style.top = Math.min(y, innerHeight - tip.offsetHeight - 12) + 'px';
  }};
  dot.addEventListener('mouseenter', show);
  dot.addEventListener('mousemove', show);
  dot.addEventListener('focus', show);
  dot.addEventListener('mouseleave', () => tip.style.opacity = 0);
  dot.addEventListener('blur', () => tip.style.opacity = 0);
}}
</script>
</body></html>
"""


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("log", type=click.Path(exists=True, dir_okay=False), default="crawl.log")
@click.option("-o", "--out", type=click.Path(dir_okay=False), default="crawl-report.html",
              show_default=True, help="Where to write the HTML page.")
def main(log, out):
    """Draw the crawl recorded in LOG as a single self-contained HTML page."""
    rows = load(log)
    if not rows:
        raise click.ClickException(f"{log} has no crawled pages in it")
    crawler.superdomain("example.com")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(render(rows, log))
    click.echo(f"{len(rows):,} pages -> {out}", err=True)


if __name__ == "__main__":
    main()
