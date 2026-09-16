import json
import base64
from collections import Counter
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
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


def css_declarations(source: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", source)
    assert match, selector
    return {
        name.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


def relative_luminance(hex_color: str) -> float:
    match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", hex_color)
    assert match, hex_color
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(channel * 2 for channel in digits)
    channels = [int(digits[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class SupportFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


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


def test_p6b_v01_focused_skip_link_stacks_above_fixed_navigation(tmp_path: Path) -> None:
    source = (ROOT / "css/style.css").read_text(encoding="utf-8")
    skip_z = int(css_declarations(source, ".skip-link")["z-index"])
    nav_z = int(css_declarations(source, ".site-nav")["z-index"])
    assert skip_z > nav_z

    nav_height = int(css_declarations(source, ":root")["--nav-height-tall"].removesuffix("px"))
    assert nav_height > 0
    main_margin = css_declarations(source, "main")["scroll-margin-top"]
    extra = int(re.search(r"\+\s*(\d+)px", main_margin).group(1))
    assert main_margin.startswith("calc(var(--nav-height-tall)")
    assert extra > 0
    focus_offset = int(css_declarations(source, "main:focus")["top"].removesuffix("px"))
    assert focus_offset > 0

    for name in PAGES:
        parser = SupportFormParser()
        parser.feed((ROOT / name).read_text(encoding="utf-8"))
        skip_links = [attrs for tag, attrs in parser.elements if tag == "a" and attrs.get("class") == "skip-link"]
        mains = [attrs for tag, attrs in parser.elements if tag == "main" and attrs.get("id") == "main"]
        assert skip_links == [{"class": "skip-link", "href": "#main"}], name
        assert len(mains) == 1, name

    navigation = run_navigation_harness()
    assert navigation == {
        "defaultPrevented": True,
        "focused": "main",
        "scrolled": "main",
        "hash": "#main",
        "tabindex": "-1",
        "navState": "compact",
    }
    measurements = run_browser_skip_geometry(
        tmp_path,
        routes=(("index.html", ".hero"),),
        widths=(1280,),
        states=("compact",),
        paths=("focused-skip",),
    )
    assert len(measurements) == 1
    measurement = measurements[0]
    assert measurement["targetTop"] - measurement["navBottom"] >= 1, measurement


def test_p6b_f03_browser_geometry_keeps_fragment_and_focused_skip_paths_distinct(tmp_path: Path) -> None:
    measurements = run_browser_skip_geometry(tmp_path)
    assert len(measurements) == 48
    for measurement in measurements:
        assert measurement["viewportWidth"] == measurement["width"], measurement
        if measurement["path"] == "fragment":
            assert measurement["hash"] == "#main", measurement
        else:
            assert measurement["focused"] == "MAIN", measurement
        # 1px is the minimum baseline observed in P6b-fix3/clearance.tsv.
        assert measurement["targetTop"] - measurement["navBottom"] >= 1, measurement
        assert measurement["layoutPresent"] is True, measurement
        expected_focus = "BODY" if measurement["path"] == "fragment" else "MAIN"
        assert measurement["focused"] == expected_focus, measurement
        # The compact nav state is a measured condition, not a label: a state the
        # browser never entered would otherwise report as covered.
        assert measurement["compactBefore"] is (measurement["state"] == "compact"), measurement
        # Clicking the skip link scrolls, so the nav ends compact whatever it started as;
        # fragment navigation does not scroll and leaves the starting state alone.
        expected_compact = True if measurement["path"] == "focused-skip" else measurement["state"] == "compact"
        assert measurement["compactAtMeasure"] is expected_compact, measurement


def run_browser_skip_geometry(
    tmp_path: Path,
    routes: tuple[tuple[str, str], ...] = (
        ("index.html", ".hero"),
        ("about.html", ".page-header"),
        ("404.html", ".error-section"),
    ),
    widths: tuple[int, ...] = (390, 412, 768, 1280),
    states: tuple[str, ...] = ("tall", "compact"),
    paths: tuple[str, ...] = ("fragment", "focused-skip"),
) -> list[dict[str, object]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SilentRequestHandler, directory=str(ROOT)))
    Thread(target=server.serve_forever, daemon=True).start()
    port = reserve_port()
    browser = subprocess.Popen(
        [
            "chromium",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path / 'chromium-profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = wait_for_browser_target(port, browser)
        with cdp_socket(target["webSocketDebuggerUrl"]) as connection:
            cdp(connection, "Page.enable")
            cdp(connection, "Runtime.enable")
            measurements = []
            activations = {
                "fragment": "location.hash = 'main';",
                "focused-skip": "const skip = document.querySelector('.skip-link[href=\"#main\"]'); skip.focus(); skip.click();",
            }
            for route, layout_class in routes:
                for width in widths:
                    cdp(connection, "Emulation.setDeviceMetricsOverride", {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
                    for state in states:
                        for path in paths:
                            navigate(connection, f"http://127.0.0.1:{server.server_port}/{route}")
                            cdp(connection, "Runtime.evaluate", {"expression": f"document.body.classList.toggle('is-scrolled', {json.dumps(state == 'compact')});"})
                            compact_before = cdp(connection, "Runtime.evaluate", {"expression": "document.body.classList.contains('is-scrolled')", "returnByValue": True})["result"]["value"]
                            measurement = evaluate_geometry(connection, activations[path], layout_class)
                            measurements.append({
                                "route": route,
                                "layoutClass": layout_class,
                                "width": width,
                                "state": state,
                                "path": path,
                                "compactBefore": compact_before,
                                **measurement,
                            })
            return measurements
    finally:
        browser.terminate()
        browser.wait(timeout=10)
        server.shutdown()
        server.server_close()


class SilentRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


def reserve_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_browser_target(port: int, browser: subprocess.Popen[bytes]) -> dict[str, str]:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    for _ in range(100):
        if browser.poll() is not None:
            raise AssertionError("chromium exited before opening its debugging endpoint")
        try:
            with urlopen(endpoint, timeout=0.1) as response:
                targets = json.load(response)
        except OSError:
            time.sleep(0.05)
            continue
        page = next((target for target in targets if target["type"] == "page"), None)
        if page:
            return page
    raise AssertionError("chromium did not open its debugging endpoint")


class cdp_socket:
    def __init__(self, url: str) -> None:
        match = re.fullmatch(r"ws://([^:/]+):(\d+)(/.+)", url)
        assert match, url
        self.host, port, self.path = match.groups()
        self.connection = socket.create_connection((self.host, int(port)), timeout=10)
        self.connection.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = "\r\n".join((
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "",
            "",
        )).encode()
        self.connection.sendall(request)
        response = self.connection.recv(4096).decode()
        expected = base64.b64encode(hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest()).decode()
        assert " 101 " in response and expected in response, response

    def __enter__(self) -> "cdp_socket":
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()

    def send(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytearray((0x81, 0x80))
        if len(encoded) < 126:
            header[1] |= len(encoded)
        elif len(encoded) < 65536:
            header[1] |= 126
            header.extend(struct.pack("!H", len(encoded)))
        else:
            header[1] |= 127
            header.extend(struct.pack("!Q", len(encoded)))
        self.connection.sendall(bytes(header) + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(encoded)))

    def receive(self) -> dict[str, object]:
        first, second = recv_exact(self.connection, 2)
        length = second & 0x7f
        if length == 126:
            length = struct.unpack("!H", recv_exact(self.connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(self.connection, 8))[0]
        mask = recv_exact(self.connection, 4) if second & 0x80 else b""
        payload = recv_exact(self.connection, length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        assert first & 0x0f == 1
        return json.loads(payload)


def recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = connection.recv(size)
        assert chunk
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def cdp(connection: cdp_socket, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    cdp.next_id += 1
    message_id = cdp.next_id
    connection.send({"id": message_id, "method": method, "params": params or {}})
    while True:
        message = connection.receive()
        if message.get("id") == message_id:
            assert "error" not in message, message
            result = message["result"]
            assert "exceptionDetails" not in result, (method, params, result["exceptionDetails"])
            return result


cdp.next_id = 0


def navigate(connection: cdp_socket, url: str) -> None:
    navigate.counter += 1
    target = f"{url}?p6b_geometry={navigate.counter}"
    cdp(connection, "Page.navigate", {"url": target})
    for _ in range(100):
        result = cdp(connection, "Runtime.evaluate", {"expression": "JSON.stringify({url: location.href, ready: document.readyState})"})
        state = json.loads(result["result"]["value"])
        if state == {"url": target, "ready": "complete"}:
            return
        time.sleep(0.05)
    raise AssertionError(f"browser did not finish loading {target}")


navigate.counter = 0


def evaluate_geometry(connection: cdp_socket, activation: str, layout_class: str) -> dict[str, float | str | bool]:
    expression = f'''(async () => {{
        {activation}
        await new Promise(resolve => setTimeout(resolve, 350));
        const target = document.querySelector('main#main').getBoundingClientRect();
        const nav = document.querySelector('.site-nav').getBoundingClientRect();
        return {{ targetTop: target.top, navBottom: nav.bottom, viewportWidth: window.innerWidth,
            hash: location.hash, focused: document.activeElement.tagName,
            layoutPresent: Boolean(document.querySelector({json.dumps(layout_class)})),
            compactAtMeasure: document.body.classList.contains('is-scrolled') }};
    }})()'''
    result = cdp(connection, "Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
    return result["result"]["value"]


def test_p6b_v02_active_donate_label_meets_normal_text_contrast() -> None:
    source = (ROOT / "css/style.css").read_text(encoding="utf-8")
    palette = css_declarations(source, ":root")
    parser = SupportFormParser()
    parser.feed((ROOT / "donate.html").read_text(encoding="utf-8"))
    active_donate = [
        attrs
        for tag, attrs in parser.elements
        if tag == "a"
        and attrs.get("class")
        and {"nav-donate", "active"} <= set(attrs["class"].split())
    ]
    assert len(active_donate) == 1
    assert active_donate[0]["aria-current"] == "page"
    active = css_declarations(source, ".nav-links .nav-donate.active")
    donate = css_declarations(source, ".nav-links .nav-donate")
    foreground = donate["color"].removesuffix(" !important")
    background = active["background"]
    assert background == "var(--red-dark)"
    assert contrast_ratio(foreground, palette[background.removeprefix("var(").removesuffix(")")]) >= 4.5
    assert foreground == "#fff"


def test_p6b_v04_submission_status_is_live_focusable_and_revealed() -> None:
    parser = SupportFormParser()
    parser.feed((ROOT / "support.html").read_text(encoding="utf-8"))
    success = [attrs for _tag, attrs in parser.elements if "data-fs-success" in attrs]
    form_error = [attrs for _tag, attrs in parser.elements if attrs.get("data-fs-error") is None and "data-fs-error" in attrs]
    assert len(success) == len(form_error) == 1
    assert success[0] | {"role": "status", "aria-live": "polite", "aria-atomic": "true", "tabindex": "-1"} == success[0]
    assert form_error[0] | {"role": "alert", "aria-live": "assertive", "aria-atomic": "true", "tabindex": "-1"} == form_error[0]

    assert run_form_harness("status") == {"focused": "status", "scrolled": "status"}


def test_p6b_v05_server_field_errors_are_associated_announced_and_focused() -> None:
    parser = SupportFormParser()
    parser.feed((ROOT / "support.html").read_text(encoding="utf-8"))
    fields = {
        attrs["name"]: attrs
        for tag, attrs in parser.elements
        if tag in {"input", "textarea"} and "data-fs-field" in attrs
    }
    errors = {
        attrs["data-fs-error"]: attrs
        for _tag, attrs in parser.elements
        if attrs.get("data-fs-error")
    }
    assert fields.keys() == errors.keys() == {"name", "email", "phone", "address", "message"}
    for name, field in fields.items():
        error = errors[name]
        assert field["aria-describedby"] == error["id"]
        assert error["role"] == "alert"
        assert error["aria-live"] == "assertive"
        assert error["aria-atomic"] == "true"

    assert run_form_harness("field") == {"focused": "email", "scrolled": "email"}


def run_navigation_harness() -> dict[str, object]:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const clickHandlers = {};
const main = {
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
  focus() { this.focused = true; },
  scrollIntoView() { this.scrolled = true; },
};
const skip = { addEventListener(name, callback) { clickHandlers[name] = callback; } };
const context = {
  window: { scrollY: 0, addEventListener() {}, requestAnimationFrame(callback) { callback(); }, history: { pushState(_state, _title, hash) { this.hash = hash; } } },
  document: {
    querySelector(selector) {
      if (selector === '.skip-link[href="#main"]') return skip;
      if (selector === 'main#main') return main;
      return null;
    },
    addEventListener(name, callback) { if (name === 'DOMContentLoaded') callback(); },
    body: {
      classList: {
        compact: false,
        add(name) { if (name === 'is-scrolled') this.compact = true; },
        remove(name) { if (name === 'is-scrolled') this.compact = false; },
      },
    },
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const event = { preventDefault() { this.defaultPrevented = true; } };
clickHandlers.click(event);
console.log(JSON.stringify({
  defaultPrevented: event.defaultPrevented === true,
  focused: main.focused ? 'main' : '',
  scrolled: main.scrolled ? 'main' : '',
  hash: context.window.history.hash,
  tabindex: main.attributes.tabindex,
  navState: context.document.body.classList.compact ? 'compact' : 'tall',
}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "js/nav.js")],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def run_form_harness(mode: str) -> dict[str, str]:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const mode = process.argv[2];
const form = {
  listeners: {},
  addEventListener(name, callback) { this.listeners[name] = callback; },
  elements: { namedItem() { return field; } },
};
const field = target('email');
const status = target('status');
const fieldError = { dataset: { fsError: 'email' } };
const feedbackRoot = {
  querySelector(selector) {
    if (selector.startsWith('[data-fs-error][data-fs-active]')) return mode === 'field' ? fieldError : null;
    if (selector.startsWith('[data-fs-success]')) return mode === 'status' ? status : null;
    return null;
  },
};
let observer;
const context = {
  window: {},
  formspree() {},
  document: { querySelector(selector) { return selector === '#support-form' ? form : null; } },
  HTMLElement: function HTMLElement() {},
  MutationObserver: function MutationObserver(callback) { observer = callback; this.observe = function() {}; },
  queueMicrotask(callback) { callback(); },
};
form.parentElement = feedbackRoot;
Object.setPrototypeOf(field, context.HTMLElement.prototype);
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
form.listeners.submit();
observer();
const active = mode === 'field' ? field : status;
console.log(JSON.stringify({ focused: active.focused || '', scrolled: active.scrolled || '' }));
function target(name) {
  return {
    focus() { this.focused = name; },
    scrollIntoView() { this.scrolled = name; },
  };
}
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "js/formspree-init.js"), mode],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


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


CAMPAIGN_PRINCIPLES = (
    "Experience That Matters",
    "Integrity & Independence",
    "Community Commitment",
)


def elements_owning_text(node: HtmlElement, text: str) -> list[HtmlElement]:
    """Elements whose own direct text is exactly `text`, ignoring descendant text.

    Ownership is what distinguishes a heading from its wrapper: the card div
    around a principle contributes no text of its own, so only the heading is
    returned. A principle rewritten as body copy is owned by that element
    instead, which is how a demotion becomes visible here.
    """
    found = []
    for child in node.children:
        if normalized_text(child.text) == text:
            found.append(child)
        found.extend(elements_owning_text(child, text))
    return found


def test_campaign_principles_are_third_level_headings_with_exact_text() -> None:
    parser = TreeParser()
    parser.feed((ROOT / "index.html").read_text(encoding="utf-8"))
    for principle in CAMPAIGN_PRINCIPLES:
        owners = elements_owning_text(parser.root, principle)
        assert [node.tag for node in owners] == ["h3"], (
            principle,
            [node.tag for node in owners],
        )


# This revision is the content baseline. The guards compare parsed semantics,
# not source formatting, classes, or wrappers.
CONTENT_GUARD_BASELINE = "18f4047"
NON_CONTENT_TAGS = frozenset({"script", "style", "svg", "path", "source"})
VOID_INPUT_TYPES = frozenset({"hidden"})


def parse_html(source: str) -> TreeParser:
    parser = TreeParser()
    parser.feed(source)
    parser.close()
    return parser


def baseline_page(name: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{CONTENT_GUARD_BASELINE}:{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def walk_elements(node: HtmlElement) -> list[HtmlElement]:
    found = []
    for child in node.children:
        found.append(child)
        found.extend(walk_elements(child))
    return found


def direct_text_runs(root: HtmlElement) -> Counter[tuple[str, str]]:
    return Counter(
        (node.tag, normalized_text(node.text))
        for node in walk_elements(root)
        if node.tag not in NON_CONTENT_TAGS and normalized_text(node.text)
    )


def headings(root: HtmlElement) -> list[tuple[int, str]]:
    return [
        (int(node.tag[1]), element_text(node))
        for node in walk_elements(root)
        if re.fullmatch(r"h[1-6]", node.tag)
    ]


def anchors(root: HtmlElement) -> list[HtmlElement]:
    return [node for node in walk_elements(root) if node.tag == "a" and node.attributes.get("href")]


def page_for_local_path(path: str) -> str | None:
    if path == "/":
        return "index.html"
    candidate = path.removeprefix("/")
    if candidate in PAGES:
        return candidate
    if f"{candidate}.html" in PAGES:
        return f"{candidate}.html"
    return None


def image_intentions(root: HtmlElement) -> Counter[tuple[str, str | None, str | None]]:
    return Counter(
        (node.attributes.get("src", ""), node.attributes.get("alt"), node.attributes.get("aria-hidden"))
        for node in walk_elements(root)
        if node.tag == "img" and (node.attributes.get("alt") == "" or node.attributes.get("aria-hidden") == "true")
    )


def element_name(node: HtmlElement) -> str:
    if node.attributes.get("aria-label", "").strip():
        return node.attributes["aria-label"].strip()
    parts = list(node.text)
    for child in node.children:
        if child.tag == "img":
            parts.append(child.attributes.get("alt", ""))
        else:
            parts.append(element_name(child))
    return normalized_text(parts)


def parent_map(root: HtmlElement) -> dict[int, HtmlElement]:
    parents = {}
    for node in walk_elements(root):
        for child in node.children:
            parents[id(child)] = node
    return parents


def control_name(node: HtmlElement, labels: dict[str, HtmlElement], parents: dict[int, HtmlElement]) -> str:
    name = element_name(node)
    if name:
        return name
    control_id = node.attributes.get("id")
    if control_id and control_id in labels:
        return element_name(labels[control_id])
    ancestor = parents.get(id(node))
    while ancestor:
        if ancestor.tag == "label":
            return element_name(ancestor)
        ancestor = parents.get(id(ancestor))
    return ""


def interactive_control_names(root: HtmlElement) -> Counter[tuple[str, str]]:
    nodes = walk_elements(root)
    labels = {node.attributes["for"]: node for node in nodes if node.tag == "label" and node.attributes.get("for")}
    parents = parent_map(root)
    controls = [
        node for node in nodes
        if node.tag in {"a", "button", "select", "textarea"}
        or (node.tag == "input" and node.attributes.get("type", "text").lower() not in VOID_INPUT_TYPES)
    ]
    return Counter(
        (control.tag, control_name(control, labels, parents))
        for control in controls
        if control.attributes.get("aria-hidden") != "true"
    )


def test_campaign_content_runs_and_ownership_match_baseline() -> None:
    for name in PAGES:
        expected_root = parse_html(baseline_page(name)).root
        actual_root = parse_html((ROOT / name).read_text(encoding="utf-8")).root
        expected = direct_text_runs(expected_root)
        actual = direct_text_runs(actual_root)
        missing = expected - actual
        unexpected = actual - expected
        assert not missing and not unexpected, (name, "missing", list(missing.elements()), "unexpected", list(unexpected.elements()))

        for (tag, text), count in expected.items():
            owners = elements_owning_text(actual_root, text)
            assert sum(owner.tag == tag for owner in owners) == count, (name, text, tag, [owner.tag for owner in owners])


def test_campaign_heading_outline_matches_baseline() -> None:
    for name in PAGES:
        expected = headings(parse_html(baseline_page(name)).root)
        actual = headings(parse_html((ROOT / name).read_text(encoding="utf-8")).root)
        assert actual == expected, (name, "expected", expected, "actual", actual)
        assert [level for level, _text in actual].count(1) == 1, (name, actual)
        assert all(current <= previous + 1 for (previous, _), (current, _) in zip(actual, actual[1:])), (name, actual)


def test_campaign_links_targets_and_image_alternatives_match_baseline() -> None:
    baseline_external = Counter()
    for name in PAGES:
        expected_root = parse_html(baseline_page(name)).root
        actual_root = parse_html((ROOT / name).read_text(encoding="utf-8")).root
        baseline_external.update(
            anchor.attributes["href"]
            for anchor in anchors(expected_root)
            if urlsplit(anchor.attributes["href"]).scheme or anchor.attributes["href"].startswith("//")
        )
        expected_links = Counter((anchor.attributes["href"], element_name(anchor)) for anchor in anchors(expected_root))
        actual_links = Counter((anchor.attributes["href"], element_name(anchor)) for anchor in anchors(actual_root))
        assert actual_links == expected_links, (name, "expected", expected_links, "actual", actual_links)
        assert image_intentions(actual_root) == image_intentions(expected_root), name
        for image in (node for node in walk_elements(actual_root) if node.tag == "img"):
            alt = image.attributes.get("alt")
            hidden = image.attributes.get("aria-hidden") == "true"
            assert alt is not None, (name, image.attributes.get("src"))
            assert alt.strip() or hidden or alt == "", (name, image.attributes.get("src"))

        ids = {node.attributes["id"] for node in walk_elements(actual_root) if node.attributes.get("id")}
        for anchor in anchors(actual_root):
            href = anchor.attributes["href"]
            target = urlsplit(href)
            if target.scheme or href.startswith("//"):
                continue
            target_page = page_for_local_path(target.path) if target.path else name
            assert target_page in PAGES, (name, href)
            if target.fragment:
                target_root = actual_root if target_page == name else parse_html((ROOT / target_page).read_text(encoding="utf-8")).root
                target_ids = ids if target_page == name else {node.attributes["id"] for node in walk_elements(target_root) if node.attributes.get("id")}
                assert target.fragment in target_ids, (name, href)

    actual_external = Counter(
        anchor.attributes["href"]
        for name in PAGES
        for anchor in anchors(parse_html((ROOT / name).read_text(encoding="utf-8")).root)
        if urlsplit(anchor.attributes["href"]).scheme or anchor.attributes["href"].startswith("//")
    )
    assert actual_external == baseline_external, ("external hrefs", baseline_external - actual_external, actual_external - baseline_external)


def test_campaign_interactive_controls_have_accessible_names() -> None:
    for name in PAGES:
        expected = interactive_control_names(parse_html(baseline_page(name)).root)
        actual = interactive_control_names(parse_html((ROOT / name).read_text(encoding="utf-8")).root)
        assert actual == expected, (name, "expected", expected, "actual", actual)
        assert all(accessible_name for _tag, accessible_name in actual), (name, actual)
