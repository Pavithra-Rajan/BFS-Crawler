#!/usr/bin/env python3

import dataclasses
import ssl
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request

import click
import tldextract

_tld = tldextract.TLDExtract(include_psl_private_domains=True)


#  Fetching 
MAX_BYTES = 5 * 1024 * 1024

CHUNK_BYTES = 64 * 1024

_SSL_CONTEXT = ssl.create_default_context()


def is_http_url(url: str) -> bool:
    """True for the only two schemes we are ever willing to open.

    urllib will happily open file:// and ftp://, so a hyperlink could otherwise
    make us read the local disk. URL normalization applies the same rule, so it
    lives here in one place rather than as two prefix tests that drift.
    """
    try:
        return urllib.parse.urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


@dataclasses.dataclass
class FetchResult:
    """The outcome of one download attempt. Never an exception -- a request
    that failed is still a result, because the log has to record it."""

    url: str
    final_url: str
    status: int | None = None
    reason: str | None = None
    content_type: str | None = None
    charset: str | None = None
    size: int = 0
    elapsed: float = 0.0
    body: bytes | None = None
    error: str | None = None
    redirects: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    """Remembers the hops so every URL in the chain can be marked seen.

    Holds per-request state, so build a fresh one per fetch rather than
    sharing an opener across threads.
    """

    def __init__(self):
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_capped(response, max_bytes: int) -> tuple[bytes, bool]:
    """Read up to max_bytes, in chunks. Returns (body, was_truncated)."""
    chunks, total = [], 0
    while total <= max_bytes:
        chunk = response.read(min(CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    body = b"".join(chunks)
    return (body[:max_bytes], True) if total > max_bytes else (body, False)


def fetch(
    url, *, user_agent, timeout, accept=("text/html",), max_bytes=MAX_BYTES
) -> FetchResult:
    """Download one URL. Returns a FetchResult for every outcome, including
    failures -- callers check .status and .error rather than catching.

    The body is read only when the response Content-Type is in `accept`;
    anything else is identified from the headers and the connection dropped
    without downloading it. Fetching robots.txt means passing
    accept=("text/plain",).
    """
    started = time.monotonic()
    recorder = _RedirectRecorder()

    def result(**kw):
        kw.setdefault("final_url", url)
        return FetchResult(
            url=url,
            elapsed=time.monotonic() - started,
            redirects=tuple(recorder.chain),
            **kw,
        )

    if not is_http_url(url):
        return result(error="unsupported scheme")

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "identity",
            },
        )
        opener = urllib.request.build_opener(
            recorder, urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
        )
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type in accept:
                body, truncated = _read_capped(response, max_bytes)
                size = len(body)
            else:
                body, truncated, size = None, False, response.length or 0
            return result(
                final_url=response.geturl(),
                status=response.status,
                reason=response.reason or None,
                content_type=content_type,
                charset=response.headers.get_content_charset(),
                body=body,
                size=size,
                truncated=truncated,
            )
    except urllib.error.HTTPError as exc:
        with exc:
            return result(
                final_url=exc.url,
                status=exc.code,
                reason=exc.reason,
                content_type=exc.headers.get_content_type(),
            )
    except urllib.error.URLError as exc:
        reason = exc.reason
        name = (
            type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
        )
        return result(error=f"{name}: {reason}")
    except Exception as exc:
        return result(error=f"{type(exc).__name__}: {exc}")


# CLI 


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("query", required=False)
@click.option(
    "--seeds",
    "seeds_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read seed URLs from FILE instead of querying a search engine.",
)
@click.option(
    "--pages",
    type=click.IntRange(min=1),
    default=1000,
    show_default=True,
    help="Stop after crawling this many pages.",
)
@click.option(
    "--threads",
    type=click.IntRange(min=1),
    default=16,
    show_default=True,
    help="Number of fetcher threads.",
)
@click.option(
    "--delay",
    type=click.FloatRange(min=0),
    default=1.0,
    show_default=True,
    help="Minimum seconds between requests to the same host.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=10.0,
    show_default=True,
    help="Per-request timeout in seconds.",
)
@click.option(
    "--max-depth",
    type=click.IntRange(min=0),
    default=10,
    show_default=True,
    help="Maximum link depth from the seeds.",
)
@click.option(
    "--log",
    "log_file",
    type=click.Path(dir_okay=False),
    default="crawl.log",
    show_default=True,
    help="Write the visit log here.",
)
@click.option(
    "--user-agent",
    default="CS6913-Crawler/0.1",
    show_default=True,
    help="User-Agent to send.",
)
@click.option(
    "--fetch",
    "fetch_urls",
    metavar="URL",
    multiple=True,
    help="Debugging aid: fetch each URL, print what came back, and exit. Repeatable.",
)
def main(query, seeds_file, fetch_urls, user_agent, timeout, **opts):
    """Crawl the web breadth-first, preferring under-crawled domains.

    Seeds come from a search engine result page for QUERY, or from --seeds FILE.
    """
    if fetch_urls:
        for url in fetch_urls:
            show_fetch(fetch(url, user_agent=user_agent, timeout=timeout))
        return

    if not query and not seeds_file:
        raise click.UsageError("give a QUERY or --seeds FILE")

    click.echo("config:", err=True)
    config = dict(
        opts, query=query, seeds_file=seeds_file, user_agent=user_agent, timeout=timeout
    )
    for k, v in sorted(config.items()):
        click.echo(f"{k:12} {v}", err=True)


def show_fetch(r: FetchResult):
    """Print a FetchResult the way a human wants to read it."""

    def row(label, value):
        click.echo(f"  {label:8} {value}")

    click.echo(f"{r.status or '---'} {r.url}  ({r.elapsed:.2f}s)")
    if r.final_url != r.url:
        row("->", f"{r.final_url}  ({len(r.redirects)} redirect(s))")
    if r.reason and not r.ok:
        row("reason", r.reason)
    if r.content_type:
        row("type", r.content_type)
    if r.charset:
        row("charset", r.charset)
    if r.size:
        row("bytes", r.size)
    if r.error:
        row("error", r.error)
    if r.truncated:
        row("note", "truncated at the read cap")
    if r.body is not None:
        text = r.body[:2000].decode(r.charset or "utf-8", errors="replace")
        row("body", textwrap.shorten(text, width=150, placeholder=" ..."))
    elif r.ok:
        row("body", "(Not read due to unwanted content type)")
    click.echo()


if __name__ == "__main__":
    main()
