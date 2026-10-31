from __future__ import annotations

import json
import unittest

import test_procurement_mail as fixtures
from mailagent.engine import Engine
from mailagent.natural_control import execute_queries
from mailagent.table_updates import bulk_supplier_email_plan
from procurementdb import Database,DataError
from procurementdb.tables import parse_table
from procurementdb.templates import csv_data
from procurementdb.updates import apply_updates,attachment_changes


class TableUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProcurementMailTests('test_authenticated_control_prepares_template_and_sends_without_ai')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg=self.fixture.cfg
        self.cfg.control_mode='natural_language'
        self.cfg.control_actions=['inquire','query','answer','update_data']
        self.engine=Engine(self.cfg,self.fixture.store,self.fixture.ai,self.fixture.factory)
        self.fixture.engine=self.engine
        self.ai=self.fixture.ai
        self.ai.route_control.return_value={'action':'update_data','resource':'suppliers','fields':['email']}

    def send(self,text='请按附件更新供应商表的邮箱',data=None,plan=None,sender='controller@example.com'):
        if plan is not None:self.ai.route_control.return_value=plan
        item=self.fixture.archive(text,'供应商资料',sender,data,filename='供应商表.csv')
        self.engine.process_one(item)
        return item

    def emails(self):
        with Database(self.fixture.path,readonly=True) as db:
            return dict(db.connection.execute('SELECT id,email FROM suppliers'))

    def partial(self,rows):
        return csv_data(['供应商唯一编号','供应商邮箱'],rows)

    def update(self,table,changes,key='manual-test'):
        with Database(self.fixture.path) as db:
            return apply_updates(db,account=self.cfg.address,controller='controller@example.com',
                                 message_key=key,table=table,changes=changes)

    def seed_requisition(self):
        with Database(self.fixture.path) as db,db.transaction():
            db._insert_rows('requisitions',parse_table(fixtures.table_data('requisitions',[self.fixture.req()]),'r.csv','requisitions'))

    def seed_quotation(self):
        self.seed_requisition()
        with Database(self.fixture.path) as db,db.transaction():
            db._insert_rows('quotations',parse_table(fixtures.table_data('quotations',[
                ['Q1','R1','S1','M-01','10','10','2027-04-30','1','CNY','1','13%']]),'q.csv','quotations'))

    def test_partial_csv_updates_two_existing_supplier_emails(self):
        item=self.send(data=self.partial([['S1','new1@example.com'],['S2','new2@example.com']]))
        result=item['control_result']
        self.assertTrue(result['ok'])
        self.assertEqual((result['matched'],result['updated'],result['inserted']),(2,2,0))
        self.assertEqual(self.emails(),{'S1':'new1@example.com','S2':'new2@example.com'})
        self.fixture.factory.assert_not_called()

    def test_full_csv_only_updates_requested_column(self):
        data=fixtures.table_data('suppliers',[['S1','不应更新的公司','new@example.com','不应更新的称呼']])
        item=self.send(data=data)
        self.assertTrue(item['control_result']['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            row=db.connection.execute("SELECT company,salutation FROM suppliers WHERE id='S1'").fetchone()
            self.assertEqual(tuple(row),('甲公司','张先生'))

    def test_invalid_email_rolls_back_whole_batch(self):
        before=self.emails()
        item=self.send(data=self.partial([['S1','new@example.com'],['S2','not-an-address']]))
        self.assertFalse(item['control_result']['ok'])
        self.assertEqual(self.emails(),before)
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT COUNT(*) FROM data_update_requests').fetchone()[0],0)

    def test_plain_email_can_bulk_change_only_all_supplier_email(self):
        request='请把全部供应商邮箱统一改为 xyinchina@163.com'
        plan=bulk_supplier_email_plan(request)
        self.assertEqual(plan,{'action':'update_data','resource':'suppliers',
                               'bulk_change':{'scope':'all','field':'email','value':'xyinchina@163.com'}})
        item=self.send(text=request)
        self.assertTrue(item['control_result']['ok'])
        self.assertEqual(self.emails(),{'S1':'xyinchina@163.com','S2':'xyinchina@163.com'})
        self.ai.route_control.assert_not_called()
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT COUNT(*) FROM data_update_requests').fetchone()[0],1)

    def test_bulk_update_requires_explicit_scope_and_value(self):
        self.assertIsNone(bulk_supplier_email_plan('如何把全部供应商邮箱改为 demo@example.com？'))
        self.assertIsNone(bulk_supplier_email_plan('请把供应商 S1 邮箱改为 demo@example.com'))
        self.assertIsNone(bulk_supplier_email_plan('请把所有供应商邮箱改为 demo@example.com 或 other@example.com'))

    def test_unknown_id_rolls_back_and_does_not_insert(self):
        before=self.emails()
        item=self.send(data=self.partial([['S1','new@example.com'],['unknown','other@example.com']]))
        self.assertFalse(item['control_result']['ok'])
        self.assertIn('未匹配到编号',item['control_result']['reply_body'])
        self.assertEqual(self.emails(),before)

    def test_blank_selected_field_does_not_erase_existing_values(self):
        item=self.send(data=self.partial([['S1','']]))
        self.assertFalse(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'supplier1@example.com')

    def test_syntactically_valid_unusual_email_is_preserved(self):
        item=self.send(data=self.partial([['S1','xyinchina@164.com']]))
        self.assertTrue(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'xyinchina@164.com')

    def test_duplicate_keys_are_rejected(self):
        item=self.send(data=self.partial([['S1','new@example.com'],['S1','other@example.com']]))
        self.assertFalse(item['control_result']['ok'])
        self.assertIn('重复',item['control_result']['reply_body'])

    def test_noop_is_reported_and_audit_is_real(self):
        item=self.send(data=self.partial([['S1','supplier1@example.com']]))
        self.assertEqual((item['control_result']['updated'],item['control_result']['unchanged']),(0,1))
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT COUNT(*) FROM data_update_requests').fetchone()[0],1)

    def test_success_audit_captures_before_and_after(self):
        item=self.send(data=self.partial([['S1','new@example.com']]))
        with Database(self.fixture.path,readonly=True) as db:
            record=db.connection.execute('SELECT * FROM data_update_changes').fetchone()
            self.assertEqual(json.loads(record['before_json']),{'email':'supplier1@example.com'})
            self.assertEqual(json.loads(record['after_json']),{'email':'new@example.com'})
            self.assertEqual(record['request_id'],item['control_result']['audit_id'])
        evidence=execute_queries(self.cfg,[{'resource':'data_updates','filters':{'audit_id':item['control_result']['audit_id']}}])
        self.assertEqual(evidence[0]['total'],1)

    def test_retry_after_commit_does_not_repeat_updates(self):
        item=self.send(data=self.partial([['S1','new@example.com']]))
        result=item['control_result']
        item.pop('control_result')
        self.engine.process_one(item)
        self.assertTrue(item['control_result']['duplicate'])
        self.assertEqual(item['control_result']['audit_id'],result['audit_id'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT COUNT(*) FROM data_update_changes').fetchone()[0],1)

    def test_same_mail_cannot_mutate_to_different_changes(self):
        changes=[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}]
        self.update('suppliers',changes)
        changes[0]['values']['email']='other@example.com'
        with self.assertRaises(DataError):self.update('suppliers',changes)
        self.assertEqual(self.emails()['S1'],'new@example.com')

    def test_inline_update_takes_explicit_id_and_email(self):
        plan={'action':'update_data','resource':'suppliers','changes':[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}]}
        item=self.send('把供应商S1的邮箱改为new@example.com',plan=plan)
        self.assertTrue(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'new@example.com')

    def test_subject_only_inline_update_uses_subject_id_and_value(self):
        self.ai.route_control.return_value={'action':'update_data','resource':'suppliers','changes':[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}]}
        item=self.fixture.archive('','把供应商S1的邮箱改为new@example.com','controller@example.com')
        self.engine.process_one(item)
        self.assertTrue(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'new@example.com')

    def test_subject_only_attachment_update_uses_requested_field(self):
        item=self.fixture.archive('','请按附件更新供应商邮箱','controller@example.com',
                                  self.partial([['S1','new@example.com']]),filename='供应商表.csv')
        self.engine.process_one(item)
        self.assertTrue(item['control_result']['ok'])
        self.assertEqual(item['control_result']['updated'],1)
        self.assertEqual(self.emails()['S1'],'new@example.com')

    def test_model_cannot_invent_inline_new_value_or_id(self):
        for changes in ([{'key':{'id':'S1'},'values':{'email':'invented@example.com'}}],
                        [{'key':{'id':'S2'},'values':{'email':'new@example.com'}}]):
            item=self.send('把供应商S1的邮箱改为new@example.com',plan={'action':'update_data','resource':'suppliers','changes':changes})
            self.assertFalse(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'supplier1@example.com')

    def test_identifier_prefix_is_not_authorization_for_another_record(self):
        plan={'action':'update_data','resource':'suppliers','changes':[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}]}
        item=self.send('请把S10的邮箱改为new@example.com',plan=plan)
        self.assertFalse(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'supplier1@example.com')

    def test_model_cannot_change_other_attachment_fields(self):
        plan={'action':'update_data','resource':'suppliers','fields':['email','company']}
        data=fixtures.table_data('suppliers',[['S1','新公司','new@example.com','称呼']])
        item=self.send('请按附件更新供应商邮箱',data=data,plan=plan)
        self.assertFalse(item['control_result']['ok'])

    def test_explicit_clear_only_nullable_field(self):
        plan={'action':'update_data','resource':'suppliers','changes':[{'key':{'id':'S1'},'values':{'salutation':None}}]}
        item=self.send('请清空供应商S1的称呼',plan=plan)
        self.assertTrue(item['control_result']['ok'])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertIsNone(db.connection.execute("SELECT salutation FROM suppliers WHERE id='S1'").fetchone()[0])

    def test_do_not_update_request_cannot_write(self):
        for text in ('只查看更新差异，暂不修改','请先不要更新供应商邮箱'):
            item=self.send(text,data=self.partial([['S1','new@example.com']]))
            self.assertFalse(item['control_result']['ok'])
        self.assertEqual(self.emails()['S1'],'supplier1@example.com')

    def test_unauthorized_controller_cannot_write(self):
        item=self.fixture.archive('更新邮箱','[AGENT] 更新','stranger@example.com',self.partial([['S1','new@example.com']]))
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'control_rejected_sender')
        self.ai.route_control.assert_not_called()
        self.assertEqual(self.emails()['S1'],'supplier1@example.com')

    def test_config_disables_update_action_or_table_or_dry_run(self):
        self.cfg.control_actions=['query','answer']
        item=self.send(data=self.partial([['S1','new@example.com']]))
        self.assertFalse(item['control_result']['ok'])
        self.cfg.control_actions.append('update_data')
        self.cfg.control_update_tables=[]
        self.assertFalse(self.send(data=self.partial([['S1','new@example.com']]))['control_result']['ok'])
        self.cfg.control_update_tables=['suppliers'];self.cfg.dry_run=True
        self.assertFalse(self.send(data=self.partial([['S1','new@example.com']]))['control_result']['ok'])

    def test_max_rows_formulas_and_duplicate_headers_rejected(self):
        self.cfg.control_update_max_rows=1
        self.assertFalse(self.send(data=self.partial([['S1','new@example.com'],['S2','other@example.com']]))['control_result']['ok'])
        self.cfg.control_update_max_rows=1000
        self.assertFalse(self.send(data=self.partial([['S1','=malicious()']]))['control_result']['ok'])
        data=csv_data(['供应商唯一编号','供应商邮箱','email'],[['S1','new@example.com','other@example.com']])
        self.assertFalse(self.send(data=data)['control_result']['ok'])

    def test_material_group_and_status_names_can_be_updated(self):
        self.update('material_groups',[{'key':{'id':'G1'},'values':{'name':'电机设备'}}])
        self.update('statuses',[{'key':{'id':10},'values':{'name':'等待询价'}}],key='status')
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute("SELECT name FROM material_groups WHERE id='G1'").fetchone()[0],'电机设备')
            self.assertEqual(db.connection.execute('SELECT name FROM statuses WHERE id=10').fetchone()[0],'等待询价')

    def test_relationship_change_validates_both_keys_and_foreign_key(self):
        self.update('supplier_groups',[{'key':{'supplier_id':'S1','group_id':'G1'},'values':{'group_id':'G2'}}])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute("SELECT group_id FROM supplier_groups WHERE supplier_id='S1'").fetchone()[0],'G2')
        with self.assertRaises(DataError):
            self.update('supplier_groups',[{'key':{'supplier_id':'S2','group_id':'G1'},'values':{'group_id':'missing'}}],key='bad-group')

    def test_relationship_attachment_supports_old_and_new_group_columns(self):
        data=csv_data(['供应商唯一编号','原物料分组唯一编号','新物料分组唯一编号'],[['S1','G1','G2']])
        changes=attachment_changes(data,'关系.csv','supplier_groups',['group_id'])
        self.update('supplier_groups',changes)
        self.assertEqual(changes,[{'key':{'supplier_id':'S1','group_id':'G1'},'values':{'group_id':'G2'}}])

    def test_requisition_update_validates_numbers_and_status_foreign_key(self):
        self.seed_requisition()
        self.update('requisitions',[{'key':{'id':'R1'},'values':{'quantity':'12.50','required_date':'2027-06-01'}}])
        for values in ({'quantity':'-1'},{'status_id':666},{'group_id':'G2'}):
            with self.subTest(values=values),self.assertRaises(DataError):
                self.update('requisitions',[{'key':{'id':'R1'},'values':values}],key=str(values))
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT quantity FROM requisitions').fetchone()[0],'12.5')

    def test_inquired_requisition_fields_remain_document_snapshots(self):
        self.seed_requisition()
        with Database(self.fixture.path) as db:db.prepare_inquiries(['R1'],'initial-inquiry')
        with self.assertRaises(DataError):
            self.update('requisitions',[{'key':{'id':'R1'},'values':{'quantity':'20'}}])

    def test_quote_updates_decimal_and_tax_but_cannot_change_identity(self):
        self.seed_quotation()
        self.update('quotations',[{'key':{'id':'Q1'},'values':{'price':'12.30','tax_rate':'9%'}}])
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(tuple(db.connection.execute('SELECT price,tax_rate FROM quotations').fetchone()),('12.3','0.09'))
        with self.assertRaises(DataError):
            self.update('quotations',[{'key':{'id':'Q1'},'values':{'supplier_id':'S2'}}],key='identity')

    def test_order_referenced_quote_guard_and_order_update(self):
        self.seed_quotation()
        with Database(self.fixture.path) as db,db.transaction():
            db._insert_rows('orders',parse_table(fixtures.table_data('orders',[
                ['R1','O1','S1','M-01','2','2027-05-01','Q1','10','CNY','1','13%']]),'o.csv','orders'))
        with self.assertRaises(DataError):
            self.update('quotations',[{'key':{'id':'Q1'},'values':{'price':'11'}}])
        self.update('orders',[{'key':{'id':'O1'},'values':{'delivery_date':'2027-05-05'}}],key='order')
        with Database(self.fixture.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT delivery_date FROM orders').fetchone()[0],'2027-05-05')
            self.assertEqual(db.check()['foreign_key_errors'],[])

    def test_only_existing_business_tables_are_writable(self):
        for resource in ('inquiry_outbox','runtime_metadata','sqlite_schema','suppliers; DROP TABLE suppliers'):
            with self.subTest(resource=resource),self.assertRaises(DataError):
                self.update(resource,[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}])

    def test_changes_do_not_retarget_existing_inquiry_but_future_uses_new_email(self):
        self.seed_requisition()
        with Database(self.fixture.path) as db:db.prepare_inquiries(['R1'],'initial-inquiry')
        self.update('suppliers',[{'key':{'id':'S1'},'values':{'email':'new@example.com'}}])
        with Database(self.fixture.path) as db:
            self.assertEqual(db.connection.execute("SELECT recipient FROM inquiry_outbox WHERE inquiry_id IN (SELECT id FROM inquiries WHERE supplier_id='S1')").fetchone()[0],'supplier1@example.com')
            db.import_requisitions(fixtures.table_data('requisitions',[self.fixture.req('R2')]),'new.csv')
            new=db.prepare_inquiries(['R2'],'future')
            row=db.connection.execute("SELECT recipient_email FROM inquiries WHERE supplier_id='S1' AND batch_id=?",(new['batch_id'],)).fetchone()
            self.assertEqual(row[0],'new@example.com')


if __name__=='__main__':unittest.main()
