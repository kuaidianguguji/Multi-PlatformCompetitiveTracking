from __future__ import annotations

from copy import deepcopy
import re

from bs4 import BeautifulSoup

ID = re.compile(r"^ML[A-Z]\d+$")
NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"


def number(text: str):
    text = text.strip().replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return None
    return float(text) if "." in text else int(text)


def product_id(row) -> str | None:
    # Anchor to 商品ID, never confuse 被跟卖商品ID or catalog IDs with the target ID.
    for span in row.select("span"):
        direct = "".join(str(t) for t in span.find_all(string=True, recursive=False)).strip()
        if re.match(r"^商品ID\s*[:：]", direct):
            child = span.select_one("span.color-primary-copy")
            value = child.get_text(strip=True).upper() if child else ""
            if ID.fullmatch(value):
                return value
    return None


def _cell(cell) -> dict:
    cell = deepcopy(cell)
    missing = 0
    for canvas in cell.select("canvas"):
        value = canvas.get("data-ct-text", "").strip()
        missing += not bool(value)
        canvas.replace_with(value if value else "[CANVAS_UNAVAILABLE]")
    for element in cell.select("button,script,style,svg,.operateDiv"):
        element.decompose()
    return {
        "text": cell.get_text(" ", strip=True),
        "fragments": list(cell.stripped_strings),
        "links": [{"text": a.get_text(" ", strip=True), "url": a["href"]}
                  for a in cell.select("a[href]") if a["href"].startswith(("https://", "http://"))],
        "images": list(dict.fromkeys(img.get("src") or img.get("data-src") for img in cell.select("img") if img.get("src") or img.get("data-src"))),
        "titles": list(dict.fromkeys(x["title"] for x in cell.select("[title]") if x["title"])),
        "unreadable_canvas_count": missing,
    }


def merge_product(existing: dict, incoming: dict) -> dict:
    """Merge horizontal/vertical samples without replacing useful cells by blanks."""
    result = deepcopy(existing)
    for name, cell in incoming["raw_fields"].items():
        old = result["raw_fields"].get(name)
        score = lambda x: (-(x["unreadable_canvas_count"]), len(x["text"]) + len(str(x["images"])))
        if old is None or score(cell) > score(old):
            result["raw_fields"][name] = cell
    return enrich(result)


def parse_html(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "lxml")
    products = {}
    for table in soup.select(".vxe-table") or [soup]:
        headers = {}
        for th in table.select("th[colid]"):
            label = th.select_one(".vxe-cell--title") or th
            text = label.get_text(" ", strip=True)
            if text:
                headers[th["colid"]] = text
        # Fixed image columns duplicate rows. Merge by rowid within this DOM snapshot.
        grouped = {}
        for index, row in enumerate(table.select("tr.vxe-body--row")):
            grouped.setdefault(row.get("rowid", f"anonymous_{index}"), []).append(row)
        for rows in grouped.values():
            pid = next((value for row in rows if (value := product_id(row))), None)
            if not pid:
                continue
            raw = {}
            for row in rows:
                for td in row.select("td[colid]"):
                    name = headers.get(td["colid"])
                    if not name:
                        continue
                    cell = _cell(td)
                    old = raw.get(name)
                    if old is None or len(cell["text"]) + len(str(cell["images"])) > len(old["text"]) + len(str(old["images"])):
                        raw[name] = cell
            item = enrich({"platform": "mercado", "product_id": pid, "raw_fields": raw})
            products[pid] = merge_product(products[pid], item) if pid in products else item
    return products


def enrich(item: dict) -> dict:
    raw = item["raw_fields"]
    text = lambda name: raw.get(name, {}).get("text", "")
    fragments = lambda name: raw.get(name, {}).get("fragments", [])
    prices = {currency: number(value) for value, currency in re.findall(rf"({NUMBER})\s*\(([A-Z]{{3}})\)", text("价格"))}
    sales = {key: None for key in ("7d", "30d", "60d", "90d", "total")}
    for label, value in re.findall(rf"(7天|30天|60天|90天|总销量)\s*[:：]\s*({NUMBER})(?![\d天])", text("销量")):
        sales["total" if label == "总销量" else label.replace("天", "d")] = number(value)
    growth = {label.replace("天", "d"): number(value) for label, value in re.findall(rf"(7天|30天)\s*[:：]\s*({NUMBER})%", text("销量环比"))}
    revenue = {}
    for header, cell in raw.items():
        if "销售额" in header:
            currency = re.search(r"[A-Z]{3}", header)
            revenue[header] = {"amount": number(cell["text"]), "currency": currency.group() if currency else None}
    name_cell = raw.get("商品名称", {})
    links = name_cell.get("links", [])
    reviews = re.search(r"评论数\s*[:：]\s*([\d,]+)", text("商品名称"))
    rating = re.search(r"([\d.]+)\s*分", text("商品名称"))
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text("上架日期"))
    conversion = re.search(rf"({NUMBER})%", text("转化率"))
    item.update({
        "name": links[0]["text"] if links else (name_cell.get("fragments") or [None])[0],
        "url": links[0]["url"] if links else None,
        "images": raw.get("图片", {}).get("images", []),
        "categories": fragments("类目"), "prices": prices, "sales": sales,
        "revenue": revenue, "sales_growth_percent": growth,
        "review_count": number(reviews.group(1)) if reviews else None,
        "rating": number(rating.group(1)) if rating else None,
        "dimensions": text("商品尺寸") or None, "bsr": text("bsr") or None,
        "inventory_and_fulfillment": fragments("库存数"),
        "brand_and_seller": fragments("品牌/卖家"),
        "listed_date": dates[0] if dates else None,
        "source_updated_date": dates[1] if len(dates) > 1 else None,
        "conversion_rate_percent": number(conversion.group(1)) if conversion else None,
        "visits": text("访问量") or None,
        "warnings": [f"{name}: {cell['unreadable_canvas_count']} 个 Canvas 未读取到绘制文字"
                     for name, cell in raw.items() if cell["unreadable_canvas_count"]],
    })
    return item


def pagination(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    pager = soup.select_one(".el-pagination")
    if not pager:
        raise RuntimeError("未发现分页控件，不能确认收藏是否扫描完整")
    current = pager.select_one(".el-pager .is-active, .el-pager [aria-current=true]")
    total = pager.select_one(".el-pagination__total")
    next_button = pager.select_one(".btn-next")
    if current is None or next_button is None:
        raise RuntimeError("分页控件结构变化：缺少当前页或下一页")
    count = re.search(r"[\d,]+", total.get_text()) if total else None
    return {
        "current": int(current.get_text(strip=True)),
        "total": int(count.group().replace(",", "")) if count else None,
        "has_next": not (next_button.has_attr("disabled") or next_button.get("aria-disabled") == "true"
                         or "is-disabled" in next_button.get("class", [])),
    }
