from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch, call
from urllib.parse import parse_qs, urlsplit

from competitive_tracking.config import load_config
from competitive_tracking.models import TrackingTarget
from competitive_tracking.platforms.tiktok.collector import TikTokCollector, LoginError
from competitive_tracking.platforms.tiktok.parser import numeric, pagination, parse_html, attach_raw_product
from competitive_tracking.runner import run_once

PID = '1731916815699052495'


def fixture(pid=PID, page=1, next_page=False):
    headers = ['商品','所属店铺','达人出单率','近7天销量趋势','近7天销量','近7天销售额','总销量','总销售额','关联达人','操作']
    goods = f'''<a href="/zh/e-commerce/detail/{pid}"><div class="avatar-view"><img src="https://example.com/product.webp"></div>
      <h3 text="Complete title">Complete title</h3>售价： R$ 10,99
      <img src="https://example.com/regions/br.svg"><div class="custom-category-container"><span class="ant-tag">分类</span></div>
      <span class="ant-tag">4.8</span>佣金比例：10% SKU库存</a>'''
    shop = '<a href="/zh/shop-marketing/detail/7496266893040520143"><span text="店铺">店铺</span>店铺销量：28.11万</a>'
    cells = [goods,shop,'35.04%','<canvas></canvas><div>2026-09-09 日销量：3539</div>','2.85万','R$30.25万','27.62万','R$296.49万','1.12万','']
    return '<div class="ant-table-wrapper"><table><thead><tr>'+''.join(f'<th>{h}</th>' for h in headers)+'</tr></thead><tbody><tr class="ant-table-measure-row"><td></td></tr>'+f'<tr class="ant-table-row" data-row-key="{pid}">'+''.join(f'<td>{v}</td>' for v in cells)+'</tr></tbody></table>'+f'<ul class="ant-pagination"><li class="ant-pagination-item-active">{page}</li><li class="ant-pagination-next" aria-disabled="{str(not next_page).lower()}"></li></ul></div>'


class TikTokTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(Path(__file__).resolve().parents[1]/'config.example.toml')
        self.c = TikTokCollector(self.cfg)

    def test_brazil_currency_units_and_full_width_columns(self):
        p = parse_html(fixture())[PID]
        self.assertEqual(p['product_id'], PID)
        self.assertEqual(p['prices'], {'BRL':10.99})
        self.assertEqual(p['sales'], {'7d':28500,'total':276200})
        self.assertEqual(p['revenue']['total']['amount'],2964900)
        self.assertEqual(p['creator_order_rate_percent'],35.04)
        self.assertEqual(p['commission_rate_percent'],10)
        self.assertEqual(p['rating'],4.8)
        self.assertEqual(p['related_creators'],11200)
        self.assertEqual(p['shop']['sales_total'],281100)
        self.assertEqual(p['shop']['id'],'7496266893040520143')
        self.assertEqual(p['region'],'BR')
        self.assertEqual(len(p['raw_fields']),10)
        self.assertIsNone(p['sales_trend'])
        self.assertNotIn('conversion_rate_percent',p)
        self.assertIn('约数',p['number_note'])

    def test_zero_missing_and_localized_numbers(self):
        for value, expected, comma in [('0',0,False),('N/A',None,False),('--',None,False),('R$1.234,56',1234.56,True),
                                       ('1.23亿',123000000,False),('1,234',1234,False),('R$30.25万',302500,True),('$12',None,False)]:
            self.assertEqual(numeric(value,decimal_comma=comma),expected)

    def test_login_background_is_allowed_when_product_table_exists(self):
        self.assertIn(PID, parse_html('<section role="dialog">您当前是游客身份</section>'+fixture()))
        with self.assertRaisesRegex(ValueError,'背景商品'):
            parse_html('<section role="dialog">您当前是游客身份</section>')
        with self.assertRaisesRegex(ValueError,'ID'):
            parse_html(fixture().replace('data-row-key="'+PID+'"','data-row-key="123"'))
        with self.assertRaisesRegex(ValueError,'表头'):
            parse_html(fixture().replace('<th>操作</th>',''))

    def test_loading_is_not_empty_and_pagination(self):
        with self.assertRaises(ValueError): pagination('<div>加载中</div>')
        self.assertFalse(pagination(fixture())['has_next'])
        self.assertTrue(pagination(fixture(next_page=True))['has_next'])
        self.assertTrue(pagination('<div class="ant-empty-description">暂无数据</div>')['empty'])

    def test_long_id_url_preserved_and_non_numeric_rejected(self):
        self.assertEqual(urlsplit(self.c.search_url(PID)).query,
                         'region=BR&page=1&words=' + PID)
        params = parse_qs(urlsplit(self.c.search_url(PID)).query)
        self.assertEqual(params,{'words':[PID],'page':['1'],'region':['BR']})
        with self.assertRaises(ValueError): self.c.search_url('1e18')

    @patch('competitive_tracking.platforms.tiktok.collector.time.sleep')
    def test_login_requeries_current_id_before_next(self, sleep):
        c=self.c; c.page=Mock(); c._wait=Mock(); c._login=Mock()
        c._needs_login=Mock(side_effect=[True,True,False]); c._result=Mock(return_value=({},{}))
        c._await_response=Mock(return_value={})
        c._query(PID,1)
        urls=[call.args[0] for call in c.page.get.call_args_list]
        self.assertEqual(urls,['about:blank',c.search_url(PID),'about:blank',c.search_url(PID)])
        c._login.assert_called_once()
        self.assertEqual(c._result.call_args_list[0].args, (PID, 1, None))
        self.assertEqual(c._result.call_args_list[1:], [call(PID, 1, set()), call(PID, 1, set())])

    @patch('competitive_tracking.platforms.tiktok.collector.time.sleep')
    def test_product_result_is_used_while_login_modal_is_visible(self, sleep):
        c = self.c
        c.page = Mock()
        c._wait = Mock()
        c._needs_login = Mock(return_value=True)
        c._login = Mock()
        product = parse_html(fixture())[PID]
        c._await_response = Mock(return_value={PID: {'id': PID, 'product_id': PID, 'region': 'BR'}})
        c._result = Mock(return_value=({PID: product}, {'current': 1, 'has_next': False}))

        products, info = c._query(PID, 1)

        self.assertIn(PID, products)
        self.assertEqual(info['current'], 1)
        c._login.assert_not_called()

    def test_stable_dom_result_requires_no_login_state(self):
        self.cfg['tiktok'].update(result_timeout_seconds=.2, poll_seconds=.001, result_settle_seconds=.001)
        c = self.c
        c.page = Mock()
        c._needs_login = Mock(side_effect=AssertionError('must not gate readable results'))
        c._logged_in = Mock(side_effect=AssertionError('must not require authenticated header'))
        c.page.run_js.return_value = {'html': fixture(), 'word': PID, 'busy': False, 'url': c.search_url(PID)}
        products, info = c._result(PID, 1)
        self.assertIn(PID, products)
        self.assertEqual(info['current'], 1)

    @patch('competitive_tracking.platforms.tiktok.collector.time.sleep')
    def test_missing_response_uses_dom_without_login(self, sleep):
        for error in (TimeoutError('no response'), RuntimeError('HTTP 403')):
            with self.subTest(error=type(error).__name__):
                c = self.c
                c.page = Mock()
                c._login = Mock()
                c._needs_login = Mock(return_value=True)
                c._await_response = Mock(side_effect=error)
                c._result = Mock(return_value=(parse_html(fixture()), {'current': 1, 'has_next': False}))
                products, _ = c._query(PID, 1)
                self.assertIn(PID, products)
                self.assertTrue(any('登录弹窗存在' in w for w in products[PID]['warnings']))
                c._await_response.assert_not_called()
                c._result.assert_called_once()
                c._login.assert_not_called()
                c.page.listen.stop.assert_called_once()

    def test_raw_response_restores_exact_numbers_and_complete_trend(self):
        raw={'id':PID,'product_id':PID,'region':'BR','currency':'BRL','day7_sold_count':28512,
             'day7_sale_amount':302549.12,'sold_count':276222,'sale_amount':2964910.5,
             'global':{'global_day7_sale_amount':123}, 'shop_info':{'sold_count':281111},
             'detail_url':'https://shop.tiktok.com/view/product/'+PID+'?region=BR',
             'trend':[{'product_id':PID,'region':'BR','dt':'2026-09-09','inc_sold_count':0,'inc_sale_amount':0}]}
        p=attach_raw_product(parse_html(fixture())[PID],raw)
        self.assertEqual(p['sales']['7d'],28512)
        self.assertEqual(p['revenue']['7d']['amount'],302549.12)
        self.assertEqual(p['display_values']['sales']['7d'],28500)
        self.assertEqual(p['sales_trend'],[{'date':'2026-09-09','sales':0}])
        self.assertEqual(p['url_type'],'tiktok_product')
        self.assertEqual(p['warnings'],[])
        self.assertEqual(p['raw_product'],raw)
        raw['product_id']='123'
        with self.assertRaises(ValueError): attach_raw_product(parse_html(fixture())[PID],raw)

    def test_response_requires_exact_query_and_drops_login_envelope(self):
        body={'code':200,'ext':{'params':{'words':PID,'region':'BR','page':'1','login_uid':'private'}},
              'data':{'ext':{'is_ok':True},'product_list':[{'product_id':PID,'region':'BR'}]}}
        packet=SimpleNamespace(response=SimpleNamespace(status=200,body=body))
        self.assertEqual(self.c._decode_response(packet,PID,1),{PID:{'product_id':PID,'region':'BR'}})
        self.assertIsNone(self.c._decode_response(packet,'123',1))
        self.assertIsNone(self.c._decode_response(packet,PID,2))
        body['code']=403
        with self.assertRaisesRegex(RuntimeError,'查询失败'): self.c._decode_response(packet,PID,1)

    def test_matching_product_on_second_page_and_country_check(self):
        wanted=parse_html(fixture())[PID]
        other=parse_html(fixture('123'))['123']
        self.c._query=Mock(side_effect=[({'123':other},{'has_next':True}),({PID:wanted},{'has_next':False})])
        result=self.c._search(PID)
        self.assertEqual(result['product_id'],PID)
        self.assertIn('captured_at',result)
        wanted['region']='US'
        self.c._query=Mock(return_value=({PID:wanted},{'has_next':False}))
        with self.assertRaisesRegex(RuntimeError,'国家'): self.c._search(PID)

    def test_duplicate_pages_error_not_not_found(self):
        p=parse_html(fixture('123'))['123']
        self.c._query=Mock(return_value=({'123':p},{'has_next':True}))
        with self.assertRaisesRegex(RuntimeError,'重复'): self.c._search(PID)

    def test_stale_query_input_is_never_accepted(self):
        self.cfg['tiktok'].update(result_timeout_seconds=.01,poll_seconds=.001,result_settle_seconds=.001)
        self.c.page=Mock(); self.c._needs_login=Mock(return_value=False); self.c._logged_in=Mock(return_value=True)
        self.c.page.run_js.return_value={'html':fixture(),'word':'other','busy':False,'url':self.c.search_url(PID)}
        with self.assertRaises(TimeoutError): self.c._result(PID,1)

    def test_abort_remaining_targets_on_login_failure_and_close_session(self):
        session=Mock(); page=Mock(); session.__enter__=Mock(return_value=page); session.__exit__=Mock()
        c=TikTokCollector(self.cfg,session_factory=Mock(return_value=session))
        c._search=Mock(side_effect=LoginError('login failed'))
        result=c.collect([TrackingTarget('tiktok',PID),TrackingTarget('tiktok','123')])
        self.assertEqual(len(result),2)
        self.assertTrue(all(e['status']=='error' for e in result.values()))
        c._search.assert_called_once(); session.__exit__.assert_called_once(); page.set.window.max.assert_called_once()

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_disabled_tiktok_outputs_do_not_use_other_platform_outputs(self,bitable,sheets,messages):
        cfg=deepcopy(self.cfg); cfg['app']['platforms']=['tiktok']
        cfg['feishu_messages']['platforms']=['mercado','shopee']
        for setting in ('feishu_output','feishu_sheets','feishu_messages','shopee_feishu_output','shopee_feishu_sheets'): cfg[setting]['enabled']=True
        source=Mock(); source.read_records.return_value=[{'fields':{'平台':'tiktok','商品ID':PID,'监控开关':'开启','推送开关':'开启','数据推送人':[{'id':'ou_test'}]}}]
        collector=Mock(); collector.return_value.collect.return_value={'tiktok:'+PID:{'status':'partial','product':parse_html(fixture())[PID]}}
        sink=Mock(); result=run_once(cfg,source=source,registry={'tiktok':collector},sink=sink)
        self.assertIn('tiktok:'+PID,result['products']); sink.write.assert_called_once()
        bitable.assert_not_called(); sheets.assert_not_called(); messages.assert_not_called()


if __name__=='__main__': unittest.main()
