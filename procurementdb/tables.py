"""Exact mappings from the seven supplied Chinese CSV/XLSX templates."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from mailagent.tabular import extract_table


class DataError(ValueError):
    pass


FIELDS = {
    'material_groups': [('物料分组唯一编号', 'id'), ('物料分组', 'name')],
    'suppliers': [('供应商唯一编号', 'id'), ('供应商公司名', 'company'),
                  ('供应商邮箱', 'email'), ('供应商称呼', 'salutation')],
    'supplier_groups': [('供应商唯一编号', 'supplier_id'), ('物料分组唯一编号', 'group_id')],
    'statuses': [('状态唯一编号', 'id'), ('状态', 'name')],
    'requisitions': [('请购单唯一编号', 'id'), ('状态唯一编号', 'status_id'),
                     ('物料分组唯一编号', 'group_id'), ('物料分组', 'group_name'),
                     ('归集项目', 'project'), ('审核日期', 'approved_at'),
                     ('物料编码', 'material_code'), ('物料名称', 'material_name'),
                     ('规格型号', 'model'), ('申请单位', 'unit'), ('申请数量', 'quantity'),
                     ('需求日期', 'required_date'), ('品牌', 'brand'), ('历史供应商', 'historical_supplier')],
    'quotations': [('报价单唯一编号', 'id'), ('请购单唯一编号', 'requisition_id'),
                   ('供应商唯一编号', 'supplier_id'), ('报价型号', 'model'),
                   ('报价价格', 'price'), ('报价数量', 'quantity'), ('交货期', 'delivery_date'),
                   ('最小起订量', 'min_order_quantity'), ('币种', 'currency'),
                   ('含税标识', 'tax_included'), ('税率', 'tax_rate')],
    'orders': [('请购单唯一编号', 'requisition_id'), ('订单唯一编号', 'id'),
               ('供应商唯一编号', 'supplier_id'), ('订单型号', 'model'),
               ('订单数量', 'quantity'), ('订单交货期', 'delivery_date'),
               ('报价单唯一编号', 'quotation_id'), ('订单单价', 'unit_price'),
               ('币种', 'currency'), ('含税标识', 'tax_included'), ('税率', 'tax_rate')],
}
FILES = {
    'material_groups': '物料分组表.csv', 'suppliers': '供应商表.csv',
    'supplier_groups': '供应商-物料分类-对应表.csv', 'statuses': '状态表.xlsx',
    'requisitions': '请购单表.xlsx', 'quotations': '报价单表.xlsx', 'orders': '订单表.xlsx',
}
BUSINESS_TABLES = tuple(FIELDS)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def email_address(value):
    value = str(value).strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise DataError('需要单个有效邮箱地址')
    if '..' in value or value.startswith('.') or '.@' in value:
        raise DataError('邮箱地址格式错误')
    return value


def decimal_text(value, *, positive=False, maximum=None):
    try:
        text = str(value).strip()
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text):
            raise InvalidOperation()
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise DataError('数值格式错误：' + str(value)) from None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise DataError('数值必须为' + ('正数' if positive else '非负数'))
    if maximum is not None and number > maximum:
        raise DataError('数值超出范围：' + str(value))
    if abs(number.adjusted()) > 30 or len(number.as_tuple().digits) > 40:
        raise DataError('数值精度或位数过大')
    result = format(number, 'f')
    if '.' in result:
        result = result.rstrip('0').rstrip('.')
    return '0' if number == 0 else result


def parse_table(data: bytes, filename: str, table: str):
    """Never execute formulas/macros; reject partial extraction and bad headers."""
    kind = Path(filename).suffix.lower().lstrip('.')
    if kind not in {'csv', 'xlsx'}:
        raise DataError('业务导入仅支持 CSV 和 XLSX')
    parsed = extract_table(data, kind)
    if parsed['status'] != 'ok':
        raise DataError(parsed.get('error', '表格无法完整提取'))
    expected = [chinese for chinese, _ in FIELDS[table]]
    records, saw_header = [], False
    for sheet in parsed['sheets']:
        rows = [(i, row) for i, row in enumerate(sheet['rows'], 1) if any(str(v).strip() for v in row)]
        if not rows:
            continue
        header = [str(v).strip() for v in rows[0][1]]
        if len(header) != len(set(header)) or set(header) != set(expected):
            raise DataError(f'{filename}/{sheet["name"]} 表头不匹配；需要：' + '、'.join(expected))
        saw_header = True
        for row_number, values in rows[1:]:
            if len(values) > len(header):
                raise DataError(f'{filename} 第 {row_number} 行超出表头列数')
            values = values + [''] * (len(header) - len(values))
            source = dict(zip(header, values))
            if any(str(v).startswith('=') for v in values):
                raise DataError(f'{filename} 第 {row_number} 行包含公式，须先转为值')
            try:
                records.append(normalize(table, source))
            except DataError as exc:
                raise DataError(f'{filename} 第 {row_number} 行：{exc}') from exc
    if not saw_header:
        raise DataError(filename + ' 缺少表头')
    return records


def normalize(table, source):
    record = {column: str(source[label]).strip() or None for label, column in FIELDS[table]}
    required = {
        'material_groups': ('id', 'name'), 'suppliers': ('id', 'company', 'email'),
        'supplier_groups': ('supplier_id', 'group_id'), 'statuses': ('id', 'name'),
        'requisitions': ('id', 'status_id', 'group_id', 'material_name', 'quantity'),
        'quotations': ('id', 'requisition_id', 'supplier_id', 'price', 'quantity', 'currency'),
        'orders': ('id', 'requisition_id', 'supplier_id', 'quotation_id', 'unit_price', 'quantity', 'currency'),
    }[table]
    for key in required:
        if record[key] is None:
            raise DataError('缺少必填字段：' + key)
    if table == 'suppliers':
        record['email'] = email_address(record['email'])
    for key in (('id',) if table == 'statuses' else ('status_id',) if table == 'requisitions' else ()):
        value = record[key]
        if not re.fullmatch(r'\d+', value):
            raise DataError('状态编号须为整数')
        record[key] = int(value)
    for key in ('quantity', 'price', 'unit_price', 'min_order_quantity'):
        if key in record and record[key] is not None:
            record[key] = decimal_text(record[key], positive=key == 'quantity')
    if 'tax_rate' in record and record['tax_rate'] is not None:
        text = record['tax_rate']
        if text.endswith('%'):
            text = str(Decimal(decimal_text(text[:-1])) / 100)
        record['tax_rate'] = decimal_text(text, maximum=Decimal(1))
    if 'tax_included' in record and record['tax_included'] is not None:
        text = record['tax_included'].lower()
        choices = {'1': 1, 'true': 1, '是': 1, '含税': 1,
                   '0': 0, 'false': 0, '否': 0, '不含税': 0}
        if text not in choices:
            raise DataError('含税标识须为 1/0、是/否或含税/不含税')
        record['tax_included'] = choices[text]
    for key in ('approved_at', 'required_date'):
        if key in record and record[key] is not None:
            try:
                parsed = datetime.fromisoformat(record[key])
            except ValueError:
                raise DataError(key + ' 须为 ISO 日期或日期时间') from None
            record[key] = parsed.date().isoformat() if key == 'required_date' else parsed.isoformat()
    if 'currency' in record:
        record['currency'] = record['currency'].upper()
        if not re.fullmatch('[A-Z]{3}', record['currency']):
            raise DataError('币种须为三个字母，例如 CNY、USD')
    record['source_json'] = dump(source)
    return record
