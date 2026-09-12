#!/usr/bin/env python3

import dataclasses
import html.parser
import re
import ssl
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import click
import tldextract

TLD = tldextract.TLDExtract(include_psl_private_domains=True)

# Fetching

MAX_BYTES = 5 * 1024 * 1024

CHUNK_BYTES = 64 * 1024

DEADLINE_FACTOR = 3

_SSL_CONTEXT = ssl.create_default_context()


def is_http_url(url: str) -> bool:
    """
    To only open http/https and avoid file://, ftp:// and more
    """
    try:
        return urllib.parse.urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


@dataclasses.dataclass
class FetchResult:
    """
    The outcome of one download attempt, success or failure
    """

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


class FetchAttempt(urllib.request.HTTPRedirectHandler):
    """
    Per-fetch state: the URL, the clock, and the redirect hops taken
    """

    def __init__(self, url: str):
        self.url = url
        self.started = time.monotonic()
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def result(self, **kw) -> FetchResult:
        kw.setdefault("final_url", self.url)
        return FetchResult(
            url=self.url,
            elapsed=time.monotonic() - self.started,
            redirects=tuple(self.chain),
            **kw,
        )


def fetch(
    url, *, user_agent, timeout, accept=("text/html",), max_bytes=MAX_BYTES
) -> FetchResult:
    """
    Download one URL and describe the outcome as a FetchResult
    """
    attempt = FetchAttempt(url)

    if not is_http_url(url):
        return attempt.result(error="unsupported scheme")

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",  # priority of returned values html, xhtml 0.9 and anything else 0.1
                "Accept-Encoding": "identity", # to avoid gzipping
            },
        )
        opener = urllib.request.build_opener(
            attempt, urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
        )
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in accept:
                return attempt.result(
                    final_url=response.geturl(),
                    status=response.status,
                    reason=response.reason or None,
                    content_type=content_type,
                    size=response.length or 0,
                )
            deadline = attempt.started + DEADLINE_FACTOR * timeout
            chunks, total, ran_long = [], 0, False
            while total <= max_bytes:
                chunk = response.read(min(CHUNK_BYTES, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if time.monotonic() > deadline:
                    ran_long = True
                    break
            body = b"".join(chunks)
            return attempt.result(
                final_url=response.geturl(),
                status=response.status,
                reason=response.reason or None,
                content_type=content_type,
                charset=response.headers.get_content_charset(),
                body=body[:max_bytes],
                size=min(total, max_bytes),
                truncated=ran_long or total > max_bytes,
            )
    except urllib.error.HTTPError as exc:
        with exc:
            return attempt.result(
                final_url=exc.url,
                status=exc.code,
                reason=exc.reason,
                content_type=exc.headers.get_content_type(),
            )
    except Exception as exc:
        return attempt.result(error=repr(exc))


# Parsing

META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)  # regex that has <meta and charset with spaces around and quotes. Case insensitive

LINK_TAGS = ("a", "area")


def decode_html(html: bytes, charset: str | None = None) -> str:
    """
    Decode a page to text, preferring the server's charset, and never raising
    """
    declared = META_CHARSET.search(html[:4096])
    for candidate in (
        charset,
        declared.group(1).decode("ascii", "ignore") if declared else None,
        "utf-8",
    ):
        if candidate:
            try:
                return html.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue
    return html.decode("utf-8", errors="replace")


class LinkParser(html.parser.HTMLParser):
    """
    Collect raw href values, plus the document's <base href> if it has one
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in LINK_TAGS and tag != "base":
            return
        href = dict(attrs).get("href")
        if not href or not href.strip():
            return
        if tag == "base":
            if self.base is None:
                self.base = href.strip()
        else:
            self.hrefs.append(href.strip())


def extract_links(html: bytes, base_url: str, charset: str | None = None) -> list[str]:
    """
    The absolute URLs an HTML document links to, in document order
    """
    parser = LinkParser()
    try:
        parser.feed(decode_html(html, charset))
        parser.close()
    except Exception:
        pass

    base = urllib.parse.urljoin(base_url, parser.base) if parser.base else base_url
    seen, links = set(), []
    for href in parser.hrefs:
        try:
            url = urllib.parse.urljoin(base, href)
        except ValueError:
            continue
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


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
