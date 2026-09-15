from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from competitive_tracking.config import load_config
from competitive_tracking.browser import BrowserSession
from competitive_tracking.platforms.shopee.collector import ShopeeCollector
from competitive_tracking.platforms.shopee.parser import parse_html, pagination, numeric
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.feishu_messages import FeishuMessageSink


def fixture():
    headers = ['#','产品信息','日销量 月销量 月销量增长率','价格','评分数 留评率','星级 月新增评分数',
               '点赞数 月新增点赞数','上架时间','收藏日期','新增指标']
    cells = ['1', '<div class="td-goods-title"><div class="title-info"><div class="title">Full product title</div></div>'
             '<div class="link-info"><a class="link">123456789</a></div><img class="thumb" src="https://example.com/image"/></div>',
             ['','400,000','+2.50%'],['R$23.39'],['0','0.30%'],['5','0'],['765','0'],['2025-03-26','17个月'],['2026-07-24 10:33:48'],['保留新字段']]
    row = ''.join('<td>'+(''.join('<div class="td-item">'+x+'</div>' for x in c) if isinstance(c,list) else c)+'</td>' for c in cells)
    return '<div class="custom-table"><table><thead><tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr></thead><tbody>' \
           '<tr class="row-main">'+row+'</tr><tr class="row-sub"><td class="span-list"><div class="row">' \
           '<span>类目路径：<span>父类-子类</span></span><span>卖家：<a class="normal-link" href="https://shopee.com.br/shop/987">卖家名称</a>跨境</span>' \
           '<span>品牌：测试品牌</span><span>变体数：5</span><span>店铺类型：普通</span></div></td></tr></tbody></table></div>' \
           '<div class="t-pagination"><div class="t-pagination__total">共 1 项数据</div><li class="t-pagination__number t-is-current">1</li>' \
           '<div class="t-pagination__btn-next t-is-disabled"></div></div>'


class ShopeeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.toml')
        self.collector = ShopeeCollector(self.cfg)

    def test_all_columns_subrow_blank_daily_and_zero_are_preserved(self):
        p = parse_html(fixture())['123456789']
        self.assertEqual(p['sales'], {'daily':None,'monthly':400000})
        self.assertEqual(p['prices'], {'BRL':23.39})
        self.assertEqual(p['review_rate_percent'], 0.3)
        self.assertEqual(p['review_count'], 0)
        self.assertNotIn('conversion_rate_percent', p)
        self.assertEqual(p['name'], 'Full product title')
        self.assertEqual(p['categories'], ['父类','子类'])
        self.assertEqual(p['seller']['shop_id'], '987')
        self.assertEqual(p['url'], 'https://shopee.com.br/product/987/123456789')
        self.assertEqual(p['variant_count'],5)
        self.assertEqual(p['raw_fields']['新增指标']['items'],['保留新字段'])
        self.assertEqual(pagination(fixture()), {'total':1,'current':1,'has_next':False})

    def test_api_scaled_raw_fields_not_confused_with_display_values(self):
        p = parse_html(fixture(),raw_items={'123456789':{'itemId':'123456789','price':'2339000','ratingRateTotal':30}})['123456789']
        self.assertEqual(p['prices']['BRL'],23.39)
        self.assertEqual(p['raw_product']['price'],'2339000')
        self.assertEqual(p['review_rate_percent'],0.3)
        self.assertEqual(numeric(0), 0)

    def test_incomplete_dom_fails_instead_of_silent_skip(self):
        with self.assertRaises(ValueError):
            parse_html(fixture().replace('class="link"','class="changed"'))
        with self.assertRaises(ValueError):
            pagination('<div>加载中</div>')

    def test_favorites_scan_next_page_and_stop_immediately_when_all_found(self):
        c = self.collector
        c._open, c._reset = Mock(), Mock()
        p = parse_html(fixture())['123456789']
        c._query = Mock(side_effect=[({'other':p},{'current':1,'total':3,'has_next':True}),
                                    ({'123456789':p},{'current':2,'total':3,'has_next':True})])
        result = {}
        self.assertEqual(c._favorites({'123456789'},result),set())
        self.assertEqual(c._query.call_count,2)
        self.assertEqual(result['shopee:123456789']['product']['origin'],'favorite')

    def test_incomplete_favorites_do_not_become_not_found(self):
        c = self.collector
        c._open, c._reset = Mock(), Mock()
        c._query = Mock(return_value=({}, {'current':1,'total':1,'has_next':False}))
        with self.assertRaisesRegex(RuntimeError,'数量'):
            c._favorites({'123456789'}, {})

    def test_search_exact_id_not_first_result(self):
        c = self.collector
        c._open,c._reset,c._ele = Mock(),Mock(),Mock()
        c._query = Mock(return_value=({'123456789':{'name':'other'}},{'current':1,'total':1,'has_next':False}))
        self.assertIsNone(c._search('999999999'))

    def test_failed_query_response_is_not_accepted(self):
        packet = Mock()
        packet.response.status=200
        packet.response.body={'ok':False,'code':'permission','data':{'list':[]}}
        with self.assertRaisesRegex(RuntimeError,'查询失败'):
            self.collector._response(packet)

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_shopee_once_does_not_invoke_any_remote_sink(self,bitable,sheets,messages):
        cfg=deepcopy(self.cfg)
        cfg['app']['platforms']=['shopee']
        for key in ('feishu_output','feishu_sheets','feishu_messages'):
            cfg[key]['enabled']=True
        source=Mock()
        source.read_records.return_value=[{'record_id':'r','fields':{'平台':'shopee','商品ID':'123456789','监控开关':'开启','推送开关':'开启','数据推送人':[{'id':'ou_test'}]}}]
        factory=Mock()
        factory.return_value.collect.return_value={'shopee:123456789':{'status':'ok','product':parse_html(fixture())['123456789']}}
        result=run_once(cfg,source=source,registry={'shopee':factory},sink=Mock())
        self.assertEqual(result['status'],'ok')
        bitable.assert_not_called(); sheets.assert_not_called(); messages.assert_not_called()

    def test_direct_message_replay_also_excludes_shopee(self):
        source=Mock()
        source.read_records.return_value=[{'fields':{'平台':'shopee','商品ID':'123456789','监控开关':'开启','推送开关':'开启','数据推送人':[{'id':'ou_test'}]}}]
        result={'run_id':'test','products':{'shopee:123456789':{'status':'ok','platform':'shopee','product_id':'123456789','product':parse_html(fixture())['123456789']}}}
        routes=FeishuMessageSink(self.cfg,client=Mock(),source=source)._routes(result,{'errors':[]})
        self.assertEqual(routes,{})

    def test_new_tab_session_saves_storage_and_closes_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cfg['app']['session_dir'] = Path(directory)
            session = BrowserSession(self.cfg,'shopee',origin_url='https://www.shopdora.com/')
            browser = SimpleNamespace(quit=Mock())
            session.page = SimpleNamespace(browser=browser, run_js=Mock(side_effect=['https://www.shopdora.com',{'test':'value'}]))
            session.__exit__(None,None,None)
            browser.quit.assert_called_once()
            self.assertTrue((session.path/'session_storage.json').exists())


if __name__=='__main__':
    unittest.main()
