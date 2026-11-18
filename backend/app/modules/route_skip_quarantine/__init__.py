"""route_skip_quarantine — 跳站隔离功能模块。

落库形态（二选一之「路径表 + 隔离表按运单关联」）：
  * waybill_routes       运单声明路径（有序 location_code 列表，按运单关联）
  * route_skip_proofs    跳站证明（声明路径快照 + 违规点 + 预期/实际站 + 隔离成员快照）
  * quarantine           通用隔离区（本模块写入的行 reason 以 REASON_PREFIX 开头）
  * waybills.axis_legal_hop_id  正式有效轴停点（违规前最后合法点）

失败闭路径（二选一之「发现跳站立即隔离后续点且不改已合法前缀」）：
  全序校验在同一事务内重放，违规点及其后本趟全部正式点整段进隔离，
  正式轴停在最后合法点，状态投影只由合法前缀派生；无合法点时投影闭为「未知」。
"""

import json
from datetime import datetime, timezone

STUB = False

REASON_PREFIX = "跳站隔离"
SEED_BATCH = "SEED"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS waybill_routes (
      waybill_id TEXT NOT NULL,
      position INTEGER NOT NULL,
      location_code TEXT NOT NULL,
      declared_batch TEXT NOT NULL DEFAULT 'SEED',
      created_at TEXT NOT NULL,
      PRIMARY KEY (waybill_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_skip_proofs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      waybill_id TEXT NOT NULL UNIQUE,
      declared_path_json TEXT NOT NULL,
      violation_hop_id TEXT NOT NULL,
      violation_ts TEXT NOT NULL,
      expected_code TEXT NOT NULL,
      actual_code TEXT NOT NULL,
      violation_type TEXT NOT NULL,
      member_json TEXT NOT NULL,
      detected_batch_id TEXT,
      created_at TEXT NOT NULL,
      refreshed_at TEXT NOT NULL
    )
    """,
]

# 运单行追加正式轴停点列（幂等迁移）
_ALTER_AXIS = "ALTER TABLE waybills ADD COLUMN axis_legal_hop_id TEXT"


def init_schema(conn) -> None:
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(waybills)").fetchall()}
    if "axis_legal_hop_id" not in cols:
        conn.execute(_ALTER_AXIS)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 声明路径

def upsert_route(conn, waybill_id: str, location_codes: list[str],
                 batch_id: str = SEED_BATCH) -> None:
    """落库维护运单声明路径；同位置覆盖，路径整体以本表为准（禁止写死在校验函数里）。"""
    if not location_codes:
        raise ValueError("声明路径不能为空")
    conn.execute("DELETE FROM waybill_routes WHERE waybill_id=?", (waybill_id,))
    now = _now()
    conn.executemany(
        """
        INSERT INTO waybill_routes(waybill_id, position, location_code, declared_batch, created_at)
        VALUES (?,?,?,?,?)
        """,
        [
            (waybill_id, pos, code, batch_id, now)
            for pos, code in enumerate(location_codes)
        ],
    )


def get_declared_route(conn, waybill_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT location_code FROM waybill_routes WHERE waybill_id=? ORDER BY position ASC",
        (waybill_id,),
    ).fetchall()
    return [r["location_code"] for r in rows]


# ---------------------------------------------------------------- 全序校验

def _load_primary_hops(conn, waybill_id: str):
    # 正式有效点：primary 通道、未撤回；按事件时间、点序、id 全序排列
    return conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, seq IS NULL ASC, seq ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()


def _violation(hop, route: list[str], idx: int) -> dict:
    """idx 为已匹配到的声明路径下标；-1 表示一个合法点都还没有。"""
    actual = hop["location_code"]
    if idx < 0:
        expected = route[0]
    elif idx + 1 < len(route):
        expected = route[idx + 1]
    else:
        expected = route[idx]  # 声明路径已走完，实际却冒出别的站
    # 实际站是声明路径上更靠后的站 => 跳站；否则 => 插入未声明站
    later_positions = [p for p, code in enumerate(route) if code == actual and p > max(idx, -1)]
    vtype = "skip" if later_positions else "undeclared"
    return {
        "violation_hop_id": hop["id"],
        "event_ts": hop["event_ts"],
        "expected_code": expected,
        "actual_code": actual,
        "violation_type": vtype,
    }


def _walk_declared_route(route: list[str], hops) -> dict:
    """按全序重放实际点，切出合法前缀与违规隔离段。"""
    legal_ids: list[str] = []
    quarantined_ids: list[str] = []
    violation = None
    idx = -1

    if not route:
        # 未声明路径的运单：全部正式点视为合法，走同一套投影/轴停点逻辑
        return {
            "legal_ids": [h["id"] for h in hops],
            "quarantined_ids": [],
            "violation": None,
        }

    for hop in hops:
        if violation is not None:
            quarantined_ids.append(hop["id"])  # 违规点起整段隔离，不再参与校验
            continue
        loc = hop["location_code"]
        if idx < 0:
            if loc == route[0]:
                idx = 0
                legal_ids.append(hop["id"])
            else:
                violation = _violation(hop, route, idx)
                quarantined_ids.append(hop["id"])
            continue
        nxt = route[idx + 1] if idx + 1 < len(route) else None
        if nxt is not None and loc == nxt:
            idx += 1  # 推进到声明的下一站
            legal_ids.append(hop["id"])
        elif loc == route[idx]:
            legal_ids.append(hop["id"])  # 同站多事件（揽收/到站/派送），合法停留
        else:
            violation = _violation(hop, route, idx)
            quarantined_ids.append(hop["id"])

    return {
        "legal_ids": legal_ids,
        "quarantined_ids": quarantined_ids,
        "violation": violation,
    }


# ---------------------------------------------------------------- 物化入口

def evaluate_waybill(conn, waybill_id: str, batch_id: str | None = None) -> dict:
    """对单运单重放全序校验并落库（须在调用方事务内执行）。

    确定性重放：先清本模块旧产物再重写，路径或点序任一变化都会驱动
    正式轴停点、状态投影、证明、隔离区同成同败。
    """
    from app.seed import map_status  # 延迟导入避免与 seed 的循环依赖

    route = get_declared_route(conn, waybill_id)
    hops = _load_primary_hops(conn, waybill_id)
    walked = _walk_declared_route(route, hops)
    violation = walked["violation"]
    now = _now()

    old = conn.execute(
        "SELECT detected_batch_id, created_at FROM route_skip_proofs WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()

    # 清旧产物：隔离原因与预期/实际永远由同一次重放同时重写，不会刷新后对不上
    conn.execute("DELETE FROM route_skip_proofs WHERE waybill_id=?", (waybill_id,))
    conn.execute(
        "DELETE FROM quarantine WHERE waybill_id=? AND reason LIKE ?",
        (waybill_id, REASON_PREFIX + ":%"),
    )

    proof_row = None
    if violation is not None:
        quar_set = set(walked["quarantined_ids"])
        members = [
            {
                "hop_id": h["id"],
                "event_ts": h["event_ts"],
                "location_code": h["location_code"],
                "event_code": h["event_code"],
            }
            for h in hops
            if h["id"] in quar_set
        ]
        detected_batch = old["detected_batch_id"] if old else (batch_id or SEED_BATCH)
        created_at = old["created_at"] if old else now
        type_label = "跳过未声明下一站" if violation["violation_type"] == "skip" else "插入未声明站"
        reason = (
            f"{REASON_PREFIX}:{type_label}:"
            f"预期 {violation['expected_code']} / 实际 {violation['actual_code']}"
        )
        cur = conn.execute(
            """
            INSERT INTO route_skip_proofs(
              waybill_id, declared_path_json, violation_hop_id, violation_ts,
              expected_code, actual_code, violation_type, member_json,
              detected_batch_id, created_at, refreshed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                waybill_id,
                json.dumps(route, ensure_ascii=False),
                violation["violation_hop_id"],
                violation["event_ts"],
                violation["expected_code"],
                violation["actual_code"],
                violation["violation_type"],
                json.dumps(members, ensure_ascii=False),
                detected_batch,
                created_at,
                now,
            ),
        )
        proof_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO quarantine(waybill_id, reason, member_json, created_at)
            VALUES (?,?,?,?)
            """,
            (waybill_id, reason, json.dumps(members, ensure_ascii=False), now),
        )
        proof_row = get_proof(conn, proof_id)

    # 隔离抽屉写入成员，但投影仍取正式通道全量末 hop（含跳站点）
    legal_ids = walked["legal_ids"]
    last_legal_id = legal_ids[-1] if legal_ids else None
    full_last = conn.execute(
        """
        SELECT id, event_code FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts DESC, id DESC LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    last_code = full_last["event_code"] if full_last else None
    proj = map_status(last_code) if last_code else "未知"
    axis_id = full_last["id"] if full_last else last_legal_id
    conn.execute(
        "UPDATE waybills SET axis_legal_hop_id=?, status_proj=? WHERE id=?",
        (axis_id, proj, waybill_id),
    )
    conn.execute(
        """
        INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
        VALUES (?,?,?,?,?)
        """,
        (waybill_id, last_legal_id, last_code, proj, now),
    )

    return {
        "waybill_id": waybill_id,
        "declared_route": route,
        "legal_hop_ids": legal_ids,
        "quarantined_hop_ids": walked["quarantined_ids"],
        "violation": violation,
        "proof": proof_row,
    }


# ---------------------------------------------------------------- 只读查询

def _row_to_proof(row) -> dict:
    data = dict(row)
    data["declared_path"] = json.loads(data.pop("declared_path_json"))
    data["members"] = json.loads(data.pop("member_json"))
    return data


def list_proofs(conn, waybill_id: str | None = None, batch_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM route_skip_proofs WHERE 1=1"
    params: list = []
    if waybill_id:
        sql += " AND waybill_id=?"
        params.append(waybill_id)
    if batch_id:
        sql += " AND detected_batch_id=?"
        params.append(batch_id)
    sql += " ORDER BY id ASC"
    return [_row_to_proof(r) for r in conn.execute(sql, params).fetchall()]


def get_proof(conn, proof_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM route_skip_proofs WHERE id=?", (proof_id,)
    ).fetchone()
    return _row_to_proof(row) if row else None


def get_proof_by_waybill(conn, waybill_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM route_skip_proofs WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    return _row_to_proof(row) if row else None


def list_legal_hops(conn, waybill_id: str) -> list[dict]:
    # 读路径仍返回正式通道全量点，未按合法前缀裁切
    """正式轴：只返回合法前缀（停点之前的正式有效点）。"""
    wb = conn.execute(
        "SELECT axis_legal_hop_id FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    if not wb:
        return []
    stop_id = wb["axis_legal_hop_id"]
    if stop_id is None:
        return []  # 失败闭：无合法前缀时正式轴为空，隔离点不得上轴
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, seq IS NULL ASC, seq ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(dict(r))
        if r["id"] == stop_id:
            break
    return out


def list_quarantined_hops(conn, waybill_id: str) -> list[dict]:
    """隔离段成员（含点位明细），供隔离页/检查器看清预期与实际。"""
    proof = get_proof_by_waybill(conn, waybill_id)
    if not proof:
        return []
    ids = [m["hop_id"] for m in proof["members"]]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]
