#!/usr/bin/env python3
"""Stamp shared partials into every page.

The site has no build step and no runtime templating, so the nav and footer
are literally copied into each page. This script is the single place that
copy comes from. Each page wraps the shared blocks in marker comments:

    <!-- include:nav -->
    ...anything; replaced on render...
    <!-- /include:nav -->

`render.py` rewrites the inside of every marker pair from `_includes/<name>.html`.
For `nav`, the link matching the current page gets `class="active"` and
`aria-current="page"` (donate activates the WinRed link).

Usage, from the site root:
    python3 _includes/render.py          # rewrite pages in place
    python3 _includes/render.py --check  # exit 1 if any page is out of date

Idempotent: running it twice changes nothing.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDES = ROOT / "_includes"
MARKER = re.compile(
    r"(?P<open><!-- include:(?P<name>[a-z]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<close>\n[ \t]*<!-- /include:(?P=name) -->)",
    re.DOTALL,
)
# Pages whose nav link should read as "you are here". Keyed by file stem.
# index has no nav link to itself; 404 and privacy have none either.
ACTIVE_HREF = {
    "about": "/about",
    "vote": "/vote",
    "endorsements": "/endorsements",
    "support": "/support",
    "donate": "https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150",
}


def mark_active(nav: str, href: str) -> str:
    """Add active + aria-current to the <a> whose href is `href`."""
    tag_re = re.compile(r'<a href="%s"[^>]*>' % re.escape(href), re.DOTALL)
    m = tag_re.search(nav)
    if not m:
        raise SystemExit(f"nav.html has no link with href={href!r}")
    tag = m.group(0)
    if 'class="' in tag:
        new = re.sub(r'class="([^"]*)"', r'class="\1 active" aria-current="page"', tag, count=1)
    else:
        new = tag.replace(f'href="{href}"', f'href="{href}" class="active" aria-current="page"', 1)
    return nav[: m.start()] + new + nav[m.end() :]


def partial(name: str, page_stem: str) -> str:
    text = (INCLUDES / f"{name}.html").read_text(encoding="utf-8").rstrip("\n")
    if name == "nav" and page_stem in ACTIVE_HREF:
        text = mark_active(text, ACTIVE_HREF[page_stem])
    return text


def render(page: Path) -> str:
    src = page.read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        return m.group("open") + partial(m.group("name"), page.stem) + m.group("close")

    return MARKER.sub(sub, src)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for page in sorted(ROOT.glob("*.html")):
        before = page.read_text(encoding="utf-8")
        after = render(page)
        if after != before:
            stale.append(page.name)
            if not check:
                page.write_text(after, encoding="utf-8")
    if check and stale:
        print("out of date: " + ", ".join(stale))
        return 1
    print(("stale: " if check else "rendered: ") + (", ".join(stale) or "nothing to do"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
