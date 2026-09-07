"""Tests for crawler.py. Run: .venv/bin/python -m pytest -q"""

import http.server
import threading
import time

import click.testing
import pytest

import crawler

UA = "test-agent/1.0"

HTML = b"<html><body><a href='/next'>next</a></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path
        if path == "/ok":
            self._respond(200, "text/html; charset=utf-8", HTML)
        elif path == "/big":
            self._respond(200, "text/html", b"x" * 200_000)
        elif path == "/pdf":
            self._respond(200, "application/pdf", b"%PDF-1.4 fake")
        elif path.startswith("/redirect/"):
            n = int(path.rsplit("/", 1)[1])
            self._respond(302, "text/html", b"",
                          location=f"/redirect/{n - 1}" if n > 1 else "/ok")
        elif path == "/robots":
            self._respond(200, "text/plain", b"User-agent: *\nDisallow: /private")
        elif path == "/slow":
            time.sleep(5)
            self._respond(200, "text/html", HTML)
        else:
            self._respond(404, "text/html", b"nope")

    def _respond(self, code, ctype, body, location=None):
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def get(url, **kw):
    kw.setdefault("timeout", 5.0)
    return crawler.fetch(url, user_agent=UA, **kw)


def test_superdomain_splits_subdomains_of_a_shared_site():
    assert crawler._tld("canada.wikipedia.org").top_domain_under_public_suffix == "wikipedia.org"
    assert crawler._tld("india.wikipedia.org").top_domain_under_public_suffix == "wikipedia.org"


def test_superdomain_respects_multi_label_public_suffixes():
    assert crawler._tld("www.bbc.co.uk").top_domain_under_public_suffix == "bbc.co.uk"
    assert crawler._tld("foo.github.io").top_domain_under_public_suffix == "foo.github.io"


def _run(*argv):
    return click.testing.CliRunner().invoke(crawler.main, list(argv))


def test_cli_requires_a_query_or_seeds():
    result = _run()
    assert result.exit_code != 0
    assert "give a QUERY or --seeds FILE" in result.output


def test_cli_accepts_a_query():
    result = _run("machine learning", "--pages", "50")
    assert result.exit_code == 0
    assert "query        machine learning" in result.output
    assert "pages        50" in result.output


def test_cli_rejects_a_missing_seeds_file():
    result = _run("--seeds", "no-such-file.txt")
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_rejects_nonsense_thread_counts():
    assert _run("q", "--threads", "0").exit_code != 0


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
    assert r.truncated and r.size == 1024


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
