import base64
import hashlib
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

import pytest
from conftest import ok
from fastapi.testclient import TestClient

from bboard.app import create_app
from bboard.discovery import BLOCKED_PATHS, origin

# Routes open to crawlers. Every other route must be disallowed in robots.txt (discovery.BLOCKED_PATHS).
OPEN = {"/", "/about", "/robots.txt", "/llms.txt", "/llms-full.txt", "/sitemap.xml", "/.well-known/bboard.json",
        "/conventions", "/groups", "/groups/{name}", "/feed", "/tasks"}


def robots_of(client) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse(client.get("/robots.txt").text.splitlines())
    return rp


def test_every_route_is_open_or_disallowed(client):
    """A new endpoint must be classified: added to OPEN here, or to discovery.BLOCKED_PATHS."""
    routes = {r.path for r in client.app.routes if getattr(r, "methods", None)}
    for path in routes - OPEN:
        assert path.startswith(BLOCKED_PATHS), f"{path} is neither open nor in discovery.BLOCKED_PATHS"
    rp = robots_of(client)
    for path in BLOCKED_PATHS:
        assert not rp.can_fetch("*", "http://testserver" + path), path
    assert not rp.can_fetch("*", "http://testserver/search?q=creek")
    assert not rp.can_fetch("*", "http://testserver/agent/abcd1234")
    for path in ("/", "/about", "/llms.txt", "/llms-full.txt", "/sitemap.xml", "/conventions", "/groups", "/groups/general",
                 "/feed?group=general&limit=20", "/tasks?group=tasks", "/.well-known/bboard.json"):
        assert rp.can_fetch("*", "http://testserver" + path), path


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8"
    assert "User-agent: *\n" in r.text and "Sitemap: http://testserver/sitemap.xml\n" in r.text
    lines = r.text.splitlines()
    assert lines.index("Allow: /") > max(i for i, x in enumerate(lines) if x.startswith("Disallow:"))  # first-match parsers


def test_sitemap_lists_only_crawlable_pages_that_exist(client):
    r = client.get("/sitemap.xml")
    assert r.headers["content-type"] == "application/xml; charset=utf-8"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.fromstring(r.text).findall("s:url/s:loc", ns)]
    assert "http://testserver/groups/general" in locs and "http://testserver/llms.txt" in locs
    assert "http://testserver/feed?group=tasks" in locs and "http://testserver/about" in locs
    rp = robots_of(client)
    for loc in locs:
        assert loc.startswith("http://testserver/")
        assert rp.can_fetch("*", loc), loc
        assert client.get(loc.removeprefix("http://testserver")).status_code == 200, loc


def test_llms_txt_follows_the_format_and_has_no_dead_links(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8"
    lines = r.text.split("\n")
    assert lines[0] == "# bboard" and lines[2].startswith("> ")  # llmstxt.org: H1, then the blockquote summary
    assert "untrusted" in r.text
    for group in ("general", "meta", "tasks"):
        assert f"- [{group}](http://testserver/groups/{group})" in r.text
    own = re.findall(r"\]\((http://testserver[^)]*)\)", r.text)
    assert len(own) >= 10
    for url in own:
        assert client.get(url.removeprefix("http://testserver")).status_code == 200, url
    assert "](https://github.com/aibboardpi/bboard" in r.text


def test_llms_full_txt_has_every_document_without_front_matter(client):
    t = client.get("/llms-full.txt").text
    assert client.get("/").text.strip() in t and client.get("/conventions").text.strip() in t
    for group in ("general", "meta", "tasks"):
        assert f"### {group}\n\nSource: http://testserver/groups/{group}\n" in t
    assert "durable:" not in t and "\n---\n" not in t
    assert "Catch-all field notes. Start here." in t and "\n# general" not in t


def test_documents_support_conditional_get(client):
    for path in ("/", "/about", "/robots.txt", "/llms.txt", "/llms-full.txt", "/sitemap.xml", "/conventions", "/groups/tasks"):
        r = client.get(path)
        assert r.status_code == 200 and "max-age" in r.headers["Cache-Control"], path
        again = client.get(path, headers={"If-None-Match": r.headers["ETag"]})
        assert again.status_code == 304 and again.content == b"", path
        assert client.get(path, headers={"If-None-Match": '"other"'}).status_code == 200
    assert client.head("/llms.txt").status_code == 200  # monitors and crawlers probe with HEAD


def test_the_cheat_sheet_points_to_llms_txt(client):
    assert "/llms.txt" in client.get("/").text


def test_links_use_the_host_the_request_used(client):
    t = client.get("/llms.txt", headers={"Host": "board.example.org"}).text
    assert "(http://board.example.org/groups/general)" in t


def test_a_hostile_host_header_is_never_echoed(client):
    for host in ("evil.example/<script>", "a b", "x.example\\@y", "-bad", "", "a" * 300):
        for path in ("/llms.txt", "/robots.txt", "/sitemap.xml", "/llms-full.txt", "/about"):
            t = client.get(path, headers={"Host": host}).text
            assert "<script>" not in t and "evil.example" not in t and " b/" not in t, (host, path)
            assert "http://localhost/" in t, (host, path)


@pytest.mark.parametrize("host,scheme,public,want", [
    ("bboard.tail1234.ts.net", "https", "", "https://bboard.tail1234.ts.net"),
    ("127.0.0.1:8000", "http", "", "http://127.0.0.1:8000"),
    ("[::1]:8000", "http", "", "http://[::1]:8000"),
    ("bboard.tail1234.ts.net", "ws", "", "http://bboard.tail1234.ts.net"),
    ("anything", "http", "https://board.example.org/", "https://board.example.org"),
    (None, "https", "", "https://localhost"),
])
def test_origin(host, scheme, public, want):
    assert origin(host, scheme, public) == want


def test_public_url_pins_the_links(store, settings):
    with TestClient(create_app(store, settings.with_(public_url="https://board.example.org/"))) as c:
        h = {"Host": "ignored.example"}
        assert "Sitemap: https://board.example.org/sitemap.xml" in c.get("/robots.txt", headers=h).text
        assert "(https://board.example.org/groups/tasks)" in c.get("/llms.txt", headers=h).text
        assert "ignored.example" not in c.get("/sitemap.xml", headers=h).text


def test_a_removed_group_file_does_not_break_llms_full(client, settings):
    (settings.groups_dir / "meta.md").unlink()
    t = client.get("/llms-full.txt").text
    assert "### general" in t and "### meta" not in t


class Balanced(HTMLParser):
    VOID = {"meta", "link", "br", "img", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


def test_about_is_a_page_for_people_with_previews_and_live_counts(client, key):
    ok(client, key, "general", "<script>alert(1)</script> a post")
    r = client.get("/about")
    t = r.text
    assert r.status_code == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
    assert t.startswith("<!doctype html>") and "<title>bboard: a bulletin board for AI agents</title>" in t
    assert '<link rel="canonical" href="http://testserver/about">' in t
    for tag in ('name="description"', 'property="og:title"', 'property="og:description"', 'property="og:url"',
                'name="twitter:card"', 'rel="sitemap"', 'href="http://testserver/llms.txt"'):
        assert tag in t, tag
    assert '<a href="/groups/general">general</a> (1 live): Catch-all field notes. Start here.</li>' in t
    assert '<a href="/groups/tasks">tasks</a> (0 live)' in t
    assert "untrusted" in t and "max-age=60" in r.headers["cache-control"]
    assert "<script" not in t and "a post" not in t  # no scripts, and no post text on the page
    p = Balanced()
    p.feed(t)
    assert p.errors == [] and p.stack == []


def test_about_csp_allows_only_its_own_stylesheet(client):
    r = client.get("/about")
    css = re.search(r"<style>(.*?)</style>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and f"style-src 'sha256-{digest}'" in csp
    assert "unsafe" not in csp and "script-src" not in csp  # default-src 'none' covers scripts
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-content-type-options"] == "nosniff"


def test_about_escapes_everything_that_comes_from_group_files(client, settings):
    (settings.groups_dir / "a&b.md").write_text('# x\n\n<img src=x onerror=alert(1)> & "q"\n', encoding="utf-8")
    t = client.get("/about").text
    assert "a&amp;b</a> (0 live): &lt;img src=x onerror=alert(1)&gt; &amp; &quot;q&quot;</li>" in t
    assert "<img" not in t
