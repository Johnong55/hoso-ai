# scripts/test_crawl.py
"""
Chạy thử crawler để kiểm tra trước khi deploy.
Usage:
    python scripts/test_crawl.py
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.crawler.sources.dvcqg import DVCQGCrawler


async def test_single_procedure():
    """Test parse 1 thủ tục cụ thể."""
    url = "https://dichvucong.gov.vn/p/home/dvc-chi-tiet-thu-tuc-hanh-chinh.html?ma_thu_tuc=1.001193"
    print(f"\n{'='*60}")
    print(f"Test fetch: {url}")
    print('='*60)

    crawler = DVCQGCrawler()
    result = await crawler.fetch_procedure(url)

    if result:
        print(f"✅ Tên:              {result.get('name')}")
        print(f"   Mã:               {result.get('code')}")
        print(f"   Lĩnh vực:         {result.get('domain')}")
        print(f"   Cấp:              {result.get('authority_level')}")
        print(f"   Cơ quan:          {result.get('implementing_agency')}")
        print(f"   Thời gian:        {result.get('processing_time')}")
        print(f"   Lệ phí:           {result.get('fee')}")
        print(f"   Hồ sơ:            {len(result.get('requirements', []))} loại giấy tờ")
        print(f"   Bước thực hiện:   {len(result.get('steps', []))} bước")
        print(f"   Hash:             {result.get('content_hash', '')[:16]}...")
        print(f"\n   Hồ sơ chi tiết:")
        for r in result.get("requirements", [])[:3]:
            print(f"     - [{r.get('order')}] {r.get('name')}")
        print(f"\n   Các bước:")
        for s in result.get("steps", [])[:3]:
            print(f"     - Bước {s.get('order')}: {s.get('title')}")
    else:
        print("❌ Không parse được — xem log phía trên")


async def test_discover_groups():
    """Test lấy danh sách group URLs từ trang chủ."""
    print(f"\n{'='*60}")
    print("Test discover group URLs từ trang chủ công dân")
    print('='*60)

    from app.crawler.sources.dvcqg import DVCQGCrawler, CITIZEN_HOME
    from playwright.async_api import async_playwright

    crawler = DVCQGCrawler()
    async with async_playwright() as p:
        browser = await crawler._launch(p)
        group_urls = await crawler._get_group_urls(browser, CITIZEN_HOME)
        await browser.close()

    print(f"✅ Tìm thấy {len(group_urls)} nhóm:")
    for url in group_urls:
        group_id = url.split("group=")[-1]
        print(f"   group={group_id} → {url}")


async def test_discover_procedures_in_group():
    """Test lấy procedure URLs trong nhóm 'Có con nhỏ' (group=750)."""
    group_url = "https://dichvucong.gov.vn/p/home/dvc-chi-tiet-nhom-su-kien-cho-cong-dan.html?group=750"
    print(f"\n{'='*60}")
    print(f"Test discover procedures trong group=750 (Có con nhỏ)")
    print('='*60)

    from playwright.async_api import async_playwright
    crawler = DVCQGCrawler()

    async with async_playwright() as p:
        browser = await crawler._launch(p)
        urls = await crawler._get_procedure_urls_from_group(browser, group_url)
        await browser.close()

    print(f"✅ Tìm thấy {len(urls)} thủ tục:")
    for url in urls[:10]:
        ma = url.split("ma_thu_tuc=")[-1]
        print(f"   ma_thu_tuc={ma}")
    if len(urls) > 10:
        print(f"   ... và {len(urls) - 10} thủ tục khác")


if __name__ == "__main__":
    print("Chọn test:")
    print("  1 - Parse 1 thủ tục (đăng ký khai sinh)")
    print("  2 - Lấy danh sách nhóm từ trang chủ")
    print("  3 - Lấy thủ tục trong nhóm 'Có con nhỏ'")

    choice = input("Nhập số (1/2/3): ").strip()

    if choice == "1":
        asyncio.run(test_single_procedure())
    elif choice == "2":
        asyncio.run(test_discover_groups())
    elif choice == "3":
        asyncio.run(test_discover_procedures_in_group())
    else:
        asyncio.run(test_single_procedure())
