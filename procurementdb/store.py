"""Transactional business storage. This module never connects to a mail server."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import uuid

from .tables import BUSINESS_TABLES, FILES, FIELDS, DataError, dump, parse_table


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def new_id():
    return uuid.uuid4().hex


class Database:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path).resolve()
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rwc'),
                                          uri=True, isolation_level=None, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA busy_timeout=15000')
        if readonly:
            self.connection.execute('PRAGMA query_only=ON')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def initialize(self):
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        tables = self.connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if version not in (0, 1, 2, 3) or (version == 0 and tables):
            raise DataError('目标数据库不是本模块的空库或兼容数据库；请选择新文件')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
        try:
            self.connection.executescript('BEGIN IMMEDIATE;\n' + schema + '\nPRAGMA user_version=3;\nCOMMIT;')
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def _insert_rows(self, table, rows, *, extra=None):
        """Same key/same values is a no-op; conflicting values never overwrite."""
        key_columns = ['supplier_id', 'group_id'] if table == 'supplier_groups' else ['id']
        inserted = 0
        for source in rows:
            record = dict(source)
            if extra:
                record.update(extra)
            where = ' AND '.join(f'{key}=?' for key in key_columns)
            previous = self.connection.execute(f'SELECT * FROM {table} WHERE {where}',
                                               [record[key] for key in key_columns]).fetchone()
            if previous:
                compare = [column for _, column in FIELDS[table]]
                # Operational state may have advanced since the original requisition import.
                if table == 'requisitions':
                    compare.remove('status_id')
                if any(previous[key] != record[key] for key in compare):
                    raise DataError(f'{table} 主键冲突：' + ','.join(str(record[key]) for key in key_columns))
                if table == 'quotations' and extra and any(previous[k] != v for k, v in extra.items()):
                    raise DataError('报价编号已由其他邮件使用')
                continue
            if table == 'requisitions' and record['group_name']:
                group = self.connection.execute('SELECT name FROM material_groups WHERE id=?', (record['group_id'],)).fetchone()
                if group and group['name'] != record['group_name']:
                    raise DataError('物料分组编号与名称不一致：' + record['id'])
            if table == 'quotations':
                record['created_at'] = now()
            columns = list(record)
            try:
                self.connection.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                                        [record[key] for key in columns])
            except sqlite3.IntegrityError as exc:
                raise DataError(f'{table} 主键或关联数据无效：{exc}') from exc
            inserted += 1
        return inserted

    def _import_file(self, table, data, filename, rows, *, extra=None):
        checksum = hashlib.sha256(data).hexdigest()
        previous = self.connection.execute('SELECT * FROM import_files WHERE table_name=? AND sha256=?',
                                           (table, checksum)).fetchone()
        if previous and not extra:
            return {'source_rows': len(rows), 'inserted': 0, 'already_imported': True}
        inserted = self._insert_rows(table, rows, extra=extra)
        if not previous:
            self.connection.execute('INSERT INTO import_files VALUES (?,?,?,?,?,?,?)',
                                    (new_id(), table, Path(filename).name, checksum, len(rows), inserted, now()))
        return {'source_rows': len(rows), 'inserted': inserted, 'already_imported': False}

    def import_sources(self, directory):
        """Import the six non-requisition sources atomically; inspect only its header."""
        directory = Path(directory)
        sources = []
        for table, filename in FILES.items():
            data = (directory / filename).read_bytes()
            sources.append((table, data, filename, parse_table(data, filename, table)))
        report = {}
        with self.transaction():
            for table, data, filename, rows in sources:
                if table == 'requisitions':
                    report[table] = {'source_rows': len(rows), 'inserted': 0, 'excluded': True}
                else:
                    report[table] = self._import_file(table, data, filename, rows)
        return report

    def import_requisitions(self, data, filename, *, source_message_id=None):
        rows = parse_table(data, filename, 'requisitions')
        if not rows:
            raise DataError('请购单为空')
        with self.transaction():
            report = self._import_file('requisitions', data, filename, rows,
                                       extra={'source_message_id': source_message_id} if source_message_id else None)
        return dict(report, requisition_ids=[r['id'] for r in rows])

    def candidates(self, requisition_ids=None):
        if requisition_ids is not None and not requisition_ids:
            return []
        query = 'SELECT * FROM v_requisition_suppliers'
        params = []
        if requisition_ids is not None:
            params = list(requisition_ids)
            query += ' WHERE requisition_id IN (' + ','.join('?' for _ in params) + ')'
        return [dict(r) for r in self.connection.execute(query + ' ORDER BY requisition_id,supplier_id', params)]

    def summary(self):
        tables=BUSINESS_TABLES+('inquiry_batches','inquiries','inquiry_outbox','mail_messages','import_files')
        if self.connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='data_update_requests'").fetchone():
            tables+=('data_update_requests','data_update_changes')
        return {table:self.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}

    def check(self):
        return {'integrity': [r[0] for r in self.connection.execute('PRAGMA integrity_check')],
                'foreign_key_errors': [tuple(r) for r in self.connection.execute('PRAGMA foreign_key_check')],
                'counts': self.summary()}

    def backup(self, destination):
        """Consistent online backup including committed WAL data."""
        target = Path(destination).resolve()
        if target == self.path or target.exists():
            raise DataError('备份目标必须是另一个尚不存在的文件')
        target.parent.mkdir(parents=True, exist_ok=True)
        other = sqlite3.connect(target)
        try:
            self.connection.backup(other)
        finally:
            other.close()

    def prepare_inquiries(self, requisition_ids, request_key, **kwargs):
        from .workflow import prepare_inquiries
        return prepare_inquiries(self, requisition_ids, request_key, **kwargs)

    def receive_quote(self, **kwargs):
        from .workflow import receive_quote
        return receive_quote(self, **kwargs)
