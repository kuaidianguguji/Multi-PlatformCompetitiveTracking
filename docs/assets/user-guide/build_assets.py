"""Build tutorial illustrations and a standalone HTML preview (requires Pillow)."""
from pathlib import Path
import html
import re
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = Path('C:/Windows/Fonts/msyh.ttc')
BLUE, INK, MUTED = '#2563EB', '#172B4D', '#53657D'


def font(size):
    return ImageFont.truetype(str(FONT), size)


def canvas(title, subtitle, height):
    image = Image.new('RGB', (1320, height), '#F3F6FC')
    draw = ImageDraw.Draw(image)
    draw.text((48, 36), title, font=font(36), fill=INK)
    draw.text((48, 96), subtitle, font=font(21), fill=MUTED)
    return image, draw


def box(draw, xy, fill='#FFFFFF', outline=None):
    draw.rounded_rectangle(xy, radius=18, fill=fill, outline=outline, width=2)


def text(draw, xy, value, size=24, color=INK):
    draw.text(xy, value, font=font(size), fill=color, spacing=14)


def arrow(draw, start, end):
    draw.line((start, end), fill=BLUE, width=4)
    x, y = end
    draw.polygon([(x, y), (x-13, y-8), (x-13, y+8)], fill=BLUE)


def flow():
    image, draw = canvas('一条任务，三个结果入口', '填写任务后，等待管理员设置的下一次执行时间。', 650)
    panels = [(48, 176, 386, 486), (492, 176, 830, 486), (936, 176, 1272, 486)]
    for p in panels:
        box(draw, p)
    text(draw, (74, 202), '01  填写任务', 28, BLUE)
    text(draw, (74, 264), '商品ID + 平台\n监控开关 = 开启\n数据推送人已选择', 25)
    text(draw, (74, 428), '自定义名称帮助识别商品', 20, MUTED)
    text(draw, (518, 202), '02  定时采集', 28, BLUE)
    text(draw, (518, 264), 'Mercado · 蓝鲸\nShopee · Shopdora\nTikTok · FastMoss', 24)
    text(draw, (518, 428), '目前均查询巴西站', 20, MUTED)
    text(draw, (962, 202), '03  查看结果', 28, BLUE)
    text(draw, (962, 264), '最新表：更新本商品\n历史表：追加新记录\n消息：发给数据推送人', 24)
    text(draw, (962, 428), '发送还需推送开关开启', 20, MUTED)
    arrow(draw, (400, 332), (478, 332))
    arrow(draw, (844, 332), (922, 332))
    box(draw, (48, 526, 1272, 606), '#E7EEFF')
    text(draw, (76, 550), '同平台 + 同商品ID：采集一次；不同接收人：分别推送。', 26, BLUE)
    image.save(ROOT / '01-flow.png')


def switches():
    image, draw = canvas('两个开关要分开看', '前提：平台正确、商品ID完整、数据推送人已选择，管理员已启用对应模块。', 690)
    x = [48, 388, 670, 1272]
    headers = ['监控开关', '推送开关', '下一次运行']
    box(draw, (48, 164, 1272, 242), BLUE)
    for i, h in enumerate(headers):
        text(draw, (x[i]+28, 187), h, 26, '#FFFFFF')
    rows = [('开启', '开启', '采集 + 写表 + 发送消息'),
            ('开启', '暂停 / 留空', '采集 + 写表'),
            ('暂停 / 留空', '任意值', '不采集、不发送')]
    for i, row in enumerate(rows):
        top = 260 + i*92
        box(draw, (48, top, 1272, top+78))
        for j, value in enumerate(row):
            color = '#16834E' if value == '开启' else INK
            text(draw, (x[j]+28, top+23), value, 26, color)
    box(draw, (48, 556, 1272, 652), '#FFF0DF')
    text(draw, (76, 574), '常见误区：推送开启，监控空白 → 仍然不采集。', 25, '#9A4D00')
    text(draw, (76, 611), '即使暂停推送，数据推送人也不能留空。', 22, '#9A4D00')
    image.save(ROOT / '02-switches.png')


def ids():
    image, draw = canvas('商品ID怎么取？看这三种结构', '只填写完整ID，不填整条网址；长数字保持文本，Mercado保留字母前缀。', 930)
    specs = [
        (164, 'TikTok', '/view/product/', '1737194607628027090', '?region=BR&local=en', '取 /product/ 后、? 前的全部数字。'),
        (406, 'Shopee', '/product/955204066/', '22329034432', '', '955204066 是店铺ID，最后一段才是商品ID。'),
        (648, 'Mercado', '/up/', 'MLBU4813895273', '', 'MLBU 是有效前缀，不要漏掉 U。')]
    for top, name, before, pid, after, note in specs:
        box(draw, (48, top, 1272, top+220))
        text(draw, (76, top+22), name, 28, BLUE)
        xx, yy = 76, top+80
        text(draw, (xx, yy), before, 24, MUTED)
        xx += draw.textlength(before, font=font(24)) + 6
        width = draw.textlength(pid, font=font(26))
        box(draw, (xx-8, yy-8, xx+width+12, yy+44), '#DBEAFE')
        text(draw, (xx, yy-2), pid, 26, BLUE)
        if after:
            text(draw, (xx+width+20, yy), after, 22, MUTED)
        text(draw, (76, top+154), note, 25)
    image.save(ROOT / '03-product-ids.png')


def inline(value):
    value = html.escape(value)
    value = re.sub(r'`([^`]+)`', r'<code>\1</code>', value)
    value = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', value)
    value = re.sub(r'\[([^\]]+)\]\((https://[^)]+)\)', r'<a href="\2">\1</a>', value)
    return value


def preview():
    source = ROOT.parents[1] / '新手图文使用指南.md'
    parts = []
    table = ordered = False
    for line in source.read_text(encoding='utf-8').splitlines():
        if table and not line.startswith('|'):
            parts.append('</tbody></table>')
            table = False
        if ordered and not re.match(r'^\d+\. ', line):
            parts.append('</ol>')
            ordered = False
        if not line:
            continue
        if line.startswith('|'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            if all(re.fullmatch(r'[-: ]+', c) for c in cells):
                continue
            if not table:
                parts.append('<table><thead><tr>' + ''.join(f'<th>{inline(c)}</th>' for c in cells) + '</tr></thead><tbody>')
                table = True
            else:
                parts.append('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in cells) + '</tr>')
        elif line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            parts.append(f'<h{level}>{inline(line[level:].strip())}</h{level}>')
        elif line.startswith('!['):
            match = re.match(r'!\[([^]]+)\]\(([^)]+)\)', line)
            parts.append(f'<figure><img alt="{html.escape(match[1])}" src="{html.escape(match[2])}"><figcaption>教学示意图</figcaption></figure>')
        elif re.match(r'^\d+\. ', line):
            if not ordered:
                parts.append('<ol>')
                ordered = True
            parts.append('<li>' + inline(re.sub(r'^\d+\. ', '', line)) + '</li>')
        else:
            parts.append('<p>' + inline(line) + '</p>')
    if table:
        parts.append('</tbody></table>')
    if ordered:
        parts.append('</ol>')
    style = '''body{font-family:"Microsoft YaHei",sans-serif;color:#172b4d;background:#f3f6fc;line-height:1.85;margin:0}main{max-width:1060px;margin:32px auto;background:white;padding:48px 60px;border-radius:16px}h1{font-size:34px;line-height:1.4}h2{margin-top:42px;border-bottom:2px solid #e7eeff;padding-bottom:12px;color:#1747a6}h3{margin-top:28px}p,li{font-size:16px}a{color:#2563eb}code{background:#edf2fa;padding:3px 6px;border-radius:4px;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:15px;margin:20px 0}th,td{border:1px solid #dce4f0;padding:12px 14px;text-align:left}th{background:#edf3ff}figure{margin:28px 0}img{max-width:100%;height:auto}figcaption{text-align:center;color:#64748b;font-size:12px}@media print{body{background:white}main{padding:0;margin:0}figure, tr{break-inside:avoid}h2,h3{break-after:avoid}}'''
    output = source.with_suffix('.html')
    output.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>商品监控控制台 · 新手使用指南</title><style>'+style+'</style><main>'+''.join(parts)+'</main></html>', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    flow()
    switches()
    ids()
    preview()
