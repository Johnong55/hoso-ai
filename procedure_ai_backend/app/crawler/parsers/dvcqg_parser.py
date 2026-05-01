# app/crawler/parsers/dvcqg_parser.py
"""
Parser cho trang chi tiết thủ tục hành chính của dichvucong.gov.vn.

Hỗ trợ 2 loại URL:
  - /dvc-chi-tiet-thu-tuc-hanh-chinh.html?ma_thu_tuc=1.001193
  - /dvc-chi-tiet-thu-tuc-nganh-doc.html?ma_thu_tuc=1.004222

HTML thực tế (đã xác nhận từ page3.html):
  - Tên:    <h1 class="main-title -none">Đăng ký thường trú</h1>  (h1 thứ 2 có text)
  - Section: <h2 class="main-title-sub">Trình tự thực hiện</h2>
  - Bước:   div.list-expand > div.item.active > div.content > p  (text "Bước 1: ... Bước 2:...")
  - Cách thức + Thời hạn + Phí:
      <table class="table-result-tthc table-result">
        <td data-title="Thời hạn giải quyết">07 Ngày làm việc</td>
        <td data-title="Phí, lệ phí">...</td>
  - Hồ sơ:  h2 "Thành phần hồ sơ" → div.list-expand div.item div.content table.table-result
      <td data-title="Tên giấy tờ">...</td>
      <td data-title="Số lượng">...</td>
  - Cơ quan: h2 "Cơ quan thực hiện" → div.article p
"""
import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from loguru import logger


class DVCQGParser:
    """
    Parse trang chi tiết 1 thủ tục → dict chuẩn để đưa vào ProcedureChunker.
    """

    def parse(self, html: str, source_url: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "lxml")

        name = self._get_procedure_name(soup)
        if not name:
            logger.warning(f"Parser | cannot find procedure name | url={source_url}")
            return None

        code = self._extract_ma_thu_tuc(source_url)

        return {
            "code":                 code or self._slug_code(name),
            "name":                 name,
            "domain":               self._get_section_text(soup, ["Lĩnh vực"]),
            "authority_level":      self._map_authority(self._get_section_text(soup, ["Cấp thực hiện"])),
            "implementing_agency":  self._get_implementing_agency(soup),
            "coordinating_agency":  self._get_section_text(soup, ["Cơ quan phối hợp", "Cơ quan có thẩm quyền"]),
            "processing_time":      self._get_processing_time(soup),
            "fee":                  self._get_fee(soup),
            "result":               self._get_section_text(soup, ["Kết quả thực hiện"]),
            "legal_basis":          self._get_section_text(soup, ["Căn cứ pháp lý", "Cơ sở pháp lý"]),
            "description":          self._get_section_text(soup, ["Yêu cầu, điều kiện thực hiện", "Điều kiện thực hiện"]),
            "requirements":         self._get_requirements(soup),
            "steps":                self._get_steps(soup),
        }

    # ── Tên thủ tục ───────────────────────────────────────────────────────────

    def _get_procedure_name(self, soup: BeautifulSoup) -> str | None:
        """
        Trang chi tiết có 2 thẻ h1.main-title, thẻ đầu tiên rỗng, thẻ thứ hai có tên.
        Fallback thêm các selector khác.
        """
        # Ưu tiên: lấy h1 có text (bỏ qua h1 rỗng)
        for h1 in soup.find_all("h1"):
            text = h1.get_text(strip=True)
            if len(text) > 5:
                return text

        # Fallback selectors
        for sel in [
            "h1.main-title",
            "h1.title-detail",
            ".procedure-detail h1",
            ".detail-title h1",
        ]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if len(text) > 5:
                    return text

        return None

    # ── Tìm section header ────────────────────────────────────────────────────

    def _find_h2_section(self, soup: BeautifulSoup, labels: list[str]) -> Tag | None:
        """
        Tìm <h2 class="main-title-sub"> chứa một trong các label.
        """
        for h2 in soup.find_all("h2", class_="main-title-sub"):
            h2_text = h2.get_text(strip=True).lower()
            if any(label.lower() in h2_text for label in labels):
                return h2
        return None

    def _get_next_content(self, h2: Tag) -> Tag | None:
        """Lấy element anh em tiếp theo của h2 (thường là div hoặc table)."""
        return h2.find_next_sibling(["div", "table", "p", "ul", "ol"])

    # ── Lấy text của section ──────────────────────────────────────────────────

    def _get_section_text(self, soup: BeautifulSoup, labels: list[str]) -> str | None:
        """
        Tìm h2.main-title-sub chứa label → lấy text từ element kế tiếp.
        """
        h2 = self._find_h2_section(soup, labels)
        if not h2:
            return None
        sib = self._get_next_content(h2)
        if sib:
            text = sib.get_text(" ", strip=True)
            return text[:1000] if text else None
        return None

    # ── Cơ quan thực hiện ────────────────────────────────────────────────────

    def _get_implementing_agency(self, soup: BeautifulSoup) -> str | None:
        """
        h2 "Cơ quan thực hiện" → div.article p
        """
        h2 = self._find_h2_section(soup, ["Cơ quan thực hiện"])
        if not h2:
            return None
        sib = self._get_next_content(h2)
        if sib:
            # Lấy từ div.article p hoặc trực tiếp
            p = sib.find("p")
            if p:
                return p.get_text(" ", strip=True)
            return sib.get_text(" ", strip=True) or None
        return None

    # ── Thời hạn giải quyết ──────────────────────────────────────────────────

    def _get_processing_time(self, soup: BeautifulSoup) -> str | None:
        """
        Lấy từ bảng table.table-result-tthc, cột data-title="Thời hạn giải quyết".
        Ghép tất cả các hàng nếu có nhiều hình thức nộp.
        """
        table = soup.find("table", class_=lambda c: c and "table-result-tthc" in " ".join(c))
        if table:
            times = []
            for td in table.find_all("td", attrs={"data-title": "Thời hạn giải quyết"}):
                text = td.get_text(strip=True)
                if text and text not in times:
                    times.append(text)
            if times:
                return "; ".join(times)

        # Fallback: tìm text trực tiếp
        return self._get_section_text(soup, ["Thời hạn giải quyết"])

    # ── Lệ phí ────────────────────────────────────────────────────────────────

    def _get_fee(self, soup: BeautifulSoup) -> str | None:
        """
        Lấy từ bảng table.table-result-tthc, cột data-title="Phí, lệ phí".
        """
        table = soup.find("table", class_=lambda c: c and "table-result-tthc" in " ".join(c))
        if table:
            parts = []
            for td in table.find_all("td", attrs={"data-title": "Phí, lệ phí"}):
                text = td.get_text(" ", strip=True)
                if text:
                    parts.append(text)
            if parts:
                return "; ".join(parts)

        # Fallback
        return self._get_section_text(soup, ["Lệ phí", "Phí, lệ phí"])

    # ── Thành phần hồ sơ ──────────────────────────────────────────────────────

    def _get_requirements(self, soup: BeautifulSoup) -> list[dict]:
        """
        h2 "Thành phần hồ sơ" → div.list-expand chứa nhiều div.item,
        mỗi item có bảng với <td data-title="Tên giấy tờ">.

        HTML thực tế:
          <div class="list-expand">
            <div class="item active">
              <div class="title" title="...">...</div>
              <div class="content">
                <table class="table table-result">
                  <tr>
                    <td data-title="Tên giấy tờ">...</td>
                    <td data-title="Mẫu đơn, tờ khai">...</td>
                    <td data-title="Số lượng">...</td>
                  </tr>
                </table>
              </div>
            </div>
          </div>
        """
        requirements = []
        h2 = self._find_h2_section(soup, ["Thành phần hồ sơ", "Hồ sơ cần nộp"])
        if not h2:
            return requirements

        container = self._get_next_content(h2)
        if not container:
            return requirements

        order = 1

        # Mỗi trường hợp hồ sơ là 1 div.item
        items = container.find_all("div", class_="item") if container.name == "div" else [container]

        for item in items:
            tables = item.find_all("table") if hasattr(item, 'find_all') else []
            for table in tables:
                for row in table.find_all("tr"):
                    name_td = row.find("td", attrs={"data-title": "Tên giấy tờ"})
                    if not name_td:
                        # Fallback: lấy td đầu tiên nếu có headers khớp
                        tds = row.find_all("td")
                        if tds and tds[0].get_text(strip=True):
                            name_td = tds[0]

                    if not name_td:
                        continue

                    name_text = name_td.get_text(strip=True)
                    if not name_text or len(name_text) < 3:
                        continue

                    req: dict = {
                        "order": order,
                        "name": name_text,
                        "is_mandatory": True,
                    }

                    qty_td = row.find("td", attrs={"data-title": "Số lượng"})
                    if qty_td:
                        req["quantity"] = qty_td.get_text(strip=True)

                    form_td = row.find("td", attrs={"data-title": re.compile(r"mẫu|tờ khai", re.I)})
                    if form_td:
                        a_tag = form_td.find("a")
                        req["form_name"] = a_tag.get_text(strip=True) if a_tag else form_td.get_text(strip=True)

                    requirements.append(req)
                    order += 1

        # Nếu không tìm được qua div.item, thử tìm table trực tiếp
        if not requirements:
            table = container.find("table")
            if table:
                for i, row in enumerate(table.find_all("tr")[1:], 1):
                    tds = row.find_all("td")
                    if tds and tds[0].get_text(strip=True):
                        requirements.append({
                            "order": i,
                            "name": tds[0].get_text(strip=True),
                            "is_mandatory": True,
                        })

        return requirements

    # ── Trình tự thực hiện ────────────────────────────────────────────────────

    def _get_steps(self, soup: BeautifulSoup) -> list[dict]:
        """
        h2 "Trình tự thực hiện" → div.list-expand div.item.active div.content p
        Text dạng: "Bước 1: ... Bước 2: ..."

        HTML thực tế (page3):
          <h2 class="main-title-sub">Trình tự thực hiện</h2>
          <div class="list-expand">
            <div>
              <div class="item active">
                <div class="title" title=" "></div>
                <div class="content">
                  <p>Bước 1: ... <br>Bước 2: ...</p>
                </div>
              </div>
            </div>
          </div>
        """
        steps = []
        h2 = self._find_h2_section(soup, ["Trình tự thực hiện", "Các bước thực hiện"])
        if not h2:
            return steps

        container = self._get_next_content(h2)
        if not container:
            return steps

        # Tìm text trong div.content p
        content_div = container.find("div", class_="content")
        if not content_div:
            # Thử trực tiếp
            content_div = container

        p_tag = content_div.find("p") if content_div else None
        step_text = p_tag.get_text(" ", strip=True) if p_tag else content_div.get_text(" ", strip=True)

        if not step_text:
            return steps

        # Tách theo "Bước N:"
        parts = re.split(r'Bước\s+(\d+)\s*:', step_text)
        # parts = ['tiền tố', '1', 'nội dung bước 1', '2', 'nội dung bước 2', ...]
        i = 1
        while i + 1 < len(parts):
            try:
                order = int(parts[i].strip())
                title_text = parts[i + 1].strip()
                # Chỉ lấy câu đầu tiên nếu quá dài
                if title_text:
                    steps.append({"order": order, "title": title_text[:400]})
            except (ValueError, IndexError):
                pass
            i += 2

        # Fallback: nếu không tách được "Bước N:", thử bảng
        if not steps:
            table = container.find("table")
            if table:
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        order_text = cells[0].get_text(strip=True)
                        title = cells[1].get_text(strip=True)
                        try:
                            order = int(re.sub(r"[^\d]", "", order_text)) if order_text else len(steps) + 1
                        except ValueError:
                            order = len(steps) + 1
                        step = {"order": order, "title": title[:400]}
                        if len(cells) > 2:
                            step["responsible_party"] = cells[2].get_text(strip=True)
                        if len(cells) > 3:
                            step["duration"] = cells[3].get_text(strip=True)
                        steps.append(step)

        return steps

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_ma_thu_tuc(self, url: str) -> str | None:
        m = re.search(r"ma_thu_tuc=([^&]+)", url)
        return m.group(1) if m else None

    def _map_authority(self, text: str | None) -> str:
        if not text:
            return "central"
        t = text.lower()
        if any(k in t for k in ["cấp tỉnh", "tỉnh", "thành phố trực thuộc trung ương"]):
            return "provincial"
        if any(k in t for k in ["cấp huyện", "huyện", "quận", "thị xã"]):
            return "district"
        if any(k in t for k in ["cấp xã", "xã", "phường", "thị trấn"]):
            return "commune"
        return "central"

    def _slug_code(self, name: str) -> str:
        words = name.split()[:3]
        return "TTHC-" + "-".join(w[:3].upper() for w in words if w)
