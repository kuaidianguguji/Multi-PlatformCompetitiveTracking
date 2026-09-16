from __future__ import annotations

from decimal import Decimal, InvalidOperation
from copy import deepcopy
from datetime import date
import math
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

ID = re.compile(r'[0-9]+')
BASE = 'https://www.fastmoss.com'


def text(node):
    return node.get_text(' ', strip=True) if node else ''


def numeric(value, *, decimal_comma=False):
    """Normalize displayed numbers only; 万/亿 values remain rounded estimates."""
    value = re.sub(r'(?:R\$|BRL|%|\s)', '', str(value if value is not None else ''))
    multiplier = 1
    if value.endswith(('万', '亿')):
        multiplier = 10000 if value[-1] == '万' else 100000000
        value = value[:-1]
    if ',' in value and '.' in value:
        value = value.replace('.', '').replace(',', '.') if value.rfind(',') > value.rfind('.') else value.replace(',', '')
    elif ',' in value:
        value = value.replace(',', '.') if decimal_comma and value.count(',') == 1 else value.replace(',', '')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value):
        return None
    try:
        number = Decimal(value) * multiplier
        return int(number) if number == number.to_integral_value() else float(number)
    except InvalidOperation:
        return None


def snapshot(cell):
    return {'text': text(cell), 'links': [{'text': text(a), 'url': urljoin(BASE, a['href'])} for a in cell.select('a[href]')],
            'images': [urljoin(BASE, x['src']) for x in cell.select('img[src]') if not x['src'].startswith('data:')],
            'full_text': list(dict.fromkeys(x.get('text') or x.get('title') for x in cell.select('[text],[title]'))),
            'canvas_count': len(cell.select('canvas'))}


def pagination(html):
    soup = BeautifulSoup(html, 'lxml')
    pager = soup.select_one('.ant-pagination')
    empty = soup.select_one('.ant-empty-description')
    if not pager and empty and re.search(r'暂无数据|No data|无数据', text(empty), re.I):
        return {'current': 1, 'has_next': False, 'empty': True}
    current = pager.select_one('.ant-pagination-item-active') if pager else None
    next_button = pager.select_one('.ant-pagination-next') if pager else None
    if not current or not text(current).isdigit() or not next_button:
        raise ValueError('FastMoss 分页未就绪，不能把加载中或异常页面当作空结果')
    disabled = ('ant-pagination-disabled' in next_button.get('class', []) or
                next_button.get('aria-disabled') == 'true' or bool(next_button.select_one('button[disabled]')))
    return {'current': int(text(current)), 'has_next': not disabled, 'empty': bool(empty)}


def parse_html(html):
    soup = BeautifulSoup(html, 'lxml')
    for dialog in soup.select('[role="dialog"]'):
        if re.search(r'游客身份|手机号登录/注册', text(dialog)) and 'display: none' not in dialog.get('style', ''):
            raise ValueError('FastMoss 登录页面中的背景商品不是有效搜索结果')
    products = {}
    for table in soup.select('table'):
        headers = [re.sub(r'\s+', '', text(h)) for h in table.select('thead th')]
        if '商品' not in headers:
            continue
        for row in table.select('tbody tr.ant-table-row'):
            pid = row.get('data-row-key', '')
            link = row.select_one('a[href*="/e-commerce/detail/"]')
            linked = re.search(r'/e-commerce/detail/(\d+)(?:[/?#]|$)', link['href']) if link else None
            if not ID.fullmatch(pid) or not linked or linked[1] != pid:
                raise ValueError('FastMoss 行 ID 与商品链接不一致，拒绝按搜索框或首行推断 ID')
            cells = row.find_all('td', recursive=False)
            if len(cells) != len(headers) or len(set(headers)) != len(headers):
                raise ValueError('FastMoss 表头与商品列不一致')
            fields = {h: snapshot(c) for h, c in zip(headers, cells)}
            main = cells[headers.index('商品')]
            title = main.select_one('h3')
            flag = main.select_one('img[src*="/regions/"]')
            region_match = re.search(r'/regions/([a-z]+)\.', flag['src']) if flag else None
            p = {'platform': 'tiktok', 'product_id': pid, 'region': region_match[1].upper() if region_match else None,
                 'name': (title.get('text') or text(title)) if title else None,
                 'source_url': urljoin(BASE, link['href']), 'url': urljoin(BASE, link['href']), 'url_type': 'fastmoss_detail',
                 'images': [urljoin(BASE, i['src']) for i in main.select('.avatar-view img[src]')],
                 'prices': {}, 'sales': {}, 'revenue': {}, 'shop': {}, 'raw_fields': fields, 'warnings': []}
            content = text(main)
            price = re.search(r'售价[：:]\s*(R\$\s*[\d.,]+)', content)
            if price:
                p['prices']['BRL'] = numeric(price[1], decimal_comma=True)
            commission = re.search(r'佣金比例[：:]\s*([\d.,]+%)', content)
            p['commission_rate_percent'] = numeric(commission[1]) if commission else None
            category = main.select_one('.custom-category-container')
            p['category'] = text(category) or None
            ratings = [numeric(text(x)) for x in main.select('.ant-tag') if not x.find_parent(class_='custom-category-container')]
            p['rating'] = next((r for r in ratings if r is not None), None)
            p['sku_inventory_available'] = 'SKU库存' in content
            for header, cell in zip(headers, cells):
                value = fields[header]['text']
                if header in ('近7天销量', '总销量'):
                    p['sales']['7d' if header == '近7天销量' else 'total'] = numeric(value)
                elif header in ('近7天销售额', '总销售额'):
                    p['revenue']['7d' if header == '近7天销售额' else 'total'] = {
                        'amount': numeric(value, decimal_comma=True) if 'R$' in value else None,
                        'currency': 'BRL' if 'R$' in value else None, 'raw': value}
                elif header == '达人出单率':
                    p['creator_order_rate_percent'] = numeric(value)
                elif header == '关联达人':
                    p['related_creators'] = numeric(value)
                elif header == '所属店铺':
                    a = cell.select_one('a[href*="/shop-marketing/detail/"]')
                    shop_id = re.search(r'/detail/(\d+)', a['href']) if a else None
                    name = cell.select_one('[text]')
                    sales = re.search(r'店铺销量[：:]\s*([\d.,万亿]+)', value)
                    p['shop'] = {'id': shop_id[1] if shop_id else None, 'name': name.get('text') if name else None,
                                 'source_url': urljoin(BASE, a['href']) if a else None,
                                 'sales_total': numeric(sales[1]) if sales else None,
                                 'images': fields[header]['images']}
                elif header == '近7天销量趋势':
                    p['sales_trend'] = None
                    p['trend_tooltip_text'] = value or None
                    if cell.select_one('canvas'):
                        p['warnings'].append('近7天趋势为 Canvas；当前保留提示文本，不将单个提示值视为完整曲线')
            p['number_note'] = '标准数值从页面显示文本换算；万/亿为页面约数，原文本见 raw_fields；达人出单率不是商品转换率'
            if pid in products and products[pid] != p:
                raise ValueError('FastMoss 同一商品出现冲突行')
            products[pid] = p
    return products


def attach_raw_product(product, raw):
    """Exact ID/region checked browser response; keep displayed rounding separately."""
    pid = product['product_id']
    if str(raw.get('product_id')) != pid or str(raw.get('id', pid)) != pid or raw.get('region') != product['region']:
        raise ValueError('FastMoss 接口商品 ID 或国家与页面不一致')
    p = deepcopy(product)
    p['raw_product'] = deepcopy(raw)
    p['display_values'] = {key: deepcopy(p.get(key)) for key in ('prices', 'sales', 'revenue', 'related_creators', 'shop')}
    def number(key, data=raw):
        value = data.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    for period, key in [('yesterday', 'yday'), ('7d', 'day7'), ('14d', 'day14'), ('28d', 'day28'), ('total', '')]:
        prefix = key + '_' if key else ''
        count = number(prefix+'sold_count')
        if count is not None:
            p['sales'][period] = count
        amount = number(prefix+'sale_amount')
        if raw.get('currency') == 'BRL' and amount is not None:
            p['revenue'][period] = {'amount': amount, 'currency': 'BRL'}
    for dest, source in [('related_creators', 'relate_author_count'), ('total_creators', 'total_author_count'),
                         ('related_videos', 'relate_video_count'), ('related_livestreams', 'relate_live_count')]:
        p[dest] = number(source)
    shop = raw.get('shop_info') or {}
    if number('sold_count', shop) is not None:
        p['shop']['sales_total'] = shop['sold_count']
    p['listed_at'] = raw.get('launch_time')
    p['categories_by_level'] = {str(i): raw.get('category_name_l'+str(i)) for i in (1,2,3)}
    p['off_shelves'] = raw.get('off_shelves')
    p['free_shipping'] = raw.get('is_free_shipping')
    link = raw.get('detail_url')
    from urllib.parse import urlsplit
    if isinstance(link, str) and urlsplit(link).scheme == 'https' and urlsplit(link).hostname in ('shop.tiktok.com', 'www.tiktok.com'):
        p.update(url=link, url_type='tiktok_product')
    trend = raw.get('trend')
    if isinstance(trend, list) and trend:
        points, dates = [], set()
        for item in trend:
            if not isinstance(item, dict) or str(item.get('product_id')) != pid or item.get('region') != product['region']:
                raise ValueError('FastMoss 趋势的商品 ID 或国家不一致')
            day = date.fromisoformat(item['dt']).isoformat()
            if day in dates or number('inc_sold_count', item) is None:
                raise ValueError('FastMoss 趋势日期重复或销量无效')
            dates.add(day)
            points.append({'date': day, 'sales': item['inc_sold_count']})
        p['sales_trend'] = sorted(points, key=lambda x:x['date'])
        p['warnings'] = [w for w in p['warnings'] if not w.startswith('近7天趋势为 Canvas')]
    p['number_note'] = '销量和销售额优先使用本次商品查询接口原值，币种以 currency=BRL 校验；页面约数见 display_values/raw_fields。原始 global 换算数据不混入 BRL；趋势销售额占位原值仅保留在 raw_product。'
    return p
