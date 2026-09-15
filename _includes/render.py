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
    python3 _includes/render.py --check  # exit 1 if stale or structurally invalid

Idempotent: running it twice changes nothing.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDES = ROOT / "_includes"
PARTIAL_NAMES = ("nav", "footer")
MARKER = re.compile(
    r"(?P<open><!-- include:(?P<name>[a-z]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<close>\n[ \t]*<!-- /include:(?P=name) -->)",
    re.DOTALL,
)
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
MARKER_COMMENT = re.compile(r"<!-- (?P<close>/?)include:(?P<name>[a-z]+) -->")
MARKER_LIKE = re.compile(r"include\s*:", re.IGNORECASE)
STRUCTURES = (
    ("skip link", '<a class="skip-link" href="#main">'),
    ("nav", '<nav class="site-nav">'),
    ("main#main", '<main id="main">'),
    ("footer", '<footer class="site-footer">'),
    ("js/nav.js script", '<script src="/js/nav.js"></script>'),
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
    match = tag_re.search(nav)
    if not match:
        raise ValueError(f"nav.html has no link with href={href!r}")
    tag = match.group(0)
    if 'class="' in tag:
        new = re.sub(r'class="([^"]*)"', r'class="\1 active" aria-current="page"', tag, count=1)
    else:
        new = tag.replace(f'href="{href}"', f'href="{href}" class="active" aria-current="page"', 1)
    return nav[: match.start()] + new + nav[match.end() :]


def partial(name: str, page_stem: str) -> str:
    """Read an allowed partial and apply page-specific navigation state."""
    if name not in PARTIAL_NAMES:
        raise ValueError(f"unknown partial {name!r}")
    text = (INCLUDES / f"{name}.html").read_text(encoding="utf-8").rstrip("\n")
    if name == "nav" and page_stem in ACTIVE_HREF:
        text = mark_active(text, ACTIVE_HREF[page_stem])
    return text


def validate_markers(source: str) -> list[str]:
    """Return every marker-grammar error in a page."""
    errors: list[str] = []
    tokens: list[tuple[bool, str]] = []
    for comment in COMMENT.findall(source):
        if not MARKER_LIKE.search(comment):
            continue
        match = MARKER_COMMENT.fullmatch(comment)
        if not match:
            errors.append(f"malformed marker {comment!r}")
            continue
        name = match.group("name")
        if name not in PARTIAL_NAMES:
            errors.append(f"unknown partial {name!r}")
            continue
        tokens.append((bool(match.group("close")), name))

    for name in PARTIAL_NAMES:
        openings = tokens.count((False, name))
        closings = tokens.count((True, name))
        if openings == 0:
            errors.append(f"missing opening marker for {name}")
        if closings == 0:
            errors.append(f"missing closing marker for {name}")
        if openings > 1 and closings > 1:
            errors.append(f"duplicate marker pair for {name}")
        elif openings > 1:
            errors.append(f"extra opening marker for {name}")
        elif closings > 1:
            errors.append(f"extra closing marker for {name}")

    stack: list[str] = []
    nested = False
    mismatched = False
    for closing, name in tokens:
        if not closing:
            if stack:
                nested = True
            stack.append(name)
        elif not stack or stack[-1] != name:
            mismatched = True
        else:
            stack.pop()
    if nested:
        errors.append("nested marker pairs")
    if mismatched:
        errors.append("closing marker without its matching opening marker")

    expected = [
        (False, "nav"),
        (True, "nav"),
        (False, "footer"),
        (True, "footer"),
    ]
    if tokens != expected and all(tokens.count(token) == 1 for token in expected):
        errors.append("marker pairs are out of order")
    return errors


def validate_structures(source: str) -> list[str]:
    """Return count and ordering errors for the shared page structures."""
    errors: list[str] = []
    positions: list[int] = []
    for label, token in STRUCTURES:
        count = source.count(token)
        if count != 1:
            errors.append(f"{label} must appear exactly once (found {count})")
        else:
            positions.append(source.index(token))
    if len(positions) == len(STRUCTURES) and positions != sorted(positions):
        errors.append("shared structures are out of order")
    return errors


def validate(source: str) -> list[str]:
    """Validate the complete marker and shared-structure grammar."""
    return validate_markers(source) + validate_structures(source)


def render(page: Path, source: str) -> str:
    """Return one page with its already-validated partial bodies replaced."""
    def sub(match: re.Match[str]) -> str:
        return match.group("open") + partial(match.group("name"), page.stem) + match.group("close")

    return MARKER.sub(sub, source)


def report_invalid(invalid: dict[str, list[str]]) -> None:
    """Print one page-named diagnostic for every invalid page."""
    for page_name in sorted(invalid):
        print(f"invalid: {page_name}: " + "; ".join(dict.fromkeys(invalid[page_name])))


def main(argv: list[str]) -> int:
    check = "--check" in argv
    pages = sorted(ROOT.glob("*.html"))
    sources = {page: page.read_text(encoding="utf-8") for page in pages}
    invalid = {page.name: errors for page, source in sources.items() if (errors := validate(source))}
    if invalid:
        report_invalid(invalid)
        return 1

    rendered: dict[Path, str] = {}
    for page, source in sources.items():
        try:
            rendered[page] = render(page, source)
        except (OSError, ValueError) as error:
            invalid[page.name] = [str(error)]
    for page, output in rendered.items():
        errors = validate(output)
        if errors:
            invalid[page.name] = [f"rendered output: {error}" for error in errors]
    if invalid:
        report_invalid(invalid)
        return 1

    stale = [page for page in pages if rendered[page] != sources[page]]
    if not check:
        for page in stale:
            page.write_text(rendered[page], encoding="utf-8")
    if check and stale:
        print("out of date: " + ", ".join(page.name for page in stale))
        return 1
    print(("stale: " if check else "rendered: ") + (", ".join(page.name for page in stale) or "nothing to do"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
