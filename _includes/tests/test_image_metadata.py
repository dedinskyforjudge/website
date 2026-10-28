"""Inspect image containers, including unlinked assets and PNG frames in ICOs.

Use structural parsing rather than string searches: metadata can be compressed,
and arbitrary strings can occur in compressed pixels. Unknown containers/chunks
fail closed so a new format needs an explicit review before it is served.
"""

import hashlib
import os
from pathlib import Path
import re
import struct
from xml.parsers import expat
import zlib

import pytest


ROOT = Path(__file__).resolve().parents[2]
PNG = b"\x89PNG\r\n\x1a\n"
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico",
              ".gif", ".tif", ".tiff", ".avif", ".heic", ".bmp", ".jxl"}
# Exact standard sRGB profiles, including their standard description/copyright.
# Changing even one byte requires review; a familiar profile name is not proof.
STANDARD_ICC = {
    # Artifex Software sRGB ICC Profile (Ghostscript srgb.icc).
    "eddaf344b5edea13269e0d20055f335610e5e0b6e33e6e536f2701bc18c5f7d5",
    # IEC61966-2.1 sRGB (TeX colorprofiles sRGB.icc, Graeme W. Gill).
    "049e05b4b9f85e2b65d43e1bed89e301d157f14ba8dadefe3a92e12814d763da",
}


def check_icc(data: bytes) -> list[str]:
    return [] if hashlib.sha256(data).hexdigest() in STANDARD_ICC else [
        "ICC profile is not a reviewed standard color profile"
    ]


def png_chunks(data: bytes):
    assert data.startswith(PNG), "invalid PNG signature"
    offset = 8
    while offset < len(data):
        assert offset + 12 <= len(data), "truncated PNG chunk"
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + size + 12
        assert end <= len(data), "truncated PNG payload"
        payload = data[offset + 8:end - 4]
        assert zlib.crc32(kind + payload) == int.from_bytes(data[end - 4:end], "big"), "invalid PNG CRC"
        yield kind, payload
        offset = end
        if kind == b"IEND":
            assert offset == len(data), "data after PNG IEND"
            return
    raise AssertionError("missing PNG IEND")


def png_metadata(data: bytes) -> list[str]:
    problems = []
    # Preserve color, transparency, density, background and animation controls.
    rendering = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM",
                 b"gAMA", b"sRGB", b"bKGD", b"pHYs", b"sBIT", b"acTL",
                 b"fcTL", b"fdAT", b"cICP", b"mDCV", b"cLLI"}
    for kind, payload in png_chunks(data):
        if kind == b"iCCP":
            name, packed = payload.split(b"\0", 1)
            if name not in {b"sRGB", b"ICC Profile", b"icc", b"icc_profile"}:
                problems.append("PNG nonstandard ICC profile name")
            assert packed[:1] == b"\0", "invalid ICC compression method"
            problems += check_icc(zlib.decompress(packed[1:]))
        elif kind not in rendering:
            problems.append(f"PNG {kind.decode('ascii')} metadata/unknown chunk")
    return problems


def jpeg_metadata(data: bytes) -> list[str]:
    problems, profiles = [], []
    offset = 2
    while offset < len(data):
        assert data[offset] == 255, "invalid JPEG marker"
        while data[offset] == 255:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            assert offset == len(data), "data after JPEG EOI"
            if profiles:
                profiles.sort()
                count = profiles[0][1]
                assert len(profiles) == count and [p[0] for p in profiles] == list(range(1, count + 1)), "invalid ICC sequence"
                assert all(p[1] == count for p in profiles), "inconsistent ICC sequence"
                problems += check_icc(b"".join(p[2] for p in profiles))
            return problems
        size = int.from_bytes(data[offset:offset + 2], "big")
        assert size >= 2 and offset + size <= len(data), "invalid JPEG segment length"
        payload = data[offset + 2:offset + size]
        offset += size
        if marker == 0xE0 and payload.startswith(b"JFIF\0"):
            # JFIF density/version are structural; embedded thumbnails are not.
            if len(payload) != 14 or payload[12:14] != b"\0\0":
                problems.append("JPEG JFIF thumbnail/extra data")
        elif marker == 0xE2 and payload.startswith(b"ICC_PROFILE\0"):
            profiles.append((payload[12], payload[13], payload[14:]))
        elif marker == 0xEE and payload.startswith(b"Adobe") and len(payload) == 12:
            pass  # Adobe color-transform marker affects decoding.
        elif marker == 0xFE or 0xE0 <= marker <= 0xEF:
            problems.append(f"JPEG {'COM' if marker == 0xFE else 'APP' + str(marker - 0xE0)} metadata")
        if marker == 0xDA:
            # Scan through entropy-coded bytes, including progressive scans.
            # Stuffed FF00 and restart markers are image data, not segments.
            while offset < len(data):
                offset = data.index(b"\xff", offset)
                start = offset
                while data[offset] == 255:
                    offset += 1
                if data[offset] == 0 or 0xD0 <= data[offset] <= 0xD7:
                    offset += 1
                else:
                    offset = start
                    break
    raise AssertionError("missing JPEG EOI")


def webp_metadata(data: bytes) -> list[str]:
    assert data[8:12] == b"WEBP", "invalid WebP signature"
    assert int.from_bytes(data[4:8], "little") + 8 == len(data), "invalid RIFF length"
    problems = []
    offset = 12
    while offset < len(data):
        assert offset + 8 <= len(data), "truncated WebP chunk"
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + size
        assert end + size % 2 <= len(data), "truncated WebP payload"
        payload = data[offset + 8:end]
        if kind == b"ICCP":
            problems += check_icc(payload)
        elif kind == b"VP8X":
            assert len(payload) == 10, "invalid VP8X"
            if payload[0] & 0x0C:
                problems.append("WebP EXIF/XMP feature flags")
        elif kind not in {b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF"}:
            problems.append(f"WebP {kind.decode('ascii')} metadata/unknown chunk")
        offset = end + size % 2
    return problems


def svg_metadata(data: bytes) -> list[str]:
    problems = []
    parser = expat.ParserCreate(namespace_separator="|")

    def element(name, attrs):
        if name.rsplit("|", 1)[-1].lower() == "metadata":
            problems.append("SVG metadata element")
        for key in (name, *attrs):
            if "|" in key and key.rsplit("|", 1)[0] not in {
                "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink",
                "http://www.w3.org/XML/1998/namespace",
            }:
                problems.append("SVG foreign/editor metadata")

    parser.StartElementHandler = element
    parser.CommentHandler = lambda value: problems.append("SVG comment")
    parser.ProcessingInstructionHandler = lambda *args: problems.append("SVG processing instruction")
    parser.StartDoctypeDeclHandler = lambda *args: problems.append("SVG doctype/entity declarations")
    parser.Parse(data, True)
    return problems


def image_metadata(data: bytes, suffix: str) -> list[str]:
    if data.startswith(PNG):
        return png_metadata(data)
    if data.startswith(b"\xff\xd8"):
        return jpeg_metadata(data)
    if data.startswith(b"RIFF"):
        return webp_metadata(data)
    if data.startswith(b"\0\0\1\0"):
        count = int.from_bytes(data[4:6], "little")
        assert count and 6 + count * 16 <= len(data), "invalid ICO directory"
        problems, ranges = [], []
        for index in range(count):
            size, offset = struct.unpack_from("<II", data, 6 + index * 16 + 8)
            assert offset >= 6 + count * 16 and offset + size <= len(data), "invalid ICO frame"
            frame = data[offset:offset + size]
            assert frame.startswith(PNG), "ICO frame requires PNG metadata review"
            problems += png_metadata(frame)
            ranges.append((offset, offset + size))
        cursor = 6 + count * 16
        for start, end in sorted(set(ranges)):
            assert start == cursor, "extra/overlapping data in ICO"
            cursor = end
        assert cursor == len(data), "data after ICO frames"
        return problems
    if suffix == ".svg" or re.match(rb"\s*(?:<\?xml\b|<svg\b)", data):
        return svg_metadata(data)
    raise AssertionError(f"unsupported image format {suffix}: metadata review required")


def served_images(root: Path):
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders
                            if name not in {".git", "_includes", "__pycache__", ".pytest_cache"})
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() in EXTENSIONS:
                yield path
            else:
                # Also catch images saved without a recognized extension.
                with path.open("rb") as stream:
                    head = stream.read(256)
                if head.startswith((PNG, b"\xff\xd8", b"RIFF", b"GIF8", b"\0\0\1\0")) or re.match(rb"\s*(?:<\?xml[^>]*>\s*)?<svg\b", head):
                    yield path


def test_served_images_have_no_embedded_metadata() -> None:
    images = list(served_images(ROOT))
    assert images, "no served images found"
    problems = []
    for path in images:
        try:
            findings = image_metadata(path.read_bytes(), path.suffix.lower())
        except (AssertionError, ValueError, IndexError, struct.error, zlib.error, expat.ExpatError) as error:
            findings = [str(error)]
        problems.extend(f"{path.relative_to(ROOT)}: {finding}" for finding in findings)
    assert not problems, "\n".join(problems)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


@pytest.mark.parametrize("kind,payload", [
    (b"eXIf", b"Exif\0\0private"), (b"tEXt", b"Comment\0private"),
    (b"zTXt", b"Comment\0\0" + zlib.compress(b"private")),
    (b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0<xmp>private</xmp>"),
    (b"tIME", b"\x07\xea\x01\x01\0\0\0"),
    (b"iCCP", b"sRGB\0\0" + zlib.compress(b"sRGB with private text")),
])
def test_rejects_png_metadata_and_metadata_inside_ico(kind, payload) -> None:
    clean = (ROOT / "favicon.png").read_bytes()
    dirty = clean[:-12] + png_chunk(kind, payload) + clean[-12:]
    assert image_metadata(dirty, ".png")
    ico = b"\0\0\1\0\1\0" + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(dirty), 22) + dirty
    assert image_metadata(ico, ".ico")


@pytest.mark.parametrize("marker,payload", [
    (0xE1, b"Exif\0\0private"), (0xE1, b"http://ns.adobe.com/xap/1.0/\0private"),
    (0xED, b"Photoshop 3.0\0private"), (0xFE, b"private comment"),
    (0xE0, b"JFXX\0thumbnail"), (0xE2, b"ICC_PROFILE\0\1\1sRGB private"),
])
def test_rejects_jpeg_metadata_before_and_after_scans(marker, payload) -> None:
    clean = (ROOT / "img/Paul Dedinsky1.jpeg").read_bytes()
    segment = b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload
    assert image_metadata(clean[:2] + segment + clean[2:], ".jpeg")
    assert image_metadata(clean[:-2] + segment + clean[-2:], ".jpeg")


@pytest.mark.parametrize("kind", [b"EXIF", b"XMP ", b"ICCP"])
def test_rejects_webp_metadata(kind) -> None:
    clean = (ROOT / "img/dedinsky-family.webp").read_bytes()
    dirty = clean + kind + struct.pack("<I", 8) + b"private!"
    dirty = dirty[:4] + struct.pack("<I", len(dirty) - 8) + dirty[8:]
    assert image_metadata(dirty, ".webp")


@pytest.mark.parametrize("content", [
    "<!-- private -->", "<metadata>private</metadata>",
    '<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#">private</r:RDF>',
    '<?xpacket private?>',
])
def test_rejects_svg_metadata(content) -> None:
    assert image_metadata(f'<svg xmlns="http://www.w3.org/2000/svg">{content}</svg>'.encode(), ".svg")


def test_inventory_includes_unlinked_images_in_any_served_directory(tmp_path) -> None:
    nested = tmp_path / "new-event"
    nested.mkdir()
    (nested / "unlinked.PNG").write_bytes((ROOT / "favicon.png").read_bytes())
    (nested / "no-extension").write_bytes((ROOT / "favicon.png").read_bytes())
    (nested / "future.avif").write_bytes(b"unknown container")
    assert {p.name for p in served_images(tmp_path)} == {"unlinked.PNG", "no-extension", "future.avif"}
    with pytest.raises(AssertionError, match="unsupported image format"):
        image_metadata(b"unknown container", ".avif")


def test_rejects_data_appended_to_images() -> None:
    for name in ("favicon.png", "favicon.ico", "img/Paul Dedinsky1.jpeg", "img/dedinsky-family.webp"):
        with pytest.raises(AssertionError):
            image_metadata((ROOT / name).read_bytes() + b"private", Path(name).suffix)
