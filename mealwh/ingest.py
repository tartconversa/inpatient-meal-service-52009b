"""持续入仓：按接收批次登记原始文件与逐行版本。

- 每次接收是一个调用者明确次序的批次(batch)，接收时刻即该批事实的最早可知时刻；
- 同一表内相同文件摘要不再接收，不增加事实；
- 同一记录键的新行表示整行修订，旧版本保留，截点查询按接收次序取新；
- 原始文件文本与接收历史保留，业务时点与接收时点分开存放。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sourcepack.catalog import read_table

from .store import next_id


def load_contract(catalog_path):
    """读取资料目录作为字段契约(只取表结构，不核对摘要)。"""
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    return {table["name"]: table for table in catalog["tables"]}


def default_catalog():
    """仓库自带资料包的目录路径。"""
    return Path(__file__).resolve().parents[1] / "data" / "catalog.json"


def parse_moment(value, field):
    """解析带时区的时刻；接收时刻规范为 UTC 文本。"""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{field} 必须带时区：{value}")
    return moment.astimezone(timezone.utc).isoformat()


def _row_hash(row):
    text = json.dumps(row, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def receive(con, contract, files, batch_id, received_at=None, note=None):
    """接收一批文件。files 为 (表名, 路径) 列表，返回接收摘要。"""
    if not files:
        raise ValueError("本批没有文件")
    for table_name, _ in files:
        if table_name not in contract:
            raise ValueError("未知资料表：" + table_name)
    exists = con.execute(
        "SELECT 1 FROM receive_batch WHERE batch_id = ?", [batch_id]
    ).fetchone()
    if exists:
        raise ValueError("批次已存在：" + batch_id)
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
    else:
        received_at = parse_moment(received_at, "received_at")
    seq = next_id(con, "receive_batch", "seq")
    con.execute(
        "INSERT INTO receive_batch VALUES (?, ?, ?, ?)",
        [batch_id, seq, received_at, note],
    )
    file_id = next_id(con, "raw_file", "file_id")
    summary = {"batch_id": batch_id, "seq": seq, "received_at": received_at,
               "files": [], "rows_added": 0}
    for table_name, path in files:
        table = contract[table_name]
        content = Path(path).read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        duplicate = con.execute(
            "SELECT 1 FROM raw_file WHERE table_name = ? AND sha256 = ?",
            [table_name, digest],
        ).fetchone()
        if duplicate:
            summary["files"].append({"table": table_name, "path": str(path),
                                     "sha256": digest, "skipped": "相同文件摘要"})
            continue
        rows = read_table(path, table)
        key_columns = table.get("key", [])
        added = 0
        for row in rows:
            record_key = json.dumps([row[name] for name in key_columns],
                                    ensure_ascii=False)
            row_hash = _row_hash(row)
            same = con.execute(
                "SELECT 1 FROM raw_row WHERE table_name = ? AND record_key = ? AND row_hash = ?",
                [table_name, record_key, row_hash],
            ).fetchone()
            if same:
                continue
            con.execute(
                "INSERT INTO raw_row VALUES (?, ?, ?, ?, ?, ?)",
                [table_name, record_key, row_hash,
                 json.dumps(row, ensure_ascii=False), batch_id, seq],
            )
            added += 1
        con.execute(
            "INSERT INTO raw_file VALUES (?, ?, ?, ?, ?, ?, ?)",
            [file_id, batch_id, table_name, str(path), digest, len(rows), content],
        )
        file_id += 1
        summary["files"].append({"table": table_name, "path": str(path),
                                 "sha256": digest, "rows": len(rows), "rows_added": added})
        summary["rows_added"] += added
    return summary


def list_batches(con):
    """接收历史：批次、次序、可知时刻与文件数。"""
    rows = con.execute(
        """
        SELECT b.batch_id, b.seq, b.received_at, b.note,
               COUNT(f.file_id) AS files, COALESCE(SUM(f.row_count), 0) AS rows
        FROM receive_batch b
        LEFT JOIN raw_file f ON f.batch_id = b.batch_id
        GROUP BY b.batch_id, b.seq, b.received_at, b.note
        ORDER BY b.seq
        """
    ).fetchall()
    return [{"batch_id": r[0], "seq": r[1], "received_at": r[2],
             "note": r[3], "files": r[4], "rows": r[5]} for r in rows]
