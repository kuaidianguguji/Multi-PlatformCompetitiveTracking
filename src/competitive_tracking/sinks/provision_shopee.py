"""Explicit, resumable initialization; normal collection never changes table schemas."""
from datetime import datetime, timezone

from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.sinks.shopee_fields import COLUMNS, KINDS
from competitive_tracking.sinks.tiktok_fields import COLUMNS as TIKTOK_COLUMNS, KINDS as TIKTOK_KINDS
from competitive_tracking.sinks.feishu_sheets import blank, same_rows
from competitive_tracking.storage import atomic_json


def field_definition(key, label, kinds=None):
    kind = (KINDS if kinds is None else kinds)[key]
    result = {'field_name': label, 'type': {'text': 1, 'number': 2, 'percent': 2,
                                           'select': 3, 'people': 11, 'url': 15, 'datetime': 5}[kind]}
    if kind in ('number', 'percent'):
        decimals = key.endswith('_brl') or key == 'rating'
        result['property'] = {'formatter': '0.00%' if kind == 'percent' else ('0.00' if decimals else '0')}
    elif kind == 'datetime':
        result['property'] = {'date_formatter': 'yyyy-MM-dd HH:mm', 'auto_fill': False}
    elif kind == 'people':
        result['property'] = {'multiple': True}
    elif kind == 'select':
        result['property'] = {'options': [{'name': '是'}, {'name': '否'}]}
    return result


def provision(config, client=None, *, dry_run=False):
    c = client or FeishuClient(config['feishu'])
    output, sheets = config['feishu_output'], config['feishu_sheets']
    platform = output.get('platform')
    if platform not in ('shopee', 'tiktok') or sheets.get('platform') != platform:
        raise ValueError('仅支持同平台 Shopee/TikTok 目标初始化')
    columns, kinds, last_column = (COLUMNS, KINDS, 'AB') if platform == 'shopee' else (TIKTOK_COLUMNS, TIKTOK_KINDS, 'AG')
    if sheets['data_start_row'] != 2:
        raise ValueError('初始化要求第 1 行表头、第 2 行起数据')
    base = c.table_path(output['app_token'], output['table_id'])
    fields = c.list_all(base + '/fields', page_size=100)
    records = c.list_all(base + '/records', page_size=500)
    definitions = [field_definition(key, output['fields'][key], kinds) for key, _ in columns]
    labels = [f['field_name'] for f in definitions]
    existing = [f['field_name'] for f in fields]
    rename = bool(len(fields) == 1 and fields[0].get('is_primary') and fields[0]['type'] == 1 and
                  existing[0] != labels[0] and all(blank(value) for row in records for value in row.get('fields', {}).values()))
    if rename:
        existing[0] = labels[0]
    if existing != labels[:len(existing)]:
        raise ValueError('多维表现有字段不是目标顺序的前缀；请先核对已有结构，初始化不会删除或移动现有列')
    for field, definition in zip(fields, definitions):
        if field['type'] != definition['type']:
            raise ValueError(f"已有字段类型不匹配：{field['field_name']}")
    token, sheet_id = sheets['spreadsheet_token'], sheets['sheet_id']
    sheet_base = f'/sheets/v2/spreadsheets/{token}'
    meta_path = f'/sheets/v3/spreadsheets/{token}/sheets/query'
    def metadata():
        return next(s for s in c.request('GET', meta_path)['sheets'] if s['sheet_id'] == sheet_id)
    meta = metadata()
    header_range = f'{sheet_id}!A1:{last_column}1'
    def read_header():
        data = c.request('GET', sheet_base+'/values/'+header_range, params={'valueRenderOption': 'Formula'})
        rows = data.get('valueRange', {}).get('values')
        if not isinstance(rows, list) or not rows or any(not isinstance(row, list) for row in rows):
            raise RuntimeError('读取二维表表头失败，拒绝视作空白')
        return rows
    header = read_header()
    sheet_labels = [[label for _, label in columns]]
    if not same_rows(header, sheet_labels) and any(not blank(cell) for row in header for cell in row):
        raise ValueError('二维表第 1 行已有其他内容，初始化不会覆盖')
    if meta.get('merges'):
        raise ValueError('二维表存在合并单元格，请先核对结构')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    report_path = config['app']['output_dir'] / f'{platform}_schema_{stamp}.json'
    report = {'status': 'preview', 'rename_primary': rename, 'create_fields': definitions[len(fields):],
              'add_columns': max(0, len(columns)-meta['grid_properties']['column_count']),
              'write_header': not same_rows(header, sheet_labels), 'header_range': header_range,
              'before': {'fields': fields, 'records': records, 'metadata': meta, 'header': header},
              'report_path': str(report_path)}
    atomic_json(report_path, report)
    if not dry_run:
        if rename:
            c.request('PUT', base+'/fields/'+fields[0]['field_id'], json=definitions[0])
        for definition in report['create_fields']:
            c.request('POST', base+'/fields', json=definition)
        if report['add_columns']:
            c.request('POST', sheet_base+'/dimension_range', json={'dimension': {
                'sheetId': sheet_id, 'majorDimension': 'COLUMNS', 'length': report['add_columns']}})
        if report['write_header']:
            # Check again immediately before the write to avoid overwriting concurrent edits.
            current = read_header()
            if not same_rows(current, sheet_labels):
                if any(not blank(cell) for row in current for cell in row):
                    raise RuntimeError('表头已被其他人修改，停止写入')
                c.request('PUT', sheet_base+'/values', json={'valueRange': {'range': header_range, 'values': sheet_labels}})
        final_fields = c.list_all(base+'/fields', page_size=100)
        if [f['field_name'] for f in final_fields] != labels or not same_rows(read_header(), sheet_labels):
            raise RuntimeError('初始化后字段或表头顺序核对失败')
        if metadata()['grid_properties']['column_count'] < len(columns):
            raise RuntimeError('二维表扩展列数未生效')
        report['status'] = 'ok'
        atomic_json(report_path, report)
    return {key: report[key] for key in ('status', 'rename_primary', 'add_columns', 'write_header', 'header_range', 'report_path')} | {'field_count': len(labels)}
