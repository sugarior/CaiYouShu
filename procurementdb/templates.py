"""The shared outgoing inquiry CSV and supplier reply contract."""
from __future__ import annotations

import csv
from datetime import date
import io
import json
from pathlib import Path

from .tables import FIELDS, DataError, normalize
from mailagent.tabular import extract_table


CONTEXT_FIELDS = [
    ('询价编号', 'inquiry_id'), ('物料分组', 'group_name'), ('物料编码', 'material_code'),
    ('物料名称', 'material_name'), ('需求规格型号', 'requested_model'),
    ('申请单位', 'unit'), ('申请数量', 'requested_quantity'),
    ('需求日期', 'required_date'), ('品牌', 'brand'),
]
INQUIRY_HEADERS = ['询价编号', '报价单唯一编号', '请购单唯一编号', '供应商唯一编号',
                   '物料分组', '物料编码', '物料名称', '需求规格型号', '申请单位',
                   '申请数量', '需求日期', '品牌', '报价型号', '报价价格', '报价数量',
                   '交货期', '最小起订量', '币种', '含税标识', '税率']
QUOTE_HEADERS = [label for label, _ in FIELDS['quotations']]


def safe_cell(value):
    value = '' if value is None else str(value)
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value


def csv_data(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def inquiry_row(inquiry, supplier_id, req, index):
    return [inquiry, f'QT-{inquiry[4:]}-{index:03}', req['id'], supplier_id,
            req['group_name'], req['material_code'], req['material_name'], req['model'],
            req['unit'], req['quantity'], req['required_date'], req['brand'],
            '', '', '', '', '', '', '', '']


def inquiry_attachment(inquiry, supplier_id, requests):
    return csv_data(INQUIRY_HEADERS, [[safe_cell(v) for v in inquiry_row(inquiry, supplier_id, req, i)]
                                     for i, req in enumerate(requests, 1)])


def supplier_instructions(inquiry):
    return (
        '本邮件为采购询价，不是采购订单，也不构成下单承诺。\n'
        f'询价编号：{inquiry}\n\n'
        '请直接点击“回复”，保留原标题和询价编号；请使用收到本邮件的供应商邮箱回复。\n'
        '附件是本次询价单。前12列为需求信息及关联编号，请勿修改。\n'
        '请在后8列逐行填写：报价型号、报价价格、报价数量、交货期、最小起订量、币种、含税标识、税率。\n'
        '报价价格为每申请单位的单价，按“含税标识”说明是否含税；本轮不计算运费或其他附加费用。\n'
        '数值只填数字，勿写货币符号或千位分隔符；报价数量为本次可供数量。\n'
        '交货期请填 YYYY-MM-DD；最小起订量填数字。币种用 CNY、USD 等三位代码。\n'
        '含税标识填 1（含税）或 0（未税）；税率填 13% 或 0.13，零税率填 0，不能留空。\n'
        '填写示例：报价型号=M-01；报价价格=128.50；报价数量=10；交货期=2027-05-01；'
        '最小起订量=1；币种=CNY；含税标识=1；税率=13%。示例不是本次需求或参考价。\n\n'
        '完成后保留表头，将填好的 CSV 或 XLSX 作为唯一表格附件回复；正文写“报价见附件”。\n'
        '可以分次返回已完成的行；暂不能报价的物料请删除该行，并在正文说明，勿填0报价数量。\n'
        '若修订已发送报价，请给“报价单唯一编号”追加 -R2、-R3 等版本后缀，并在正文说明修订。\n'
        '请勿只在正文写自由格式价格，或仅发送图片/PDF；这些内容无法自动入库。'
    )


def parse_supplier_quote(data, filename, inquiry_id):
    parsed = extract_table(data, filename.rsplit('.', 1)[-1].lower())
    if parsed['status'] != 'ok':
        raise DataError(parsed.get('error', '报价表无法完整提取'))
    result, found = [], False
    for sheet in parsed['sheets']:
        rows = [r for r in sheet['rows'] if any(str(v).strip() for v in r)]
        if not rows:
            continue
        header = [str(v).strip() for v in rows[0]]
        extended = set(header) == set(INQUIRY_HEADERS)
        if len(header) != len(set(header)) or not (extended or set(header) == set(QUOTE_HEADERS)):
            raise DataError('报价表头不匹配，请填写原询价单附件后回复')
        found = True
        for index, row in enumerate(rows[1:], 2):
            if len(row) > len(header):
                raise DataError(f'报价第{index}行列数超过表头')
            raw = dict(zip(header, row + [''] * (len(header) - len(row))))
            if any(str(v).startswith('=') for v in raw.values()):
                raise DataError('报价表包含公式，请转换为实际数值')
            if extended and raw['询价编号'].strip().upper() != inquiry_id:
                raise DataError('附件询价编号与邮件询价编号不一致')
            source = {key: raw[key] for key in QUOTE_HEADERS}
            for key in ('报价型号', '交货期', '最小起订量', '含税标识', '税率'):
                if not str(source[key]).strip():
                    raise DataError(f'报价第{index}行缺少：{key}')
            record = normalize('quotations', source)
            try:
                date.fromisoformat(record['delivery_date'])
            except ValueError:
                raise DataError('交货期须填写 YYYY-MM-DD 的实际交货日期') from None
            if extended:
                record['source_json'] = json.dumps(raw, ensure_ascii=False)
            result.append(record)
    if not found:
        raise DataError('报价附件缺少表头')
    return result


def export_templates(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'询价单模板.csv').write_bytes(csv_data(INQUIRY_HEADERS, []))
    inquiry = 'RFQ-20261031-000000000001'
    req = {'id':'PR-DEMO-001', 'group_name':'PLC及模块', 'material_code':'06.04.0047',
           'material_name':'示例物料', 'model':'M-01', 'unit':'件', 'quantity':'10',
           'required_date':'2027-05-25', 'brand':'示例品牌'}
    row = inquiry_row(inquiry,'SUP0001',req,1)
    (directory/'询价单示例.csv').write_bytes(csv_data(INQUIRY_HEADERS,[row]))
    row[12:] = ['M-01','128.50','10','2027-05-01','1','CNY','1','13%']
    (directory/'供应商报价填写示例.csv').write_bytes(csv_data(INQUIRY_HEADERS,[row]))
    (directory/'询价邮件模板.txt').write_text(
        '主题：[询价编号:RFQ-…] 物料询价\n\n{供应商称呼}：\n\n' +
        supplier_instructions('{本封邮件实际询价编号}') +
        '\n\n需求清单：\n{请购单编号} | {物料名称} | 型号:{规格型号} | 数量:{数量} {单位} | 需求日期:{需求日期} | 品牌:{品牌}\n', encoding='utf-8')
