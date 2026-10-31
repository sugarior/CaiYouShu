"""Convert explicitly requested attachment/inline edits into audited DB updates."""
from __future__ import annotations

from decimal import Decimal,InvalidOperation
import re

from procurementdb import Database,DataError
from procurementdb.tables import FIELDS
from procurementdb.updates import UPDATE_TABLES,attachment_changes,apply_updates,render_update
from .procurement import attachment_csv


FIELD_WORDS = {
    'email':['邮箱','邮件地址','email'], 'company':['公司名','公司名称','公司','company'],
    'salutation':['称呼','salutation'], 'name':['名称','分组名','状态名','name'],
    'status_id':['状态','status_id'], 'group_id':['分组','分类','group_id'],
    'group_name':['分组名称','物料分组','group_name'], 'project':['项目','project'],
    'approved_at':['审核日期','审核时间','approved_at'], 'material_code':['物料编码','material_code'],
    'material_name':['物料名称','material_name'], 'model':['型号','规格','model'],
    'unit':['单位','unit'], 'quantity':['数量','quantity'],
    'required_date':['需求日期','交期','required_date'], 'brand':['品牌','brand'],
    'historical_supplier':['历史供应商','historical_supplier'],
    'price':['价格','单价','报价','price'], 'unit_price':['单价','价格','unit_price'],
    'delivery_date':['交货','交期','delivery_date'], 'min_order_quantity':['最小起订','起订量','min_order_quantity'],
    'currency':['币种','currency'], 'tax_included':['含税','未税','tax_included'],
    'tax_rate':['税率','tax_rate'],
}


def explicit_update(request):
    if re.search(r'(不要|先别|暂不|不必|无需|不需要|不执行)\s*(实际|立即|直接|进行|帮我|数据库|表格|表中|数据)*(更新|修改|替换|清空|写入|改动)',request):
        return False
    if re.search(r'(只|仅)(想|需要|要)?\s*(查询|查看|检查|预览|分析|解释)',request):
        return False
    return bool(re.search(r'更新|修改|更正|改为|改成|调整|替换|修正|清空|变更',request))


def _mentioned_field(table,field,request):
    label=next(label for label,column in FIELDS[table] if column==field)
    words=[label,field]+FIELD_WORDS.get(field,[])
    return any(word.lower() in request.lower() for word in words)


def _literal(value,request,*,identifier=False):
    if value is None:
        return bool(re.search(r'清空|设为空|置空',request))
    text=str(value).strip()
    if identifier or '@' in text:
        return bool(re.search(r'(?<![A-Za-z0-9_.@+\-])'+re.escape(text)+r'(?![A-Za-z0-9_.@+\-])',request,re.I))
    if text.lower() in request.lower():
        return True
    try:
        number=Decimal(text)
        for candidate in re.findall(r'(?<![\w.])[+-]?\d+(?:\.\d+)?%?',request):
            compared=Decimal(candidate.rstrip('%'))/(100 if candidate.endswith('%') else 1)
            if compared==number:
                return True
    except InvalidOperation:
        pass
    return False


def update_from_mail(config,store,item,sender,plan,request):
    if sender not in config.allowed_senders:
        raise DataError('此邮箱没有更新数据库的权限')
    if config.dry_run:
        raise DataError('当前处于预览模式，未写入数据库')
    if not explicit_update(request):
        raise DataError('本次未执行更新。需要实际修改时，请明确写“请按附件更新供应商邮箱”等要求。')
    table=plan.get('resource')
    if not isinstance(table,str) or table not in UPDATE_TABLES or table not in config.control_update_tables:
        raise DataError('该表未在config.yml的control.update_tables中启用更新')
    common={'action','resource'}
    filename=None
    if 'changes' in plan:
        if set(plan)-common-{'changes'}:
            raise DataError('正文更新不能混用附件或其他执行参数')
        changes=plan['changes']
        if not isinstance(changes,list) or not changes:
            raise DataError('请提供唯一编号、要修改的字段和新值')
        for change in changes:
            if not isinstance(change,dict) or set(change)!={'key','values'} or not isinstance(change['key'],dict) or not isinstance(change['values'],dict):
                raise DataError('更新记录须包含唯一编号和字段值')
            for value in change['key'].values():
                if not _literal(value,request,identifier=True):
                    raise DataError('要更新的唯一编号必须由当前邮件正文明确提供')
            for field,value in change['values'].items():
                if field not in UPDATE_TABLES[table]['fields'] or not _mentioned_field(table,field,request):
                    raise DataError('模型提出了当前邮件没有要求更新的字段')
                if not _literal(value,request):
                    raise DataError('新值必须来自当前邮件正文，未使用模型猜测的数据')
    else:
        if set(plan)-common-{'attachment','fields'}:
            raise DataError('附件更新仅接受表名、附件文件名和要更新的字段')
        fields=plan.get('fields')
        if not isinstance(fields,list) or not fields or any(not isinstance(field,str) or field not in UPDATE_TABLES[table]['fields'] for field in fields):
            raise DataError('请说明附件中需要更新哪些字段')
        broad=bool(re.search(r'全部字段|所有字段|整张表|整个表|全部内容|所有内容|表的内容|表内容',request))
        for field in fields:
            if not broad and not _mentioned_field(table,field,request):
                raise DataError('本次只更新邮件明确提到的字段；如需整表列更新，请明确说明')
        selected=plan.get('attachment')
        if selected is not None and (not isinstance(selected,str) or len(selected)>240):
            raise DataError('附件文件名格式错误')
        data,filename=attachment_csv(item['parsed'],selected)
        changes=attachment_changes(data,filename,table,fields,max_rows=config.control_update_max_rows)
    path=config.storage/'procurement.sqlite3'
    if not path.exists():
        raise DataError('采购数据库尚未初始化')
    with Database(path) as db:
        if db.connection.execute('PRAGMA user_version').fetchone()[0]<3:
            db.initialize()
        result=apply_updates(db,account=config.address,controller=sender,message_key=item['id'],
                             table=table,changes=changes,source_filename=filename,max_rows=config.control_update_max_rows)
    return dict(result,action='update_data',reply_body=render_update(result))
