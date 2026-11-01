"""Seed isolated tenants from a versioned snapshot of the original business data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from procurementdb.store import Database, new_id, now
from procurementdb.tables import BUSINESS_TABLES, FIELDS, DataError, dump, email_address, normalize


# Version 1 values are retained only to recognize unchanged synthetic rows during
# migration. New accounts always use demo-seed/procurement.json.
GROUPS = [
    ("G-DEMO-01", "机械标准件"),
    ("G-DEMO-02", "电气元件"),
    ("G-DEMO-03", "包装材料"),
    ("G-DEMO-04", "工装夹具"),
]

SUPPLIERS = [
    ("S-DEMO-01", "启航精密制造（演示）", "周经理", ("G-DEMO-01", "G-DEMO-04")),
    ("S-DEMO-02", "青禾工业科技（演示）", "陈经理", ("G-DEMO-02", "G-DEMO-04")),
    ("S-DEMO-03", "远川包装材料（演示）", "林经理", ("G-DEMO-03",)),
]

STATUSES = [
    (10, "待询价"), (20, "待报价"), (30, "待下单"), (40, "已下单"),
    (50, "已完成"), (90, "已关闭"), (99, "已取消"),
]

REQUISITIONS = [
    ("PR-DEMO-001", 10, "G-DEMO-01", "标准件采购演示", "不锈钢六角螺栓", "M8 x 30", "个", "500", "2026-12-15"),
    ("PR-DEMO-002", 10, "G-DEMO-01", "标准件采购演示", "平垫圈", "M8", "个", "1000", "2026-12-15"),
    ("PR-DEMO-003", 10, "G-DEMO-02", "设备维护演示", "交流接触器", "CJX2-1810 220V", "个", "12", "2026-12-20"),
    ("PR-DEMO-004", 10, "G-DEMO-02", "设备维护演示", "接近开关", "LJ12A3-4-Z/BX", "个", "20", "2026-12-20"),
    ("PR-DEMO-005", 10, "G-DEMO-03", "包装耗材演示", "瓦楞纸箱", "五层 400x300x250mm", "个", "300", "2026-12-25"),
    ("PR-DEMO-006", 10, "G-DEMO-03", "包装耗材演示", "缠绕膜", "宽500mm 透明", "卷", "40", "2026-12-25"),
    ("PR-DEMO-007", 10, "G-DEMO-04", "工装采购演示", "定位销", "直径10mm 长度40mm", "个", "50", "2026-12-30"),
    ("PR-DEMO-008", 10, "G-DEMO-04", "工装采购演示", "夹具手柄", "M12 外螺纹", "个", "16", "2026-12-30"),
]


SEED_PATH = Path(__file__).resolve().parent / "demo-seed" / "procurement.json"
SEED_VERSION = "data-v2"
PLACEHOLDER_EMAIL = "supplier@example.invalid"


def snapshot_from_database(source):
    """Export only the seven business tables, without mail, queues or secrets."""
    tables = {}
    with Database(source, readonly=True) as db:
        db.connection.execute("BEGIN")
        for table in BUSINESS_TABLES:
            columns = [column for _, column in FIELDS[table]]
            keys = "supplier_id,group_id" if table == "supplier_groups" else "id"
            rows = [dict(row) for row in db.connection.execute(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY {keys}")]
            if table == "suppliers":
                for row in rows:
                    row["email"] = PLACEHOLDER_EMAIL
            tables[table] = rows
        db.connection.rollback()
    # The user requested fresh demo inquiries. No historical inquiry queues are
    # copied, so unquoted/unordered waiting rows must be ready to inquire again.
    original_sha256 = hashlib.sha256(dump(tables).encode("utf-8")).hexdigest()
    referenced = {row["requisition_id"] for table in ("quotations", "orders") for row in tables[table]}
    reset_waiting = 0
    for row in tables["requisitions"]:
        if row["status_id"] == 20 and row["id"] not in referenced:
            row["status_id"] = 10
            reset_waiting += 1
    return {"format": 1, "source": "data/procurement.sqlite3",
            "original_business_sha256": original_sha256,
            "reset_waiting_requisitions": reset_waiting,
            "sha256": hashlib.sha256(dump(tables).encode("utf-8")).hexdigest(),
            "tables": tables}


def load_snapshot(path=SEED_PATH):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    tables = snapshot.get("tables", {})
    if snapshot.get("format") != 1 or set(tables) != set(BUSINESS_TABLES):
        raise DataError("演示初始快照格式无效")
    if snapshot.get("sha256") != hashlib.sha256(dump(tables).encode("utf-8")).hexdigest():
        raise DataError("演示初始快照校验失败")
    for table, rows in tables.items():
        columns = {column for _, column in FIELDS[table]}
        if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != columns for row in rows):
            raise DataError("演示初始快照字段无效：" + table)
    return snapshot


def _merge_snapshot(db, snapshot, supplier_email):
    inserted, preserved = {}, {}
    for table in BUSINESS_TABLES:
        inserted[table] = preserved[table] = 0
        keys = ["supplier_id", "group_id"] if table == "supplier_groups" else ["id"]
        for source in snapshot["tables"][table]:
            row = dict(source)
            if table == "suppliers":
                row["email"] = supplier_email
            previous = db.connection.execute(
                f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys),
                [row[key] for key in keys]).fetchone()
            if previous:
                if any(previous[key] != value for key, value in row.items()):
                    preserved[table] += 1
                continue
            row["source_json"] = dump({label: row[column] for label, column in FIELDS[table]})
            inserted[table] += db._insert_rows(table, [row])
    db.connection.execute("INSERT OR REPLACE INTO runtime_metadata VALUES('demo_seed',?)", (SEED_VERSION,))
    db.connection.execute("INSERT OR REPLACE INTO runtime_metadata VALUES('demo_seed_sha256',?)", (snapshot["sha256"],))
    return {"inserted": inserted, "preserved_existing_differences": preserved}


def initialize_demo_database(path, supplier_email, agent_email, *, snapshot_path=SEED_PATH):
    """Create the original business dataset, with this tenant's test addresses."""
    snapshot = load_snapshot(snapshot_path)
    supplier_email = email_address(supplier_email)
    path = Path(path)
    with Database(path) as db:
        db.initialize()
        with db.transaction():
            report = _merge_snapshot(db, snapshot, supplier_email)
            db.connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('mail_account',?)", (agent_email,))
            if db.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise DataError("演示初始数据存在无效关联")
    return report


def _legacy_requisitions():
    for identity, status, group_id, project, material, model, unit, quantity, required_date in REQUISITIONS:
        yield normalize("requisitions", {
            "请购单唯一编号": identity, "状态唯一编号": status,
            "物料分组唯一编号": group_id, "物料分组": dict(GROUPS)[group_id],
            "归集项目": project, "审核日期": "2026-10-31",
            "物料编码": "", "物料名称": material, "规格型号": model, "申请单位": unit,
            "申请数量": quantity, "需求日期": required_date,
            "品牌": "演示数据", "历史供应商": "",
        })


def _remove_unchanged_legacy_rows(db, supplier_email, snapshot):
    """Only remove recognized v1 fixtures that have no live business references."""
    con = db.connection
    removed = {table: 0 for table in ("requisitions", "supplier_groups", "suppliers", "material_groups")}
    for expected in _legacy_requisitions():
        row = con.execute("SELECT * FROM requisitions WHERE id=?", (expected["id"],)).fetchone()
        if not row or any(row[key] != value for key, value in expected.items()):
            continue
        used = any(con.execute(f"SELECT 1 FROM {table} WHERE requisition_id=? LIMIT 1", (row["id"],)).fetchone()
                   for table in ("inquiry_items", "quotations", "orders"))
        if not used:
            con.execute("DELETE FROM requisitions WHERE id=?", (row["id"],))
            removed["requisitions"] += 1
    for identity, company, salutation, groups in SUPPLIERS:
        row = con.execute("SELECT * FROM suppliers WHERE id=?", (identity,)).fetchone()
        # Email updates rewrite source_json as an audit snapshot. Matching the
        # actual business fields still identifies the old supplier; its uniform
        # test mailbox is carried forward to the real source suppliers.
        if not row or (row["company"], row["salutation"], row["email"]) != (
                company, salutation, supplier_email):
            continue
        if any(con.execute(f"SELECT 1 FROM {table} WHERE supplier_id=? LIMIT 1", (identity,)).fetchone()
               for table in ("inquiries", "quotations", "orders")):
            continue
        for group_id in groups:
            if con.execute("SELECT 1 FROM requisitions WHERE group_id=? LIMIT 1", (group_id,)).fetchone():
                continue
            removed["supplier_groups"] += con.execute(
                "DELETE FROM supplier_groups WHERE supplier_id=? AND group_id=? AND source_json=?",
                (identity, group_id, dump({"demo": True}))).rowcount
        if not con.execute("SELECT 1 FROM supplier_groups WHERE supplier_id=? LIMIT 1", (identity,)).fetchone():
            con.execute("DELETE FROM suppliers WHERE id=?", (identity,))
            removed["suppliers"] += 1
    for identity, name in GROUPS:
        if any(con.execute(f"SELECT 1 FROM {table} WHERE group_id=? LIMIT 1", (identity,)).fetchone()
               for table in ("requisitions", "supplier_groups")):
            continue
        removed["material_groups"] += con.execute(
            "DELETE FROM material_groups WHERE id=? AND name=? AND source_json=?",
            (identity, name, dump({"demo": True}))).rowcount
    original_statuses = {row["id"]: row["name"] for row in snapshot["tables"]["statuses"]}
    for identity, name in STATUSES:
        if identity in original_statuses:
            con.execute("UPDATE statuses SET name=?,source_json=? WHERE id=? AND name=? AND source_json=?",
                        (original_statuses[identity], dump({"状态唯一编号": identity, "状态": original_statuses[identity]}),
                         identity, name, dump({"demo": True})))
    return removed


def migrate_demo_database(path, supplier_email, agent_email):
    """Backup and migrate v1 tenant data; never import or replay the old queues."""
    snapshot = load_snapshot()
    supplier_email = email_address(supplier_email)
    path = Path(path).resolve()
    with Database(path) as db:
        metadata = dict(db.connection.execute("SELECT key,value FROM runtime_metadata"))
        if metadata.get("demo_seed") == SEED_VERSION and metadata.get("demo_seed_sha256") == snapshot["sha256"]:
            return {"migrated": False}
        if metadata.get("demo_seed") != "v1" or metadata.get("mail_account") != agent_email:
            raise DataError("演示数据库版本或邮箱绑定不匹配，未自动迁移")
        if db.check()["foreign_key_errors"]:
            raise DataError("演示数据库已有无效关联，未自动迁移")
        # A prior natural-language bulk update can have changed the test address
        # without changing platform settings. Carry its uniform address forward.
        emails = [row[0] for row in db.connection.execute("SELECT DISTINCT email FROM suppliers")]
        if len(emails) == 1:
            supplier_email = email_address(emails[0])
        backup = path.parent / "seed-backups" / f"before-{SEED_VERSION}-{new_id()}.sqlite3"
        db.backup(backup)
        with db.transaction():
            removed = _remove_unchanged_legacy_rows(db, supplier_email, snapshot)
            report = _merge_snapshot(db, snapshot, supplier_email)
            if db.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise DataError("演示数据库迁移关联校验失败，已回滚")
            report.update(migrated=True, removed_unchanged_legacy=removed,
                          backup=str(backup), migrated_at=now())
            db.connection.execute("INSERT OR REPLACE INTO runtime_metadata VALUES('demo_seed_migration',?)", (dump(report),))
        return report


def main():
    parser = argparse.ArgumentParser(description="从原 data/ 数据库生成演示业务快照，不复制邮件或发件队列")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=SEED_PATH)
    args = parser.parse_args()
    snapshot = snapshot_from_database(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": {table: len(rows) for table, rows in snapshot["tables"].items()},
                      "sha256": snapshot["sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
