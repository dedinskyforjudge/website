import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


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
