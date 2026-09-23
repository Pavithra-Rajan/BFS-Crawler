================================================================
CS 6913, Fall 2026: Assignment #1
================================================================

This was developed with Python 3.12 on Linux Ubuntu. 
Two third-party libraries: click (command line) and tldextract (public suffix list). 
Everything else is the standard library. Section 6 lists every outside resource.


1. WHAT IS IN THIS SUBMISSION

CODE

  crawler.py          The crawler. Fetching, parsing, URL
                      normalization, the frontier, politeness,
                      robots.txt, the log, and the command line
                      are all in this one file. This is the file
                      to run.

  profile_crawl.py    Runs the same crawl at several thread
                      counts and reports where the time went.
                      This is where the "more than about 48
                      threads buys nothing" claim comes from.

INPUT FILES

  requirements.txt    pip requirements: click, tldextract, pytest.
  seeds.txt           20 fallback seed URLs, one per line, for
                      when the search engine rate-limits the IP.

SAMPLE LOGS

  crawl-1k.log        A 1000-page run 
  crawl-10k.log       A 10,000-page run with 64 threads
  crawl-20k.log       A 20k page run with 48 threads

2. HOW TO INSTALL AND RUN

Create a virtual env and source it. 

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate

Crawl from a search-engine query:

python crawler.py "brooklyn bridge" --pages 1000

Crawl from a file of seeds (no search engine involved):

python crawler.py --seeds seeds.txt --pages 10000 --threads 48

Crawl from a results page saved out of a browser:

python crawler.py --seeds-from ~/mosaics.html --pages 1000

Look at a single URL and exit, which is the quickest way to see
how one page is handled (Used this for initial debugging):

python crawler.py --fetch https://www.nasa.gov/


Ctrl-C stops a crawl cleanly: the log is closed, the statistics block is
written, and the summary is printed.


3. COMMAND-LINE PARAMETERS


Exactly one source of seeds is required: a QUERY argument,
--seeds, --seeds-from, or --fetch.

  QUERY                 A search phrase, in quotes. The crawler
                        asks DuckDuckGo's html endpoint, then its
                        lite endpoint, then Marginalia, and takes
                        at most one seed per registrable domain.

  --seeds FILE          Read seeds from FILE instead, one URL per
                        line. Blank lines and lines starting with
                        # are ignored.

  --seeds-from PAGE     Read seeds out of a search-results page
                        saved from a browser (Ctrl-S, "Webpage,
                        HTML only"). Click-tracker wrappers,
                        sign-in links and engine navigation are
                        stripped. 

  --pages N             Stop after N pages have been retrieved.
                        Default 1000. This counts pages actually
                        kept, not requests spent: a 404, a login
                        wall, a non-HTML file and a redirect onto
                        an already-crawled page are all requests
                        with no page to show for them. They are
                        written to the log.

  --threads N           Number of fetcher threads. Default 16.

  --delay SECONDS       Minimum seconds between requests to the
                        same host. Default 1.0. A Crawl-delay in
                        robots.txt raises this floor for that
                        site but never lowers it.

  --timeout SECONDS     Per-request socket timeout. Default 10.0.
                        A whole page body must arrive within
                        three times this value.

  --max-depth N         Maximum link distance from the seeds.
                        Default 10.

  --log FILE            Where to write the visit log. Default
                        crawl.log. Overwritten, not appended.

  --user-agent STRING   User-Agent to send, and the name that
                        robots.txt rules are matched against.
                        Default CS6913-Crawler/0.1.

  --fetch URL           Debugging aid: fetch each URL, print what
                        came back, and exit without crawling.
                        Repeatable.

  -h, --help            Full help text.


5. THE LOG FILE


Tab-separated, with a header row. One line per request actually
made and not one line per hyperlink parsed. URLs that robots.txt
put off-limits were never requested and so do not appear and they
are counted in the statistics block. Requests for robots.txt
itself are not logged as rows either, and are counted in the
block.

Columns:

  time         when the request finished, ISO 8601, milliseconds
  url          the URL requested
  status       HTTP status and 0 means no response arrived at all
  bytes        response body size
  secs         how long the fetch took
  page_prio    the page priority score, as it was at the moment
               this URL was chosen
  domain_prio  the domain priority score at that same moment
  depth        link distance from the seeds
  note         anything unusual: a transport error, access
               denied, redirected to ..., already crawled,
               not html: ..., truncated, etc
  parent       the page this URL was found on and is empty for a seed

After the last request line comes the statistics block. Every one
of its lines begins with "# " and is "# label<TAB>value". It reports pages
crawled, requests made, robots.txt files fetched, total bytes,
total seconds, pages and requests per second, hosts and
superdomains reached, the counts of 404, 403 and 401, rate-limit
responses, transport failures, URLs blocked by robots.txt,
backoffs, thread-seconds spent fetching and parsing (summed
across threads, so they can exceed the wall-clock total), the
state of the frontier at exit, and the parameters the run was
started with, so each log describes its own run. A full
status-code breakdown follows, one line per code.


6. LIBRARIES AND OUTSIDE RESOURCES


Third-party, from PyPI:

  click 8.5.0        command-line parsing and stderr output
  tldextract 5.3.2   public suffix list, used to find the
                     registrable domain of a host. 
  pytest 9.1.1       test runner and not needed to run the crawler

Note on tldextract: on its first use it downloads the public
suffix list from publicsuffix.org and caches it under
~/.cache/python-tldextract. If that fetch fails it falls back to
the snapshot bundled inside the library. So the very first run
touches the network before the crawl starts and later runs do not.

Standard library only for everything else, in particular
urllib.request for downloading, urllib.robotparser for robots
exclusion, urllib.parse for URL handling, html.parser for
parsing, heapq for the frontier, and concurrent.futures for the
thread pool.

No crawling framework is used. 
