"""Offline procurement database CLI; does not send or fetch mail."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from procurementdb import Database, DataError


def main():
    parser = argparse.ArgumentParser(description='采购 SQLite 数据库与离线工作流')
    parser.add_argument('--db', default=str(Path(__file__).parent / 'data' / 'procurement.sqlite3'))
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='建表并导入六张基础表；请购单不导入')
    init.add_argument('--source-dir', required=True)
    commands.add_parser('status')
    commands.add_parser('check')
    backup = commands.add_parser('backup')
    backup.add_argument('destination')
    ingest = commands.add_parser('import-requisitions', help='显式导入一份请购单')
    ingest.add_argument('file')
    candidates = commands.add_parser('candidates', help='查询物料对应供应商及邮箱')
    candidates.add_argument('ids', nargs='*')
    prepare = commands.add_parser('prepare', help='按供应商创建询价邮件待发记录；不会实际发送')
    prepare.add_argument('--request-key', required=True)
    prepare.add_argument('--message-domain', default='procurement.local')
    prepare.add_argument('ids', nargs='+')
    args = parser.parse_args()
    try:
        readonly = args.command in {'status', 'check', 'candidates', 'backup'}
        with Database(args.db, readonly=readonly) as db:
            if args.command == 'init':
                db.initialize()
                result = {'import': db.import_sources(args.source_dir), 'check': db.check()}
            elif args.command == 'status':
                result = db.summary()
            elif args.command == 'check':
                result = db.check()
            elif args.command == 'backup':
                db.backup(args.destination)
                result = {'backup': str(Path(args.destination).resolve())}
            elif args.command == 'import-requisitions':
                path = Path(args.file)
                result = db.import_requisitions(path.read_bytes(), path.name)
            elif args.command == 'candidates':
                result = db.candidates(args.ids or None)
            else:
                result = db.prepare_inquiries(args.ids, args.request_key, message_domain=args.message_domain)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DataError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
