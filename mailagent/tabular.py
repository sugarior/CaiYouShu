"""Bounded in-memory table extraction. Cells are data, never agent instructions."""
from __future__ import annotations

import base64
import csv
from datetime import date, datetime, time
import io
from pathlib import PurePosixPath
import re
from zipfile import ZipFile

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5001  # includes the header
MAX_COLUMNS = 100
MAX_SHEETS = 20
MAX_CHARS = 120000  # shared by all tables in one mail
VERSION = 1


def table_kind(attachment):
    suffix = PurePosixPath((attachment.get('filename') or '').lower()).suffix
    if suffix in {'.csv', '.tsv', '.xlsx', '.xls'}:
        return suffix[1:]
    return {'text/csv': 'csv', 'application/csv': 'csv',
            'text/tab-separated-values': 'tsv',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
            'application/vnd.ms-excel': 'xls'}.get(attachment.get('content_type', ''))


def decode_csv(data):
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        encodings = ['utf-32']
    elif data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    elif b'\x00' in data[:512]:
        even, odd = data[:512:2].count(0), data[1:512:2].count(0)
        encodings = ['utf-16-be', 'utf-16-le'] if even > odd else ['utf-16-le', 'utf-16-be']
    else:
        encodings = ['utf-8-sig', 'gb18030', 'big5']
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            if '\x00' not in text:
                return text, encoding
        except UnicodeError:
            pass
    raise ValueError('unsupported_encoding')


def cell_text(value, number_format=''):
    if value is None:
        return ''
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        if re.fullmatch('0{2,30}', number_format) and float(value).is_integer():
            return str(int(value)).zfill(len(number_format))
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value)


class LimitReached(Exception):
    pass


def extract_table(data, kind, budget=MAX_CHARS, row_limit=MAX_ROWS, sheet_limit=MAX_SHEETS):
    result = {'version': VERSION, 'format': kind, 'status': 'ok', 'sheets': [],
              'extracted_rows': 0, 'characters': 0, 'notes': []}
    if len(data) > MAX_BYTES:
        return dict(result, status='error', error='附件超过 5 MiB，未提取')
    if budget <= 0:
        return dict(result, status='truncated', error='本封邮件的表格提取总量已达上限')

    def add_sheet(name, rows):
        if len(result['sheets']) >= sheet_limit:
            raise LimitReached()
        sheet = {'name': name, 'rows': []}
        result['sheets'].append(sheet)
        for row in rows:
            values = list(row)
            if result['extracted_rows'] >= row_limit or len(values) > MAX_COLUMNS:
                raise LimitReached()
            cost = sum(len(value) + 4 for value in values) + 4
            if result['characters'] + cost > budget:
                raise LimitReached()
            sheet['rows'].append(values)
            result['characters'] += cost
            result['extracted_rows'] += 1

    try:
        if kind in {'csv', 'tsv'}:
            text, encoding = decode_csv(data)
            result['encoding'] = encoding
            delimiter = '\t' if kind == 'tsv' else ','
            if text.lower().startswith('sep=') and text[4:5] in {',', ';', '\t', '|'}:
                delimiter = text[4]
                text = text.partition('\n')[2]
            else:
                try:
                    delimiter = csv.Sniffer().sniff(text[:65536], delimiters=',;\t|').delimiter
                except csv.Error:
                    pass
            result['delimiter'] = delimiter
            add_sheet('CSV', csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True))
        elif kind == 'xlsx':
            # Reject oversized expansion before XML parsing; never extract ZIP members to disk.
            with ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 2048 or sum(v.file_size for v in members) > 50 * 1024 * 1024:
                    raise ValueError('xlsx_expansion_limit')
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            try:
                result['notes'].append('公式按原文提取，不执行或重新计算；不提取图片、图表和宏。')
                for sheet in workbook.worksheets:
                    sheet.reset_dimensions()
                    def rows():
                        for row in sheet.iter_rows(max_col=MAX_COLUMNS + 1):
                            cells = list(row)
                            while cells and cells[-1].value is None:
                                cells.pop()
                            yield [cell_text(c.value, getattr(c, 'number_format', '')) for c in cells]
                    add_sheet(sheet.title, rows())
            finally:
                workbook.close()
        elif kind == 'xls':
            import xlrd
            workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                result['notes'].append('读取文件中已保存的单元格值，不执行公式或宏。')
                def values(sheet):
                    for index in range(sheet.nrows):
                        yield [cell_text(xlrd.xldate_as_datetime(c.value, workbook.datemode))
                               if c.ctype == xlrd.XL_CELL_DATE else cell_text(c.value)
                               for c in sheet.row(index, end_colx=min(sheet.ncols, MAX_COLUMNS + 1))]
                for index in range(workbook.nsheets):
                    sheet = workbook.sheet_by_index(index)
                    add_sheet(sheet.name, values(sheet))
                    workbook.unload_sheet(index)
            finally:
                workbook.release_resources()
        else:
            raise ValueError('unsupported_format')
    except LimitReached:
        result.update(status='truncated', error='超出单封邮件 5001 行、每行 100 列、20 个工作表或 120000 字符的提取限制；仅展示已提取部分')
    except Exception as exc:
        result.update(status='error', error='附件无法完整解析：' + type(exc).__name__ + '（可能为编码、加密或文件格式问题）')
    return result


def ensure_extractions(parsed):
    budget, rows_left, sheets_left = MAX_CHARS, MAX_ROWS, MAX_SHEETS
    changed = False
    for attachment in parsed.get('attachments', []):
        kind = table_kind(attachment)
        if not kind:
            continue
        extracted = attachment.get('extraction')
        if not extracted or extracted.get('version') != VERSION:
            try:
                data = base64.b64decode(attachment['content_base64'], validate=True)
                extracted = extract_table(data, kind, budget if rows_left > 0 and sheets_left > 0 else 0,
                                          rows_left, sheets_left)
            except (KeyError, ValueError):
                extracted = {'version': VERSION, 'format': kind, 'status': 'error', 'sheets': [],
                             'error': '原附件不可用，无法补提取'}
            attachment['extraction'] = extracted
            changed = True
        budget -= extracted.get('characters', 0)
        rows_left -= extracted.get('extracted_rows', 0)
        sheets_left -= len(extracted.get('sheets', []))
    return changed


def render_extractions(parsed, csv_only=False):
    parts = []
    for attachment in parsed.get('attachments', []):
        if csv_only and table_kind(attachment) != 'csv':
            continue
        result = attachment.get('extraction', {})
        if not result:
            continue
        parts.append('附件：' + (attachment.get('filename') or '未命名表格'))
        parts.append('提取状态：' + {'ok': '完整', 'truncated': '部分提取', 'error': '解析失败'}.get(result.get('status'), '未知'))
        if result.get('error'):
            parts.append(result['error'])
        for sheet in result.get('sheets', []):
            parts.append(f"工作表：{sheet['name']}；共 {len(sheet['rows'])} 行（包含表头及空行）")
            stream = io.StringIO(newline='')
            csv.writer(stream, lineterminator='\n').writerows(sheet['rows'])
            parts.append(stream.getvalue())
        parts.extend(result.get('notes', []))
    return '\n\n'.join(parts)
