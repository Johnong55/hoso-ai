# scripts/debug_auto.py
"""Auto-run both debug dumps without interactive input."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def dump_procedure_page():
    from playwright.async_api import async_playwright
    url = "https://dichvucong.gov.vn/p/home/dvc-chi-tiet-thu-tuc-hanh-chinh.html?ma_thu_tuc=1.001193"
    print(f"\n{'='*60}")
    print(f"PROCEDURE PAGE: {url}")
    print('='*60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="vi-VN",
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=40000)
        await page.wait_for_timeout(4000)
        html = await page.content()
        await browser.close()

    out_file = "scripts/debug_procedure.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved: {out_file} ({len(html):,} chars)")

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    print("\n── H1 tags ──")
    for el in soup.find_all("h1"):
        print(f"  class={el.get('class')} text={el.get_text(strip=True)[:120]!r}")

    print("\n── H2 tags ──")
    for el in soup.find_all("h2"):
        print(f"  class={el.get('class')} text={el.get_text(strip=True)[:120]!r}")

    print("\n── H3 tags ──")
    for el in soup.find_all("h3"):
        print(f"  class={el.get('class')} text={el.get_text(strip=True)[:120]!r}")

    print("\n── Elements with class containing 'title' or 'name' ──")
    for el in soup.find_all(class_=True):
        classes = " ".join(el.get("class", []))
        if any(k in classes for k in ["title", "name", "tthc", "procedure", "detail"]):
            text = el.get_text(strip=True)[:80]
            if text and el.name not in ["script", "style", "html", "body", "div"] or el.name == "div":
                if text:
                    print(f"  <{el.name} class='{classes}'> {text!r}")

    print("\n── Tables (first row preview) ──")
    for i, table in enumerate(soup.find_all("table")):
        first_row = table.find("tr")
        if first_row:
            print(f"  Table[{i}] classes={table.get('class')} → {first_row.get_text(' ', strip=True)[:100]!r}")

    print("\n── Divs/sections containing 'Lĩnh vực' or 'Cơ quan' ──")
    for el in soup.find_all(string=lambda t: t and any(k in t for k in ["Lĩnh vực", "Cơ quan", "Thời hạn", "Lệ phí", "Trình tự"])):
        parent = el.parent
        if parent and parent.name not in ["script", "style"]:
            gp = parent.parent
            print(f"  <{parent.name} class={parent.get('class')}> in <{gp.name if gp else '?'} class={gp.get('class') if gp else '?'}> → {el.strip()[:80]!r}")

    print("\n── Body classes ──")
    body = soup.find("body")
    if body:
        print(f"  body class={body.get('class')}")

    print("\n── Main container divs (top-level) ──")
    main_divs = soup.select("body > div, body > main, body > section")
    for d in main_divs[:10]:
        print(f"  <{d.name} class={d.get('class')} id={d.get('id')}> children={len(d.find_all(recursive=False))}")


async def dump_home_page():
    from playwright.async_api import async_playwright
    url = "https://dichvucong.gov.vn/p/home/dvc-trang-chu-cong-dan.html"
    print(f"\n{'='*60}")
    print(f"HOME PAGE: {url}")
    print('='*60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(locale="vi-VN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=40000)
        await page.wait_for_timeout(4000)
        html = await page.content()
        await browser.close()

    out_file = "scripts/debug_home.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved: {out_file} ({len(html):,} chars)")

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    print("\n── All links containing 'group=' ──")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "group=" in href:
            print(f"  <a class={a.get('class')} href={href!r}> text={a.get_text(strip=True)[:50]!r}")
            # show parent chain
            p = a.parent
            for _ in range(3):
                if p:
                    print(f"    ↑ <{p.name} class={p.get('class')} id={p.get('id')}>")
                    p = p.parent

    print("\n── Elements with class containing 'group' or 'targetgroup' ──")
    for el in soup.find_all(class_=True):
        classes = " ".join(el.get("class", []))
        if any(k in classes for k in ["group", "targetgroup", "wrap"]):
            children_a = len(el.find_all("a"))
            print(f"  <{el.name} class='{classes}'> anchors={children_a} text={el.get_text(strip=True)[:60]!r}")

    print("\n── Body > div structure ──")
    body = soup.find("body")
    if body:
        for d in body.find_all(recursive=False)[:10]:
            print(f"  <{d.name} class={d.get('class')} id={d.get('id')}>")
            for child in d.find_all(recursive=False)[:5]:
                print(f"    <{child.name} class={child.get('class')} id={child.get('id')}>")


async def main():
    await dump_procedure_page()
    await dump_home_page()


if __name__ == "__main__":
    asyncio.run(main())
