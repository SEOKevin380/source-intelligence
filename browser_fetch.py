"""
Browser-based fetching for JS-rendered product pages.

Optional dependency — all callers degrade gracefully when Playwright is not installed.
Uses Playwright's sync API to render pages in headless Chromium, then returns the
fully-rendered HTML. Includes anti-detection stealth patches and resource blocking
for speed.

Usage:
    from browser_fetch import BrowserSession, PLAYWRIGHT_AVAILABLE, is_content_thin

    if PLAYWRIGHT_AVAILABLE:
        with BrowserSession() as session:
            html = session.fetch("https://example.com")
"""

import re

# ============================================================================
# AVAILABILITY CHECK
# ============================================================================

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception as _pw_err:
    import sys
    print(f"[browser_fetch] Playwright import failed: {type(_pw_err).__name__}: {_pw_err}", file=sys.stderr)


# ============================================================================
# THIN CONTENT DETECTION
# ============================================================================

def is_content_thin(html, min_text_chars=800):
    """Detect if fetched HTML is a JS shell that needs browser rendering.

    Returns True if the content is likely a JS-rendered page that urllib
    couldn't fully render. Conservative — only triggers Playwright when
    it's highly likely the page needs it.

    Signals checked:
    1. Very little visible text after stripping tags (< min_text_chars)
    2. JS framework loading indicators (React, Vue, Next.js, Angular)
    3. High script-to-content ratio with low visible text
    """
    if not html:
        return True

    # Strip scripts and styles, then all tags to get visible text
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Signal 1: Very little visible text
    if len(text) < min_text_chars:
        return True

    # Signal 2: JS framework shell markers + low text
    js_shell_markers = [
        'id="__next"',             # Next.js
        'id="app"',                # Vue
        'id="root"',               # React
        '<noscript>',              # JS-required notice
        'window.__NUXT__',         # Nuxt
        'data-reactroot',          # React
        'ng-app',                  # Angular
        'enable javascript',       # Explicit JS requirement
        'javascript is required',
        'please enable javascript',
        'you need to enable javascript',
    ]
    html_lower = html.lower()
    shell_hits = sum(1 for m in js_shell_markers if m.lower() in html_lower)

    if shell_hits >= 2 and len(text) < 2000:
        return True

    # Signal 3: Mostly script content with almost no visible text
    script_bytes = sum(len(m) for m in re.findall(
        r'<script[^>]*>.*?</script>', html, flags=re.DOTALL | re.IGNORECASE
    ))
    if script_bytes > 0 and len(text) < 1000 and script_bytes / max(len(html), 1) > 0.6:
        return True

    return False


# ============================================================================
# BROWSER SESSION
# ============================================================================

class BrowserSession:
    """Manages a single Playwright browser instance for reuse across fetches.

    Usage:
        with BrowserSession() as session:
            if session.available:
                html = session.fetch("https://example.com")
                html2 = session.fetch("https://example.com/page2")
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self):
        if not PLAYWRIGHT_AVAILABLE:
            return self
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-gpu',
                ]
            )
            self._context = self._browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/126.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 800},
                locale='en-US',
                timezone_id='America/New_York',
            )
            self._apply_stealth()
        except Exception as e:
            self._log(f"Browser launch failed: {e}")
            self._cleanup()
        return self

    def __exit__(self, *args):
        self._cleanup()

    @property
    def available(self):
        """True if browser context was created successfully."""
        return self._context is not None

    def fetch(self, url, max_bytes=120000, wait_until='networkidle',
              timeout_ms=25000):
        """Fetch a URL using a real browser. Returns HTML string or empty string."""
        if not self.available:
            return ""

        # Validate URL before browser navigation (SSRF protection)
        try:
            from net import validate_url
            validate_url(url)
        except ValueError as e:
            self._log(f"URL blocked: {e}")
            return ""

        page = None
        try:
            page = self._context.new_page()

            # Block unnecessary resources + validate all outbound URLs (SSRF)
            page.route('**/*', self._safe_route_handler)

            page.goto(url, wait_until=wait_until, timeout=timeout_ms)

            # Extra wait for late-loading JS content
            page.wait_for_timeout(2000)

            # Scroll to bottom to trigger lazy-loaded content (ingredients,
            # pricing, testimonials often sit below VSL videos)
            self._scroll_to_bottom(page)

            # Try to expand common accordion/tab patterns
            self._expand_hidden_content(page)

            html = page.content()
            page.close()
            page = None

            # Truncate to max_bytes
            encoded = html.encode('utf-8', errors='ignore')[:max_bytes]
            return encoded.decode('utf-8', errors='ignore')

        except Exception as e:
            self._log(f"Browser fetch failed for {url}: {e}")
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            return ""

    def _apply_stealth(self):
        """Apply anti-detection patches to the browser context."""
        if not self._context:
            return
        self._context.add_init_script("""
            // Hide webdriver flag
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Fake plugins (empty = headless signal)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            // Language consistency
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            // Chrome runtime object
            window.chrome = {runtime: {}};
            // Permissions API
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : originalQuery(parameters);
        """)

    @staticmethod
    def _safe_route_handler(route):
        """Block unnecessary resources AND validate all outbound URLs.

        Every request the browser makes — including redirects, XHR, fetch() —
        is validated against SSRF rules before being sent. This prevents
        a malicious page from loading subresources from internal/private IPs.
        """
        resource_type = route.request.resource_type
        if resource_type in ('image', 'media', 'font'):
            route.abort()
            return
        try:
            from net import validate_url
            validate_url(route.request.url)
            route.continue_()
        except ValueError:
            route.abort()

    @staticmethod
    def _route_handler(route):
        """Block unnecessary resources for faster page loads. (Legacy, unused)"""
        resource_type = route.request.resource_type
        if resource_type in ('image', 'media', 'font'):
            route.abort()
        else:
            route.continue_()

    @staticmethod
    def _scroll_to_bottom(page):
        """Incrementally scroll the page to trigger lazy-loaded content.

        Many landing pages (especially BuyGoods/ClickBank) load ingredients,
        pricing tables, and testimonials only when scrolled into view. This
        scrolls in steps (like a human) to trigger intersection observers
        and lazy-load handlers.
        """
        try:
            page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const height = () => document.body.scrollHeight;
                    let prev = 0;
                    let curr = height();
                    // Scroll in 600px steps, pause for lazy loaders
                    for (let y = 0; y < curr; y += 600) {
                        window.scrollTo(0, y);
                        await delay(150);
                        curr = height();  // page may grow as content loads
                    }
                    // Final scroll to absolute bottom
                    window.scrollTo(0, height());
                    await delay(500);
                    // If page grew, do one more pass
                    if (height() > curr) {
                        window.scrollTo(0, height());
                        await delay(500);
                    }
                    // Scroll back to top (some pages hide nav on scroll-down)
                    window.scrollTo(0, 0);
                }
            """)
            # Wait for any final content to render after scrolling
            page.wait_for_timeout(1000)
        except Exception:
            pass  # Non-critical — don't fail the fetch

    @staticmethod
    def _expand_hidden_content(page):
        """Click common accordion/tab elements to reveal hidden content.

        Many product pages hide ingredient lists, FAQs, and policy text
        behind accordions or tabs. This tries to expand them.
        """
        try:
            # Common accordion selectors
            selectors = [
                'button[aria-expanded="false"]',
                '.accordion-header:not(.active)',
                '.collapsible-trigger:not(.is-open)',
                '[data-toggle="collapse"]:not(.collapsed)',
                '.faq-question',
                'details:not([open])',
            ]
            for selector in selectors:
                elements = page.query_selector_all(selector)
                for el in elements[:10]:  # Cap at 10 to avoid infinite loops
                    try:
                        el.click(timeout=500)
                    except Exception:
                        pass
            # Brief wait for accordion content to render
            if any(page.query_selector_all(s) for s in selectors):
                page.wait_for_timeout(500)
        except Exception:
            pass  # Non-critical — don't fail the fetch

    def _cleanup(self):
        """Close all browser resources."""
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None
        self._context = None

    @staticmethod
    def _log(message):
        """Log via research_product._emit if available, else print."""
        try:
            from research_product import _emit
            _emit(f"  [!] {message}")
        except ImportError:
            print(f"  [!] {message}")  # noqa: T201
