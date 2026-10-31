from __future__ import annotations

from datetime import datetime,timedelta,timezone
from pathlib import Path
import unittest

import test_procurement_mail as fixtures
from mailagent.config import Config,ConfigError
from mailagent.engine import Engine
from mailagent.natural_control import execute_queries
from procurementdb import Database,DataError
from procurementdb.tables import parse_table

table_data=fixtures.table_data


class NaturalControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProcurementMailTests('test_authenticated_control_prepares_template_and_sends_without_ai')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg=self.fixture.cfg
        self.cfg.control_mode='natural_language'
        self.engine=Engine(self.cfg,self.fixture.store,self.fixture.ai,self.fixture.factory)
        self.fixture.engine=self.engine
        self.ai=self.fixture.ai
        self.ai.route_control.return_value={'action':'query','queries':[{'resource':'quotations','filters':{}}]}
        self.ai.answer_control.return_value={'answer':'这是基于实际记录的回答。'}

    def mail(self,body='查一下报价单信息',subject='报价查询',sender='controller@example.com',data=None,**kwargs):
        return self.fixture.archive(body,subject,sender,data,**kwargs)

    def process(self,item):
        self.engine.process_one(item)
        return item['control_result']

    def seed_quotes(self,count=1):
        with Database(self.fixture.path) as db,db.transaction():
            db._insert_rows('requisitions',parse_table(table_data('requisitions',[self.fixture.req()]),'r.csv','requisitions'))
            rows=[[f'Q{i}','R1','S1','M-01','0.10','10','2027-04-30','1','CNY','1','13%'] for i in range(count)]
            db._insert_rows('quotations',parse_table(table_data('quotations',rows),'q.csv','quotations'))

    def test_plain_request_needs_no_yaml_token_or_subject_prefix(self):
        self.ai.route_control.return_value={'action':'inquire'}
        item=self.mail('请按附件请购单发起询价。','采购申请',data=table_data('requisitions',[self.fixture.req()]))
        result=self.process(item)
        self.assertTrue(result['ok'])
        self.assertEqual(result['供应商询价封数'],2)
        self.assertIn('已生成2封',result['reply_body'])
        self.engine.deliver()
        self.assertEqual(len(self.fixture.inquiries()),2)
        self.ai.parse_command.assert_not_called()

    def test_subject_only_query_reaches_model_and_database(self):
        item=self.mail('', '查询供应商S1的信息')
        self.ai.route_control.return_value={'action':'query','queries':[{'resource':'suppliers','filters':{'supplier_id':'S1'}}]}
        result=self.process(item)
        self.assertTrue(result['ok'])
        self.assertIn('甲公司',result['reply_body'])
        request,context=self.ai.route_control.call_args.args
        self.assertEqual(request,'查询供应商S1的信息')
        self.assertEqual(context['body'],'')
        self.assertEqual(context['subject'],'查询供应商S1的信息')
        self.assertFalse(context['subject_is_inherited'])

    def test_subject_only_question_gets_model_reply(self):
        self.ai.route_control.return_value={'action':'answer','queries':[]}
        self.ai.answer_control.return_value={'answer':'最小起订量是供应商接受的最低订购数量。'}
        result=self.process(self.mail('', '什么是最小起订量？'))
        self.assertTrue(result['ok'])
        self.assertIn('最低订购数量',result['reply_body'])
        self.assertEqual(self.ai.answer_control.call_args.args[0],'什么是最小起订量？')

    def test_subject_only_inquiry_with_attachment_executes(self):
        self.ai.route_control.return_value={'action':'inquire'}
        result=self.process(self.mail('', '请按附件请购单发起询价',data=table_data('requisitions',[self.fixture.req()])))
        self.assertTrue(result['ok'])
        self.assertEqual(result['供应商询价封数'],2)
        self.engine.deliver()
        self.assertEqual(len(self.fixture.inquiries()),2)

    def test_subject_and_body_are_both_used_in_one_request(self):
        self.seed_quotes()
        self.ai.route_control.return_value={'action':'query','queries':[{'resource':'quotations','filters':{'requisition_id':'R1'}}]}
        result=self.process(self.mail('只查请购单R1。','查询报价单'))
        self.assertTrue(result['ok'])
        self.assertEqual(self.ai.route_control.call_args.args[0],'查询报价单\n只查请购单R1。')
        self.assertIn('报价单编号：Q0',result['reply_body'])

    def test_body_no_send_overrides_inquiry_subject(self):
        self.ai.route_control.return_value={'action':'inquire'}
        result=self.process(self.mail('只检查格式，暂不发送询价。','请按附件发起询价',data=table_data('requisitions',[self.fixture.req()])))
        self.assertFalse(result['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['inquiries'],0)

    def test_empty_subject_and_body_request_more_information(self):
        result=self.process(self.mail('  ','  '))
        self.assertFalse(result['ok'])
        self.assertIn('主题或正文',result['reply_body'])
        self.ai.route_control.assert_not_called()

    def test_empty_reply_does_not_repeat_inherited_subject_command(self):
        for title in ('Re: 请按附件发起询价','回复：请更新供应商邮箱','Fwd: 查询报价单'):
            with self.subTest(title=title):
                result=self.process(self.mail('',title))
                self.assertFalse(result['ok'])
                self.assertIn('正文为空的回复邮件',result['reply_body'])
        self.ai.route_control.assert_not_called()

    def test_subject_only_request_is_retained_in_followup_context(self):
        first=self.mail('','查询报价单')
        self.process(first)
        job=next(job for _,job in self.fixture.store.jobs())
        self.ai.route_control.return_value={'action':'answer','queries':[]}
        self.process(self.mail('这些结果代表什么？','Re: 查询报价单',reply_id=job['message_id']))
        context=self.ai.route_control.call_args.args[1]
        self.assertTrue(context['subject_is_inherited'])
        self.assertEqual(context['conversation'][0]['request'],'查询报价单')

    def test_empty_database_returns_real_zero_without_invented_quote(self):
        result=self.process(self.mail())
        self.assertTrue(result['ok'])
        self.assertIn('匹配0条',result['reply_body'])
        self.ai.answer_control.assert_not_called()

    def test_query_returns_real_supplier_currency_tax_and_exact_price(self):
        self.seed_quotes()
        self.ai.route_control.return_value={'action':'query','queries':[{'resource':'quotations','filters':{'requisition_id':'R1'}}]}
        result=self.process(self.mail('查R1的报价信息'))
        for text in ('Q0','甲公司','单价：0.1','币种：CNY','税率：0.13','单位：件'):
            self.assertIn(text,result['reply_body'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['quotations'],1)

    def test_queries_cannot_supply_sql_or_unknown_resources(self):
        for spec in ({'resource':'quotations','sql':'DROP TABLE suppliers'},
                     {'resource':'sqlite_schema'},
                     {'resource':'quotations','filters':{'sql':'1=1'}}):
            with self.subTest(spec=spec):
                self.ai.route_control.return_value={'action':'query','queries':[spec]}
                self.assertFalse(self.process(self.mail())['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['suppliers'],2)

    def test_sql_fragment_in_filter_is_only_a_literal(self):
        self.seed_quotes()
        evidence=execute_queries(self.cfg,[{'resource':'quotations','filters':{'quotation_id':"' OR 1=1 --"}}])
        self.assertEqual(evidence[0]['total'],0)

    def test_display_limit_reports_total_and_partial_coverage(self):
        self.seed_quotes(4)
        self.cfg.control_query_limit=2
        evidence=execute_queries(self.cfg,[{'resource':'quotations','limit':100}])
        self.assertEqual((evidence[0]['total'],evidence[0]['shown'],evidence[0]['truncated']),(4,2,True))

    def test_database_answer_contains_verified_evidence(self):
        self.seed_quotes()
        self.ai.route_control.return_value={'action':'answer','queries':[{'resource':'quotations','filters':{'requisition_id':'R1'}}]}
        self.ai.answer_control.return_value={'answer':'R1目前有一条报价，请留意含税口径。'}
        result=self.process(self.mail('R1的报价情况怎么样？'))
        self.assertIn('R1目前有一条',result['reply_body'])
        self.assertIn('数据库核对结果',result['reply_body'])
        self.assertEqual(self.ai.answer_control.call_args.args[1][0]['rows'][0]['quotation_id'],'Q0')

    def test_general_qa_works_without_procurement_enabled(self):
        self.cfg.procurement_enabled=False
        self.ai.route_control.return_value={'action':'answer','queries':[]}
        self.ai.answer_control.return_value={'answer':'含税单价包含税款，未税单价不含税款。'}
        result=self.process(self.mail('含税单价和未税单价有什么区别？','采购问题'))
        self.assertTrue(result['ok'])
        self.assertIn('含税单价包含税款',result['reply_body'])
        self.assertEqual(self.ai.answer_control.call_args.args[1],[])

    def test_explicit_dont_send_blocks_model_misclassified_write(self):
        self.ai.route_control.return_value={'action':'inquire'}
        item=self.mail('只检查附件请购单，暂不发送询价。',data=table_data('requisitions',[self.fixture.req()]))
        self.assertFalse(self.process(item)['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['inquiries'],0)

    def test_quoted_old_request_does_not_authorize_new_inquiry(self):
        self.ai.route_control.return_value={'action':'inquire'}
        item=self.mail('帮我看看现在的报价。\n> 请按附件发起询价。','Re: 发起请购',data=table_data('requisitions',[self.fixture.req()]))
        self.assertFalse(self.process(item)['ok'])

    def test_inquire_requires_attachment_and_disallows_model_recipients(self):
        for plan in ({'action':'inquire'},{'action':'inquire','recipients':['stranger@example.com']}):
            self.ai.route_control.return_value=plan
            result=self.process(self.mail('请帮我发起询价'))
            self.assertFalse(result['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['inquiries'],0)

    def test_actions_are_configured_locally(self):
        self.cfg.control_actions=['query','answer']
        self.ai.route_control.return_value={'action':'inquire'}
        item=self.mail('请按附件发起询价',data=table_data('requisitions',[self.fixture.req()]))
        self.assertFalse(self.process(item)['ok'])
        self.assertIn('未在config.yml',item['control_result']['reply_body'])

    def test_non_controller_never_routes_control(self):
        item=self.mail('请按附件发起询价','[AGENT] 请购','supplier1@example.com',table_data('requisitions',[self.fixture.req()]))
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'control_rejected_sender')
        self.ai.route_control.assert_not_called()

    def test_historical_mail_is_not_replayed_after_mode_change(self):
        item=self.mail(received=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'historical_control_archived_only')
        self.ai.route_control.assert_not_called()

    def test_retry_uses_cached_result_without_second_execution(self):
        self.ai.route_control.return_value={'action':'inquire'}
        item=self.mail('请按附件发起询价',data=table_data('requisitions',[self.fixture.req()]))
        self.process(item)
        self.process(item)
        self.assertEqual(self.ai.route_control.call_count,1)
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.summary()['inquiries'],2)

    def test_reply_headers_and_same_controller_thread_context(self):
        first=self.mail('先查一下报价单','报价情况')
        self.process(first)
        job=next(job for _,job in self.fixture.store.jobs())
        self.assertEqual(job['in_reply_to'],first['parsed']['message_id'])
        self.ai.route_control.return_value={'action':'answer','queries':[]}
        second=self.mail('为什么现在没有报价？','Re: 报价情况',reply_id=job['message_id'])
        self.process(second)
        context=self.ai.route_control.call_args.args[1]['conversation']
        self.assertEqual(len(context),1)
        self.assertIn('先查一下',context[0]['request'])
        self.cfg.allowed_senders.append('other-controller@example.com')
        third=self.mail('继续解释','Re: 报价情况',sender='other-controller@example.com',reply_id=job['message_id'])
        self.process(third)
        self.assertEqual(self.ai.route_control.call_args.args[1]['conversation'],[])

    def test_model_reply_recipient_is_not_accepted(self):
        self.ai.route_control.return_value={'action':'answer','queries':[],'recipients':['stranger@example.com']}
        result=self.process(self.mail('请回答我的采购问题'))
        self.assertFalse(result['ok'])
        self.assertTrue(all(job['recipients']==['controller@example.com'] for _,job in self.fixture.store.jobs()))

    def test_model_reply_never_exposes_configured_secrets(self):
        self.ai.route_control.return_value={'action':'answer','queries':[]}
        self.ai.answer_control.return_value={'answer':'这里不能发送：'+self.cfg.token+' '+self.cfg.api_key}
        self.process(self.mail('解释一下口令的用途'))
        job=next(job for _,job in self.fixture.store.jobs())
        self.assertNotIn(self.cfg.token,job['body'])
        self.assertNotIn(self.cfg.api_key,job['body'])

    def test_config_overrides_old_procurement_enable_state(self):
        self.cfg.procurement_enabled=False
        self.ai.route_control.return_value={'action':'inquire'}
        result=self.process(self.mail('请按附件发起询价',data=table_data('requisitions',[self.fixture.req()])))
        self.assertFalse(result['ok'])

    def test_config_controls_supplier_send_batch_size(self):
        self.cfg.procurement_enabled=True
        self.cfg.procurement_max_sends=1
        self.ai.route_control.return_value={'action':'inquire'}
        self.process(self.mail('请按附件发起询价',data=table_data('requisitions',[self.fixture.req()])))
        self.engine.deliver()
        self.assertEqual(len(self.fixture.inquiries()),1)

    def test_supported_resources_and_filters_query_actual_relations(self):
        self.seed_quotes()
        with Database(self.fixture.path) as db,db.transaction():
            db._insert_rows('orders',parse_table(table_data('orders',[
                ['R1','O1','S1','M-01','2','2027-04-30','Q0','0.1','CNY','1','13%']]),'o.csv','orders'))
        specs=[{'resource':'suppliers','filters':{'group_id':'G1'}},
               {'resource':'orders','filters':{'quotation_id':'Q0'}},
               {'resource':'requisitions','filters':{'status_id':10,'material_name':'伺服'}}]
        evidence=execute_queries(self.cfg,specs)
        self.assertEqual([r['total'] for r in evidence],[2,1,1])
        self.assertEqual(evidence[1]['rows'][0]['order_id'],'O1')

    def test_current_business_date_filter_uses_shanghai_timezone(self):
        self.seed_quotes()
        with Database(self.fixture.path) as db:
            db.connection.execute("UPDATE quotations SET created_at='2026-10-30T20:30:00+00:00'")
        today=execute_queries(self.cfg,[{'resource':'quotations','filters':{'created_from':'2026-10-31','created_to':'2026-10-31'}}])
        yesterday=execute_queries(self.cfg,[{'resource':'quotations','filters':{'created_to':'2026-10-30'}}])
        self.assertEqual(today[0]['total'],1)
        self.assertEqual(yesterday[0]['total'],0)

    def test_supplier_reply_does_not_use_controller_model_router(self):
        self.ai.route_control.return_value={'action':'inquire'}
        self.process(self.mail('请按附件发起询价',data=table_data('requisitions',[self.fixture.req()])))
        self.engine.deliver()
        message=self.fixture.inquiries()[0]
        self.ai.route_control.reset_mock()
        item=self.mail('报价见附件','Re: '+str(message['Subject']),str(message['To']),self.fixture.filled(message))
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'procurement_imported')
        self.ai.route_control.assert_not_called()

    def test_configuration_needs_no_control_token_in_natural_mode(self):
        path=self.cfg.path
        path.write_text('mail:\n  address: agent@163.com\n  authorization_code: dummy\ndeepseek:\n  api_key: dummy-key\ncontrol:\n  mode: natural_language\n  allowed_senders: [controller@example.com]\n  actions: [inquire, query, answer]\nprocurement:\n  enabled: true\n  max_sends_per_cycle: 3\n',encoding='utf-8')
        cfg=Config.load(path)
        self.assertEqual(cfg.missing(),[])
        self.assertEqual(cfg.procurement_max_sends,3)
        path.write_text('control:\n  mode: bad-mode\n',encoding='utf-8')
        with self.assertRaises(ConfigError):
            Config.load(path)


if __name__=='__main__':
    unittest.main()
