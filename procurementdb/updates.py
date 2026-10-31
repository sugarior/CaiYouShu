"""Allowlisted existing-record updates, validation, and transactional audit."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3

from .store import new_id, now
from .tables import FIELDS, DataError, dump, normalize
from mailagent.tabular import extract_table


UPDATE_TABLES = {
    'suppliers': {'title':'供应商表', 'keys':['id'], 'fields':['company','email','salutation']},
    'material_groups': {'title':'物料分组表','keys':['id'],'fields':['name']},
    'supplier_groups': {'title':'供应商物料对应表','keys':['supplier_id','group_id'],'fields':['group_id']},
    'statuses': {'title':'状态表','keys':['id'],'fields':['name']},
    'requisitions': {'title':'请购单表','keys':['id'],
                     'fields':['status_id','group_id','group_name','project','approved_at','material_code',
                               'material_name','model','unit','quantity','required_date','brand','historical_supplier']},
    'quotations': {'title':'报价单表','keys':['id'],
                   'fields':['model','price','quantity','delivery_date','min_order_quantity','currency','tax_included','tax_rate']},
    'orders': {'title':'订单表','keys':['id'],
               'fields':['model','quantity','delivery_date','unit_price','currency','tax_included','tax_rate']},
}


def update_schema():
    return {name:{'description':info['title'],'keys':info['keys'],
                  'fields':{field:next(label for label,column in FIELDS[name] if column==field)
                            for field in info['fields']}}
            for name,info in UPDATE_TABLES.items()}


def key_value(table, field, value):
    if value is None or isinstance(value,(list,dict,bool)):
        raise DataError('缺少有效的唯一编号：'+field)
    text=str(value).strip()
    if not text or len(text)>200 or any(ord(c)<32 for c in text):
        raise DataError('唯一编号为空或格式错误：'+field)
    if table=='statuses':
        if not text.isascii() or not text.isdigit():
            raise DataError('状态编号须为整数')
        return int(text)
    return text


def attachment_changes(data,filename,table,fields,*,max_rows=1000):
    """Partial CSVs are sufficient. Only the explicitly selected columns change."""
    if table not in UPDATE_TABLES:
        raise DataError('不支持更新该业务表')
    info=UPDATE_TABLES[table]
    if not isinstance(fields,list) or not fields or any(not isinstance(f,str) or f not in info['fields'] for f in fields):
        raise DataError('请说明要更新的受支持字段')
    if len(fields)!=len(set(fields)):
        raise DataError('更新字段重复')
    extracted=extract_table(data,filename.rsplit('.',1)[-1].lower())
    if extracted['status']!='ok':
        raise DataError(extracted.get('error','更新附件未完整提取'))
    columns=dict(FIELDS[table])
    columns.update({column:column for _,column in FIELDS[table]})
    results=[]
    for sheet in extracted['sheets']:
        rows=[row for row in sheet['rows'] if any(str(v).strip() for v in row)]
        if not rows:
            continue
        headers=[str(v).strip() for v in rows[0]]
        if len(headers)!=len(set(headers)):
            raise DataError('更新附件表头重复')
        mapped={}
        for index,header in enumerate(headers):
            if header in columns:
                mapped.setdefault(columns[header],[]).append(index)
        if any(len(indices)>1 for indices in mapped.values()):
            raise DataError('更新附件有多个列对应同一字段')
        # A relationship move uses explicit old/new group columns.
        old_group=new_group=None
        if table=='supplier_groups':
            for label in ('原物料分组唯一编号','old_group_id'):
                if label in headers:old_group=headers.index(label)
            for label in ('新物料分组唯一编号','new_group_id'):
                if label in headers:new_group=headers.index(label)
            if old_group is None or new_group is None:
                raise DataError('修改供货关系请提供供应商唯一编号、原物料分组唯一编号、新物料分组唯一编号')
        needed=set(info['keys']+fields)-({'group_id'} if table=='supplier_groups' else set())
        missing=needed-set(mapped)
        if missing:
            labels={column:label for label,column in FIELDS[table]}
            raise DataError('更新附件缺少列：'+'、'.join(labels.get(v,v) for v in sorted(missing)))
        for line,row in enumerate(rows[1:],2):
            if len(row)>len(headers):
                raise DataError(f'更新附件第{line}行超过表头列数')
            row=row+['']*(len(headers)-len(row))
            key={field:row[mapped[field][0]] for field in info['keys'] if field!='group_id' or table!='supplier_groups'}
            values={field:row[mapped[field][0]] for field in fields if field!='group_id' or table!='supplier_groups'}
            if table=='supplier_groups':
                key['group_id']=row[old_group];values['group_id']=row[new_group]
            if any(str(v).startswith('=') for v in list(key.values())+list(values.values())):
                raise DataError(f'更新附件第{line}行包含公式，请转换成实际值')
            # Blank selected cells require an explicit clearing request, never erase silently.
            if any(not str(value).strip() for value in values.values()):
                raise DataError(f'更新附件第{line}行更新列为空；如需清空，请在正文明确指定记录和字段')
            results.append({'key':key,'values':values})
            if len(results)>max_rows:
                raise DataError('更新行数超过config.yml配置上限')
    if not results:
        raise DataError('更新附件没有数据行')
    return results


def _guard_related_records(db,table,before,changed):
    con=db.connection
    if table=='requisitions' and set(changed)-{'status_id'}:
        active=con.execute("SELECT 1 FROM inquiry_items ii JOIN inquiries i ON i.id=ii.inquiry_id WHERE ii.requisition_id=? AND i.state!='cancelled' LIMIT 1",(before['id'],)).fetchone()
        if active:
            raise DataError('请购单'+before['id']+'已有询价记录；修改需求请使用新请购单编号')
    if table=='quotations':
        linked=con.execute('SELECT 1 FROM orders WHERE quotation_id=? LIMIT 1',(before['id'],)).fetchone()
        if linked:
            raise DataError('报价单'+before['id']+'已被订单引用，不能直接修改原报价')


def apply_updates(db,*,account,controller,message_key,table,changes,source_filename=None,max_rows=1000):
    if table not in UPDATE_TABLES:
        raise DataError('不支持更新该业务表')
    if not isinstance(changes,list) or not changes or len(changes)>max_rows:
        raise DataError('更新记录为空或超过配置行数限制')
    info=UPDATE_TABLES[table]
    checksum=hashlib.sha256(json.dumps({'table':table,'changes':changes},ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
    con=db.connection
    with db.transaction():
        owner=con.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()
        if not owner or owner[0]!=account:
            raise DataError('数据库邮箱绑定不一致，未执行更新')
        previous=con.execute('SELECT * FROM data_update_requests WHERE account=? AND message_key=?',(account,message_key)).fetchone()
        if previous:
            if previous['payload_hash']!=checksum or previous['controller']!=controller:
                raise DataError('同一邮件的更新内容已变化，未重复执行')
            return dict(json.loads(previous['result_json']),duplicate=True)
        staged=[];seen=set();unchanged=0
        for index,change in enumerate(changes,1):
            if not isinstance(change,dict) or set(change)!={'key','values'}:
                raise DataError(f'第{index}条更新须包含唯一编号和字段值')
            key,values=change['key'],change['values']
            if not isinstance(key,dict) or set(key)!=set(info['keys']):
                raise DataError(f'第{index}条更新缺少精确的唯一编号')
            if not isinstance(values,dict) or not values or set(values)-set(info['fields']):
                raise DataError(f'第{index}条更新包含不允许修改的字段或编号')
            key={field:key_value(table,field,key[field]) for field in info['keys']}
            identity=tuple(key[field] for field in info['keys'])
            if identity in seen:
                raise DataError('附件或正文包含重复唯一编号：'+str(identity))
            seen.add(identity)
            where=' AND '.join(field+'=?' for field in info['keys'])
            before=con.execute('SELECT * FROM '+table+' WHERE '+where,list(identity)).fetchone()
            if not before:
                raise DataError(info['title']+'未匹配到编号：'+str(identity)+'；本次整批未更新，不自动新增记录')
            before=dict(before)
            raw={label:before[column] for label,column in FIELDS[table]}
            for field,value in values.items():
                if isinstance(value,(dict,list)) or isinstance(value,bool):
                    raise DataError('更新值必须为文本、数字或明确的空值')
                if isinstance(value,str) and (value.startswith('=') or '\x00' in value or len(value)>4000):
                    raise DataError('更新值含公式、空字符或长度过大')
                label=next(label for label,column in FIELDS[table] if column==field)
                raw[label]='' if value is None else str(value)
            # normalize expects textual cells, including unchanged NULL values.
            normalized=normalize(table,{label:'' if value is None else str(value) for label,value in raw.items()})
            after={field:normalized[field] for field in values}
            changed={field:value for field,value in after.items() if value!=before[field]}
            if not changed:
                unchanged+=1
                continue
            _guard_related_records(db,table,before,changed)
            if table=='requisitions':
                group=con.execute('SELECT name FROM material_groups WHERE id=?',(normalized['group_id'],)).fetchone()
                if not group or normalized['group_name'] and normalized['group_name']!=group[0]:
                    raise DataError('请购单物料分组编号与名称不一致')
            staged.append((index,key,before,changed,normalized['source_json']))
        request_id=new_id()
        details=[]
        for index,key,before,changed,source_json in staged:
            where=' AND '.join(field+'=?' for field in info['keys'])
            assignments=','.join(field+'=?' for field in changed)+',source_json=?'
            try:
                con.execute('UPDATE '+table+' SET '+assignments+' WHERE '+where,
                            list(changed.values())+[source_json]+[key[field] for field in info['keys']])
            except sqlite3.IntegrityError:
                raise DataError(info['title']+'更新违反编号或关联约束；整批已回滚') from None
            verify_key={field:changed.get(field,key[field]) for field in info['keys']}
            verified=con.execute('SELECT * FROM '+table+' WHERE '+where,[verify_key[field] for field in info['keys']]).fetchone()
            if not verified or any(verified[field]!=value for field,value in changed.items()):
                raise DataError('更新后回读核对失败，整批已回滚')
            details.append({'key':key,'before':{field:before[field] for field in changed},'after':changed})
        result={'ok':True,'table':table,'title':info['title'],'matched':len(changes),
                'updated':len(staged),'unchanged':unchanged,'inserted':0,'duplicate':False,
                'audit_id':request_id,'details':details,'updated_at':now()}
        con.execute('INSERT INTO data_update_requests VALUES (?,?,?,?,?,?,?,?,?)',
                    (request_id,account,message_key,controller,table,checksum,source_filename,dump(result),result['updated_at']))
        for index,detail in enumerate(details,1):
            con.execute('INSERT INTO data_update_changes VALUES (?,?,?,?,?)',
                        (request_id,index,dump(detail['key']),dump(detail['before']),dump(detail['after'])))
        return result


def render_update(result):
    text=(f'{result["title"]}更新完成：匹配{result["matched"]}条，更新{result["updated"]}条，'
          f'未变化{result["unchanged"]}条，新增0条。\n审计编号：{result["audit_id"]}')
    if result.get('duplicate'):
        text+='\n此邮件已处理，本次沿用原结果，没有再次写入。'
    labels={column:label for label,column in FIELDS[result['table']]}
    for detail in result['details'][:30]:
        key='、'.join(labels.get(field,field)+'='+str(value) for field,value in detail['key'].items())
        changes='；'.join(labels.get(field,field)+'：'+str(detail['before'][field])+' → '+str(value)
                         for field,value in detail['after'].items())
        text+='\n'+key+'；'+changes
    if len(result['details'])>30:
        text+=f'\n以上展示前30条变更，全部{len(result["details"])}条变更已写入数据库审计记录。'
    if result['table']=='suppliers':
        text+='\n更新的是供应商主数据；后续新询价使用更新后的邮箱。既有询价保留原收件邮箱，不重发历史询价。'
    return text
