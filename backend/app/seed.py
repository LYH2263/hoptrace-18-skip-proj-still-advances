import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import route_skip_quarantine as rsq


SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

EVENT_STATUS = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    rsq.init_schema(conn)  # 声明路径表 / 跳站证明表 / 正式轴停点列
    n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], wb.get("status_proj", "未知"), wb["created_at"]),
        )
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
    # 声明路径落库（有序 location_code 列表），路径来自夹具而非校验函数写死
    for route in data.get("declared_routes", []):
        rsq.upsert_route(conn, route["waybill_id"], route["location_codes"], rsq.SEED_BATCH)
    _materialize_waybills(conn)


def _materialize_waybills(conn):
    """装载/物化统一入口：点序或路径变化在此驱动正式轴与投影同成败。"""
    ids = [r["id"] for r in conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()]
    results = []
    for wid in ids:
        results.append(rsq.evaluate_waybill(conn, wid, rsq.SEED_BATCH))
    return results


def map_status(event_code: str) -> str:
    return EVENT_STATUS.get(event_code, "未知")
