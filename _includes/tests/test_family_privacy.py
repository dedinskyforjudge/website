"""Keep identifying family names and the old photo filename out of public files."""

from html import unescape
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[2]
NAMES = re.compile(r"\b(?:Charlie|Nicholas|Nick)\b", re.IGNORECASE)
PHOTO_STEM = "Dedinsky family (Paul, Lisa, Natalia, Charlie, Abby)"
# The packet freezes S12/S20 rosters. This unrelated endorser is the only
# exception: one exact name element in endorsements.html, never metadata,
# another page, or another occurrence. The roster tests pin its membership.
ROSTER_ENTRY = '<div class="endorser-name">Nick Ollinger</div>'


def decoded(source: str) -> str:
    for _ in range(2):
        source = unescape(unquote(source))
        source = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), source)
    return source


def public_files(root: Path):
    for directory, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in {"__pycache__", ".pytest_cache"}]
        if Path(directory) == root:
            # /_includes/* is forcibly 404 by _redirects; repository metadata
            # is not deployment content. Keep .well-known and unlinked files.
            folders[:] = [name for name in folders if name not in {".git", "_includes"}]
        for name in sorted(files):
            if Path(directory) == root and name == ".git":
                continue
            yield Path(directory) / name


def identifying_files(root: Path, *, allow_roster: bool = True) -> list[str]:
    problems = []
    for path in public_files(root):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        source = decoded(data.decode("utf-8", errors="replace"))
        if allow_roster and relative == "endorsements.html":
            source = source.replace(ROSTER_ENTRY, "", 1)
        if path.suffix.lower() == ".pdf":
            # PDF streams may compress strings that a byte scan cannot see.
            result = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, check=True)
            source += "\n" + decoded(result.stdout.decode("utf-8"))
        combined = decoded(relative) + "\n" + source
        matches = sorted({match.group().lower() for match in NAMES.finditer(combined)})
        if matches or PHOTO_STEM.casefold() in combined.casefold():
            problems.append(f"{relative}: identifying name or photo filename ({', '.join(matches)})")
    return problems


def test_public_files_have_no_identifying_family_names_or_old_photo_filename() -> None:
    assert identifying_files(ROOT) == []


def test_literal_name_scan_has_only_the_frozen_unrelated_endorser() -> None:
    assert identifying_files(ROOT, allow_roster=False) == [
        "endorsements.html: identifying name or photo filename (nick)"
    ]


@pytest.mark.parametrize("source", [
    "CHARLIE", "nicholas", "Nick", "cHaRlIe", "Charlie Dedinsky",
    "Nicholas Dedinsky", "Nick Dedinsky", "Charlie%20Dedinsky",
    "Char&#108;ie", r"Char\u006cie", PHOTO_STEM + ".jpeg",
    "/img/Dedinsky%20family%20%28Paul%2C%20Lisa%2C%20Natalia%2C%20Charlie%2C%20Abby%29.webp",
])
def test_guard_rejects_names_in_unlinked_served_content(tmp_path: Path, source: str) -> None:
    (tmp_path / "orphan.txt").write_text(source)
    assert identifying_files(tmp_path)


def test_guard_checks_filenames_and_well_known_files(tmp_path: Path) -> None:
    (tmp_path / "Nicholas.jpg").write_bytes(b"image")
    (tmp_path / ".well-known").mkdir()
    (tmp_path / ".well-known/security.txt").write_text("Contact: Charlie")
    assert len(identifying_files(tmp_path)) == 2


def test_roster_exception_is_limited_to_one_exact_element(tmp_path: Path) -> None:
    page = tmp_path / "endorsements.html"
    page.write_text(ROSTER_ENTRY)
    assert identifying_files(tmp_path) == []
    page.write_text(ROSTER_ENTRY * 2)
    assert identifying_files(tmp_path)
    page.write_text('<meta name="description" content="Nick Ollinger">')
    assert identifying_files(tmp_path)
    page.write_text(ROSTER_ENTRY)
    (tmp_path / "about.html").write_text(ROSTER_ENTRY)
    assert identifying_files(tmp_path)


def test_names_are_word_matches(tmp_path: Path) -> None:
    (tmp_path / "copy.txt").write_text("nickname nickel Nicholasville")
    assert identifying_files(tmp_path) == []


def test_guard_allows_restored_family_photo_and_generic_alt(tmp_path: Path) -> None:
    (tmp_path / "img").mkdir()
    for name in ("dedinsky-family.jpg", "dedinsky-family.webp"):
        (tmp_path / "img" / name).write_bytes((ROOT / "img" / name).read_bytes())
    (tmp_path / "about.html").write_text(
        '<picture><source srcset="/img/dedinsky-family.webp" type="image/webp">'
        '<img src="/img/dedinsky-family.jpg" alt="Paul and Lisa Dedinsky with their family">'
        '</picture>'
    )
    assert identifying_files(tmp_path) == []
