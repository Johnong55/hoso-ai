# scripts/debug_html.py
"""
Dump HTML thật của trang để xác định đúng CSS selectors.
Usage: python scripts/debug_html.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def dump_procedure_page():
    from playwright.async_api import async_playwright

    url = "https://dichvucong.gov.vn/p/home/dvc-chi-tiet-thu-tuc-hanh-chinh.html?ma_thu_tuc=1.001193"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="vi-VN",
        )
        page = await context.new_page()

        print(f"Đang tải: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Chờ thêm để JS render xong
        await page.wait_for_timeout(3000)

        html = await page.content()
        await browser.close()

    # Lưu HTML ra file
    out_file = "scripts/debug_procedure.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Đã lưu HTML vào: {out_file} ({len(html):,} bytes)")

    # In ra các tag h1, h2 và class quan trọng
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    print("\n── H1 tags ──────────────────────────────")
    for el in soup.find_all("h1"):
        print(f"  class={el.get('class')} → {el.get_text(strip=True)[:100]}")

    print("\n── H2 tags ──────────────────────────────")
    for el in soup.find_all("h2"):
        print(f"  class={el.get('class')} → {el.get_text(strip=True)[:100]}")

    print("\n── Divs có class 'title' ─────────────────")
    for el in soup.find_all(class_=lambda c: c and "title" in " ".join(c)):
        text = el.get_text(strip=True)[:80]
        if text:
            print(f"  <{el.name} class={el.get('class')}> → {text}")

    print("\n── Tables ───────────────────────────────")
    for i, table in enumerate(soup.find_all("table")):
        first_row = table.find("tr")
        if first_row:
            print(f"  Table[{i}] → {first_row.get_text(' ', strip=True)[:100]}")

    print("\n── Tìm text 'thủ tục' ──────────────────")
    for el in soup.find_all(string=lambda t: t and "thủ tục" in t.lower()):
        parent = el.parent
        if parent and parent.name not in ["script", "style"]:
            print(f"  <{parent.name} class={parent.get('class')}> → {el.strip()[:100]}")


async def dump_group_page():
    from playwright.async_api import async_playwright

    url = "https://dichvucong.gov.vn/p/home/dvc-trang-chu-cong-dan.html"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(locale="vi-VN")
        page = await context.new_page()

        print(f"Đang tải: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        html = await page.content()
        await browser.close()

    out_file = "scripts/debug_home.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Đã lưu HTML vào: {out_file} ({len(html):,} bytes)")

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    print("\n── Links chứa 'group=' ──────────────────")
    for a in soup.find_all("a", href=True):
        if "group=" in a.get("href", ""):
            print(f"  class={a.get('class')} href={a['href']} → {a.get_text(strip=True)[:50]}")

    print("\n── Divs có class 'targetgroup' ──────────")
    for el in soup.find_all(class_=lambda c: c and any("targetgroup" in x for x in c)):
        print(f"  <{el.name} class={el.get('class')}> children={len(el.find_all('a'))}")


if __name__ == "__main__":
    print("Chọn trang cần debug:")
    print("  1 - Trang chi tiết thủ tục")
    print("  2 - Trang chủ công dân (tìm group URLs)")
    choice = input("Nhập (1/2): ").strip()

    if choice == "2":
        asyncio.run(dump_group_page())
    else:
        asyncio.run(dump_procedure_page())
