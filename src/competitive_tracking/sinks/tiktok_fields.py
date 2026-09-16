"""TikTok Brazil destination contract; IDs remain strings and unknowns stay blank."""
from competitive_tracking.platforms.tiktok.parser import numeric

COLUMNS = [
    ('product_id', '商品ID'), ('name', '完整标题'), ('custom_name', '自定义-商品名'),
    ('updated_at', '更新日期'), ('competitor', '竞品'), ('owners', '负责人'),
    ('price_brl', '当前价格-BRL'), ('original_price_brl', '原价-BRL'), ('commission_rate', '佣金比例'),
    ('sales_yesterday', '昨日销量'), ('sales_7d', '近7天销量'), ('sales_14d', '近14天销量'),
    ('sales_28d', '近28天销量'), ('sales_total', '总销量'),
    ('revenue_yesterday_brl', '昨日销售额-BRL'), ('revenue_7d_brl', '近7天销售额-BRL'),
    ('revenue_14d_brl', '近14天销售额-BRL'), ('revenue_28d_brl', '近28天销售额-BRL'),
    ('revenue_total_brl', '总销售额-BRL'), ('rating', '星级'),
    ('creator_order_rate', '达人出单率'), ('related_creators', '关联达人数'),
    ('total_creators', '总关联达人数'), ('related_videos', '关联视频数'),
    ('related_livestreams', '关联直播数'), ('shop_id', '店铺ID'), ('shop_name', '店铺名称'),
    ('shop_sales_total', '店铺总销量'), ('shop_url', '店铺分析链接'), ('url', '商品链接'),
    ('category', '当前类目'), ('captured_at', '采集时间'), ('off_shelves', '是否下架'),
]
KINDS = {key: 'number' for key, _ in COLUMNS}
KINDS.update({key: 'text' for key in ('product_id', 'name', 'custom_name', 'shop_id', 'shop_name', 'category')})
KINDS.update({key: 'url' for key in ('shop_url', 'url')})
KINDS.update(updated_at='datetime', captured_at='datetime', competitor='select', owners='people',
             commission_rate='percent', creator_order_rate='percent', off_shelves='select')


def values(p):
    result = {key: p.get(key) for key in ('product_id', 'name', 'rating', 'related_creators',
              'total_creators', 'related_videos', 'related_livestreams', 'url', 'category')}
    raw = p.get('raw_product') or {}
    original = raw.get('ori_price')
    brl = raw.get('currency') == 'BRL' or str(original).strip().startswith(('R$', 'BRL'))
    flag = p.get('off_shelves')
    result.update(price_brl=(p.get('prices') or {}).get('BRL'),
                  original_price_brl=numeric(original, decimal_comma=True) if brl else None,
                  commission_rate=p.get('commission_rate_percent'),
                  creator_order_rate=p.get('creator_order_rate_percent'),
                  off_shelves={'0': '否', '1': '是'}.get(str(flag)))
    for period in ('yesterday', '7d', '14d', '28d', 'total'):
        result['sales_' + period] = (p.get('sales') or {}).get(period)
        revenue = (p.get('revenue') or {}).get(period) or {}
        result['revenue_' + period + '_brl'] = revenue.get('amount') if revenue.get('currency') == 'BRL' else None
    shop = p.get('shop') or {}
    result.update(shop_id=shop.get('id'), shop_name=shop.get('name'), shop_sales_total=shop.get('sales_total'),
                  shop_url=shop.get('source_url'))
    return result
