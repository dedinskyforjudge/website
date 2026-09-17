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
    "Law Enforcement": (
        "Eric Severson",
        "Nick Ollinger",
        "Arnold Moncada",
        "Alfonso Morales",
    ),
    "Local Officials": (
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
    "Hon. Rebecca Grassl Bradley": "(Former)",
    "Hon. Daniel Kelly": "(Former)",
    "Hon. Michael Bohren": "(Retired)",
    "Hon. Kathryn Foster": "(Retired)",
    "Hon. Randy R. Koschnick": "(Former)",
    "Nick Ollinger": "(Elect)",
    "Arnold Moncada": "(Former)",
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


def test_endorsement_tier_order_on_the_page_is_exact() -> None:
    """Tier order is the page's prominence ordering, so it is pinned, not incidental.

    EXPECTED_ENDORSEMENT_TIERS is a dict literal, and dicts keep insertion
    order, so the order its tiers are written in is the order the page must
    render them in. Membership is checked separately; this pins only sequence.
    """
    rendered = tuple(dict.fromkeys(tier for _name, _title, _note, tier in endorsement_entries()))
    assert rendered == tuple(EXPECTED_ENDORSEMENT_TIERS)


def test_endorsement_tier_membership_is_exact() -> None:
    actual = {}
    for name, _title, _note, tier in endorsement_entries():
        actual.setdefault(tier, []).append(name)
    assert {tier: tuple(names) for tier, names in actual.items()} == EXPECTED_ENDORSEMENT_TIERS


def test_required_endorsement_name_set_and_count_are_exact() -> None:
    names = [entry[0] for entry in endorsement_entries()]
    expected = {name for tier in EXPECTED_ENDORSEMENT_TIERS.values() for name in tier}
    assert len(names) == 45
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
    assert matches == ["2026-09-16"]


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


VOID_INPUT_TYPES = frozenset({"hidden"})

# Curated campaign content contracts.  These are intentionally literal: a
# layout revision may add presentation copy or wrappers, but it cannot remove
# or alter any listed campaign statement.
CONTENT_TEXTS = {
    "index.html": (
        "Dedinsky for Judge — Waukesha County Circuit Court",
        "Waukesha County Circuit Court", "for Circuit Court Judge",
        "A career built on experience, integrity, and a deep commitment to justice.",
        "About Paul", "Paul Dedinsky is a 25-year veteran prosecutor who has worked in the criminal justice system for over thirty years. Paul received his law degree from the University of Wisconsin–Madison Law School in 1993, and his Ph.D. in Education and Leadership in 2012.",
        "Paul has taught law at Marquette University Law School, as well as business law, legal ethics, and business leadership to undergrads and graduate students at Cardinal Stritch. Paul is married to Lisa, and the couple raised their three children — Abby, Charlie, and Natalia — in Delafield, Wisconsin.",
        "Governor Walker first appointed Paul as Chief Legal Counsel to the Wisconsin Department of Agriculture, Trade, and Consumer Protection (DATCP) from 2017–2018. Governor Walker then appointed Paul to serve as a circuit court judge (2019–2020). With experience as a judge, as well as a sexual assault and domestic violence prosecutor who worked with hundreds upon hundreds of victims, Paul is acutely aware of the toll of the system upon victims of crime.",
        "30+ years of legal experience", "150+ trials", "25 years as a State Prosecutor",
        "Main author/editor, Wisconsin Domestic Violence Prosecution Manual, 2004",
        "Ph.D. in Education & Leadership", "Trained police officers at Waukesha County Technical College and Statewide",
        "Support the Campaign", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer",
        "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018",
    ),
    "about.html": (
        "About Paul — Dedinsky for Judge", "A life of service to Wisconsin's justice system",
        "25 years as a prosecutor — and a career path twice recognized by Governor Scott Walker, who appointed Paul as Chief Legal Counsel at WI DATCP and later to the circuit court bench.",
        "Paul Dedinsky is a dedicated public servant with a proven commitment to justice and community safety. Paul's extensive experience as a veteran prosecutor and deep understanding of the law have prepared him to serve the people of Waukesha County as a fair and impartial judge.",
        "Paul grew up in southeastern Wisconsin and attended Marquette University High School and Creighton University. He graduated from the University of Wisconsin–Madison Law School in 1993 and earned his Ph.D. in Education and Leadership in 2012.",
        "Paul is married to Lisa, and the couple raised three children — Abby, Charlie, and Natalia — in Delafield, Wisconsin.",
        "As a Waukesha County Judge, Paul Dedinsky pledges to ensure the safety of Waukesha County residents, uphold the rule of law, and never legislate from the bench or engage in judicial activism. Paul will continue to serve the community with integrity and firm justice.",
        "Front row seat. As a longtime prosecutor in Milwaukee, Paul saw firsthand how crime and violence can devastate, destabilize, and destroy communities. Paul will stand in the breach and prevent that from happening in Waukesha County.",
        "Paul has a long, proven, conservative track record of holding criminals accountable and keeping families safe. He earned a strong reputation for securing justice for those victimized by crime.",
        "Education", "1985", "Marquette University High School", "1989", "B.A. — Creighton University", "1993", "J.D. — University of Wisconsin–Madison Law School", "2012", "Ph.D., Education & Leadership",
        "1997 – 2017", "Prosecutor — Appellate Division", "Handles mainly Homicide and Sexual Assault appellate matters. Previously served as Homicide-Violent Gun Prosecutor and State Prosecutor Representative to the Republican National Convention.",
        "2017 – 2018", "Chief Legal Counsel — WI DATCP", "Chief Legal Counsel for the Wisconsin Department of Agriculture, Trade and Consumer Protection. Consumer protection, administrative rule-making, ethics counsel and trainer for the agency.",
        "2019 – 2020", "Milwaukee County Circuit Court Judge", "Appointed by Governor Scott Walker.",
        "2021 – Present", "Assistant District Attorney", "As a prosecutor, reviewed thousands of investigations, charged criminal and civil matters, litigated over 50 jury trials and 100 court trials. Director of the DA's Domestic Violence Unit (2001–2007). Sexual Assault prosecutor (1999–2001). Main Author and Editor of the .", "Wisconsin Domestic Violence Prosecution Manual, 2004",
        "Private Practice — Brookfield, WI", "Decades-long commitment to holding criminals accountable and keeping families safe.",
        "Faith & Community", "Parish lector, pastoral council member, teacher of Christian formation. Longtime involvement with Waukesha's Schoenstatt International Province & Retreat Center.",
        "St. Thomas More Lawyers Society", "President, 2022. Board of Directors, 2013–2019 and 2022–present. Co-organized the annual Youth Law Day at Marquette University Law School, 2010–2018.",
        "SOFA, Inc. — Oconomowoc, WI", "Board member supporting the “Jump for Archie” anti-opiate addiction event, in memory of Archie Badura of Oconomowoc.",
        "Earlier Service", "Big Brothers/Big Sisters of Metro Milwaukee. St. Catherine's Residence for Women committee member. St. Aemilian's Pre-School board member.",
        "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018",
    ),
    "vote.html": (
        "Vote — Dedinsky for Judge", "MARK YOUR CALENDAR", "April 6, 2027", "Tuesday — Wisconsin Spring Election", "Polls Open 7:00 AM – 8:00 PM",
        "Plan Ahead", "Absentee ballot request", "by Mar 17", "Mail / online registration", "through Apr 2", "In-person at municipal clerk", "by Apr 1, 5pm", "Bring proof of residence", "Same-day registration at polls", "Election Day", "Apr 6", "Polls open 7am – 8pm",
        "All dates and procedures should be confirmed at — Wisconsin's official voter resource.", "Judicial Philosophy", "Paul believes the best judges lead with firmness, intelligence, and fairness. They are public servants — approachable, accessible, and committed to justice and the rule of law.", "Paul understands the justice system from every perspective. He has presided over hundreds of cases and knows that every person who enters a courtroom deserves to be treated with dignity and respect.",
        "Stand With Paul", "Registered voters in Waukesha County. Circuit court judges serve the entire county.", "Spread the word. Help win this race.",
        "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018",
    ),
    "endorsements.html": (
        "Endorsements — Dedinsky for Judge", "Trusted by leaders across Wisconsin", "In Their Own Words",
        "\"I could not be more excited to endorse former Governor Walker-appointed judge and career prosecutor, Paul Dedinsky, for the Waukesha County bench. Paul is well known to so many of us precisely because he has already devoted his life's work to making Wisconsin a safer place for our families. I also know him to be a person of deep faith and true integrity. He is the perfect match for this important position.\"",
        "\"I wholeheartedly endorse Paul Dedinsky for Waukesha County Circuit Court. He is a principled conservative who has demonstrated that he will apply the law as written; support our Constitutions; and faithfully guard against encroachment of our liberties. He has my full support.\"",
        "\"Now, more than ever, we need judges who not only respect the law, but who stand up for everyone's rights and freedoms. Paul Dedinsky will bring the perfect blend of constitutionalism, compassion, and justice to Waukesha's bench.\"",
        "\"Paul and I have been close friends for 20 years. With his experience as a longtime prosecutor, he's the one I trust to keep my family — and all of our families in Waukesha County — safe.\"",
        "Hon. Mark Gundrum", "Hon. Shelley A. Grogan", "Hon. Maria Lazar", "Hon. Anthony LoCoco", "Wisconsin Court of Appeals Judge, District II",
        "Full List of Endorsements", "Wisconsin Supreme Court", "Hon. Annette Kingsland Ziegler", "Hon. Rebecca Grassl Bradley", "Hon. Daniel Kelly", "Wisconsin Court of Appeals, District II", "Waukesha County Circuit Court", "Hon. Michael Aprahamian", "Hon. Jennifer Dorow", "Hon. Cody Horlacher", "Hon. David Maas", "Hon. Michael Maxwell", "Hon. J. Arthur Melvin III", "Hon. Jack Pitzo", "Hon. Scott Wagner", "Hon. Zach Wittchow", "Hon. Michael Bohren", "(Retired)", "Hon. Kathryn Foster", "Additional Wisconsin Jurists", "Hon. T. Christopher Dee", "Hon. Robert Dehring", "Hon. Grant Scaife", "Hon. Randy R. Koschnick", "State Senators", "Julian Bradley", "State Senator", "Steve Nass", "Rob Hutton", "State Representatives", "Barb Dittrich", "State Representative", "Adam Neylon", "Chuck Wichgers", "Scott Allen", "Dan Knodl", "Jim Piwowarczyk", "Law Enforcement", "Eric Severson", "Nick Ollinger", "(Elect)", "Arnold Moncada", "Alfonso Morales", "Fitchburg Chief of Police", "Local Officials", "Lesli Boese", "Tim Aicher", "Mayor of Delafield", "Matt Rosek", "Mayor of Oconomowoc", "Jeff Pfannerstill", "Hartland Village President", "Steve Ponto", "Mayor of Brookfield", "Gary Mahkorn", "Brookfield Common Council President", "Organizations and Businesses", "Milwaukee Police Association", "Waukesha County Young Republicans", "5 Riders Organization", "Hernandez Roofing",
        "Stand With Paul", "Stand with Paul.", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018",
    ),
    "support.html": (
        "Support — Dedinsky for Judge", "Every conversation matters in a local election", "Show your support to neighbors.", "Introduce Paul to your neighbors.", "Help canvass Waukesha County.", "Open your home or a local venue.",
        "Sign Up", "Thanks for signing up!", "The campaign will be in touch soon.", "Name", "*", "Email", "Phone", "(Optional)", "Address", "(Optional — helps with yard sign delivery)", "Message", "Ways you'd like to help", "Display a yard sign", "Host a meet & greet", "Host a fundraiser", "Knock on doors", "Other", "Paul has my permission to publicly list me as a Supporter", "Stay in touch", "I'd like to receive campaign email updates", "I'd like to receive text updates (opt-out anytime)", "Contribute", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018",
    ),
    "donate.html": (
        "Donate — Dedinsky for Judge", "Donate", "Your support makes a difference", "Every dollar helps Paul reach voters across Waukesha County. Contributions are processed securely through WinRed.", "Donate via WinRed →", "Opens in a new tab.", "Mailing address", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018", "More Ways to Help", "Can't Donate? You Can Still Help", "Display a yard sign, host a meet & greet, or knock on doors. Every conversation matters in a local race.", "Get Involved", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "© 2026 All rights reserved. · ·",
    ),
    "privacy.html": (
        "Privacy Policy — Dedinsky for Judge", "Privacy Policy", "Plain answers about the information you share with us", "Effective July 22, 2026.", "This site belongs to the Paul Dedinsky for Judge campaign. It exists to introduce Paul to Waukesha County voters, not for tracking purposes. Here's exactly what we collect and what we do with it.",
        "What we collect", "Only what you choose to submit through the volunteer form on our Support page: your name, email, and optionally your phone number, address, a message, and which ways you'd like to help. Browsing the site requires no account and submits nothing.",
        "Where it goes", "Form submissions are delivered by , our form processor, and kept by the campaign in a private database. It is used by campaign volunteers to follow up with you by arranging yard signs, coordinating events, and sending the updates you opted into. We do not sell, rent, or share your information with anyone else.", "Formspree",
        "Donations happen entirely on WinRed's website under — this site never sees your payment details. Campaign finance law requires donations to be reported under Wisconsin's disclosure rules.", "WinRed's privacy policy",
        "Cookies and tracking", "None. This site sets no cookies and runs no analytics, advertising, or tracking scripts. Fonts and all code are served from our own domain. Like nearly every website, our hosting provider (Netlify) keeps standard server logs, including IP addresses, to serve pages and prevent abuse.",
        "Email and text updates", "We contact you only in ways you opted into. To stop hearing from us, reply to any message or email the campaign and we'll take you off the list.", "Your choices", "Want your information corrected or deleted from our volunteer list? Email and we'll take care of it.", "privacy@dedinsky4judge.com", "This site is not directed at children under 13, and we don't knowingly collect their information. If this policy changes, the update appears on this page with a new effective date.", "Donate", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018", "© 2026 All rights reserved. · ·",
    ),
    "404.html": (
        "Page Not Found — Dedinsky for Judge", "404", "Page Not Found", "The page you're looking for doesn't exist or has been moved. Let's get you back on track.", "Return to Home", "Donate", "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer", "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018", "© 2026 All rights reserved. · ·",
    ),
}

CONTENT_COUNTS = {
    "vote.html": {"Apr 6": 2},
    "endorsements.html": {
        "Hon. Mark Gundrum": 2, "Hon. Shelley A. Grogan": 2,
        "Hon. Maria Lazar": 2, "Hon. Anthony LoCoco": 2,
        "Wisconsin Court of Appeals Judge, District II": 4,
        "(Former)": 4, "(Retired)": 2, "State Senator": 3,
        "State Representative": 6,
    },
    "support.html": {"*": 2, "(Optional)": 2},
    "donate.html": {"Donate": 3, "Paul Dedinsky for Judge PO Box 180051 Delafield, WI 53018": 2},
    "privacy.html": {"Donate": 2},
    "404.html": {"Donate": 2},
}

EXPECTED_HEADINGS = {
    "index.html": ((1, "PaulDedinsky"), (2, "A Career Dedicated to Justice"), (3, "By the Numbers"), (3, "Experience That Matters"), (3, "Integrity & Independence"), (3, "Community Commitment"), (2, "Donate Today")),
    "about.html": ((1, "About Paul"), (2, "Meet Paul"), (2, "Commitment to Justice and Safety"), (2, "Academic Background"), (2, "A Career in Service"), (3, "Prosecutor — Appellate Division"), (3, "Milwaukee County Circuit Court Judge"), (3, "Chief Legal Counsel — WI DATCP"), (3, "Assistant District Attorney"), (3, "Private Practice — Brookfield, WI"), (2, "Civic & Volunteer Involvement"), (3, "Faith & Community"), (3, "St. Thomas More Lawyers Society"), (3, "SOFA, Inc. — Oconomowoc, WI"), (3, "Earlier Service")),
    "vote.html": ((1, "How to Vote"), (2, "Key Dates & Deadlines"), (2, "What kind of Judge will Paul be?"), (2, "Help Get Out the Vote")),
    "endorsements.html": ((1, "Endorsements"), (2, "What Wisconsin Judges Are Saying"), (2, "Endorsed By"), (3, "Wisconsin Supreme Court"), (3, "Wisconsin Court of Appeals, District II"), (3, "Waukesha County Circuit Court"), (3, "Additional Wisconsin Jurists"), (3, "State Senators"), (3, "State Representatives"), (3, "Law Enforcement"), (3, "Local Officials"), (3, "Organizations and Businesses"), (2, "Join This Coalition")),
    "support.html": ((1, "Support Paul"), (2, "Ways You Can Support Paul"), (3, "Display a Yard Sign"), (3, "Host a Fundraiser"), (3, "Knock on Doors"), (3, "Host a Meet & Greet"), (2, "Get Involved Today"), (2, "Support the Campaign Financially")),
    "donate.html": ((1, "Donate"), (2, "Mailing address"), (2, "Can't Donate? You Can Still Help")),
    "privacy.html": ((1, "Privacy Policy"), (2, "What we collect"), (2, "Where it goes"), (2, "Cookies and tracking"), (2, "Email and text updates"), (2, "Your choices")),
    "404.html": ((1, "Page Not Found"),),
}

COMMON_LINKS = (("#main", "Skip to main content"), ("/", "Dedinsky for Judge"), ("/about", "About"), ("/vote", "Vote"), ("/endorsements", "Endorsements"), ("/support", "Support"), ("/donate", "Donate"), ("/privacy", "Privacy"), ("https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150", "Donate"))
EXPECTED_LINKS = {
    "index.html": COMMON_LINKS + (("/about", "Read Full Bio"), ("https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150", "Contribute")),
    "about.html": COMMON_LINKS,
    "vote.html": COMMON_LINKS + (("/support", "Get Involved"), ("https://myvote.wi.gov", "Find Your Polling Place →"), ("https://myvote.wi.gov", "myvote.wi.gov")),
    "endorsements.html": COMMON_LINKS + (("https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150", "Contribute"),),
    "support.html": COMMON_LINKS + (("https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150", "Donate"),),
    "donate.html": COMMON_LINKS + (("/support", "Get Involved"), ("https://secure.winred.com/paul-dedinsky-for-judge/donate-today?amount=150", "Donate via WinRed →")),
    "privacy.html": COMMON_LINKS + (("https://formspree.io/legal/privacy-policy/", "Formspree"), ("https://winred.com/privacy", "WinRed's privacy policy"), ("mailto:privacy@dedinsky4judge.com", "privacy@dedinsky4judge.com")),
    "404.html": COMMON_LINKS + (("/", "Return to Home"),),
}

EXPECTED_IMAGES = {
    "index.html": (("/img/logo.svg", "Dedinsky for Judge"), ("/img/Paul%20Dedinsky1.jpeg", "Paul Dedinsky"), ("/img/logo.svg", "Dedinsky for Judge")),
    "about.html": (("/img/logo.svg", "Dedinsky for Judge"), ("/img/Dedinsky%20family%20%28Paul%2C%20Lisa%2C%20Natalia%2C%20Charlie%2C%20Abby%29.jpeg", "Paul and Lisa Dedinsky with their children Natalia, Charlie, and Abby"), ("/img/logo.svg", "Dedinsky for Judge")),
    **{name: (("/img/logo.svg", "Dedinsky for Judge"), ("/img/logo.svg", "Dedinsky for Judge")) for name in PAGES if name not in {"index.html", "about.html"}},
}


def parse_html(source: str) -> TreeParser:
    parser = TreeParser()
    parser.feed(source)
    parser.close()
    return parser


def walk_elements(node: HtmlElement) -> list[HtmlElement]:
    found = []
    for child in node.children:
        found.append(child)
        found.extend(walk_elements(child))
    return found


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


def expected_control_names(page: str) -> Counter[tuple[str, str]]:
    expected = Counter(("a", name) for _href, name in EXPECTED_LINKS[page])
    expected[("button", "Menu")] += 1
    if page == "support.html":
        expected.update({
            ("button", "Sign Me Up"): 1,
            ("input", "Name *"): 1,
            ("input", "Email *"): 1,
            ("input", "Phone (Optional)"): 1,
            ("input", "Address (Optional — helps with yard sign delivery)"): 1,
            ("textarea", "Message (Optional)"): 1,
            ("input", "Display a yard sign"): 1,
            ("input", "Host a meet & greet"): 1,
            ("input", "Host a fundraiser"): 1,
            ("input", "Knock on doors"): 1,
            ("input", "Other"): 1,
            ("input", "Paul has my permission to publicly list me as a Supporter"): 1,
            ("input", "I'd like to receive campaign email updates"): 1,
            ("input", "I'd like to receive text updates (opt-out anytime)"): 1,
        })
    return expected


def test_campaign_content_runs_and_ownership_match_baseline() -> None:
    for name in PAGES:
        actual_root = parse_html((ROOT / name).read_text(encoding="utf-8")).root
        expected = Counter(CONTENT_TEXTS[name])
        for text, count in CONTENT_COUNTS.get(name, {}).items():
            expected[text] = count
        for text, count in expected.items():
            owners = elements_owning_text(actual_root, text)
            assert len(owners) == count, (name, text, count, [owner.tag for owner in owners])


def test_campaign_heading_outline_matches_baseline() -> None:
    for name in PAGES:
        actual = headings(parse_html((ROOT / name).read_text(encoding="utf-8")).root)
        for level, text in EXPECTED_HEADINGS[name]:
            owners = elements_owning_text(parse_html((ROOT / name).read_text(encoding="utf-8")).root, text)
            assert sum(node.tag == f"h{level}" for node in owners) == 1, (name, text, level, [node.tag for node in owners])
        assert [level for level, _text in actual].count(1) == 1, (name, actual)
        assert all(current <= previous + 1 for (previous, _), (current, _) in zip(actual, actual[1:])), (name, actual)


def test_campaign_links_targets_and_image_alternatives_match_baseline() -> None:
    expected_external = Counter()
    for name in PAGES:
        actual_root = parse_html((ROOT / name).read_text(encoding="utf-8")).root
        expected_links = Counter(EXPECTED_LINKS[name])
        actual_links = Counter((anchor.attributes["href"], element_name(anchor)) for anchor in anchors(actual_root))
        assert not expected_links - actual_links, (name, "missing", expected_links - actual_links)
        expected_external.update(
            href for href, _label in EXPECTED_LINKS[name]
            if urlsplit(href).scheme or href.startswith("//")
        )
        actual_images = Counter(
            (node.attributes.get("src", ""), node.attributes.get("alt"))
            for node in walk_elements(actual_root) if node.tag == "img"
        )
        assert not Counter(EXPECTED_IMAGES[name]) - actual_images, (name, "images", Counter(EXPECTED_IMAGES[name]) - actual_images)
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
    assert actual_external == expected_external, ("external hrefs", expected_external - actual_external, actual_external - expected_external)


def test_campaign_interactive_controls_have_accessible_names() -> None:
    for name in PAGES:
        actual = interactive_control_names(parse_html((ROOT / name).read_text(encoding="utf-8")).root)
        expected = expected_control_names(name)
        assert not expected - actual, (name, "missing", expected - actual)
        assert all(accessible_name for _tag, accessible_name in actual), (name, actual)


# A form's action is the URL a visitor's own data is sent to, so one wrong
# character loses every submission silently — the same failure the anchor
# hrefs above are pinned against. Pinned by value, page by page, so adding a
# form that posts somewhere new has to be a deliberate edit here too.
EXPECTED_FORM_ENDPOINTS = {
    "support.html": (("https://formspree.io/f/mojyzvpo", "POST"),),
    **{name: () for name in PAGES if name != "support.html"},
}


def form_endpoints(root: HtmlElement) -> list[tuple[str, str]]:
    return [
        (node.attributes.get("action", ""), node.attributes.get("method", "get").upper())
        for node in walk_elements(root)
        if node.tag == "form"
    ]


def test_campaign_form_endpoints_match_baseline() -> None:
    for name in PAGES:
        root = parse_html((ROOT / name).read_text(encoding="utf-8")).root
        actual = form_endpoints(root)
        expected = list(EXPECTED_FORM_ENDPOINTS[name])
        assert actual == expected, (name, "form endpoint", expected, actual)
