"""Guards for unlisted event landing pages (SPEC S13)."""
from __future__ import annotations

import re
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
    assert ATTRIBUTION in text
    assert 'href="/"' in text
    assert "/donate" not in text and "winred" not in text.lower()


@pytest.mark.parametrize("page", EVENTS, ids=lambda p: p.name)
def test_event_route_has_cache_rule(page: Path) -> None:
    """S13.4: the extensionless route revalidates on every load."""
    headers = (ROOT / "_headers").read_text(encoding="utf-8")
    rule = f"/events/{page.stem}\n  Cache-Control: public, max-age=0, must-revalidate\n"
    assert rule in headers
