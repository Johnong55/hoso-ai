# app/crawler/sources/dvcqg.py
"""
Crawler 3 cấp cho dichvucong.gov.vn:
  Cấp 1: Trang chủ (dvc-trang-chu.html) → danh sách group URLs
  Cấp 2: Nhóm → click từng event → thu thập procedure URLs
  Cấp 3: Procedure detail → parse nội dung đầy đủ
"""
import asyncio
import hashlib
import random
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.core.config import settings


BASE_URL = "https://dichvucong.gov.vn"
PAGE_BASE = f"{BASE_URL}/p/home"
MAIN_HOME = f"{PAGE_BASE}/dvc-trang-chu.html"
CITIZEN_HOME    = MAIN_HOME
ENTERPRISE_HOME = MAIN_HOME

# Headers giả lập browser thật
EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class DVCQGCrawler:
    """
    Crawl toàn bộ thủ tục từ dichvucong.gov.vn theo cấu trúc 3 cấp.
    Có change detection (SHA256), random delay, retry, anti-bot headers.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    async def discover_all_procedure_urls(self) -> list[str]:
        """
        Bước 1: Quét trang chủ → nhóm → sự kiện → toàn bộ procedure URLs.
        Trả về list URL duy nhất (deduplicated).
        """
        async with async_playwright() as p:
            browser = await self._launch(p)
            try:
                urls: set[str] = set()
                group_urls = await self._get_group_urls(browser, MAIN_HOME)
                logger.info(f"Crawler | found {len(group_urls)} groups | source={MAIN_HOME}")

                for group_url in group_urls:
                    await self._random_delay()
                    proc_urls = await self._get_procedure_urls_from_group(browser, group_url)
                    urls.update(proc_urls)
                    logger.info(
                        f"Crawler | group={group_url.split('group=')[-1]} "
                        f"| procedures={len(proc_urls)}"
                    )

                logger.info(f"Crawler | total unique procedure URLs = {len(urls)}")
                return list(urls)
            finally:
                await browser.close()

    async def fetch_procedure(self, url: str) -> dict[str, Any] | None:
        """
        Bước 2: Lấy và parse chi tiết 1 thủ tục.
        Trả về dict đã parse + content_hash, hoặc None nếu thất bại.
        """
        async with async_playwright() as p:
            browser = await self._launch(p)
            try:
                context = await self._new_context(browser)
                # Override navigator.webdriver để tránh detect
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await context.new_page()

                for attempt in range(settings.CRAWLER_MAX_RETRIES):
                    try:
                        # Dùng "load" thay vì "networkidle" — tránh timeout trên trang AJAX
                        await page.goto(
                            url,
                            wait_until="load",
                            timeout=settings.CRAWLER_TIMEOUT * 1000,
                        )
                        # Chờ thêm để JS render xong
                        await page.wait_for_timeout(3000)

                        html = await page.content()
                        content_hash = hashlib.sha256(html.encode()).hexdigest()

                        from app.crawler.parsers.dvcqg_parser import DVCQGParser
                        parsed = DVCQGParser().parse(html, url)
                        if parsed:
                            parsed["content_hash"] = content_hash
                            parsed["source_url"] = url
                            return parsed

                        logger.warning(f"Crawler | parse returned None | url={url} | attempt={attempt+1}")

                    except Exception as exc:
                        wait = 2 ** attempt + random.uniform(0, 1)
                        logger.warning(
                            f"Crawler | fetch error | url={url} | attempt={attempt+1} | {exc}"
                        )
                        await asyncio.sleep(wait)

                logger.error(f"Crawler | all retries failed | url={url}")
                return None
            finally:
                await browser.close()

    async def fetch_procedures_batch(self, urls: list[str]) -> list[dict[str, Any] | None]:
        """
        Crawl nhiều thủ tục dùng 1 browser duy nhất — nhanh hơn fetch_procedure() nhiều lần.
        """
        results = []
        async with async_playwright() as p:
            browser = await self._launch(p)
            try:
                context = await self._new_context(browser)
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                for url in urls:
                    page = await context.new_page()
                    result = await self._fetch_one(page, url)
                    results.append(result)
                    await page.close()
                    await self._random_delay()
            finally:
                await browser.close()
        return results

    # ── Cấp 1: Trang chủ → group URLs ────────────────────────────────────────

    async def _get_group_urls(self, browser: Browser, home_url: str) -> list[str]:
        context = await self._new_context(browser)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        try:
            await page.goto(home_url, wait_until="load", timeout=settings.CRAWLER_TIMEOUT * 1000)
            await page.wait_for_timeout(2000)
            html = await page.content()
        finally:
            await context.close()

        soup = BeautifulSoup(html, "lxml")
        urls = []
        for a in soup.select(".targetgroup-body .item a.wrap[href]"):
            href = a["href"]
            if "group=" in href:
                full = self._full_url(href)
                urls.append(full)

        logger.debug(f"Crawler | _get_group_urls | found {len(urls)} | home={home_url}")
        return list(set(urls))

    # ── Cấp 2: Group → event list → procedure URLs ────────────────────────────

    async def _get_procedure_urls_from_group(self, browser: Browser, group_url: str) -> list[str]:
        context = await self._new_context(browser)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        urls: set[str] = set()

        try:
            await page.goto(group_url, wait_until="load", timeout=settings.CRAWLER_TIMEOUT * 1000)
            await page.wait_for_timeout(1500)

            event_titles = await page.query_selector_all("li.item .title[onclick]")
            logger.debug(f"Crawler | group={group_url.split('group=')[-1]} | events={len(event_titles)}")

            for title in event_titles:
                try:
                    await title.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        await page.wait_for_timeout(1500)
                except Exception as e:
                    logger.debug(f"Crawler | event click error: {e}")

            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            self._collect_procedure_links(soup, "ul.list-document li a[href]", urls)

            view_more_btns = await page.query_selector_all("a[id^='eventViewMore_']")
            for btn in view_more_btns:
                try:
                    is_visible = await btn.is_visible()
                    if is_visible:
                        await btn.click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=4000)
                        except Exception:
                            await page.wait_for_timeout(1500)
                        modal_html = await page.content()
                        modal_soup = BeautifulSoup(modal_html, "lxml")
                        self._collect_procedure_links(modal_soup, "#procedures_body li a[href]", urls)
                except Exception as e:
                    logger.debug(f"Crawler | viewmore error: {e}")

        except Exception as exc:
            logger.error(f"Crawler | group page failed | url={group_url} | {exc}")
        finally:
            await context.close()

        return list(urls)

    # ── Cấp 3: Fetch 1 procedure (dùng page đã mở sẵn) ───────────────────────

    async def _fetch_one(self, page: Page, url: str) -> dict[str, Any] | None:
        for attempt in range(settings.CRAWLER_MAX_RETRIES):
            try:
                await page.goto(url, wait_until="load", timeout=settings.CRAWLER_TIMEOUT * 1000)
                await page.wait_for_timeout(3000)
                html = await page.content()
                content_hash = hashlib.sha256(html.encode()).hexdigest()

                from app.crawler.parsers.dvcqg_parser import DVCQGParser
                parsed = DVCQGParser().parse(html, url)
                if parsed:
                    parsed["content_hash"] = content_hash
                    parsed["source_url"] = url
                    return parsed

                logger.warning(f"Crawler | parse returned None | url={url} | attempt={attempt+1}")
            except Exception as exc:
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"Crawler | fetch error | url={url} | attempt={attempt+1} | {exc}")
                await asyncio.sleep(wait)

        logger.error(f"Crawler | all retries failed | url={url}")
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _collect_procedure_links(self, soup: BeautifulSoup, selector: str, urls: set) -> None:
        for a in soup.select(selector):
            href = a.get("href", "")
            if "ma_thu_tuc=" in href:
                full = self._full_url(href)
                urls.add(full)

    def _full_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"{BASE_URL}{href}"
        return f"{PAGE_BASE}/{href}"

    async def _launch(self, playwright) -> Browser:
        return await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",  # tránh detect
                "--disable-infobars",
                "--disable-extensions",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )

    async def _new_context(self, browser: Browser) -> BrowserContext:
        return await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            viewport={"width": 1280, "height": 800},
            extra_http_headers=EXTRA_HEADERS,
            java_script_enabled=True,
            # Giả lập browser thật hơn
            color_scheme="light",
            device_scale_factor=1,
        )

    async def _random_delay(self) -> None:
        delay = random.uniform(settings.CRAWLER_DELAY_MIN, settings.CRAWLER_DELAY_MAX)
        await asyncio.sleep(delay)
