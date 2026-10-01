"""Guards for the unlisted unsubscribe page (SPEC S14)."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "unsubscribe.html"
SCRIPT = ROOT / "js" / "unsubscribe-form.js"
ATTRIBUTION = "Authorized and paid for by Paul Dedinsky for Judge | Lane Ruhland, Treasurer"
FORM_ID = "moevbzkk"


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def source() -> str:
    return PAGE.read_text(encoding="utf-8")


def elements() -> list[tuple[str, dict[str, str | None]]]:
    parser = Parser()
    parser.feed(source())
    return parser.elements


def text_only(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


class _QuietHandler(SimpleHTTPRequestHandler):
    """Serve the candidate page for the browser-only S14.3.1 journey."""

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class _DevTools:
    """Small dependency-free DevTools client for the layout journey below."""

    def __init__(self, url: str) -> None:
        parsed = urlsplit(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = self.socket.recv(4096)
        assert b" 101 " in response, response
        self.next_id = 1
        self.remote_requests: list[str] = []

    def close(self) -> None:
        self.socket.close()

    def _read(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.socket.recv(length - len(chunks))
            if not chunk:
                raise RuntimeError("DevTools socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes((0x81, 0x80 | len(data)))
        elif len(data) < 65536:
            header = bytes((0x81, 0x80 | 126)) + len(data).to_bytes(2, "big")
        else:
            header = bytes((0x81, 0x80 | 127)) + len(data).to_bytes(8, "big")
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.socket.sendall(header + mask + masked)

    def _send_notification(self, method: str, **params: object) -> None:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})

    def _handle_event(self, response: dict[str, object]) -> None:
        if response.get("method") != "Fetch.requestPaused":
            return
        params = response["params"]
        assert isinstance(params, dict), response
        request = params["request"]
        assert isinstance(request, dict), response
        url = request["url"]
        assert isinstance(url, str), response
        hostname = urlsplit(url).hostname
        if hostname in {"127.0.0.1", "::1", "localhost"} or url.startswith(("about:", "blob:", "data:")):
            self._send_notification("Fetch.continueRequest", requestId=params["requestId"])
            return
        self.remote_requests.append(url)
        self._send_notification("Fetch.failRequest", requestId=params["requestId"], errorReason="BlockedByClient")

    def _receive(self) -> dict[str, object]:
        first, second = self._read(2)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read(8), "big")
        masked = second & 0x80
        mask = self._read(4) if masked else b""
        data = self._read(length)
        if masked:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        assert first & 0x0F == 1
        return json.loads(data)

    def call(self, method: str, **params: object) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            response = self._receive()
            self._handle_event(response)
            if response.get("id") == request_id:
                assert "error" not in response, response
                return response["result"]

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        payload = result["result"]
        assert "exceptionDetails" not in result, result
        return payload.get("value")


class _UnsubscribeBrowser:
    """A local browser journey with fail-closed request handling before navigation."""

    def __enter__(self) -> "_UnsubscribeBrowser":
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_QuietHandler, directory=str(ROOT))
        )
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        debug_socket = socket.socket()
        debug_socket.bind(("127.0.0.1", 0))
        debug_port = debug_socket.getsockname()[1]
        debug_socket.close()
        self.profile = tempfile.mkdtemp(prefix="unsubscribe-browser-")
        self.process = subprocess.Popen(
            [
                "/opt/brave-bin/brave", "--headless=new", "--no-sandbox", "--disable-gpu",
                "--remote-allow-origins=*", f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={self.profile}", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        targets: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as response:
                    targets = json.load(response)
            except OSError:
                time.sleep(0.1)
                continue
            if targets:
                break
        assert targets, "Brave DevTools did not start"
        self.tools = _DevTools(targets[0]["webSocketDebuggerUrl"])
        self.tools.call("Network.enable")
        self.tools.call(
            "Fetch.enable",
            patterns=[{"urlPattern": "*"}],
        )
        self.tools.call(
            "Page.addScriptToEvaluateOnNewDocument",
            source="window.fetch = () => new Promise(() => {});",
        )
        self.tools.call("Page.enable")
        self.tools.call("Runtime.enable")
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args: object) -> None:
        self.tools.close()
        self.process.terminate()
        self.process.wait(timeout=10)
        shutil.rmtree(self.profile, ignore_errors=True)
        self.server.shutdown()
        self.server.server_close()

    def load(self, width: int) -> None:
        self.tools.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        self.tools.call("Page.navigate", url=f"{self.origin}/unsubscribe.html")
        self.tools.evaluate(
            "new Promise(resolve => { const ready = () => document.readyState === 'complete' ? "
            "setTimeout(resolve, 50) : setTimeout(ready, 20); ready(); })"
        )

    def assert_no_remote_requests(self) -> None:
        assert not self.tools.remote_requests, self.tools.remote_requests

    def state(self, name: str) -> dict[str, dict[str, float]]:
        return self.tools.evaluate(f'''(() => {{
          const form = document.querySelector('#unsubscribe-form');
          const success = document.querySelector('[data-fs-success]');
          const formError = document.querySelector('[data-fs-error=""]');
          const fieldError = document.querySelector('[data-fs-error="email"]');
          for (const region of [success, formError, fieldError]) region.removeAttribute('data-fs-active');
          formError.textContent = '';
          fieldError.textContent = '';
          form.dispatchEvent(new Event('submit', {{ bubbles: true, cancelable: true }}));
          if ({name!r} === 'success') success.setAttribute('data-fs-active', '');
          if ({name!r} === 'form-error') {{ formError.textContent = 'Please try again.'; formError.setAttribute('data-fs-active', ''); }}
          if ({name!r} === 'field-error') {{ fieldError.textContent = 'Enter a valid email address.'; fieldError.setAttribute('data-fs-active', ''); }}
          if ({name!r} === 'network-failure') {{ formError.textContent = 'Unable to submit. Please try again.'; formError.setAttribute('data-fs-active', ''); }}
          const rect = element => {{ const box = element.getBoundingClientRect(); return {{ x: box.x + window.scrollX, y: box.y + window.scrollY, width: box.width, height: box.height, right: box.right + window.scrollX, bottom: box.bottom + window.scrollY }}; }};
          const selector = {{ success: '[data-fs-notice="success"]', 'form-error': '[data-fs-notice="form-error"]', 'network-failure': '[data-fs-notice="form-error"]' }}[{name!r}];
          return {{ form: rect(form), field: rect(document.querySelector('#email')), button: rect(document.querySelector('[data-fs-submit-btn]')), notice: selector ? rect(document.querySelector(selector)) : null }};
        }})()''')


def meta(markup: str, attr: str, name: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', markup)
    return match.group(1) if match else None


def test_s14_1_s13_2_page_is_unlisted() -> None:
    """S14.1/S13.2: noindex and no nav, footer, sitemap, or llms discovery link."""
    page = source()
    assert '<meta name="robots" content="noindex">' in page
    route = "/unsubscribe"
    discovery = [ROOT / "sitemap.xml", ROOT / "llms.txt", ROOT / "_includes/nav.html", ROOT / "_includes/footer.html"]
    discovery.extend(path for path in ROOT.glob("*.html") if path != PAGE)
    for path in discovery:
        assert route not in path.read_text(encoding="utf-8"), path


def test_s14_1_s13_2_metadata_is_complete() -> None:
    """S14.1/S13.2/S8.2: the unlisted page retains its canonical, OG, and Twitter metadata."""
    page = source()
    url = "https://dedinsky4judge.com/unsubscribe"
    title = "Unsubscribe — Dedinsky for Judge"
    description = "Remove an email address from future Dedinsky for Judge campaign mailings."
    image = "https://dedinsky4judge.com/img/Paul%20Dedinsky1-social.jpg"

    assert f'<link rel="canonical" href="{url}">' in page
    assert {name: meta(page, "property", name) for name in (
        "og:type", "og:url", "og:site_name", "og:locale", "og:title", "og:description",
        "og:image", "og:image:width", "og:image:height", "og:image:alt",
    )} == {
        "og:type": "website",
        "og:url": url,
        "og:site_name": "Dedinsky for Judge",
        "og:locale": "en_US",
        "og:title": title,
        "og:description": description,
        "og:image": image,
        "og:image:width": "1200",
        "og:image:height": "630",
        "og:image:alt": "Paul Dedinsky for Waukesha County Circuit Court Judge",
    }
    assert {name: meta(page, "name", name) for name in (
        "twitter:card", "twitter:title", "twitter:description", "twitter:image",
    )} == {
        "twitter:card": "summary_large_image",
        "twitter:title": title,
        "twitter:description": description,
        "twitter:image": image,
    }


def test_s14_1_s13_4_extensionless_route_revalidates() -> None:
    """S14.1/S13.4: the served extensionless route has its own cache rule."""
    headers = (ROOT / "_headers").read_text(encoding="utf-8")
    assert "/unsubscribe\n  Cache-Control: public, max-age=0, must-revalidate\n" in headers


def test_s14_2_one_email_field_and_honeypot() -> None:
    """S14.2/S7.1.3: one labeled required email field and the hidden _gotcha honeypot."""
    form = [(tag, attrs) for tag, attrs in elements() if tag == "form"]
    assert len(form) == 1
    inputs = [attrs for tag, attrs in elements() if tag == "input"]
    assert [attrs.get("name") for attrs in inputs] == ["email", "_subject", "_gotcha"]
    email = inputs[0]
    assert email == {
        "type": "email",
        "id": "email",
        "name": "email",
        "data-fs-field": None,
        "autocomplete": "email",
        "aria-describedby": "email-error",
        "required": None,
    }
    assert '<label for="email">' in source()
    honeypot = inputs[-1]
    assert honeypot["type"] == "text"
    assert honeypot["tabindex"] == "-1"
    assert honeypot["autocomplete"] == "off"
    assert honeypot["aria-hidden"] == "true"
    styles = (ROOT / "css" / "style.css").read_text(encoding="utf-8")
    assert '.support-form input[name="_gotcha"]' in styles and "display: none;" in styles


def test_s14_2_subject_sdk_and_native_action_share_placeholder() -> None:
    """S14.2/S7.1.4: exact subject, local SDK, and no-JS POST use the same open form id."""
    page = source()
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'name="_subject" value="Unsubscribe request — dedinsky4judge.com"' in page
    assert f'action="https://formspree.io/f/{FORM_ID}" method="POST"' in page
    assert f"formId: '{FORM_ID}'" in script
    assert page.count(FORM_ID) + script.count(FORM_ID) == 2
    assert '<script src="/js/formspree-ajax-1.1.5.js" defer></script>' in page
    assert "window.formspree" in script and "useDefaultStyles: false" in script
    assert "URLSearchParams" not in script and "location.search" not in script


def test_s14_3_feedback_regions_and_focus_behavior() -> None:
    """S14.3/S7.7-S7.7.4: named live regions are styled, revealed, focused, and reset after success."""
    page = source()
    assert 'id="email-error" data-fs-error="email" class="field-error" role="alert" aria-live="assertive"' in page
    assert 'data-fs-success class="form-success" role="status" aria-live="polite" aria-atomic="true" tabindex="-1"' in page
    assert 'data-fs-error class="form-error" role="alert" aria-live="assertive" aria-atomic="true" tabindex="-1"' in page
    styles = (ROOT / "css" / "style.css").read_text(encoding="utf-8")
    assert "[data-fs-success][data-fs-active]" in styles
    script = SCRIPT.read_text(encoding="utf-8")
    assert "new MutationObserver" in script
    assert "element.focus({ preventScroll: true })" in script
    assert "scrollIntoView({ behavior: 'auto', block: 'center' })" in script
    sdk = (ROOT / "js" / "formspree-ajax-1.1.5.js").read_text(encoding="utf-8")
    assert "C(e.form)?e.form.reset()" in sdk


def test_s14_3_1_notices_preserve_control_positions_and_lifecycle() -> None:
    """S14.3.1: notices overlay without shifting controls and follow the required lifetime."""
    page = source()
    assert 'data-fs-notice="success"' in page
    assert 'data-fs-notice="form-error"' in page
    assert 'data-fs-notice="field-error"' in page
    assert page.count("data-fs-notice-close") == 3
    assert "SUCCESS_NOTICE_TIMEOUT = 6000" in SCRIPT.read_text(encoding="utf-8")

    success = next(attrs for tag, attrs in elements() if tag == "div" and "data-fs-success" in attrs)
    form_error = next(
        attrs for tag, attrs in elements()
        if tag == "div" and "data-fs-error" in attrs and attrs["data-fs-error"] is None
    )
    assert success == {
        "data-fs-success": None,
        "class": "form-success",
        "role": "status",
        "aria-live": "polite",
        "aria-atomic": "true",
        "tabindex": "-1",
    }
    assert form_error == {
        "data-fs-error": None,
        "class": "form-error",
        "role": "alert",
        "aria-live": "assertive",
        "aria-atomic": "true",
        "tabindex": "-1",
    }

    with _UnsubscribeBrowser() as browser:
        for width in (402, 1536):
            browser.load(width)
            idle = browser.state("idle")
            for state in ("success", "form-error", "field-error", "network-failure"):
                actual = browser.state(state)
                for control in ("field", "button"):
                    for coordinate in ("x", "y", "width", "height"):
                        assert abs(actual[control][coordinate] - idle[control][coordinate]) <= 1, (
                            width, state, control, coordinate, idle, actual
                        )

            browser.state("success")
            assert browser.tools.evaluate(
                "document.querySelector('[data-fs-success]').hasAttribute('data-fs-active')"
            ) is True
            assert browser.tools.evaluate(
                "new Promise(resolve => setTimeout(() => resolve(!document.querySelector('[data-fs-success]').hasAttribute('data-fs-active')), 6100))"
            ) is True

            browser.state("form-error")
            assert browser.tools.evaluate(
                "new Promise(resolve => setTimeout(() => resolve(document.querySelector('[data-fs-error=\"\"]').hasAttribute('data-fs-active')), 100))"
            ) is True
            assert browser.tools.evaluate(
                "document.querySelector('[data-fs-notice=\"form-error\"] [data-fs-notice-close]').click(); !document.querySelector('[data-fs-error=\"\"]').hasAttribute('data-fs-active')"
            ) is True

            browser.state("field-error")
            assert browser.tools.evaluate(
                "document.querySelector('#email').dispatchEvent(new Event('input', { bubbles: true })); !document.querySelector('[data-fs-error=\"email\"]').hasAttribute('data-fs-active')"
            ) is True

            browser.state("network-failure")
            assert browser.tools.evaluate(
                "document.querySelector('#unsubscribe-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); !document.querySelector('[data-fs-error=\"\"]').hasAttribute('data-fs-active')"
            ) is True
        browser.assert_no_remote_requests()


def test_s14_3_1_notices_sit_directly_above_the_form_without_overlap() -> None:
    """S14.3.1: active success and form-error notices occupy only the space above the form."""
    with _UnsubscribeBrowser() as browser:
        for width in (402, 1536):
            browser.load(width)
            for state in ("success", "form-error", "network-failure"):
                measured = browser.state(state)
                notice = measured["notice"]
                assert notice is not None, (width, state, measured)
                assert notice["height"] > 0, (width, state, measured)
                assert 0 <= measured["form"]["y"] - notice["bottom"] <= 13, (width, state, measured)
                for control in ("field", "button"):
                    assert notice["bottom"] <= measured[control]["y"], (width, state, control, measured)
        browser.assert_no_remote_requests()


def test_s14_7_browser_request_guard_is_installed_before_navigation() -> None:
    """S14.7: the shared browser helper intercepts every request before its first page navigation."""
    test_source = Path(__file__).read_text(encoding="utf-8")
    assert '"Fetch.enable"' in test_source
    assert 'patterns=[{"urlPattern": "*"}]' in test_source
    assert test_source.index('"Fetch.enable"') < test_source.index('"Page.navigate"')
    assert '"Fetch.failRequest"' in test_source
    assert "assert not self.tools.remote_requests" in test_source


def test_s14_4_success_copy_confirms_receipt() -> None:
    """S14.4: the success region carries only the approved plain confirmation."""
    success = re.search(r'<div data-fs-success[^>]*>(.*?)</div>', source(), flags=re.S)
    assert success is not None
    assert text_only(success.group(1)) == "You're unsubscribed."


def test_s14_u1_compact_responsive_form_layout() -> None:
    """U1: a compact form shell keeps the labeled controls row-based on laptops and stacked on phones."""
    page = source()
    styles = (ROOT / "css" / "style.css").read_text(encoding="utf-8")

    assert '<section class="section section--light unsubscribe-section">' in page
    assert '<div class="section-inner unsubscribe-form-shell">' in page
    assert 'class="support-form unsubscribe-form"' in page
    assert '<div class="unsubscribe-controls">' in page
    assert page.index('<label for="email">') < page.index('<div class="unsubscribe-controls">')
    assert page.index('<input type="email"') < page.index('>Remove this email</button>')
    assert ".unsubscribe-page main {\n  display: flex;\n  flex: 1;\n}" in styles
    assert ".unsubscribe-section {\n  align-items: center;\n  display: flex;\n  flex: 1;" in styles
    assert ".unsubscribe-form-shell {\n  margin: 0 auto;\n  max-width: 560px;\n  width: 100%;\n}" in styles
    assert ".unsubscribe-controls {\n  align-items: stretch;\n  display: flex;\n  gap: 12px;\n}" in styles
    assert ".unsubscribe-controls input {\n  flex: 1 1 auto;\n  height: 52px;\n  min-width: 0;\n}" in styles
    assert '.unsubscribe-form .unsubscribe-controls button[type="submit"] {\n  align-self: stretch;\n  height: 52px;\n  margin-top: 0;\n  padding: 0 16px;\n  white-space: nowrap;\n}' in styles
    assert "@media (max-width: 640px) {\n  .unsubscribe-section" in styles
    assert ".unsubscribe-controls {\n    flex-direction: column;\n  }" in styles
    assert '.unsubscribe-form .unsubscribe-controls button[type="submit"] {\n    width: 100%;\n  }' in styles


def test_s14_1_s2_4_attribution_and_s2_5_no_donation_ask() -> None:
    """S14.1/S2.4/S2.5: a home link, verbatim attribution, and no donation solicitation."""
    page = source()
    assert 'href="/"' in page
    assert ATTRIBUTION in text_only(page)
    visible = text_only(re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S))
    assert not re.search(r"(?i)\b(donat\w*|contribut\w*|chip in|give now|pledge\w*|fundrais\w*)\b|\$\s?\d", visible)
    assert "/donate" not in page and "winred" not in page.lower()


def test_s14_5_only_local_assets_and_formspree_submission() -> None:
    """S14.5/S2.1.1: executable assets are local and the sole remote form endpoint is Formspree."""
    page = source()
    for src in re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', page):
        assert src.startswith("/js/"), src
        assert (ROOT / src.lstrip("/")).is_file(), src
    actions = re.findall(r'<form\b[^>]*\baction="([^"]+)"', page)
    assert actions == [f"https://formspree.io/f/{FORM_ID}"]
    asset_rels = {"preload", "stylesheet", "icon", "apple-touch-icon", "manifest"}
    for tag, attrs in elements():
        if tag == "img":
            asset = attrs.get("src")
        elif tag == "link" and attrs.get("rel") in asset_rels:
            asset = attrs.get("href")
        else:
            continue
        assert asset is not None
        assert not asset.startswith(("http://", "https://")), asset
