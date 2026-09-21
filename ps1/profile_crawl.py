#!/usr/bin/env python3
"""
    python profile_crawl.py --seeds seeds.txt --pages 500
    python profile_crawl.py --seeds seeds.txt --pages 300 --threads 4,16,32,64
    python profile_crawl.py "machine learning" --pages 1000 --cprofile
"""

import cProfile
import collections
import datetime
import io
import pstats
import statistics
import urllib.parse
import click
import crawler


def measure(seeds, threads, log_path, cprofile, **options) -> dict:
    """Run one crawl and return its stats dict with the profile bolted on.

    cProfile is deliberately started on this thread only, so what it reports is
    the coordinator thread's own function costs, not the worker threads' costs. 
    """
    engine = crawler.Crawler(
        threads=threads, log_path=log_path, progress_every=0, **options
    )
    profiler = cProfile.Profile() if cprofile else None
    if profiler:
        profiler.enable()
    stats = engine.run(list(seeds))
    if profiler:
        profiler.disable()
        listing = io.StringIO()
        pstats.Stats(profiler, stream=listing).sort_stats("tottime").print_stats(12)
        stats["cprofile"] = listing.getvalue()
    stats["log_path"] = log_path
    return stats


def bar(fraction: float, width: int = 28) -> str:
    """A fixed-width bar, for reading a distribution at a glance."""
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


def report(runs: list[dict]):
    """Print the sweep table, then the anatomy of the fastest run."""
    click.echo("\nthroughput by thread count")
    click.echo(f"  {'threads':>7}  {'pages':>6}  {'tried':>6}  {'wall':>7}  "
               f"{'pages/s':>8}  {'MB/s':>6}  {'busy':>5}  {'stalls':>7}")
    for run in runs:
        busy = sum(run["fetch_secs"]) / (run["elapsed"] * run["threads"])
        click.echo(
            f"  {run['threads']:>7}  {run['pages']:>6}  {run['attempts']:>6}  "
            f"{run['elapsed']:>6.1f}s  {run['rate']:>8.1f}  "
            f"{run['bytes'] / 1e6 / run['elapsed']:>6.2f}  "
            f"{busy:>4.0%}  {run['stalls']:>7}"
        )

    best = max(runs, key=lambda run: run["rate"])
    latencies = sorted(best["fetch_secs"])
    total_fetch = sum(latencies)
    click.echo(f"\nanatomy of the fastest run ({best['threads']} threads, "
               f"{best['pages']} pages from {best['attempts']} requests "
               f"in {best['elapsed']:.1f}s)")

    click.echo("\n  where the wall clock went")
    click.echo(f"    {'thread-seconds available':<28} {best['elapsed'] * best['threads']:>8.1f}s")
    click.echo(f"    {'spent waiting on sockets':<28} {total_fetch:>8.1f}s  "
               f"{bar(total_fetch / (best['elapsed'] * best['threads']))}")
    click.echo(f"    {'spent parsing HTML':<28} {best['parse_secs']:>8.1f}s  "
               f"{bar(best['parse_secs'] / (best['elapsed'] * best['threads']))}")
    idle = best["elapsed"] * best["threads"] - total_fetch - best["parse_secs"]
    click.echo(f"    {'idle (politeness, ramp-up)':<28} {idle:>8.1f}s  "
               f"{bar(idle / (best['elapsed'] * best['threads']))}")

    click.echo("\n  per-page cost")
    click.echo(f"    {'fetch p50 / p90 / p99':<28} "
               f"{statistics.median(latencies):.2f}s / "
               f"{latencies[int(0.90 * len(latencies))]:.2f}s / "
               f"{latencies[int(0.99 * len(latencies))]:.2f}s")
    click.echo(f"    {'slowest fetch':<28} {latencies[-1]:.2f}s")
    click.echo(f"    {'parse':<28} {1000 * best['parse_secs'] / best['pages']:.2f}ms")
    click.echo(f"    {'bytes':<28} {best['bytes'] / best['pages'] / 1024:.1f} KiB")
    click.echo(f"    {'robots.txt fetches':<28} {best['robots']} "
               f"({best['robots'] / best['pages']:.2f} per page)")
    click.echo(f"    {'requests per page kept':<28} "
               f"{best['attempts'] / max(1, best['pages']):.2f}")

    click.echo("\n  what came back")
    for code, count in sorted(best["status"].items()):
        click.echo(f"    {code or 'no response':<28} {count:>6}  "
                   f"{bar(count / best['attempts'])}")

    click.echo("\n  reach")
    click.echo(f"    {'distinct hosts':<28} {best['hosts']:>6}")
    click.echo(f"    {'distinct superdomains':<28} {best['superdomains']:>6}")
    click.echo(f"    {'pages per superdomain':<28} "
               f"{best['pages'] / max(1, best['superdomains']):>6.1f}")
    click.echo(f"    {'frontier left over':<28} {best['frontier']:>6}")
    click.echo(f"    {'off-limits by robots.txt':<28} {best['blocked']:>6}")
    click.echo(f"    {'backed off after 429/503':<28} {best['backoffs']:>6}")
    for name, count in best["top_superdomains"][:8]:
        click.echo(f"    {name[:28]:<28} {count:>6}  {bar(count / best['attempts'])}")

    click.echo("\n  pages per second, five seconds at a time")
    stamps = []
    with open(best["log_path"], encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            stamps.append(datetime.datetime.fromisoformat(line.split("\t", 1)[0]))
    if stamps:
        start = min(stamps)
        buckets = collections.Counter(
            int((stamp - start).total_seconds()) // 5 for stamp in stamps
        )
        peak = max(buckets.values())
        for bucket in range(max(buckets) + 1):
            count = buckets.get(bucket, 0)
            click.echo(f"    {bucket * 5:>4}s  {count / 5:>6.1f}  {bar(count / peak)}")

    if "cprofile" in best:
        click.echo("\n  coordinator thread only -- note that cProfile charges per")
        click.echo("  call, which inflates HTMLParser by roughly 40x; compare")
        click.echo("  against the unprofiled parse figure above")
        for line in best["cprofile"].splitlines()[4:20]:
            click.echo(f"    {line}")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("query", required=False)
@click.option("--seeds", "seeds_file", type=click.Path(exists=True, dir_okay=False),
              help="Read seed URLs from FILE instead of querying a search engine.")
@click.option("--pages", type=click.IntRange(min=1), default=500, show_default=True,
              help="Pages to crawl per run.")
@click.option("--threads", default="16", show_default=True,
              help="Thread count, or a comma-separated list to sweep.")
@click.option("--delay", type=click.FloatRange(min=0), default=1.0, show_default=True,
              help="Minimum seconds between requests to the same host.")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=10.0,
              show_default=True, help="Per-request timeout in seconds.")
@click.option("--max-depth", type=click.IntRange(min=0), default=10, show_default=True,
              help="Maximum link depth from the seeds.")
@click.option("--log", "log_file", type=click.Path(dir_okay=False),
              default="profile.log", show_default=True,
              help="Where each run writes its crawl log.")
@click.option("--cprofile", is_flag=True,
              help="Also profile the coordinator thread's own function costs.")
def main(query, seeds_file, pages, threads, delay, timeout, max_depth, log_file,
         cprofile):
    """Crawl QUERY (or --seeds FILE) and report where the time went."""
    if not query and not seeds_file:
        raise click.UsageError("give a QUERY or --seeds FILE")
    if seeds_file:
        with open(seeds_file, encoding="utf-8") as handle:
            seeds = [line.strip() for line in handle
                     if line.strip() and not line.startswith("#")]
    else:
        seeds = crawler.search(query, 10, timeout)
        if not seeds:
            raise click.ClickException(
                "the search engine returned no usable results; pass --seeds FILE"
            )

    crawler.superdomain("example.com")
    counts = [int(part) for part in threads.split(",")]
    runs = []
    for count in counts:
        click.echo(f"crawling {pages} pages with {count} threads ...", err=True)
        runs.append(measure(
            seeds, count, f"{log_file}.{count}" if len(counts) > 1 else log_file,
            cprofile, pages=pages, delay=delay, timeout=timeout, max_depth=max_depth,
        ))
    report(runs)


if __name__ == "__main__":
    main()
