# app/crawler/sources/dvcqg.py
"""
Crawler 3 cấp cho dichvucong.gov.vn:
  Cấp 1: Trang chủ (dvc-trang-chu.html) → danh sách group URLs
  Cấp 2: Nhóm → click từng event → thu thập procedure URLs
  Cấp 3: Procedure detail → parse nội dung đầy đủ

HTML thực tế (đã xác nhận):
  - Groups: .targetgroup-body .item a.wrap[href] (trên trang chủ CHÍNH)
  - Events: li.item .title[onclick] (trong trang nhóm)
  - Procedures sau AJAX: ul.list-document li a[href] + ul#procedures_body li a[href]
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

# Trang chủ CHÍNH — nơi có cả 2 khối công dân + doanh nghiệp
MAIN_HOME = f"{PAGE_BASE}/dvc-trang-chu.html"

# Giữ lại để tương thích (test scripts dùng)
CITIZEN_HOME    = MAIN_HOME
ENTERPRISE_HOME = MAIN_HOME


class DVCQGCrawler:
    """
    Crawl toàn bộ thủ tục từ dichvucong.gov.vn theo cấu trúc 3 cấp.
    Có change detection (SHA256), random delay, retry.
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

                # Trang chủ chính chứa cả groups công dân lẫn doanh nghiệp
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
                page = await context.new_page()

                for attempt in range(settings.CRAWLER_MAX_RETRIES):
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=settings.CRAWLER_TIMEOUT * 1000)
                        # Chờ thêm để JS render xong
                        await page.wait_for_timeout(2000)
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
                        logger.warning(f"Crawler | fetch error | url={url} | attempt={attempt+1} | {exc}")
                        await asyncio.sleep(2 ** attempt)

                logger.error(f"Crawler | all retries failed | url={url}")
                return None
            finally:
                await browser.close()

    # ── Cấp 1: Trang chủ → group URLs ────────────────────────────────────────

    async def _get_group_urls(self, browser: Browser, home_url: str) -> list[str]:
        """
        Parse trang chủ CHÍNH lấy tất cả group URLs.

        HTML thực tế (page1.html – dvc-trang-chu.html):
          <div class="targetgroup-body">
            <div class="item">
              <a class="wrap" href="/p/home/dvc-chi-tiet-nhom-su-kien-cho-cong-dan.html?group=750">
              ...
        """
        context = await self._new_context(browser)
        page = await context.new_page()
        try:
            await page.goto(home_url, wait_until="networkidle", timeout=settings.CRAWLER_TIMEOUT * 1000)
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
        """
        Vào trang nhóm (vd: group=750 "Có con nhỏ"):
        - Click từng sự kiện để trigger getProcedureByEvent(N) → AJAX load
        - Click "Xem thêm" nếu hiện → actionViewMore modal
        - Thu thập tất cả procedure URLs từ ul.list-document + ul#procedures_body

        HTML thực tế (page2.html):
          <ul class="list-expand">
            <li class="item">
              <div class="title" onclick="getProcedureByEvent(253)">Cư trú</div>
              <div class="content">
                <ul class="list-document" id="event_253"></ul>   ← AJAX populated
                <a id="eventViewMore_253" style="display:none" onclick="actionViewMore(253,...)">Xem thêm</a>
              </div>
            </li>
          </ul>
        """
        context = await self._new_context(browser)
        page = await context.new_page()
        urls: set[str] = set()

        try:
            await page.goto(group_url, wait_until="networkidle", timeout=settings.CRAWLER_TIMEOUT * 1000)
            await page.wait_for_timeout(1500)

            # Click tất cả các sự kiện để trigger AJAX
            event_titles = await page.query_selector_all("li.item .title[onclick]")
            logger.debug(f"Crawler | group={group_url.split('group=')[-1]} | events={len(event_titles)}")

            for title in event_titles:
                try:
                    await title.click()
                    # Chờ AJAX hoàn thành (networkidle hoặc timeout ngắn)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        await page.wait_for_timeout(1500)
                except Exception as e:
                    logger.debug(f"Crawler | event click error: {e}")

            # Thu thập từ ul.list-document (main list)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            self._collect_procedure_links(soup, "ul.list-document li a[href]", urls)

            # Click "Xem thêm" cho từng sự kiện nếu button hiện
            view_more_btns = await page.query_selector_all("a[id^='eventViewMore_']")
            for btn in view_more_btns:
                try:
                    is_visible = await btn.is_visible()
                    if is_visible:
                        await btn.click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            await page.wait_for_timeout(1500)

                        # Thu thập từ modal ul#procedures_body
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

    def _collect_procedure_links(self, soup: BeautifulSoup, selector: str, urls: set) -> None:
        """Thu thập procedure URLs từ một BeautifulSoup + CSS selector."""
        for a in soup.select(selector):
            href = a.get("href", "")
            if "ma_thu_tuc=" in href:
                full = self._full_url(href)
                urls.add(full)

    def _full_url(self, href: str) -> str:
        """Chuyển href tương đối thành URL đầy đủ."""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"{BASE_URL}{href}"
        # Relative path như "dvc-chi-tiet-thu-tuc-hanh-chinh.html?ma_thu_tuc=..."
        return f"{PAGE_BASE}/{href}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _launch(self, playwright) -> Browser:
        return await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )

    async def _new_context(self, browser: Browser) -> BrowserContext:
        return await browser.new_context(
            user_agent=self._random_ua(),
            locale="vi-VN",
            viewport={"width": 1280, "height": 800},
        )

    def _random_ua(self) -> str:
        return random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        ])

    async def _random_delay(self) -> None:
        delay = random.uniform(settings.CRAWLER_DELAY_MIN, settings.CRAWLER_DELAY_MAX)
        await asyncio.sleep(delay)
