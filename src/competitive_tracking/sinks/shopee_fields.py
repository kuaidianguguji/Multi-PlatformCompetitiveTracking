"""Shopee destination contract, shared by schema creation and both writers."""

COLUMNS = [
    ('product_id', '商品ID'), ('name', '商品标题'), ('custom_name', '自定义-商品名'),
    ('updated_at', '更新时间'), ('competitor', '竞品'), ('owners', '负责人'),
    ('price_brl', '价格-BRL'), ('sales_daily', '日销量'), ('sales_monthly', '月销量'),
    ('revenue_daily_brl', '日销售额'), ('revenue_monthly_brl', '月销售额'),
    ('review_count', '评分数'), ('review_rate', '留评率'), ('rating', '星级'),
    ('monthly_new_reviews', '月新增评分数'), ('like_count', '点赞数'),
    ('monthly_new_likes', '月新增点赞数'), ('category_rank', '类目排名'),
    ('rank_daily_change', '近1天排名变化'), ('rank_weekly_change', '近7天排名变化'),
    ('seller', '卖家名称'), ('shop_url', '店铺链接'), ('brand', '品牌'),
    ('variant_count', '变体数'), ('categories', '类目路径'),
    ('url', '商品链接'), ('image_url', '商品图片链接'), ('captured_at', '采集时间'),
]

KINDS = {key: 'number' for key, _ in COLUMNS}
KINDS.update({key: 'text' for key in ('product_id', 'name', 'custom_name', 'seller', 'brand', 'categories')})
KINDS.update({key: 'url' for key in ('shop_url', 'url', 'image_url')})
KINDS.update(updated_at='datetime', captured_at='datetime', competitor='select', owners='people', review_rate='percent')


def values(product):
    p = product
    result = {key: p.get(key) for key in ('product_id', 'name', 'review_count', 'rating', 'monthly_new_reviews',
                                        'like_count', 'monthly_new_likes', 'brand', 'variant_count', 'url')}
    result.update(price_brl=(p.get('prices') or {}).get('BRL'),
                  review_rate=p.get('review_rate_percent'), categories=p.get('category_path'),
                  image_url=next(iter(p.get('images') or []), None),
                  seller=(p.get('seller') or {}).get('name'), shop_url=(p.get('seller') or {}).get('url'))
    for period in ('daily', 'monthly'):
        result['sales_' + period] = (p.get('sales') or {}).get(period)
        amount = (p.get('revenue') or {}).get(period) or {}
        result['revenue_' + period + '_brl'] = amount.get('amount') if amount.get('currency') == 'BRL' else None
    rank = p.get('category_rank') or {}
    result.update(category_rank=rank.get('rank'), rank_daily_change=rank.get('daily_change'),
                  rank_weekly_change=rank.get('weekly_change'))
    return result
