"""Create a clean, isolated procurement dataset for each local demo tenant."""
from __future__ import annotations

from pathlib import Path

from procurementdb.store import Database
from procurementdb.tables import dump, normalize


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


def initialize_demo_database(path, supplier_email, agent_email):
    """Initialize one tenant's blank procurement database with demo-only rows."""
    path = Path(path)
    with Database(path) as db:
        db.initialize()
        with db.transaction():
            for identity, name in GROUPS:
                db.connection.execute("INSERT INTO material_groups(id,name,source_json) VALUES(?,?,?)",
                                      (identity, name, dump({"demo": True})))
            for identity, name in STATUSES:
                db.connection.execute("INSERT INTO statuses(id,name,source_json) VALUES(?,?,?)",
                                      (identity, name, dump({"demo": True})))
            for identity, company, salutation, groups in SUPPLIERS:
                db.connection.execute("INSERT INTO suppliers(id,company,email,salutation,source_json) VALUES(?,?,?,?,?)",
                                      (identity, company, supplier_email, salutation, dump({"demo": True})))
                for group_id in groups:
                    db.connection.execute("INSERT INTO supplier_groups(supplier_id,group_id,source_json) VALUES(?,?,?)",
                                          (identity, group_id, dump({"demo": True})))
            for identity, status, group_id, project, material, model, unit, quantity, required_date in REQUISITIONS:
                row = normalize("requisitions", {
                    "请购单唯一编号": identity, "状态唯一编号": status,
                    "物料分组唯一编号": group_id, "物料分组": dict(GROUPS)[group_id],
                    "归集项目": project, "审核日期": "2026-10-31",
                    "物料编码": "", "物料名称": material, "规格型号": model, "申请单位": unit,
                    "申请数量": quantity, "需求日期": required_date,
                    "品牌": "演示数据", "历史供应商": "",
                })
                columns = list(row)
                db.connection.execute(
                    f"INSERT INTO requisitions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row[key] for key in columns],
                )
            db.connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('demo_seed','v1')")
            db.connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('mail_account',?)", (agent_email,))
        check = db.check()
        if check["integrity"] != ["ok"] or check["foreign_key_errors"]:
            raise RuntimeError("demo_seed_database_invalid")
    return {"groups": len(GROUPS), "suppliers": len(SUPPLIERS), "requisitions": len(REQUISITIONS)}
