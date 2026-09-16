import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
PAGES = (
    "index.html",
    "about.html",
    "vote.html",
    "endorsements.html",
    "support.html",
    "donate.html",
    "privacy.html",
    "404.html",
)
ACTIVE_HREF = {
    "about.html": "/about",
    "vote.html": "/vote",
    "endorsements.html": "/endorsements",
    "support.html": "/support",
    "donate.html": "https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150",
}
MARKER_RE = re.compile(r"<!--\s*(/?)include:([A-Za-z]+)\s*-->")
ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.DOTALL)


def copy_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    includes = site / "_includes"
    includes.mkdir(parents=True)
    for name in PAGES:
        shutil.copy2(ROOT / name, site / name)
    for name in ("nav.html", "footer.html", "render.py"):
        shutil.copy2(ROOT / "_includes" / name, includes / name)
    shutil.copy2(ROOT / "_redirects", site / "_redirects")
    return site


def run_renderer(site: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "_includes/render.py", *args],
        cwd=site,
        capture_output=True,
        text=True,
    )


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, (path.name, old)
    path.write_text(source.replace(old, new), encoding="utf-8")


def assert_clean_check() -> None:
    result = run_renderer(ROOT, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "stale: nothing to do"


def assert_rejected(
    tmp_path: Path,
    page_name: str,
    mutate: Callable[[Path], None],
    reason: str,
) -> None:
    assert_clean_check()
    site = copy_site(tmp_path)
    page = site / page_name
    mutate(page)
    result = run_renderer(site, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"invalid: {page_name}:" in result.stdout
    assert reason in result.stdout
    assert "Traceback" not in result.stderr


def test_check_clean_exit_zero() -> None:
    assert_clean_check()


def test_check_stale_exit_one_and_names_every_page(tmp_path: Path) -> None:
    site = copy_site(tmp_path)
    nav = site / "_includes/nav.html"
    replace_once(nav, ">About<", ">About!<")

    result = run_renderer(site, "--check")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("out of date: ")
    reported = result.stdout.strip().removeprefix("out of date: ").split(", ")
    assert reported == sorted(PAGES)


def test_write_mode_then_check_and_second_write_are_stable(tmp_path: Path) -> None:
    site = copy_site(tmp_path)
    footer = site / "_includes/footer.html"
    replace_once(footer, "All rights reserved.", "All rights reserved!")

    first = run_renderer(site)
    first_bytes = {name: (site / name).read_bytes() for name in PAGES}
    check = run_renderer(site, "--check")
    second = run_renderer(site)
    second_bytes = {name: (site / name).read_bytes() for name in PAGES}

    assert first.returncode == 0, first.stdout + first.stderr
    assert check.returncode == 0, check.stdout + check.stderr
    assert check.stdout.strip() == "stale: nothing to do"
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.strip() == "rendered: nothing to do"
    assert second_bytes == first_bytes


def test_active_link_mapping_per_page(tmp_path: Path) -> None:
    site = copy_site(tmp_path)
    result = run_renderer(site)
    assert result.returncode == 0, result.stdout + result.stderr

    for name in PAGES:
        text = (site / name).read_text(encoding="utf-8")
        active = [tag for tag in ANCHOR_RE.findall(text) if 'aria-current="page"' in tag]
        expected = ACTIVE_HREF.get(name)
        if expected is None:
            assert active == [], name
            continue
        assert len(active) == 1, name
        assert f'href="{expected}"' in active[0], name
        class_match = re.search(r'class="([^"]*)"', active[0])
        assert class_match and "active" in class_match.group(1).split(), name


def test_marker_pairs_exactly_once_on_all_eight_top_level_pages() -> None:
    pages = sorted(ROOT.glob("*.html"))
    assert [page.name for page in pages] == sorted(PAGES)
    expected = [("", "nav"), ("/", "nav"), ("", "footer"), ("/", "footer")]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert MARKER_RE.findall(text) == expected, page.name
        assert text.count("<!-- include:nav -->") == 1, page.name
        assert text.count("<!-- /include:nav -->") == 1, page.name
        assert text.count("<!-- include:footer -->") == 1, page.name
        assert text.count("<!-- /include:footer -->") == 1, page.name


def test_shared_structure_count_and_order_on_all_pages() -> None:
    tokens = (
        '<a class="skip-link" href="#main">',
        '<nav class="site-nav">',
        '<main id="main">',
        '<footer class="site-footer">',
        '<script src="/js/nav.js"></script>',
    )
    for name in PAGES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert all(text.count(token) == 1 for token in tokens), name
        assert [text.index(token) for token in tokens] == sorted(text.index(token) for token in tokens), name


def test_redirects_blocks_includes() -> None:
    rules = [
        line.split()
        for line in (ROOT / "_redirects").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rules == [["/_includes/*", "/404.html", "404!"]]


def test_rejects_malformed_opening_marker(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "vote.html",
        lambda page: replace_once(page, "<!-- include:nav -->", "<!-- Include:nav -->"),
        "malformed marker",
    )


def test_rejects_missing_opening_marker(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(page, "  <!-- include:nav -->\n", ""),
        "missing opening marker for nav",
    )


def test_rejects_extra_nested_opening_marker(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(
            page,
            "  <!-- include:nav -->\n",
            "  <!-- include:nav -->\n  <!-- include:nav -->\n",
        ),
        "extra opening marker for nav",
    )


def test_rejects_duplicate_complete_nav_pair(tmp_path: Path) -> None:
    def duplicate(page: Path) -> None:
        source = page.read_text(encoding="utf-8")
        start = source.index("  <!-- include:nav -->")
        end = source.index("  <!-- /include:nav -->", start) + len("  <!-- /include:nav -->")
        block = source[start:end]
        page.write_text(source[:end] + "\n" + block + source[end:], encoding="utf-8")

    assert_rejected(tmp_path, "about.html", duplicate, "duplicate marker pair for nav")


def test_rejects_extra_closing_marker(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(
            page,
            "  <!-- /include:nav -->",
            "  <!-- /include:nav -->\n  <!-- /include:nav -->",
        ),
        "extra closing marker for nav",
    )


def test_rejects_missing_main_structure(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(page, '<main id="main">', '<main id="content">'),
        "main#main must appear exactly once (found 0)",
    )


def test_rejects_duplicate_main_structure(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(page, '<main id="main">', '<main id="main"><main id="main">'),
        "main#main must appear exactly once (found 2)",
    )


def test_rejects_misordered_duplicate_nav_script(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(
            page,
            '<main id="main">',
            '<script src="/js/nav.js"></script>\n  <main id="main">',
        ),
        "js/nav.js script must appear exactly once (found 2)",
    )


def test_rejects_unknown_partial_pair(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(
            page,
            "  <!-- /include:nav -->",
            "  <!-- /include:nav -->\n  <!-- include:ghost -->\n  placeholder\n  <!-- /include:ghost -->",
        ),
        "unknown partial 'ghost'",
    )


def test_rejects_nested_marker_pair(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "about.html",
        lambda page: replace_once(
            page,
            "  <!-- include:nav -->",
            "  <!-- include:nav -->\n  <!-- include:footer -->\n  <!-- /include:footer -->",
        ),
        "nested marker pairs",
    )


def test_rejects_missing_closing_marker(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "privacy.html",
        lambda page: replace_once(page, "  <!-- /include:footer -->\n", ""),
        "missing closing marker for footer",
    )


def test_rejects_marker_spacing(tmp_path: Path) -> None:
    assert_rejected(
        tmp_path,
        "index.html",
        lambda page: replace_once(page, "<!-- include:nav -->", "<!--  include:nav -->"),
        "malformed marker",
    )


def test_rejects_structures_in_wrong_order(tmp_path: Path) -> None:
    def move_script(page: Path) -> None:
        source = page.read_text(encoding="utf-8")
        script = '  <script src="/js/nav.js"></script>\n'
        assert source.count(script) == 1
        source = source.replace(script, "")
        source = source.replace('  <main id="main">', script + '  <main id="main">', 1)
        page.write_text(source, encoding="utf-8")

    assert_rejected(tmp_path, "donate.html", move_script, "shared structures are out of order")


def test_invalid_write_mode_reports_all_pages_without_writing(tmp_path: Path) -> None:
    site = copy_site(tmp_path)
    about = site / "about.html"
    vote = site / "vote.html"
    replace_once(about, "<!-- include:nav -->", "<!-- Include:nav -->")
    replace_once(vote, '<main id="main">', '<main id="content">')
    before = {name: (site / name).read_bytes() for name in PAGES}

    result = run_renderer(site)

    after = {name: (site / name).read_bytes() for name in PAGES}
    assert result.returncode == 1, result.stdout + result.stderr
    assert "invalid: about.html:" in result.stdout
    assert "invalid: vote.html:" in result.stdout
    assert after == before



def test_accepts_equivalent_nav_script_serialization(tmp_path):
    """S3.6 names the script, not its byte serialization: an equivalent tag
    with a neutral attribute before src must not be rejected."""
    import importlib.util, shutil
    site = tmp_path / "site"
    shutil.copytree(ROOT, site, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
    page = site / "about.html"
    src = page.read_text(encoding="utf-8")
    assert src.count('<script src="/js/nav.js"></script>') == 1
    page.write_text(src.replace('<script src="/js/nav.js"></script>', '<script defer src="/js/nav.js"></script>'), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("render_tmp", site / "_includes" / "render.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.validate_structures(page.read_text(encoding="utf-8")) == []


EXPECTED_ENDORSEMENT_TIERS = {
    "Statewide Officials": ("Scott Walker",),
    "Wisconsin Supreme Court": (
        "Hon. Annette Kingsland Ziegler",
        "Hon. Rebecca Grassl Bradley",
        "Hon. Daniel Kelly",
    ),
    "Wisconsin Court of Appeals, District II": (
        "Hon. Mark Gundrum",
        "Hon. Shelley A. Grogan",
        "Hon. Maria Lazar",
        "Hon. Anthony LoCoco",
    ),
    "Waukesha County Circuit Court": (
        "Hon. Michael Aprahamian",
        "Hon. Jennifer Dorow",
        "Hon. Cody Horlacher",
        "Hon. David Maas",
        "Hon. Michael Maxwell",
        "Hon. J. Arthur Melvin III",
        "Hon. Jack Pitzo",
        "Hon. Scott Wagner",
        "Hon. Zach Wittchow",
        "Hon. Michael Bohren",
        "Hon. Kathryn Foster",
    ),
    "Additional Wisconsin Jurists": (
        "Hon. T. Christopher Dee",
        "Hon. Robert Dehring",
        "Hon. Grant Scaife",
        "Hon. Randy R. Koschnick",
    ),
    "State Senators": ("Julian Bradley", "Steve Nass", "Rob Hutton"),
    "State Representatives": (
        "Barb Dittrich",
        "Adam Neylon",
        "Chuck Wichgers",
        "Scott Allen",
        "Dan Knodl",
        "Jim Piwowarczyk",
    ),
    "Local Officials": (
        "Eric Severson",
        "Lesli Boese",
        "Tim Aicher",
        "Matt Rosek",
        "Jeff Pfannerstill",
        "Steve Ponto",
        "Gary Mahkorn",
    ),
    "Organizations and Businesses": (
        "Milwaukee Police Association",
        "Waukesha County Young Republicans",
        "5 Riders Organization",
        "Hernandez Roofing",
    ),
}
EXPECTED_ENDORSEMENT_NOTES = {
    "Scott Walker": "(Former)",
    "Hon. Rebecca Grassl Bradley": "(Former)",
    "Hon. Daniel Kelly": "(Former)",
    "Hon. Michael Bohren": "(Retired)",
    "Hon. Kathryn Foster": "(Retired)",
    "Hon. Randy R. Koschnick": "(Former)",
}
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "source", "track", "wbr",
}


class HtmlElement:
    def __init__(self, tag: str, attributes=()):
        self.tag = tag
        self.attributes = dict(attributes)
        self.children: list[HtmlElement] = []
        self.text: list[str] = []


class TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlElement("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attributes) -> None:
        node = HtmlElement(tag, attributes)
        self.stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attributes) -> None:
        self.stack[-1].children.append(HtmlElement(tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        assert self.stack[-1].tag == tag, (self.stack[-1].tag, tag)
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].text.append(data)


def element_classes(node: HtmlElement) -> set[str]:
    return set(node.attributes.get("class", "").split())


def normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def element_text(node: HtmlElement) -> str:
    parts = list(node.text)
    for child in node.children:
        parts.append(element_text(child))
    return normalized_text(parts)


def children_with_class(node: HtmlElement, class_name: str) -> list[HtmlElement]:
    return [child for child in node.children if class_name in element_classes(child)]


def descendants_with_class(node: HtmlElement, class_name: str) -> list[HtmlElement]:
    found = []
    for child in node.children:
        if class_name in element_classes(child):
            found.append(child)
        found.extend(descendants_with_class(child, class_name))
    return found


def only(nodes: list[HtmlElement]) -> HtmlElement:
    assert len(nodes) == 1
    return nodes[0]


def endorsement_entries() -> list[tuple[str, str, str, str]]:
    parser = TreeParser()
    parser.feed((ROOT / "endorsements.html").read_text(encoding="utf-8"))
    entries = []
    for tier in descendants_with_class(parser.root, "endorser-tier"):
        heading = element_text(only(children_with_class(tier, "endorser-tier-header")))
        grid = only(children_with_class(tier, "endorser-grid"))
        for endorser in children_with_class(grid, "endorser"):
            name_node = only(children_with_class(endorser, "endorser-name"))
            title_nodes = children_with_class(endorser, "endorser-title")
            note_nodes = children_with_class(name_node, "endorser-note")
            assert len(title_nodes) <= 1
            assert len(note_nodes) <= 1
            name = normalized_text(name_node.text)
            title = normalized_text(title_nodes[0].text) if title_nodes else ""
            note = element_text(note_nodes[0]) if note_nodes else ""
            entries.append((name, title, note, heading))
    return entries


def test_endorsement_tier_membership_is_exact() -> None:
    actual = {}
    for name, _title, _note, tier in endorsement_entries():
        actual.setdefault(tier, []).append(name)
    assert {tier: tuple(names) for tier, names in actual.items()} == EXPECTED_ENDORSEMENT_TIERS


def test_scott_walker_entry_shape_is_exact() -> None:
    walker = [entry for entry in endorsement_entries() if entry[0] == "Scott Walker"]
    assert walker == [("Scott Walker", "Governor of Wisconsin", "(Former)", "Statewide Officials")]


def test_required_endorsement_name_set_and_count_are_exact() -> None:
    names = [entry[0] for entry in endorsement_entries()]
    expected = {name for tier in EXPECTED_ENDORSEMENT_TIERS.values() for name in tier}
    assert len(names) == 43
    assert len(names) == len(set(names))
    assert set(names) == expected


def test_endorsement_status_note_map_is_exact() -> None:
    actual = {name: note for name, _title, note, _tier in endorsement_entries() if note}
    assert actual == EXPECTED_ENDORSEMENT_NOTES


def test_endorsements_sitemap_lastmod_is_current() -> None:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap = ElementTree.parse(ROOT / "sitemap.xml")
    matches = []
    for route in sitemap.findall("sm:url", namespace):
        location = route.findtext("sm:loc", namespaces=namespace)
        if location == "https://dedinsky4judge.com/endorsements":
            matches.append(route.findtext("sm:lastmod", namespaces=namespace))
    assert matches == ["2026-09-15"]
