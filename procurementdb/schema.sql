-- Version 3. All connections must enable foreign_keys. Monetary/quantity values
-- are canonical decimal strings, calculated with Python Decimal, never float.
CREATE TABLE IF NOT EXISTS runtime_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_update_requests (
    id TEXT PRIMARY KEY NOT NULL,
    account TEXT NOT NULL,
    message_key TEXT NOT NULL,
    controller TEXT NOT NULL,
    table_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_filename TEXT,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account, message_key)
);
CREATE TABLE IF NOT EXISTS data_update_changes (
    request_id TEXT NOT NULL REFERENCES data_update_requests(id),
    row_number INTEGER NOT NULL,
    record_key_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    PRIMARY KEY(request_id, row_number)
);
CREATE TABLE IF NOT EXISTS import_files (
    id TEXT PRIMARY KEY NOT NULL,
    table_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source_rows INTEGER NOT NULL,
    inserted_rows INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(table_name, sha256)
);

CREATE TABLE IF NOT EXISTS material_groups (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY NOT NULL,
    company TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE,
    salutation TEXT,
    source_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_suppliers_email ON suppliers(email);
CREATE TABLE IF NOT EXISTS supplier_groups (
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    group_id TEXT NOT NULL REFERENCES material_groups(id),
    source_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(supplier_id, group_id)
);
CREATE INDEX IF NOT EXISTS ix_supplier_groups_group ON supplier_groups(group_id, supplier_id);
CREATE TABLE IF NOT EXISTS statuses (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS mail_messages (
    id TEXT PRIMARY KEY NOT NULL,
    account TEXT NOT NULL COLLATE NOCASE,
    message_key TEXT NOT NULL,
    internet_message_id TEXT,
    sender TEXT NOT NULL COLLATE NOCASE,
    subject TEXT NOT NULL,
    in_reply_to TEXT,
    references_header TEXT,
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('received','matched','imported','needs_review','ignored')),
    inquiry_id TEXT REFERENCES inquiries(id),
    match_method TEXT,
    error TEXT,
    UNIQUE(account, message_key)
);
CREATE INDEX IF NOT EXISTS ix_mail_internet_id ON mail_messages(account, internet_message_id);
CREATE INDEX IF NOT EXISTS ix_mail_state ON mail_messages(state, received_at);
CREATE TABLE IF NOT EXISTS mail_cursors (
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    last_uid INTEGER NOT NULL CHECK(last_uid >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account, folder)
);

-- One source row is one requisition, as defined by the supplied workbook.
CREATE TABLE IF NOT EXISTS requisitions (
    id TEXT PRIMARY KEY NOT NULL,
    status_id INTEGER NOT NULL REFERENCES statuses(id),
    group_id TEXT NOT NULL REFERENCES material_groups(id),
    group_name TEXT,
    project TEXT,
    approved_at TEXT,
    material_code TEXT,
    material_name TEXT NOT NULL,
    model TEXT,
    unit TEXT,
    quantity TEXT NOT NULL,
    required_date TEXT,
    brand TEXT,
    historical_supplier TEXT,
    source_message_id TEXT REFERENCES mail_messages(id),
    source_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_requisitions_group_status ON requisitions(group_id, status_id);

CREATE TABLE IF NOT EXISTS inquiry_batches (
    id TEXT PRIMARY KEY NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    source_message_id TEXT REFERENCES mail_messages(id),
    created_at TEXT NOT NULL
);
-- A different visible RFQ number for every supplier in a batch.
CREATE TABLE IF NOT EXISTS inquiries (
    id TEXT PRIMARY KEY NOT NULL,
    batch_id TEXT NOT NULL REFERENCES inquiry_batches(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    recipient_email TEXT NOT NULL COLLATE NOCASE,
    subject TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','sending','awaiting_quote','quoted','needs_review','cancelled')),
    sent_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, supplier_id),
    UNIQUE(id, supplier_id)
);
CREATE INDEX IF NOT EXISTS ix_inquiries_waiting ON inquiries(recipient_email, state);
CREATE TABLE IF NOT EXISTS inquiry_items (
    inquiry_id TEXT NOT NULL REFERENCES inquiries(id),
    requisition_id TEXT NOT NULL REFERENCES requisitions(id),
    PRIMARY KEY(inquiry_id, requisition_id)
);
CREATE INDEX IF NOT EXISTS ix_inquiry_items_req ON inquiry_items(requisition_id);
CREATE TABLE IF NOT EXISTS inquiry_outbox (
    id TEXT PRIMARY KEY NOT NULL,
    inquiry_id TEXT NOT NULL UNIQUE REFERENCES inquiries(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachment_name TEXT NOT NULL,
    attachment_csv BLOB NOT NULL,
    internet_message_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','uncertain','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_inquiry_outbox_state ON inquiry_outbox(state, created_at);

CREATE TABLE IF NOT EXISTS quotations (
    id TEXT PRIMARY KEY NOT NULL,
    requisition_id TEXT NOT NULL REFERENCES requisitions(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    model TEXT,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    delivery_date TEXT,
    min_order_quantity TEXT,
    currency TEXT NOT NULL,
    tax_included INTEGER CHECK(tax_included IN (0,1)),
    tax_rate TEXT,
    inquiry_id TEXT,
    source_message_id TEXT REFERENCES mail_messages(id),
    created_at TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(id, requisition_id, supplier_id),
    FOREIGN KEY(inquiry_id, supplier_id) REFERENCES inquiries(id, supplier_id),
    FOREIGN KEY(inquiry_id, requisition_id) REFERENCES inquiry_items(inquiry_id, requisition_id)
);
CREATE INDEX IF NOT EXISTS ix_quotations_req_supplier ON quotations(requisition_id, supplier_id);
CREATE INDEX IF NOT EXISTS ix_quotations_message ON quotations(source_message_id);
CREATE TABLE IF NOT EXISTS orders (
    requisition_id TEXT NOT NULL REFERENCES requisitions(id),
    id TEXT PRIMARY KEY NOT NULL,
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    model TEXT,
    quantity TEXT NOT NULL,
    delivery_date TEXT,
    quotation_id TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    currency TEXT NOT NULL,
    tax_included INTEGER CHECK(tax_included IN (0,1)),
    tax_rate TEXT,
    source_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(quotation_id, requisition_id, supplier_id)
        REFERENCES quotations(id, requisition_id, supplier_id)
);

CREATE VIEW IF NOT EXISTS v_requisition_suppliers AS
SELECT r.id AS requisition_id, r.material_name, r.model, r.quantity, r.unit,
       r.required_date, r.brand, r.group_id, g.name AS group_name,
       s.id AS supplier_id, s.company, s.email, s.salutation
FROM requisitions r
JOIN material_groups g ON g.id=r.group_id
JOIN supplier_groups sg ON sg.group_id=r.group_id
JOIN suppliers s ON s.id=sg.supplier_id;

CREATE VIEW IF NOT EXISTS "物料分组表" AS
SELECT id AS "物料分组唯一编号", name AS "物料分组" FROM material_groups;
CREATE VIEW IF NOT EXISTS "供应商表" AS
SELECT id AS "供应商唯一编号", company AS "供应商公司名", email AS "供应商邮箱",
       salutation AS "供应商称呼" FROM suppliers;
CREATE VIEW IF NOT EXISTS "供应商-物料分类-对应表" AS
SELECT supplier_id AS "供应商唯一编号", group_id AS "物料分组唯一编号" FROM supplier_groups;
CREATE VIEW IF NOT EXISTS "状态表" AS
SELECT id AS "状态唯一编号", name AS "状态" FROM statuses;
CREATE VIEW IF NOT EXISTS "请购单表" AS
SELECT id AS "请购单唯一编号", status_id AS "状态唯一编号", group_id AS "物料分组唯一编号",
       group_name AS "物料分组", project AS "归集项目", approved_at AS "审核日期",
       material_code AS "物料编码", material_name AS "物料名称", model AS "规格型号",
       unit AS "申请单位", quantity AS "申请数量", required_date AS "需求日期",
       brand AS "品牌", historical_supplier AS "历史供应商" FROM requisitions;
CREATE VIEW IF NOT EXISTS "报价单表" AS
SELECT id AS "报价单唯一编号", requisition_id AS "请购单唯一编号", supplier_id AS "供应商唯一编号",
       model AS "报价型号", price AS "报价价格", quantity AS "报价数量",
       delivery_date AS "交货期", min_order_quantity AS "最小起订量", currency AS "币种",
       tax_included AS "含税标识", tax_rate AS "税率" FROM quotations;
CREATE VIEW IF NOT EXISTS "订单表" AS
SELECT requisition_id AS "请购单唯一编号", id AS "订单唯一编号", supplier_id AS "供应商唯一编号",
       model AS "订单型号", quantity AS "订单数量", delivery_date AS "订单交货期",
       quotation_id AS "报价单唯一编号", unit_price AS "订单单价", currency AS "币种",
       tax_included AS "含税标识", tax_rate AS "税率" FROM orders;
