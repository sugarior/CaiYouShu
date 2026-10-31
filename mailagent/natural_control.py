"""Natural-language mail controls with bounded, parameterized database reads."""
from __future__ import annotations

from datetime import date,datetime,timedelta,timezone
import json
import re

from procurementdb import Database, DataError
from procurementdb.updates import update_schema
from .procurement import Procurement, ordinary_sender
from .store import now


QUERIES = {
    'material_groups': {
        'title':'物料分组','from':'material_groups g','select':'g.id AS group_id,g.name AS group_name','order':'g.id',
        'filters':{'group_id':('g.id','exact'),'group_name':('g.name','contains')},
    },
    'statuses': {
        'title':'状态','from':'statuses st','select':'st.id AS status_id,st.name AS status','order':'st.id',
        'filters':{'status_id':('st.id','integer'),'name':('st.name','contains')},
    },
    'supplier_groups': {
        'title':'供应商物料对应关系','from':'supplier_groups sg JOIN suppliers s ON s.id=sg.supplier_id JOIN material_groups g ON g.id=sg.group_id',
        'select':'sg.supplier_id,s.company,sg.group_id,g.name AS group_name','order':'sg.supplier_id,sg.group_id',
        'filters':{'supplier_id':('sg.supplier_id','exact'),'supplier_name':('s.company','contains'),
                   'group_id':('sg.group_id','exact'),'group_name':('g.name','contains')},
    },
    'data_updates': {
        'title':'数据更新记录','from':'data_update_requests u JOIN data_update_changes c ON c.request_id=u.id',
        'select':'u.id AS audit_id,u.table_name,u.controller,u.updated_at,c.record_key_json,c.before_json,c.after_json',
        'order':'u.updated_at DESC,c.row_number',
        'filters':{'audit_id':('u.id','exact'),'table_name':('u.table_name','exact'),
                   'updated_from':('u.updated_at','since'),'updated_to':('u.updated_at','until')},
    },
    'quotations': {
        'title': '报价单',
        'from': 'quotations q JOIN suppliers s ON s.id=q.supplier_id JOIN requisitions r ON r.id=q.requisition_id',
        'select': 'q.id AS quotation_id,q.requisition_id,q.supplier_id,s.company,r.material_name,r.unit,q.model,q.price,q.quantity,q.delivery_date,q.currency,q.tax_included,q.tax_rate,q.inquiry_id,q.created_at',
        'order': 'q.created_at DESC,q.id',
        'filters': {'quotation_id': ('q.id','exact'), 'requisition_id': ('q.requisition_id','exact'),
                    'supplier_id': ('q.supplier_id','exact'), 'inquiry_id': ('q.inquiry_id','exact'),
                    'supplier_name': ('s.company','contains'), 'material_name': ('r.material_name','contains'),
                    'model': ('q.model','contains'), 'currency': ('q.currency','exact'),
                    'created_from': ('q.created_at','since'), 'created_to': ('q.created_at','until')},
    },
    'requisitions': {
        'title': '请购单',
        'from': 'requisitions r JOIN statuses st ON st.id=r.status_id JOIN material_groups g ON g.id=r.group_id',
        'select': 'r.id AS requisition_id,r.status_id,st.name AS status,r.group_id,g.name AS group_name,r.project,r.material_name,r.model,r.quantity,r.unit,r.required_date,r.brand',
        'order': 'r.id DESC',
        'filters': {'requisition_id': ('r.id','exact'), 'status_id': ('r.status_id','integer'),
                    'group_id': ('r.group_id','exact'), 'material_name': ('r.material_name','contains'),
                    'model': ('r.model','contains'), 'project': ('r.project','contains')},
    },
    'inquiries': {
        'title': '供应商询价进度',
        'from': 'inquiries i JOIN suppliers s ON s.id=i.supplier_id JOIN inquiry_outbox o ON o.inquiry_id=i.id',
        'select': "i.id AS inquiry_id,i.batch_id,i.supplier_id,s.company,i.recipient_email,i.state AS inquiry_state,o.state AS delivery_state,o.last_error,i.created_at,i.sent_at,(SELECT group_concat(ii.requisition_id,',') FROM inquiry_items ii WHERE ii.inquiry_id=i.id) AS requisition_ids",
        'order': 'i.created_at DESC,i.id',
        'filters': {'inquiry_id': ('i.id','exact'), 'batch_id': ('i.batch_id','exact'),
                    'supplier_id': ('i.supplier_id','exact'), 'supplier_name': ('s.company','contains'),
                    'state': ('i.state','exact'), 'delivery_state': ('o.state','exact'),
                    'requisition_id': ('EXISTS (SELECT 1 FROM inquiry_items fi WHERE fi.inquiry_id=i.id AND fi.requisition_id=?)','exists')},
    },
    'suppliers': {
        'title': '供应商',
        'from': 'suppliers s',
        'select': "s.id AS supplier_id,s.company,s.email,s.salutation,(SELECT group_concat(g.name,',') FROM supplier_groups sg JOIN material_groups g ON g.id=sg.group_id WHERE sg.supplier_id=s.id) AS material_groups",
        'order': 's.id',
        'filters': {'supplier_id': ('s.id','exact'), 'supplier_name': ('s.company','contains'),
                    'email': ('s.email','exact'),
                    'group_id': ('EXISTS (SELECT 1 FROM supplier_groups sg WHERE sg.supplier_id=s.id AND sg.group_id=?)','exists')},
    },
    'orders': {
        'title': '订单',
        'from': 'orders o JOIN suppliers s ON s.id=o.supplier_id JOIN requisitions r ON r.id=o.requisition_id',
        'select': 'o.id AS order_id,o.requisition_id,o.supplier_id,s.company,r.material_name,o.model,o.quantity,o.delivery_date,o.quotation_id,o.unit_price,o.currency,o.tax_included,o.tax_rate',
        'order': 'o.id DESC',
        'filters': {'order_id': ('o.id','exact'), 'requisition_id': ('o.requisition_id','exact'),
                    'quotation_id': ('o.quotation_id','exact'), 'supplier_id': ('o.supplier_id','exact'),
                    'supplier_name': ('s.company','contains'), 'material_name': ('r.material_name','contains')},
    },
}
LABELS = {'quotation_id':'报价单编号','requisition_id':'请购单编号','requisition_ids':'请购单编号',
          'audit_id':'审计编号','table_name':'业务表','controller':'操作邮箱','updated_at':'更新时间',
          'record_key_json':'记录编号','before_json':'更新前','after_json':'更新后',
          'supplier_id':'供应商编号','company':'供应商','email':'邮箱','recipient_email':'询价收件邮箱',
          'material_name':'物料','model':'型号','price':'单价','unit_price':'订单单价','quantity':'数量',
          'unit':'单位','currency':'币种','delivery_date':'交货日期','tax_included':'含税标识','tax_rate':'税率',
          'inquiry_id':'询价编号','batch_id':'批次','created_at':'创建时间','sent_at':'发信时间',
          'inquiry_state':'询价状态','delivery_state':'发送状态','last_error':'发送异常','status':'请购状态',
          'status_id':'状态编号','group_id':'分组编号','group_name':'物料分组','project':'项目',
          'required_date':'需求日期','brand':'品牌','salutation':'称呼','material_groups':'供货分组',
          'order_id':'订单编号'}
STATES = {'queued':'待发送','sending':'发送中','awaiting_quote':'待报价','quoted':'已报价',
          'needs_review':'待核对','cancelled':'已取消','pending':'待发送','sent':'SMTP已接受',
          'uncertain':'发送结果待核对','failed':'发送失败'}


def query_schema():
    return {name: {'description': value['title'], 'filters': list(value['filters'])} for name,value in QUERIES.items()}


def current_text(text):
    lines = []
    for line in text.splitlines():
        if re.search(r'^(?:On .+wrote:|在.{5,}写道[：:]|[-_ ]*(?:Original Message|原始邮件|原邮件)[-_ :：]*)$', line.strip(), re.I):
            break
        if not line.lstrip().startswith('>'):
            lines.append(line)
    return '\n'.join(lines).strip()


def request_parts(parsed):
    """A fresh subject can carry the command; inherited reply subjects are context."""
    subject=parsed.get('subject','').strip()
    body=current_text(parsed.get('text',''))
    inherited=bool(re.match(r'^\s*(re|fw|fwd|回复|转发)\s*[:：]',subject,re.I))
    action_subject='' if inherited else subject
    request=(subject+'\n'+body).strip()
    actionable=(action_subject+'\n'+body).strip()
    return subject,body,request,actionable,inherited


def explicit_inquiry(text):
    if re.search(r'(不要|先别|暂不|不必|禁止|无需|不需要).{0,16}(发送|询价|请购|采购)', text):
        return False
    if re.search(r'(仅|只).{0,8}(查看|检查|查询|分析|解释)', text):
        return False
    return bool(re.search(r'请购|采购申请|发起询价|开始询价|发送询价|进行询价|安排询价|自动询价|帮我.{0,12}询价|按.{0,20}询价|请.{0,8}(发|向|对|按|为).{0,20}询价', text))


def execute_queries(config, specs):
    if not isinstance(specs, list) or len(specs) > 3:
        raise DataError('一次最多查询3类业务信息')
    if specs and 'query' not in config.control_actions:
        raise DataError('config.yml 当前没有启用数据库查询动作')
    if not specs:
        return []
    if not (config.storage/'procurement.sqlite3').exists():
        raise DataError('采购数据库尚未初始化，暂时无法查询业务记录')
    output = []
    with Database(config.storage/'procurement.sqlite3', readonly=True) as db:
        owner=db.connection.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()
        if owner and owner[0]!=config.address:
            raise DataError('采购数据库属于另一个邮箱账号，未读取其数据')
        for spec in specs:
            if not isinstance(spec, dict) or set(spec)-{'resource','filters','limit'}:
                raise DataError('查询只接受资源、过滤条件和展示条数')
            resource=spec.get('resource')
            if not isinstance(resource,str) or resource not in QUERIES:
                raise DataError('不支持的数据库查询对象')
            definition=QUERIES[resource]
            filters=spec.get('filters',{})
            limit=spec.get('limit',config.control_query_limit)
            if type(limit) is not int or limit < 1:
                raise DataError('查询条数须为正整数')
            limit=min(limit,config.control_query_limit)
            if not isinstance(filters,dict) or set(filters)-set(definition['filters']):
                raise DataError('查询包含不支持的过滤字段')
            clauses,params=[],[]
            for field,value in filters.items():
                expression,kind=definition['filters'][field]
                if kind=='integer':
                    if type(value) is not int:
                        raise DataError('状态编号须为整数')
                    clauses.append(expression+'=?');params.append(value)
                    continue
                if not isinstance(value,str) or not value.strip() or len(value)>200:
                    raise DataError('查询条件须为1到200字符的文本')
                value=value.strip()
                if kind in {'since','until'}:
                    try: parsed=date.fromisoformat(value)
                    except ValueError: raise DataError('查询日期须为YYYY-MM-DD') from None
                    clauses.append('date('+expression+",'+8 hours')"+('>=?' if kind=='since' else '<=?'))
                    params.append(parsed.isoformat())
                elif kind=='contains':
                    clauses.append(expression+" LIKE ? ESCAPE '\\'")
                    params.append('%'+value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
                elif kind=='exists':
                    clauses.append(expression);params.append(value)
                else:
                    clauses.append(expression+'=?');params.append(value)
            where=' WHERE '+' AND '.join(clauses) if clauses else ''
            total=db.connection.execute('SELECT COUNT(*) FROM '+definition['from']+where,params).fetchone()[0]
            rows=[dict(row) for row in db.connection.execute('SELECT '+definition['select']+' FROM '+definition['from']+where+' ORDER BY '+definition['order']+' LIMIT ?',params+[limit])]
            # Bound the evidence sent to the model while preserving complete rows.
            bounded=[];characters=0
            for row in rows:
                cost=len(json.dumps(row,ensure_ascii=False))
                if characters+cost>16000:
                    break
                bounded.append(row);characters+=cost
            output.append({'resource':resource,'title':definition['title'],'filters':filters,
                           'total':total,'shown':len(bounded),'truncated':len(bounded)<total,'rows':bounded})
    return output


def render_evidence(evidence):
    sections=[]
    for result in evidence:
        title=f'{result["title"]}：匹配{result["total"]}条，本次展示{result["shown"]}条。'
        if result['truncated']:
            title+=' 这里只展示部分结果，可补充编号、供应商或物料进一步筛选。'
        lines=[title]
        for index,row in enumerate(result['rows'],1):
            values=[]
            for key,value in row.items():
                if value is None or value=='':
                    continue
                if key in {'inquiry_state','delivery_state'}:
                    value=STATES.get(value,value)
                values.append(LABELS.get(key,key)+'：'+str(value))
            lines.append(str(index)+'. '+'；'.join(values))
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections)


class NaturalControl:
    def __init__(self, engine):
        self.engine,self.config,self.store,self.ai=engine,engine.config,engine.store,engine.ai

    def history(self,item,sender):
        parsed=item['parsed']
        refs=' '.join(v for k,v in parsed.get('headers',[]) if k.lower() in {'in-reply-to','references'})
        ids=set(re.findall(r'<[^<>\s]+>',refs))
        if not ids:
            return []
        related=set()
        for _,job in self.store.jobs():
            if job.get('kind')=='receipt' and job.get('control_sender')==sender and job.get('message_id') in ids:
                related.add(job.get('source_mail'))
        history=[]
        for key in related:
            if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):
                continue
            previous=self.store.mail(key)
            if previous and previous['parsed'].get('from')==[sender] and previous.get('control_result'):
                history.append({'request':request_parts(previous['parsed'])[2][:4000],
                                'reply':previous['control_result'].get('reply_body','')[:8000]})
        return history[-4:]

    def process(self,item,sender,received_at):
        if ordinary_sender(self.config,item)!=sender:
            self.store.finish(item,'control_rejected_automatic_or_ambiguous')
            return
        previous=item.get('control_result')
        if previous is not None:
            self.engine.receipt(item,sender,previous)
            self.store.finish(item,'control_processed')
            return
        subject,body,request,actionable,inherited=request_parts(item['parsed'])
        if not actionable or len(request)>16000:
            message='请在邮件主题或正文说明本次要执行的动作或问题，合计长度请控制在16000字以内。'
            if inherited and not body:
                message='这是一封正文为空的回复邮件。请在正文写明本次请求，或另发一封邮件把请求写在主题中；不会重复执行原标题里的动作。'
            result={'ok':False,'reply_body':message}
        else:
            context={'allowed_actions':self.config.control_actions,'query_schema':query_schema(),
                     'subject':subject,'body':body,'subject_is_inherited':inherited,
                     'update_schema':{key:value for key,value in update_schema().items() if key in self.config.control_update_tables},
                     'current_time':datetime.now(timezone(timedelta(hours=8))).isoformat(),
                     'timezone':'Asia/Shanghai',
                     'attachments':[{'filename':v.get('filename'),'format':v.get('extraction',{}).get('format'),
                                     'status':v.get('extraction',{}).get('status'),
                                     'headers':[next((row for row in sheet.get('rows',[]) if any(str(cell).strip() for cell in row)),[])
                                                for sheet in v.get('extraction',{}).get('sheets',[])]}
                                    for v in item['parsed'].get('attachments',[])],
                     'conversation':self.history(item,sender)}
            plan=item.get('control_plan')
            if plan is None:
                from .table_updates import bulk_supplier_email_plan
                plan=bulk_supplier_email_plan(actionable)
                if plan is None:
                    plan=self.ai.route_control(request,context)
                if not isinstance(plan,dict):
                    raise DataError('模型没有返回可识别的控制动作')
                item['control_plan']=plan
                self.store.save_mail(item)
            try:
                action=plan.get('action')
                if action!='clarify' and action not in self.config.control_actions:
                    raise DataError('该动作未在config.yml的control.actions中启用')
                if action=='inquire':
                    if set(plan)-{'action','attachment'}:
                        raise DataError('请购动作不能指定收件人或任意执行参数')
                    if not explicit_inquiry(actionable):
                        raise DataError('为避免误发，本邮件未执行询价；如需实际发信，请明确写“请按附件发起询价”。')
                    command=dict(plan,action='inquire')
                    result=Procurement(self.config,self.store).submit(item,sender,command,received_at)
                    result['reply_body']=(f'已收到请购单，共{result["请购单行数"]}行。\n'
                                          f'已生成{result["供应商询价封数"]}封供应商询价，后台将按统一模板分批发送，并等待报价入库。\n'
                                          f'询价批次：{result["batch_id"]}'+
                                          ('\n这是重复提交，沿用原询价，不重复发信。' if result['重复请求'] else ''))
                elif action=='update_data':
                    from .table_updates import update_from_mail
                    result=update_from_mail(self.config,self.store,item,sender,plan,actionable)
                elif action in {'query','answer'}:
                    if set(plan)-{'action','queries'}:
                        raise DataError('问答动作只接受受支持的查询计划')
                    specs=plan.get('queries',[])
                    if action=='query' and not specs:
                        raise DataError('请说明要查询报价单、请购单、供应商、询价进度还是订单')
                    evidence=item.get('control_evidence')
                    if evidence is None:
                        evidence=execute_queries(self.config,specs)
                        item['control_evidence']=evidence
                        self.store.save_mail(item)
                    facts=render_evidence(evidence)
                    if action=='query':
                        reply=facts
                    else:
                        reference={'conversation':context['conversation'],
                                   'attachments':[{'filename':v.get('filename'),'extraction':v.get('extraction')}
                                                  for v in item['parsed'].get('attachments',[])]}
                        answer=self.ai.answer_control(request,evidence,reference)
                        reply=answer.get('answer') if isinstance(answer,dict) else None
                        if not isinstance(reply,str) or not reply.strip() or len(reply)>self.config.control_answer_chars:
                            raise DataError('模型回答为空或超出配置长度')
                        reply=reply.strip()
                        if facts:
                            reply+='\n\n数据库核对结果：\n'+facts
                    result={'ok':True,'action':action,'reply_body':reply,'queried_at':now()}
                elif action=='clarify':
                    message=plan.get('message')
                    if not isinstance(message,str) or not 1<=len(message)<=2000:
                        raise DataError('无法识别请求，请说明要执行的动作或问题')
                    result={'ok':False,'action':'clarify','reply_body':message}
                else:
                    result=self.engine.apply_command(plan,sender,item['id'],request=request)
            except DataError as exc:
                result={'ok':False,'reply_body':str(exc)}
        item['control_result']=result
        self.store.save_mail(item)
        self.engine.receipt(item,sender,result)
        self.store.finish(item,'control_processed')
