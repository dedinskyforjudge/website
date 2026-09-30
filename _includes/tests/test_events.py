"""Guards for unlisted event landing pages (SPEC S13)."""
from __future__ import annotations

import html
import re
import urllib.parse
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVENTS = sorted((ROOT / "events").glob("*.html"))
ATTRIBUTION = "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer"


def meta(text: str, attr: str, name: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', text)
    return match.group(1) if match else None


def jpeg_size(path: Path) -> tuple[int, int]:
    """Width and height from the first SOF marker of a JPEG."""
    data = path.read_bytes()
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise ValueError(f"no SOF marker in {path}")


def test_event_pages_exist() -> None:
    assert EVENTS, "events/ has no pages"


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_page_is_unlisted(page: Path) -> None:
    """S13.2: noindex, and nothing top-level, no partial, and no discovery file points at it."""
    text = page.read_text(encoding="utf-8")
    assert meta(text, "name", "robots") == "noindex"
    route = f"/events/{page.stem}"
    discovery = [ROOT / "sitemap.xml", ROOT / "llms.txt", ROOT / "_includes/nav.html", ROOT / "_includes/footer.html"]
    for other in discovery + sorted(ROOT.glob("*.html")):
        assert route not in other.read_text(encoding="utf-8"), f"{other.name} links {route}"


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_page_metadata_and_card(page: Path) -> None:
    """S13.2-S13.3: canonical and og:url are the event route; the card is its own 1200x630 image."""
    text = page.read_text(encoding="utf-8")
    url = f"https://dedinsky4judge.com/events/{page.stem}"
    assert f'<link rel="canonical" href="{url}">' in text
    assert meta(text, "property", "og:url") == url
    image = meta(text, "property", "og:image")
    assert image and image.startswith("https://dedinsky4judge.com/img/events/"), image
    assert meta(text, "name", "twitter:image") == image
    assert meta(text, "name", "twitter:card") == "summary_large_image"
    card = ROOT / image.removeprefix("https://dedinsky4judge.com/")
    assert jpeg_size(card) == (1200, 630)


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_page_attribution_home_link_and_no_ask(page: Path) -> None:
    """S13.1: the S2.4 attribution verbatim, a link home, and no donation ask (S2.5)."""
    text = page.read_text(encoding="utf-8")
    assert ATTRIBUTION in " ".join(re.sub(r"<[^>]+>", "", text).split())
    assert 'href="/"' in text
    assert "/donate" not in text and "winred" not in text.lower()


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_route_has_cache_rule(page: Path) -> None:
    """S13.4: the extensionless route revalidates on every load."""
    headers = (ROOT / "_headers").read_text(encoding="utf-8")
    rule = f"/events/{page.stem}\n  Cache-Control: public, max-age=0, must-revalidate\n"
    assert rule in headers


# S13.6 / S13.7, first event. The service list is the flyer's wording, kept whole.
LINCOLN = ROOT / "events" / "lincoln.html"
RSVP_ADDRESS = "lbadura@mac.com"
RSVP_SUBJECT = "RSVP: October 7, Lincoln Room"
SERVICE_LIST = [
    "25-year State Prosecutor with 30+ years of legal experience",
    "Appellate prosecutor handling homicide and sexual-assault matters",
    "Litigated 50+ jury trials and 100+ court trials",
    "Directed the DA's Domestic Violence Unit, 2001–2007",
    "Sexual Assault Prosecutor, 1999–2001; reviewed thousands of investigations",
    "Milwaukee County Circuit Court Judge, 2019–2020",
    "Chief Legal Counsel, Wisconsin DATCP, 2017–2018",
    "J.D., UW-Madison (1993); Ph.D., Education & Leadership (2012)",
    "Taught at Marquette Law and Cardinal Stritch; trained police statewide",
    "Author/editor, Wisconsin Domestic Violence Prosecution Manual; longtime civic, faith and legal-community service",
]


def plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_script_loads_only_from_js_files(page: Path) -> None:
    """S13.7: the CSP allows same-origin script files only, so no inline script."""
    text = page.read_text(encoding="utf-8")
    tags = re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.S)
    for attrs, body in tags:
        src = re.search(r'src="([^"]+)"', attrs)
        assert src and src.group(1).startswith("/js/") and not body.strip(), attrs
        assert (ROOT / src.group(1).lstrip("/")).is_file(), src.group(1)


def test_lincoln_rsvp_mail_links_carry_subject() -> None:
    """S13.7: every mail link to the RSVP contact names the event in its subject."""
    text = LINCOLN.read_text(encoding="utf-8")
    links = re.findall(r'(?:href|data-touch-href)="(mailto:[^"]+)"', text)
    assert len(links) >= 2, links
    for link in links:
        address, _, query = link.removeprefix("mailto:").partition("?")
        assert address == RSVP_ADDRESS, link
        assert urllib.parse.parse_qs(query).get("subject") == [RSVP_SUBJECT], link


def test_lincoln_rsvp_address_is_text() -> None:
    """S13.7: the address itself is on the page as text, for copying by hand."""
    text = LINCOLN.read_text(encoding="utf-8")
    match = re.search(r'<span id="addr">([^<]*)</span>', text)
    assert match and match.group(1) == RSVP_ADDRESS
    assert re.search(r'<button class="copy" type="button"', text)


def test_lincoln_full_record_keeps_the_flyer_list_closed() -> None:
    """S13.6: the full two-column list stays on the page, in a disclosure closed by default."""
    text = LINCOLN.read_text(encoding="utf-8")
    match = re.search(r'<details class="record"([^>]*)>(.*?)</details>', text, re.S)
    assert match, "no full-record disclosure"
    assert "open" not in match.group(1)
    items = [plain(item) for item in re.findall(r"<li>(.*?)</li>", match.group(2), re.S)]
    assert items == SERVICE_LIST


def test_lincoln_figures() -> None:
    """S13.6: three figures, the trials combined as on the home page (S12.4)."""
    text = LINCOLN.read_text(encoding="utf-8")
    figures = [plain(f) for f in re.findall(r'<div class="stat"><b>([^<]*)</b>', text)]
    assert figures == ["25", "30+", "150+"]


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_page_has_one_main_landmark(page: Path) -> None:
    """One <main> holds the page's content, so screen readers can jump to it."""
    text = page.read_text(encoding="utf-8")
    assert len(re.findall(r"<main\b", text)) == 1 and text.count("</main>") == 1


def visible_text(page: Path) -> str:
    """The page's body text as a reader sees it: tags, scripts and styles removed."""
    body = page.read_text(encoding="utf-8").split("<body", 1)[1]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


SOLICITATION = re.compile(r"(?i)\b(donat\w*|contribut\w*|chip in|give now|pledge\w*|fundrais\w*)\b|\$\s?\d")


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_page_has_no_solicitation_language(page: Path) -> None:
    """S2.5 via S13.1: no donation ask in the page's words, linked or not."""
    assert SOLICITATION.findall(visible_text(page)) == []


def test_lincoln_rsvp_script_is_loaded() -> None:
    """S13.7: the RSVP behavior needs its script; the page must load it, and it must decide by pointer."""
    text = LINCOLN.read_text(encoding="utf-8")
    assert text.count('<script src="/js/event-rsvp.js"></script>') == 1
    script = (ROOT / "js" / "event-rsvp.js").read_text(encoding="utf-8")
    assert "(hover: none) and (pointer: coarse)" in script
    assert "navigator.clipboard" in script and "selectNodeContents" in script


def test_lincoln_logistics_match_the_contract() -> None:
    """S13.5: the event's facts, exactly as the contract states them."""
    text = visible_text(LINCOLN)
    for fact in (
        "Wednesday, October 7, 2026",
        "5:30 – 8:00 PM",
        "Speaker remarks at 6:15 PM",
        "The Lincoln Room",
        "440 Wells St., Delafield, WI 53018",
        "Lauri McHugh Badura & Lou Kowieski",
        RSVP_ADDRESS,
    ):
        assert re.search(rf"(?<!\w){re.escape(fact)}(?!\w)", text), fact  # whole words: "Rooms" must not pass
