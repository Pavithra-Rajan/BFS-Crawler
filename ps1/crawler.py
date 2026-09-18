#!/usr/bin/env python3

import base64
import collections
import concurrent.futures
import dataclasses
import datetime
import functools
import heapq
import html.parser
import itertools
import math
import posixpath
import re
import ssl
import textwrap
import threading
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


# URL normalization

DEFAULT_PORTS = {"http": 80, "https": 443}

MAX_URL_LENGTH = 512

MAX_PATH_SEGMENTS = 12

URL_WHITESPACE = str.maketrans({c: None for c in "\t\n\r\f\v\x00"} | {" ": "%20"})

INDEX_NAMES = frozenset(
    """index.html index.htm index.shtml index.php index.jsp index.asp index.aspx
       default.html default.htm default.asp main.html main.htm""".split()
)

SKIP_EXTENSIONS = frozenset(
    """jpg jpeg png gif bmp svg svgz webp ico tif tiff heic
       pdf ps eps doc docx xls xlsx ppt pptx odt ods odp rtf epub mobi djvu
       zip gz tgz bz2 xz 7z rar tar jar war exe dmg iso bin msi deb rpm apk
       mp3 mp4 avi mov wmv flv mkv webm ogg oga ogv wav m4a m4v aac opus mid
       css js mjs json jsonld xml rss atom csv tsv txt md yaml yml sql
       ttf otf woff woff2 eot swf class dll so dylib torrent ics vcf""".split()
)


def normalize(url: str) -> str | None:
    """
    The canonical form of a URL, or None if the crawler must not follow it
    """
    url = url.strip().translate(URL_WHITESPACE)
    if not is_http_url(url) or len(url) > MAX_URL_LENGTH or "cgi" in url.lower():
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or "." not in host:
        return None

    netloc = host if port in (None, DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"
    path = parts.path or "/"
    if "/." in path:
        directory = path.endswith("/")
        path = posixpath.normpath(path)
        if directory and not path.endswith("/"):
            path += "/"
    segments = path.split("/")
    last = segments[-1].lower()
    if last in INDEX_NAMES and not parts.query:
        segments[-1] = ""
    elif "." in last and last.rsplit(".", 1)[1] in SKIP_EXTENSIONS:
        return None

    named = [s for s in segments if s]
    if len(named) > MAX_PATH_SEGMENTS:
        return None
    if named and max(collections.Counter(named).values()) >= 3:
        return None
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, "/".join(segments), parts.query, "")
    )


@functools.lru_cache(maxsize=200_000)
def superdomain(host: str) -> str:
    """
    The registrable domain a host sits under: canada.wikipedia.org -> wikipedia.org
    """
    # print(TLD(host).top_domain_under_public_suffix or host)
    return TLD(host).top_domain_under_public_suffix or host


# Crawling

MAX_FRONTIER = 500_000

MAX_LINKS_PER_PAGE = 300

BACKOFF_FACTOR = 10

BACKOFF_CODES = (429, 503)

ATTEMPT_LIMIT = 4

LOG_COLUMNS = (
    "time url status bytes secs page_prio domain_prio depth note parent".split()
)


@dataclasses.dataclass
class Visit:
    """
    What a worker thread hands back to the coordinator for one URL
    """

    result: FetchResult
    links: list[str]
    delay: float
    parse_secs: float
    blocked: bool = False


class Crawler:
    """
    A frontier, a thread pool, and a log file
    """

    def __init__(
        self,
        *,
        pages=1000,
        threads=16,
        delay=1.0,
        timeout=10.0,
        max_depth=10,
        user_agent="CS6913-Crawler/0.1",
        log_path="crawl.log",
        progress_every=200,
    ):
        self.pages = pages
        self.threads = threads
        self.delay = delay
        self.timeout = timeout
        self.max_depth = max_depth
        self.user_agent = user_agent
        self.log_path = log_path
        self.progress_every = progress_every

        self.heap: list[tuple[int, float, int, str, str]] = []
        self.parked: dict[str, list] = {}
        self.waking: list[tuple[float, str]] = []
        self.tiebreak = itertools.count()
        self.seen: set[str] = set()
        self.done: set[str] = set()
        self.host_pages: collections.Counter = collections.Counter()
        self.super_pages: collections.Counter = collections.Counter()
        self.super_hosts: dict[str, set[str]] = {}
        self.ready_at: dict[str, float] = {}

        self.robots: dict[str, tuple[urllib.robotparser.RobotFileParser, float]] = {}
        self.robots_lock = threading.Lock()

        self.crawled = 0
        self.attempts = 0
        self.bytes = 0
        self.status_counts: collections.Counter = collections.Counter()
        self.fetch_secs: list[float] = []
        self.parse_secs = 0.0
        self.stalls = 0
        self.dropped = 0
        self.blocked = 0
        self.backoffs = 0

    def scores(self, url: str) -> tuple[float, float]:
        """
        (page priority, domain priority) for a URL, from the crawl so far
        """
        host = urllib.parse.urlsplit(url).hostname or ""
        parent = superdomain(host)
        subdomains = len(self.super_hosts.get(parent, ())) or 1
        page = 1.0 / math.log2(2 + self.host_pages[host])
        domain = 1.0 / math.log2(2 + self.super_pages[parent] / subdomains)
        return page, domain

    @property
    def queued(self) -> int:
        """
        URLs waiting in the frontier, runnable or parked
        """
        return len(self.heap) + sum(len(items) for items in self.parked.values())

    def push(self, url: str, depth: int, parent: str = "") -> bool:
        """
        Offer a discovered URL to the frontier; False if it was rejected
        """
        canonical = normalize(url)
        if canonical is None or depth > self.max_depth or canonical in self.seen:
            return False
        if len(self.heap) >= MAX_FRONTIER:
            self.dropped += 1
            return False
        self.seen.add(canonical)
        page, domain = self.scores(canonical)
        heapq.heappush(
            self.heap,
            (depth, -(page + domain), next(self.tiebreak), canonical, parent),
        )
        return True

    def pop(self, now: float):
        """
        The best URL that is safe to fetch right now, or None
        """
        while self.waking and self.waking[0][0] <= now:
            for item in self.parked.pop(heapq.heappop(self.waking)[1], ()):
                heapq.heappush(self.heap, item)
        while self.heap:
            depth, stale, seq, url, parent = heapq.heappop(self.heap)
            page, domain = self.scores(url)
            fresh = -(page + domain)
            if fresh > stale and self.heap and (depth, fresh) > self.heap[0][:2]:
                heapq.heappush(self.heap, (depth, fresh, seq, url, parent))
                continue
            host = urllib.parse.urlsplit(url).hostname or ""
            ready = max(
                self.ready_at.get(host, 0.0), self.ready_at.get(superdomain(host), 0.0)
            )
            if ready > now:
                if host not in self.parked:
                    self.parked[host] = []
                    heapq.heappush(self.waking, (ready, host))
                self.parked[host].append((depth, fresh, seq, url, parent))
                continue
            return url, depth, page, domain, host, parent
        self.stalls += 1
        return None

    def allowed(self, url: str) -> tuple[bool, float]:
        """
        Whether this origin's robots.txt permits the URL, and the delay it wants
        """
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self.robots.get(origin)
        if entry is None:
            response = fetch(
                origin + "/robots.txt",
                user_agent=self.user_agent,
                timeout=self.timeout,
                accept=("text/plain",),
                max_bytes=512 * 1024,
            )
            rules = urllib.robotparser.RobotFileParser()
            if response.ok and response.body is not None:
                rules.parse(decode_html(response.body, response.charset).splitlines())
            elif response.status in (401, 403) or (response.status or 0) >= 500:
                rules.disallow_all = True
            else:
                rules.allow_all = True
            entry = (rules, rules.crawl_delay(self.user_agent) or 0.0)
            with self.robots_lock:
                entry = self.robots.setdefault(origin, entry)
        rules, crawl_delay = entry
        return rules.can_fetch(self.user_agent, url), crawl_delay

    def visit(self, url: str) -> Visit:
        """
        Fetch and parse one URL on a pool thread
        """
        permitted, crawl_delay = self.allowed(url)
        delay = max(self.delay, crawl_delay)
        if not permitted:
            return Visit(
                FetchResult(url=url, final_url=url, error="robots.txt disallows"),
                [],
                delay,
                0.0,
                blocked=True,
            )

        result = fetch(url, user_agent=self.user_agent, timeout=self.timeout)
        links, parse_secs = [], 0.0
        if result.ok and result.body is not None:
            started = time.monotonic()
            links = extract_links(result.body, result.final_url, result.charset)
            parse_secs = time.monotonic() - started
        result.body = None
        return Visit(result, links, delay, parse_secs)

    def run(self, seeds) -> dict:
        """
        Crawl until the page budget is met or the frontier runs dry
        """
        for url in seeds:
            self.push(url, 0)       # add seed at depth 0
        started = time.monotonic()
        pool = concurrent.futures.ThreadPoolExecutor(self.threads, "fetch")
        inflight: dict[concurrent.futures.Future, tuple] = {}

        with open(self.log_path, "w", encoding="utf-8", buffering=1 << 16) as log:
            log.write("\t".join(LOG_COLUMNS) + "\n")
            try:
                while (
                    self.crawled < self.pages
                    and self.attempts < self.pages * ATTEMPT_LIMIT        # account for non reachable or crawlable pages
                    and (inflight or self.heap or self.parked)            # parked owing to the host in the delay window, or waiting for robots.txt to be fetched
                ):
                    while (
                        len(inflight) < self.threads
                        and self.crawled + len(inflight) < self.pages  # check if any free threads
                    ):
                        now = time.monotonic()
                        candidate = self.pop(now)
                        # print(f"Candidate: {candidate}")    
                        if candidate is None:
                            break
                        url, depth, page, domain, host, parent = candidate
                        self.ready_at[host] = now + self.delay
                        inflight[pool.submit(self.visit, url)] = candidate
                    if not inflight:
                        time.sleep(0.05)
                        continue
                    finished, _ = concurrent.futures.wait(
                        inflight,
                        timeout=0.25,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in finished:
                        self.record(log, future, *inflight.pop(future))
            except KeyboardInterrupt:
                click.echo("\nKeyboard Interrupt. Shutting down", err=True)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.monotonic() - started
        return {
            "pages": self.crawled,
            "attempts": self.attempts,
            "ok": self.status_counts[200],
            "elapsed": elapsed,
            "rate": self.crawled / elapsed if elapsed else 0.0,
            "bytes": self.bytes,
            "hosts": len(self.host_pages),
            "superdomains": len(self.super_pages),
            "status": dict(self.status_counts),
            "fetch_secs": self.fetch_secs,
            "parse_secs": self.parse_secs,
            "robots": len(self.robots),
            "blocked": self.blocked,
            "backoffs": self.backoffs,
            "frontier": self.queued,
            "seen": len(self.seen),
            "stalls": self.stalls,
            "dropped": self.dropped,
            "threads": self.threads,
            "top_hosts": self.host_pages.most_common(10),
            "top_superdomains": self.super_pages.most_common(10),
        }

    def back_off(self, host: str, delay: float):
        """
        Park a whole superdomain that just said "too many requests"
        """
        self.backoffs += 1
        until = time.monotonic() + delay * BACKOFF_FACTOR
        for key in (host, superdomain(host)):
            self.ready_at[key] = max(self.ready_at.get(key, 0.0), until)

    def record(self, log, future, url, depth, page, domain, host, parent):
        """
        Book one finished visit: log it, charge it to its domain, enqueue its links
        """
        try:
            visit = future.result()
        except Exception as exc:
            visit = Visit(
                FetchResult(
                    url=url, final_url=url, error=f"{type(exc).__name__}: {exc}"
                ),
                [],
                self.delay,
                0.0,
            )
        result = visit.result
        if visit.blocked:
            self.blocked += 1
            self.ready_at[host] = 0.0
            return
        self.ready_at[host] = time.monotonic() + visit.delay
        if result.status in BACKOFF_CODES:
            self.back_off(host, visit.delay)

        self.attempts += 1
        self.bytes += result.size
        self.parse_secs += visit.parse_secs
        self.fetch_secs.append(result.elapsed)
        self.status_counts[result.status or 0] += 1
        site = superdomain(host)
        self.host_pages[host] += 1
        self.super_pages[site] += 1
        self.super_hosts.setdefault(site, set()).add(host)

        final = normalize(result.final_url) or result.final_url
        duplicate = final in self.done
        if result.ok and result.content_type == "text/html" and not duplicate:
            self.crawled += 1
        self.done.add(final)
        self.seen.add(final)
        for hop in result.redirects:
            self.seen.add(normalize(hop) or hop)

        notes = []
        if result.error:
            notes.append(result.error)
        if result.status in (401, 403):
            notes.append("access denied")
        if result.redirects:
            notes.append(f"redirected to {final}")
        if duplicate:
            notes.append("already crawled")
        if result.ok and result.content_type != "text/html":
            notes.append(f"not html: {result.content_type}")
        if result.truncated:
            notes.append("truncated")
        log.write(
            "\t".join(
                (
                    datetime.datetime.now().isoformat(timespec="milliseconds"),
                    url,
                    str(result.status or 0),
                    str(result.size),
                    f"{result.elapsed:.3f}",
                    f"{page:.4f}",
                    f"{domain:.4f}",
                    str(depth),
                    "; ".join(notes),
                    parent,
                )
            )
            + "\n"
        )

        if result.ok and not duplicate:
            for link in visit.links[:MAX_LINKS_PER_PAGE]:
                self.push(link, depth + 1, url)
        if self.progress_every and self.attempts % self.progress_every == 0:
            click.echo(
                f"  {self.crawled:>6} pages  {self.attempts:>7} tried  "
                f"{self.queued:>7} queued  {len(self.host_pages):>5} hosts  "
                f"{self.bytes / 1e6:>7.1f} MB",
                err=True,
            )


# Seeds

SERP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

SERP_ENDPOINTS = (
    "https://html.duckduckgo.com/html/?q={}",
    "https://lite.duckduckgo.com/lite/?q={}",
    "https://old-search.marginalia.nu/search?query={}",
)

SERP_NOISE = (
    "duckduckgo.com",
    "marginalia.nu",
    "marginalia-search.com",
    "bing.com",
    "microsoft.com",
    "msn.com",
    "google.com",
    "youtube.com",
    "web.archive.org",
    "creativecommons.org",
    "ip2location.com",
    "twitter.com",
    "x.com",
)

SERP_ATTEMPTS = 5

SERP_RETRY_WAIT = 2.0

SERP_MIN_RESULTS = 5


def unwrap(link: str) -> str:
    """
    A result URL freed from whichever click-tracker the engine wrapped it in
    """
    parts = urllib.parse.urlsplit(link)
    query = urllib.parse.parse_qs(parts.query)
    if "uddg" in query:
        return query["uddg"][0]
    if parts.path == "/url" and query.get("q"):
        return query["q"][0]
    target = (query.get("u") or [""])[0]
    if target.startswith("a1"):
        payload = target[2:].replace(" ", "+")
        try:
            return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(
                "utf-8", "replace"
            )
        except (ValueError, UnicodeDecodeError):
            return link
    return link


def harvest(
    body: bytes, base_url: str, charset: str | None, sites: set[str], count: int
) -> dict[str, str]:
    """
    The usable result URLs on one search-result page, one per superdomain
    """
    found: dict[str, str] = {}
    for link in extract_links(body, base_url, charset):
        canonical = normalize(unwrap(link))
        if canonical is None:
            continue
        host = urllib.parse.urlsplit(canonical).hostname or ""
        site = superdomain(host)
        if site in sites or site in found:
            continue
        if any(host.endswith(noise) for noise in SERP_NOISE):
            continue
        found[site] = canonical
        if len(found) >= count:
            break
    # print(found)
    return found


def search(query: str, count: int, timeout: float) -> list[str]:
    """
    Seed URLs for a query, from whichever engine will answer
    """
    found: list[str] = []
    spare: list[str] = []
    sites: set[str] = set()
    for endpoint in SERP_ENDPOINTS:
        if len(found) >= count:
            break
        for attempt in range(SERP_ATTEMPTS):
            if attempt:                         # not 0th attempt
                time.sleep(SERP_RETRY_WAIT)
            response = fetch(
                endpoint.format(urllib.parse.quote_plus(query)),
                user_agent=SERP_UA,
                timeout=timeout,
            )
            if not response.ok or response.body is None:
                break
            page = harvest(
                response.body, response.final_url, response.charset, sites, count
            )
            sites.update(page)
            if len(page) >= SERP_MIN_RESULTS:
                found.extend(page.values())
                break
            spare.extend(page.values())
    return (found + spare)[:count]


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
    "--seeds-from",
    "seeds_page",
    type=click.Path(exists=True, dir_okay=False),
    metavar="PAGE",
    help="Read seeds out of a search-results page saved from your browser. "
    "Search in Google, save the page (Ctrl+S, 'HTML only'), point at it.",
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
def main(
    query,
    seeds_file,
    seeds_page,
    fetch_urls,
    user_agent,
    timeout,
    pages,
    threads,
    delay,
    max_depth,
    log_file,
):
    """
    Crawl the web breadth-first, preferring under-crawled domains
    """
    if fetch_urls:
        for url in fetch_urls:
            show_fetch(fetch(url, user_agent=user_agent, timeout=timeout))
        return

    if not query and not seeds_file and not seeds_page:
        raise click.UsageError("give a QUERY, --seeds FILE or --seeds-from PAGE")

    superdomain("example.com")
    if seeds_page:
        with open(seeds_page, "rb") as handle:
            page = handle.read()
        seeds = list(harvest(page, "", None, set(), max(10, threads)).values())
        if not seeds:
            raise click.ClickException(
                f"no off-engine links in {seeds_page}. Save the results page "
                "itself (Ctrl+S, 'Webpage, HTML only') rather than a "
                "screenshot or a printout."
            )
    elif seeds_file:
        seeds = []
        with open(seeds_file, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    seeds.append(line)
    else:
        click.echo(f"searching for {query!r}", err=True)
        seeds = search(query, max(10, threads), timeout)
        # print(seeds)
        if not seeds:
            raise click.ClickException(
                "no search engine returned usable results.\n\n"
                "DuckDuckGo rate-limits an IP for hours after a handful of "
                "queries in quick succession, and Marginalia is then carrying "
                "it alone. Wait for the limit to lapse and retry, search in a "
                "browser and use --seeds-from PAGE, or use --seeds FILE."
            )

    click.echo(
        f"{len(seeds)} seed(s), {threads} threads, {pages} page budget", err=True
    )
    for url in seeds[:10]:
        click.echo(
            f"  seed  {textwrap.shorten(url, 100, placeholder=' ...')}", err=True
        )

    crawler = Crawler(
        pages=pages,
        threads=threads,
        delay=delay,
        timeout=timeout,
        max_depth=max_depth,
        user_agent=user_agent,
        log_path=log_file,
    )
    show_summary(crawler.run(seeds), log_file)


def show_summary(stats: dict, log_file: str):
    """
    Print the end-of-crawl numbers a demo actually gets asked about
    """
    codes = " ".join(
        f"{code or 'err'}:{count}" for code, count in sorted(stats["status"].items())
    )
    click.echo("", err=True)
    click.echo(
        f"crawled     {stats['pages']} pages from {stats['attempts']} "
        f"requests -> {log_file}",
        err=True,
    )
    click.echo(
        f"elapsed     {stats['elapsed']:.1f}s  =  {stats['rate']:.1f} pages/sec "
        f"({stats['attempts'] / stats['elapsed']:.1f} requests/sec)",
        err=True,
    )
    click.echo(f"downloaded  {stats['bytes'] / 1e6:.1f} MB", err=True)
    click.echo(
        f"reached     {stats['hosts']} hosts across {stats['superdomains']} superdomains",
        err=True,
    )
    click.echo(
        f"frontier    {stats['frontier']} queued, {stats['seen']} URLs seen", err=True
    )
    click.echo(f"status      {codes}", err=True)
    click.echo(
        f"robots      {stats['robots']} sites consulted, {stats['blocked']} URLs off-limits",
        err=True,
    )
    click.echo(f"backed off  {stats['backoffs']} times after a 429 or 503", err=True)
    click.echo("top superdomains:", err=True)
    for name, count in stats["top_superdomains"]:
        click.echo(f"  {count:>5}  {name}", err=True)


def row(label, value):
    click.echo(f"  {label:8} {value}")


def show_fetch(r: FetchResult):
    """
    Print URL fetch details for debug
    """

    click.echo(f"{r.status or '---'} {r.url}  ({r.elapsed:.2f}s)")
    if r.final_url != r.url:
        row("redirect", f"{r.final_url}  ({len(r.redirects)} redirect(s))")
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
        text = decode_html(r.body[:2000], r.charset)
        row("body", textwrap.shorten(text, width=150, placeholder=" ..."))
        found = extract_links(r.body, r.final_url, r.charset)
        row("links", len(found))
        for url in found[:5]:
            row("", textwrap.shorten(url, width=140, placeholder=" ..."))
        if len(found) > 5:
            row("", f"... and {len(found) - 5} more")
    elif r.ok:
        row("body", "(Not read due to unwanted content type)")
    click.echo()


if __name__ == "__main__":
    main()
