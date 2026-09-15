from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

ID = re.compile(r"\d+")


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def numeric(value):
    value = re.sub(r"(?:R\$|BRL|%|\s|,)", "", str(value if value is not None else ""))
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        return None
    n = float(value)
    return int(n) if n.is_integer() else n


def pagination(html):
    soup = BeautifulSoup(html, "lxml")
    pager = soup.select_one('.t-pagination')
    if not pager:
        raise ValueError("Shopdora 缺少分页信息，不能确认收藏已扫描完整")
    total = re.search(r"共\s*([\d,]+)\s*项", text(pager.select_one('.t-pagination__total')))
    current = text(pager.select_one('.t-pagination__number.t-is-current'))
    next_button = pager.select_one('.t-pagination__btn-next')
    if not total or not current.isdigit() or not next_button:
        raise ValueError("Shopdora 分页结构变化，无法确认总数或下一页状态")
    disabled = 't-is-disabled' in next_button.get('class', []) or next_button.has_attr('disabled') or next_button.get('aria-disabled') == 'true'
    return {'total': int(total[1].replace(',', '')), 'current': int(current), 'has_next': not disabled}


def field_snapshot(node):
    return {'text': text(node), 'items': [text(e) for e in node.select('.td-item')],
            'links': [{'text': text(a), 'url': a.get('href')} for a in node.select('a[href]')],
            'images': [img.get('src') for img in node.select('img[src]') if not img.get('src', '').startswith('data:')],
            'titles': [e.get('title') for e in node.select('[title]')],
            'canvas_count': len(node.select('canvas'))}


def parse_html(html, *, raw_items=None, site='br'):
    soup = BeautifulSoup(html, 'lxml')
    products = {}
    for table in soup.select('.custom-table table'):
        headers = [text(h) for h in table.select('thead th')]
        if not headers:
            headers = [text(h) for h in table.select('th')]
        for row in table.select('tr.row-main'):
            pid_node = row.select_one('.td-goods-title .link-info a.link')
            pid = text(pid_node)
            if not ID.fullmatch(pid):
                raise ValueError('Shopdora 商品行缺少可精确匹配的数字产品 ID')
            cells = row.find_all('td', recursive=False)
            if len(cells) != len(headers):
                raise ValueError(f'Shopdora 商品 {pid} 表头列数与数据不一致')
            fields = {header: field_snapshot(cell) for header, cell in zip(headers, cells)}
            p = {'platform': 'shopee', 'site': site, 'product_id': pid,
                 'name': text(row.select_one('.title-info .title')) or None,
                 'images': [e.get('src') for e in row.select('.thumb[src]')],
                 'prices': {}, 'sales': {}, 'revenue': {}, 'sales_growth_percent': {},
                 'category_rank': {}, 'raw_fields': fields, 'warnings': []}
            for header, cell in zip(headers, cells):
                items = cell.select('.td-item')
                def at(i):
                    return text(items[i]) if i < len(items) else None
                h = header.replace(' ', '')
                if h.startswith('日销量'):
                    p['sales'] = {'daily': numeric(at(0)), 'monthly': numeric(at(1))}
                    if len(items) > 2:
                        p['sales_growth_percent']['monthly'] = numeric(at(2))
                elif h.startswith('日销售额'):
                    p['revenue'] = {'daily': {'amount': numeric(at(0)), 'currency': 'BRL'},
                                    'monthly': {'amount': numeric(at(1)), 'currency': 'BRL'}}
                    p['revenue_growth_percent'] = numeric(at(2))
                elif h == '价格':
                    price = at(0)
                    if price and ('R$' in price or 'BRL' in price):
                        p['prices']['BRL'] = numeric(price)
                    else:
                        p['warnings'].append('价格不是明确 BRL，保留原字段，不推断币种')
                elif h.startswith('评分数'):
                    p.update(review_count=numeric(at(0)), review_rate_percent=numeric(at(1)))
                elif h.startswith('星级'):
                    p.update(rating=numeric(at(0)), monthly_new_reviews=numeric(at(1)))
                elif h.startswith('点赞数'):
                    p.update(like_count=numeric(at(0)), monthly_new_likes=numeric(at(1)))
                elif h.startswith('类目排名'):
                    changes = items[1].find_all('span', recursive=False) if len(items) > 1 else []
                    p['category_rank'] = {'rank': numeric(at(0)), 'changes_raw': at(1),
                                          'daily_change': numeric(text(changes[0])) if changes else None,
                                          'weekly_change': numeric(text(changes[1])) if len(changes) > 1 else None}
                    # Arrow CSS determines direction when not encoded in the number itself.
                    p['category_rank']['direction_classes'] = [e.get('class') for e in items[1].select('[class]')] if len(items) > 1 else []
                elif h == '上架时间':
                    p.update(listed_at=at(0), listing_age=at(1))
                elif h == '收藏日期':
                    p['favorited_at'] = at(0)
            sub = row.find_next_sibling('tr')
            if sub and 'row-sub' in sub.get('class', []):
                p['raw_fields']['补充信息'] = field_snapshot(sub)
                for item in sub.select('.span-list > .row > span'):
                    value = text(item)
                    if value.startswith('类目路径'):
                        category = value.split('：', 1)[-1].strip()
                        p['category_path'] = category
                        p['categories'] = category.split('-')
                    elif value.startswith('卖家'):
                        name = item.select_one('.normal-link')
                        link = item.select_one('a[href*="/shop/"]')
                        p['seller'] = {'name': text(name) or None, 'url': link.get('href') if link else None,
                                       'origin': next((x for x in ('跨境', '本土') if x in value), None)}
                        if link:
                            match = re.search(r'/shop/(\d+)', link['href'])
                            if match:
                                p['seller']['shop_id'] = match[1]
                                if urlsplit(link['href']).hostname == 'shopee.com.br':
                                    p['url'] = f'https://shopee.com.br/product/{match[1]}/{pid}'
                                    p['url_source'] = 'derived_from_shop_id_and_product_id'
                    elif value.startswith('品牌'):
                        p['brand'] = value.split('：', 1)[-1].strip()
                    elif value.startswith('变体数'):
                        p['variant_count'] = numeric(value.split('：', 1)[-1])
                    elif value.startswith('店铺类型'):
                        p['shop_type'] = value.split('：', 1)[-1].strip()
            p['raw_product'] = (raw_items or {}).get(pid)
            if p['raw_product'] and p['category_rank']:
                for raw_key, key in [('cateRankChangeD', 'daily_change'), ('cateRankChangeW', 'weekly_change')]:
                    if raw_key in p['raw_product']:
                        p['category_rank'][key] = numeric(p['raw_product'][raw_key])
            p['sales_trend'] = None
            if p['raw_product']:
                # Keep the complete product object; source-specific trend key names are preserved.
                p['sales_trend'] = {k: v for k, v in p['raw_product'].items() if re.search(r'trend|history|chart', k, re.I)} or None
            if row.select_one('canvas') and not p['raw_product']:
                p['warnings'].append('销量趋势为 Canvas，静态 HTML 无法恢复曲线数值')
            products[pid] = p
    return products
