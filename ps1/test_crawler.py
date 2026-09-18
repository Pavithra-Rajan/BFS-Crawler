"""Tests for crawler.py. Run: .venv/bin/python -m pytest -q"""

import http.server
import threading
import time

import click.testing
import pytest

import crawler

UA = "test-agent/1.0"

HTML = b"<html><body><a href='/next'>next</a></body></html>"

ROBOTS = b"User-agent: *\nDisallow: /private\n"

SITE = {
    "/": b"""<a href="/a">a</a> <a href="/b/">b</a> <a href="/c">c</a>
             <a href="/private/x">private</a> <a href="/auth">auth</a>
             <a href="/gone">gone</a> <a href="/forbidden">403</a>
             <a href="/doc.pdf">pdf</a> <a href="/disguised">disguised</a>
             <a href="/hop">redirects onto a</a> <a href="/deep/1">deep</a>""",
    "/a": (b'<a href="missing1.html">404</a> <a href="/">home</a>'
           b'<a href="/a/index.html">same as /a/</a>'
           + b"".join(b'<a href="/a/%d.html">%d</a>' % (n, n) for n in range(1, 31))),
    "/b/": b"""<base href="/b/sub/"><a href="b1.html">1</a> <a href="../a">up to a</a>""",
    "/c": b"""<a href="/c">self</a> <a href="/a?x=1">a with a query</a>""",
    "/private/x": b"<a href='/'>should never be fetched</a>",
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path
        if path in SITE:
            self.respond(200, "text/html", SITE[path])
        elif path == "/ok":
            self.respond(200, "text/html; charset=utf-8", HTML)
        elif path == "/big":
            self.respond(200, "text/html", b"x" * 200_000)
        elif path == "/pdf" or path == "/disguised":
            self.respond(200, "application/pdf", b"%PDF-1.4 fake")
        elif path.startswith("/redirect/"):
            n = int(path.rsplit("/", 1)[1])
            self.respond(302, "text/html", b"",
                         location=f"/redirect/{n - 1}" if n > 1 else "/ok")
        elif path == "/hop":
            self.respond(302, "text/html", b"", location="/a")
        elif path == "/robots":
            self.respond(200, "text/plain", ROBOTS)
        elif path == "/robots.txt":
            self.respond(200, "text/plain", ROBOTS)
        elif path == "/auth":
            self.respond(401, "text/html", b"login please")
        elif path == "/forbidden":
            self.respond(403, "text/html", b"no")
        elif path == "/slow":
            time.sleep(5)
            self.respond(200, "text/html", HTML)
        elif path.startswith("/deep/"):
            n = int(path.rsplit("/", 1)[1])
            self.respond(200, "text/html", f"<a href='/deep/{n + 1}'>on</a>".encode())
        elif path.startswith("/a/") or path.startswith("/b/"):
            self.respond(200, "text/html", b"<a href='/'>home</a>")
        else:
            self.respond(404, "text/html", b"nope")

    def respond(self, code, ctype, body, location=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class GuardedHandler(http.server.BaseHTTPRequestHandler):
    """Every path, robots.txt included, demands credentials."""

    protocol_version = "HTTP/1.1"
    code = 401

    def do_GET(self):
        self.send_response(self.code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "0")
        self.send_header("WWW-Authenticate", 'Basic realm="private"')
        self.end_headers()

    def log_message(self, *args):
        pass


class BrokenHandler(GuardedHandler):
    """A server having a bad day."""

    code = 503


@pytest.fixture(scope="module")
def guarded():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GuardedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def broken():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BrokenHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def get(url, **kw):
    kw.setdefault("timeout", 5.0)
    return crawler.fetch(url, user_agent=UA, **kw)


# --- superdomains ------------------------------------------------------------

def test_superdomain_splits_subdomains_of_a_shared_site():
    assert crawler.superdomain("canada.wikipedia.org") == "wikipedia.org"
    assert crawler.superdomain("india.wikipedia.org") == "wikipedia.org"


def test_superdomain_respects_multi_label_public_suffixes():
    assert crawler.superdomain("www.bbc.co.uk") == "bbc.co.uk"
    assert crawler.superdomain("foo.github.io") == "foo.github.io"


# --- CLI ---------------------------------------------------------------------

def run_cli(*argv):
    return click.testing.CliRunner().invoke(crawler.main, list(argv))


def test_cli_requires_a_query_or_seeds():
    result = run_cli()
    assert result.exit_code != 0
    assert "give a QUERY, --seeds FILE or --seeds-from PAGE" in result.output


def test_cli_crawls_from_a_seeds_file(server, tmp_path):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text(f"# comment\n{server}/\n")
    log = tmp_path / "crawl.log"
    result = run_cli("--seeds", str(seeds), "--pages", "5", "--delay", "0",
                     "--threads", "2", "--log", str(log))
    assert result.exit_code == 0, result.output
    assert "pages/sec" in result.output
    rows = log.read_text().splitlines()[1:]
    assert sum(row.split("\t")[2] == "200" for row in rows) >= 5


def test_cli_rejects_a_missing_seeds_file():
    result = run_cli("--seeds", "no-such-file.txt")
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_rejects_nonsense_thread_counts():
    assert run_cli("q", "--threads", "0").exit_code != 0


# --- fetch -------------------------------------------------------------------

def test_fetch_reads_an_html_body(server):
    r = get(f"{server}/ok")
    assert (r.status, r.content_type, r.charset) == (200, "text/html", "utf-8")
    assert r.body == HTML and r.size == len(HTML)


def test_fetch_records_404_without_a_body(server):
    r = get(f"{server}/missing")
    assert (r.status, r.reason) == (404, "Not Found")
    assert r.body is None
    assert r.error is None and not r.ok


def test_fetch_does_not_download_non_html(server):
    r = get(f"{server}/pdf")
    assert r.status == 200 and r.content_type == "application/pdf"
    assert r.body is None
    assert r.size == len(b"%PDF-1.4 fake")


def test_fetch_follows_and_records_a_redirect_chain(server):
    r = get(f"{server}/redirect/3")
    assert r.status == 200
    assert r.final_url == f"{server}/ok"
    assert len(r.redirects) == 3


def test_fetch_truncates_an_oversized_page(server):
    r = get(f"{server}/big", max_bytes=1024)
    assert r.truncated and r.size == 1024 and len(r.body) == 1024


def test_fetch_times_out_instead_of_hanging(server):
    r = get(f"{server}/slow", timeout=0.5)
    assert r.status is None
    assert "timed out" in r.error.lower() or "timeout" in r.error.lower()
    assert r.elapsed < 3


def test_fetch_refuses_non_http_schemes():
    for url in ["file:///etc/passwd", "mailto:a@b.com", "javascript:void(0)", ""]:
        assert get(url).error == "unsupported scheme"


def test_fetch_survives_a_malformed_url():
    assert get("not a url at all").error is not None


def test_fetch_survives_a_refused_connection():
    r = get("http://127.0.0.1:9/")
    assert r.status is None and r.error and r.body is None


def test_fetch_reads_types_the_caller_asks_for(server):
    url = f"{server}/robots"
    assert get(url).body is None
    r = get(url, accept=("text/plain",))
    assert r.body.startswith(b"User-agent:") and r.content_type == "text/plain"


def test_fetch_reports_transport_failure_without_a_status():
    r = get("http://127.0.0.1:9/")
    assert r.status is None and r.error and not r.ok


def test_is_http_url_rejects_everything_we_must_not_open():
    for url in ["file:///etc/passwd", "mailto:a@b.com", "javascript:void(0)",
                "ftp://x.com/f", "", "not a url at all"]:
        assert not crawler.is_http_url(url)
    for url in ["http://x.com/a", "HTTPS://X.com/A", "https:/x.com/a",
                "https:x.com/a"]:
        assert crawler.is_http_url(url)


# --- links -------------------------------------------------------------------

def links(markup, base="http://site.test/dir/page.html", **kw):
    return crawler.extract_links(markup, base, **kw)


def test_extract_links_resolves_relative_hrefs():
    assert links(b"""
        <a href="https://other.test/x">absolute</a>
        <a href="sibling.html">sibling</a>
        <a href="/root.html">root</a>
        <a href="../up.html">up</a>
        <a href="?q=1">query only</a>
    """) == [
        "https://other.test/x",
        "http://site.test/dir/sibling.html",
        "http://site.test/root.html",
        "http://site.test/up.html",
        "http://site.test/dir/page.html?q=1",
    ]


def test_extract_links_honours_a_base_tag():
    markup = b"<head><base href='http://elsewhere.test/app/'></head><a href='x.html'>x</a>"
    assert links(markup) == ["http://elsewhere.test/app/x.html"]


def test_extract_links_resolves_a_relative_base_against_the_page():
    markup = b"<base href='/app/'><a href='x.html'>x</a>"
    assert links(markup) == ["http://site.test/app/x.html"]


def test_extract_links_applies_base_to_links_that_precede_it():
    markup = b"<a href='early.html'>e</a><base href='http://elsewhere.test/'>"
    assert links(markup) == ["http://elsewhere.test/early.html"]


def test_extract_links_uses_the_post_redirect_url_as_the_base():
    assert links(b"<a href='x.html'>x</a>", base="http://site.test/b/") == [
        "http://site.test/b/x.html"
    ]


def test_extract_links_decodes_entities_in_urls():
    assert links(b"<a href='/s?x=1&amp;y=2'>q</a>") == ["http://site.test/s?x=1&y=2"]


def test_extract_links_reads_image_map_areas():
    markup = b"<map><area href='/spot.html' coords='0,0,10,10'></map>"
    assert links(markup) == ["http://site.test/spot.html"]


def test_extract_links_drops_duplicates_but_keeps_order():
    markup = b"<a href='/b'>b</a><a href='/a'>a</a><a href='/b'>b again</a>"
    assert links(markup) == ["http://site.test/b", "http://site.test/a"]


def test_extract_links_ignores_hrefless_and_empty_anchors():
    assert links(b"<a name='x'>anchor</a><a href=''>empty</a><a href='  '>blank</a>") == []


def test_extract_links_survives_malformed_markup():
    markup = b"<a href='/ok'>fine<p><a href=/bare>bare<div><a href='/last'"
    assert "http://site.test/ok" in links(markup)


def test_extract_links_leaves_normalization_to_the_normalizer():
    got = links(b"<a href='#top'>t</a><a href='mailto:a@b.test'>m</a>")
    assert got == ["http://site.test/dir/page.html#top", "mailto:a@b.test"]


def test_decode_html_prefers_the_server_charset():
    body = "café".encode("cp1252")
    assert crawler.decode_html(body, "cp1252") == "café"


def test_decode_html_falls_back_to_a_meta_declaration():
    body = b"<meta charset='iso-8859-1'>" + "café".encode("iso-8859-1")
    assert "café" in crawler.decode_html(body)


def test_decode_html_never_raises_on_bad_bytes():
    assert crawler.decode_html(b"\xff\xfe bad bytes", "utf-8")


def test_extract_links_reads_what_fetch_returns(server):
    r = get(f"{server}/ok")
    assert crawler.extract_links(r.body, r.final_url, r.charset) == [f"{server}/next"]


# --- normalization -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("HTTP://Example.COM:80/A", "http://example.com/A"),
    ("https://Example.com:443/", "https://example.com/"),
    ("http://x.test:8080/p", "http://x.test:8080/p"),
    ("https://x.test", "https://x.test/"),
    ("http://x.test/p#section", "http://x.test/p"),
    ("http://user:pw@x.test/p", "http://x.test/p"),
    ("http://x.test/a/../b/c", "http://x.test/b/c"),
    ("http://x.test/a/./b/", "http://x.test/a/b/"),
    ("http://x.test/dir/index.html", "http://x.test/dir/"),
    ("http://x.test/main.htm", "http://x.test/"),
    ("http://x.test/index.jsp", "http://x.test/"),
    ("http://x.test/index.html?page=2", "http://x.test/index.html?page=2"),
    ("http://x.test/a b", "http://x.test/a%20b"),
    ("  http://x.test/p\n", "http://x.test/p"),
    ("http://x.test/page.html", "http://x.test/page.html"),
    ("http://x.test/script.php?id=3", "http://x.test/script.php?id=3"),
])
def test_normalize_canonicalizes(raw, expected):
    assert crawler.normalize(raw) == expected


@pytest.mark.parametrize("raw", [
    "mailto:someone@x.test",
    "javascript:void(0)",
    "file:///etc/passwd",
    "ftp://x.test/f",
    "http://x.test/photo.JPG",
    "http://x.test/paper.pdf",
    "http://x.test/style.css",
    "http://x.test/archive.tar.gz",
    "http://x.test/cgi-bin/lookup",
    "http://x.test/search.cgi?q=1",
    "http://localhost/p",
    "http://x.test/" + "/".join(str(i) for i in range(20)),
    "http://x.test/a/b/a/b/a",
    "http://x.test/" + "z" * 600,
])
def test_normalize_refuses_what_we_should_not_crawl(raw):
    assert crawler.normalize(raw) is None


def test_normalize_is_idempotent():
    once = crawler.normalize("HTTP://X.test:80/a/../b/index.html#top")
    assert once == crawler.normalize(once)


# --- priorities and the frontier ---------------------------------------------

def fresh(**kw):
    kw.setdefault("delay", 0.0)
    return crawler.Crawler(log_path="/dev/null", progress_every=0, **kw)


def test_page_priority_falls_as_a_host_is_crawled():
    c = fresh()
    first, _ = c.scores("http://x.test/p")
    c.host_pages["x.test"] = 30
    later, _ = c.scores("http://x.test/p")
    assert first == 1.0 and 0 < later < first


def test_domain_priority_falls_with_the_whole_superdomain():
    c = fresh()
    c.super_hosts["wikipedia.org"] = {"en.wikipedia.org"}
    c.super_pages["wikipedia.org"] = 100
    _, crowded = c.scores("http://fr.wikipedia.org/p")
    _, untouched = c.scores("http://other.test/p")
    assert crowded < untouched


def test_domain_priority_rewards_a_superdomain_with_many_subdomains():
    spread, narrow = fresh(), fresh()
    spread.super_hosts["u.org"] = {f"s{i}.u.org" for i in range(20)}
    spread.super_pages["u.org"] = 100
    narrow.super_hosts["u.org"] = {"www.u.org"}
    narrow.super_pages["u.org"] = 100
    assert spread.scores("http://new.u.org/p")[1] > narrow.scores("http://new.u.org/p")[1]


def test_push_refuses_duplicates_and_uncrawlable_urls():
    c = fresh(max_depth=2)
    assert c.push("http://x.test/p", 0)
    assert not c.push("http://x.test/p", 0)
    assert not c.push("http://x.test/p#other", 0)
    assert not c.push("http://x.test/img.png", 0)
    assert not c.push("http://x.test/deep", 3)
    assert len(c.heap) == 1


def test_pop_walks_the_frontier_breadth_first():
    c = fresh()
    c.push("http://a.test/", 0)
    c.push("http://b.test/", 1)
    c.push("http://c.test/", 0)
    order = [c.pop(0.0)[1] for _ in range(3)]
    assert order == [0, 0, 1]


def test_pop_prefers_a_host_we_have_crawled_less():
    c = fresh()
    c.push("http://busy.test/p", 0)
    c.push("http://quiet.test/p", 0)
    c.host_pages["busy.test"] = 50
    c.super_pages["busy.test"] = 50
    c.super_hosts["busy.test"] = {"busy.test"}
    assert c.pop(0.0)[0] == "http://quiet.test/p"


def test_pop_defers_a_host_that_was_just_hit():
    c = fresh()
    c.push("http://a.test/p", 0)
    c.push("http://b.test/p", 0)
    c.ready_at["a.test"] = 100.0
    assert c.pop(50.0)[0] == "http://b.test/p"
    assert c.pop(50.0) is None
    assert c.pop(150.0)[0] == "http://a.test/p"


def test_pop_parks_a_sleeping_host_without_losing_its_urls():
    c = fresh()
    c.push("http://a.test/p", 0)
    c.push("http://a.test/q", 0)
    c.ready_at["a.test"] = 100.0
    assert c.pop(50.0) is None
    assert c.heap == [] and c.queued == 2
    assert c.pop(150.0)[0] in ("http://a.test/p", "http://a.test/q")
    assert c.queued == 1


def test_back_off_parks_the_whole_superdomain():
    c = fresh(delay=1.0)
    c.push("http://en.wiki.org/a", 0)
    c.push("http://fr.wiki.org/b", 0)
    c.back_off("en.wiki.org", 1.0)
    assert c.pop(time.monotonic()) is None
    assert c.backoffs == 1


# --- robots ------------------------------------------------------------------

def test_robots_blocks_a_disallowed_path(server):
    c = fresh(user_agent=UA, timeout=5)
    assert c.allowed(f"{server}/")[0]
    assert not c.allowed(f"{server}/private/x")[0]


def test_robots_is_fetched_once_per_origin(server):
    c = fresh(user_agent=UA, timeout=5)
    c.allowed(f"{server}/one")
    c.allowed(f"{server}/two")
    assert len(c.robots) == 1


def test_robots_honours_a_crawl_delay():
    c = fresh(user_agent=UA)
    rules = crawler.urllib.robotparser.RobotFileParser()
    rules.parse(["User-agent: *", "Crawl-delay: 7"])
    c.robots["http://x.test"] = (rules, rules.crawl_delay(UA))
    assert c.allowed("http://x.test/p") == (True, 7)


def test_robots_missing_means_the_site_is_open():
    c = fresh(user_agent=UA, timeout=5)
    c.robots["http://x.test"] = (crawler.urllib.robotparser.RobotFileParser(), 0.0)
    c.robots["http://x.test"][0].allow_all = True
    assert c.allowed("http://x.test/anything")[0]


def test_robots_unreachable_leaves_the_site_open():
    c = fresh(user_agent=UA, timeout=1)
    assert c.allowed("http://127.0.0.1:9/p")[0]


def test_robots_behind_a_login_puts_the_whole_origin_off_limits(guarded):
    c = fresh(user_agent=UA, timeout=5)
    assert not c.allowed(f"{guarded}/anything")[0]


def test_robots_on_a_failing_server_puts_the_whole_origin_off_limits(broken):
    c = fresh(user_agent=UA, timeout=5)
    assert not c.allowed(f"{broken}/anything")[0]


def test_visit_does_not_fetch_a_disallowed_url(server):
    c = fresh(user_agent=UA, timeout=5)
    visit = c.visit(f"{server}/private/x")
    assert visit.result.error == "robots.txt disallows"
    assert visit.result.status is None and visit.links == []


# --- seeds -------------------------------------------------------------------

SERP = b"""<a href="/l/?uddg=https%3A%2F%2Ffirst.com%2Fpage">wrapped by ddg</a>
           <a href="/ck/a?u=a1aHR0cHM6Ly9zZWNvbmQuY29tL2E">wrapped by bing</a>
           <a href="https://third.com/a">plain</a>
           <a href="https://www.third.com/b">same site, another subdomain</a>
           <a href="https://duckduckgo.com/settings">engine chrome</a>
           <a href="https://fourth.com/c.pdf">a pdf</a>
           <a href="https://fourth.com/c">fourth</a>
           <a href="https://fifth.com/d">fifth</a>
           <a href="https://sixth.com/e">sixth</a>"""

CHROME = b"""<a href="https://www.bing.com/">home</a>
             <a href="https://www.youtube.com/">watch</a>"""

QUEUE_PAGE = b"""<h1>Wait For A Moment</h1>
                 <a href="https://old-search.marginalia.nu/search?query=x">retry</a>"""

THIN = b"""<a href="https://seventh.com/a">one real result</a>
           <a href="https://eighth.com/b">and another</a>
           <a href="https://duckduckgo.com/settings">engine chrome</a>"""

THIN_SITES = ["https://seventh.com/a", "https://eighth.com/b"]

SERP_SITES = ["https://first.com/page", "https://second.com/a", "https://third.com/a",
              "https://fourth.com/c", "https://fifth.com/d", "https://sixth.com/e"]


@pytest.fixture
def nosleep(monkeypatch):
    monkeypatch.setattr(crawler.time, "sleep", lambda seconds: None)


def serve(monkeypatch, *pages, status=200):
    """Answer every fetch() with the next canned page, repeating the last."""
    served = []

    def canned(url, **kw):
        body = pages[min(len(served), len(pages) - 1)]
        served.append(url)
        return crawler.FetchResult(url=url, final_url=url, status=status, body=body,
                                   content_type="text/html")

    monkeypatch.setattr(crawler, "fetch", canned)
    return served


def test_unwrap_frees_a_duckduckgo_redirect():
    assert crawler.unwrap(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa&rut=9"
    ) == "https://x.com/a"


def test_unwrap_frees_a_google_redirect():
    assert crawler.unwrap(
        "https://www.google.com/url?q=https%3A%2F%2Fx.com%2Fa&sa=U&ved=2a"
    ) == "https://x.com/a"


def test_unwrap_leaves_a_search_box_query_alone():
    box = "https://www.google.com/search?q=roman+aqueducts"
    assert crawler.unwrap(box) == box


def test_unwrap_frees_a_bing_redirect():
    assert crawler.unwrap(
        "https://www.bing.com/ck/a?!&&u=a1aHR0cHM6Ly9zZWNvbmQuY29tL2E&ntb=1"
    ) == "https://second.com/a"


def test_unwrap_leaves_a_plain_link_alone():
    assert crawler.unwrap("https://x.com/a?u=notbase64") == "https://x.com/a?u=notbase64"


def test_unwrap_survives_a_corrupt_payload():
    link = "https://www.bing.com/ck/a?u=a1!!!not!!base64!!!"
    assert crawler.unwrap(link) == link


def test_search_unwraps_results_and_keeps_one_per_site(monkeypatch, nosleep):
    serve(monkeypatch, SERP)
    assert crawler.search("anything", 10, 5.0) == SERP_SITES


def test_search_retries_an_engine_that_asks_us_to_wait(monkeypatch, nosleep):
    served = serve(monkeypatch, QUEUE_PAGE, QUEUE_PAGE, SERP)
    assert crawler.search("anything", 6, 5.0) == SERP_SITES
    assert len(served) == 3


def test_search_finds_nothing_in_a_page_of_engine_furniture(monkeypatch, nosleep):
    serve(monkeypatch, CHROME)
    assert crawler.search("anything", 10, 5.0) == []


def test_search_keeps_a_thin_page_rather_than_discarding_it(monkeypatch, nosleep):
    serve(monkeypatch, THIN)
    assert crawler.search("anything", 10, 5.0) == THIN_SITES


def test_search_puts_a_full_result_set_ahead_of_thin_leftovers(monkeypatch, nosleep):
    serve(monkeypatch, THIN, SERP)
    assert crawler.search("anything", 6, 5.0) == SERP_SITES


def test_search_moves_on_from_an_engine_that_refuses(monkeypatch):
    tried = []

    def canned(url, **kw):
        tried.append(url)
        return crawler.FetchResult(url=url, final_url=url, status=202)

    monkeypatch.setattr(crawler, "fetch", canned)
    assert crawler.search("anything", 10, 5.0) == []
    assert len(tried) == len(crawler.SERP_ENDPOINTS)


def test_search_gives_up_quietly_when_every_engine_refuses(monkeypatch, nosleep):
    monkeypatch.setattr(crawler, "fetch", lambda url, **kw: crawler.FetchResult(
        url=url, final_url=url, status=202))
    assert crawler.search("anything", 10, 5.0) == []


# --- seeds from a saved results page -----------------------------------------

GOOGLE_SAVE = b"""<html><head><title>roman aqueducts - Google Search</title></head>
<body><div id="searchform"><a href="/search?q=roman+aqueducts&tbm=isch">Images</a>
<a href="https://accounts.google.com/ServiceLogin">Sign in</a></div>
<div class="g"><a href="https://en.wikipedia.org/wiki/Roman_aqueduct"><h3>Roman aqueduct</h3></a></div>
<div class="g"><a href="/url?q=https://www.britannica.com/technology/aqueduct-engineering&amp;sa=U">
  <h3>Aqueduct | Britannica</h3></a></div>
<div class="g"><a href="https://www.historyhit.com/roman-aqueducts/"><h3>History Hit</h3></a></div>
<div class="g"><a href="https://www.nationalgeographic.com/aqueducts"><h3>Nat Geo</h3></a></div>
<div class="g"><a href="https://en.wikipedia.org/wiki/Aqua_Claudia"><h3>same site again</h3></a></div>
<div class="g"><a href="https://www.romanaqueducts.info/"><h3>Roman Aqueducts</h3></a></div>
<div class="g"><a href="https://smarthistory.org/roman-aqueducts/"><h3>Smarthistory</h3></a></div>
<footer><a href="https://policies.google.com/privacy">Privacy</a>
<a href="https://www.youtube.com/">YouTube</a></footer></body></html>"""


def test_harvest_reads_seeds_from_a_saved_google_page():
    seeds = list(crawler.harvest(GOOGLE_SAVE, "", None, set(), 10).values())
    assert seeds == [
        "https://en.wikipedia.org/wiki/Roman_aqueduct",
        "https://www.britannica.com/technology/aqueduct-engineering",
        "https://www.historyhit.com/roman-aqueducts/",
        "https://www.nationalgeographic.com/aqueducts",
        "https://www.romanaqueducts.info/",
        "https://smarthistory.org/roman-aqueducts/",
    ]


def test_harvest_honours_the_count_it_is_given():
    assert len(crawler.harvest(GOOGLE_SAVE, "", None, set(), 3)) == 3


def test_harvest_skips_sites_already_spoken_for():
    seeds = crawler.harvest(GOOGLE_SAVE, "", None, {"wikipedia.org"}, 10)
    assert not any("wikipedia" in url for url in seeds.values())


def test_cli_crawls_from_a_saved_results_page(server, tmp_path):
    page = tmp_path / "results.html"
    page.write_bytes(f'<a href="{server}/">only result</a>'.encode())
    log = tmp_path / "saved.log"
    result = run_cli("--seeds-from", str(page), "--pages", "3", "--delay", "0",
                     "--threads", "2", "--log", str(log))
    assert result.exit_code == 0, result.output
    assert "1 seed(s)" in result.output


def test_cli_rejects_a_page_with_no_results(tmp_path):
    page = tmp_path / "empty.html"
    page.write_text("<html><body>a screenshot, not a results page</body></html>")
    result = run_cli("--seeds-from", str(page), "--pages", "3")
    assert result.exit_code != 0
    assert "no off-engine links" in result.output


# --- end to end --------------------------------------------------------------

@pytest.fixture(scope="module")
def crawled(server, tmp_path_factory):
    log = tmp_path_factory.mktemp("crawl") / "crawl.log"
    c = crawler.Crawler(pages=20, threads=4, delay=0.0, timeout=5,
                        user_agent=UA, log_path=str(log), progress_every=0)
    stats = c.run([f"{server}/"])
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    return server, stats, rows[0], [dict(zip(rows[0], row)) for row in rows[1:]]


def test_crawl_logs_every_required_field(crawled):
    _, _, header, rows = crawled
    assert header == crawler.LOG_COLUMNS
    assert set(header) >= {"url", "bytes", "time", "status", "page_prio",
                           "domain_prio", "depth"}
    assert rows and all(row["url"].startswith("http://") for row in rows)


def test_crawl_visits_each_url_at_most_once(crawled):
    _, _, _, rows = crawled
    urls = [row["url"] for row in rows]
    assert len(urls) == len(set(urls))


def test_crawl_stops_at_the_page_budget(crawled):
    _, stats, _, rows = crawled
    assert stats["pages"] == 20
    assert stats["attempts"] == len(rows) > 20


def test_crawl_starts_at_depth_zero_and_goes_deeper(crawled):
    _, _, _, rows = crawled
    depths = [int(row["depth"]) for row in rows]
    assert depths[0] == 0 and max(depths) >= 1


def test_crawl_never_touches_a_robots_disallowed_path(crawled):
    _, stats, _, rows = crawled
    assert not any("/private" in row["url"] for row in rows)
    assert stats["blocked"] >= 1


def test_crawl_records_a_password_protected_page_and_moves_on(crawled):
    _, _, _, rows = crawled
    denied = [row for row in rows if row["status"] in ("401", "403")]
    assert all(row["note"] == "access denied" for row in denied)
    assert not any(row["url"].endswith("/auth/more") for row in rows)


def test_crawl_discards_404s(crawled):
    _, stats, _, rows = crawled
    assert any(row["status"] == "404" for row in rows)


def test_a_failed_request_does_not_use_up_the_budget(crawled):
    _, stats, _, rows = crawled
    wasted = [row for row in rows
              if not row["status"].startswith("2") or "not html" in row["note"]
              or "already crawled" in row["note"]]
    assert wasted, "the test site should produce some failures"
    assert stats["pages"] == 20
    assert stats["attempts"] == stats["pages"] + len(wasted)


def test_crawl_never_requests_a_blacklisted_extension(crawled):
    _, _, _, rows = crawled
    assert not any(row["url"].endswith(".pdf") for row in rows)


def test_crawl_notes_a_page_that_turned_out_not_to_be_html(crawled):
    _, _, _, rows = crawled
    disguised = [row for row in rows if row["url"].endswith("/disguised")]
    assert disguised and "not html" in disguised[0]["note"]


def test_crawl_follows_a_redirect_and_marks_the_target_seen(crawled):
    base, _, _, rows = crawled
    hop = [row for row in rows if row["url"].endswith("/hop")]
    assert hop and "redirected to" in hop[0]["note"]
    assert "already crawled" in hop[0]["note"]
    assert sum(row["url"] == f"{base}/a" for row in rows) == 1


def test_crawl_reports_throughput(crawled):
    _, stats, _, _ = crawled
    assert stats["rate"] > 0 and stats["elapsed"] > 0
    assert stats["bytes"] > 0 and stats["hosts"] == 1


def test_crawl_records_the_edge_it_arrived_by(crawled):
    _, _, _, rows = crawled
    depth_of = {row["url"]: int(row["depth"]) for row in rows}
    seeds = [row for row in rows if not row["parent"]]
    assert len(seeds) == 1 and depth_of[seeds[0]["url"]] == 0
    for row in rows:
        if row["parent"]:
            assert row["parent"] in depth_of
            assert depth_of[row["parent"]] == int(row["depth"]) - 1


def test_crawl_respects_max_depth(server, tmp_path):
    log = tmp_path / "shallow.log"
    c = crawler.Crawler(pages=50, threads=2, delay=0.0, timeout=5, max_depth=2,
                        user_agent=UA, log_path=str(log), progress_every=0)
    c.run([f"{server}/deep/1"])
    depths = [int(line.split("\t")[7]) for line in log.read_text().splitlines()[1:]]
    assert depths and max(depths) <= 2


def test_crawl_gives_up_when_nothing_can_succeed(server, tmp_path):
    """A hopeless crawl must still terminate rather than chase the budget."""
    log = tmp_path / "hopeless.log"
    c = crawler.Crawler(pages=5, threads=4, delay=0.0, timeout=5,
                        user_agent=UA, log_path=str(log), progress_every=0)
    stats = c.run([f"{server}/gone", f"{server}/forbidden", f"{server}/auth"])
    assert stats["pages"] == 0
    assert stats["attempts"] <= 5 * crawler.ATTEMPT_LIMIT


def test_crawl_survives_a_dead_host(tmp_path):
    log = tmp_path / "dead.log"
    c = crawler.Crawler(pages=3, threads=2, delay=0.0, timeout=1,
                        user_agent=UA, log_path=str(log), progress_every=0)
    stats = c.run(["http://127.0.0.1:9/", "http://no-such-host.invalid/"])
    assert stats["attempts"] == 2 and stats["pages"] == 0 and stats["ok"] == 0


def test_crawl_spreads_across_hosts_before_going_deep(server):
    c = fresh(pages=10)
    for port in range(1, 6):
        c.push(f"http://host{port}.test/", 0)
    c.host_pages["host1.test"] = 20
    c.super_pages["host1.test"] = 20
    c.super_hosts["host1.test"] = {"host1.test"}
    picked = [c.pop(0.0)[0] for _ in range(5)]
    assert picked[-1] == "http://host1.test/"
