"""route_skip_quarantine — 跳站隔离功能模块。

落库形态（二选一之「路径表 + 跳站证明表 + 通用隔离表按运单关联」）：
  * waybill_routes              运单声明路径（有序 location_code 列表，按运单关联）
  * route_skip_proofs           跳站证明（声明路径快照 + 违规点 + 预期/实际站 + 原因 + 隔离成员快照）
  * quarantine                  通用隔离区（本模块写入的行 reason 以 REASON_PREFIX 开头，proof_id 回指证明）
  * skip_tear_verifications     跳站撕裂核证（声明路径、违规点、合法前缀末 hop、投影前后、隔离成员）
  * waybills.axis_legal_hop_id  正式有效轴停点（违规前最后合法点）

提交形态（二选一之「发现跳站即同事务隔离后续点并重算合法前缀投影」）：
  全序校验在同一事务内确定性重放：
    1. 违规点及其后本趟全部正式点整段进隔离；
    2. 正式有效轴只停在违规前最后合法点；
    3. 状态投影只由该合法前缀末 hop 派生（无合法点时闭为「未知」）；
    4. 证明、隔离原因、撕裂核证、状态投影同成同败。
  任何一步校验失败即抛错，由调用方整批回滚（失败闭）。
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
      reason TEXT NOT NULL DEFAULT '',
      member_json TEXT NOT NULL,
      detected_batch_id TEXT,
      created_at TEXT NOT NULL,
      refreshed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skip_tear_verifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      waybill_id TEXT NOT NULL UNIQUE,
      batch_id TEXT,
      declared_path_json TEXT NOT NULL,
      violation_hop_id TEXT NOT NULL,
      violation_ts TEXT NOT NULL,
      expected_code TEXT NOT NULL,
      actual_code TEXT NOT NULL,
      violation_type TEXT NOT NULL,
      legal_prefix_json TEXT NOT NULL,
      legal_prefix_last_hop_id TEXT,
      projection_before TEXT NOT NULL,
      projection_before_hop_id TEXT,
      projection_after TEXT NOT NULL,
      projection_after_hop_id TEXT,
      member_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      refreshed_at TEXT NOT NULL
    )
    """,
]

# 幂等迁移：旧库补列
_MIGRATIONS = [
    ("waybills", "axis_legal_hop_id", "TEXT"),
    ("route_skip_proofs", "reason", "TEXT NOT NULL DEFAULT ''"),
    ("quarantine", "proof_id", "INTEGER"),
]


def init_schema(conn) -> None:
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    for table, column, decl in _MIGRATIONS:
        _ensure_column(conn, table, column, decl)


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 声明路径

def upsert_route(conn, waybill_id: str, location_codes: list[str],
                 batch_id: str = SEED_BATCH) -> None:
    """落库维护运单声明路径；同位置覆盖，路径整体以本表为准（禁止写死在校验函数里）。"""
    if not location_codes:
        raise ValueError("声明路径不能为空")
    codes = [str(c).strip() for c in location_codes]
    if any(not c for c in codes):
        raise ValueError("声明路径不得含空站")
    if len(set(codes)) != len(codes):
        raise ValueError("声明路径不得含重复站")
    conn.execute("DELETE FROM waybill_routes WHERE waybill_id=?", (waybill_id,))
    now = _now()
    conn.executemany(
        """
        INSERT INTO waybill_routes(waybill_id, position, location_code, declared_batch, created_at)
        VALUES (?,?,?,?,?)
        """,
        [
            (waybill_id, pos, code, batch_id, now)
            for pos, code in enumerate(codes)
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
    正式轴停点、状态投影、证明、隔离区、撕裂核证同成同败。
    """
    from app.seed import map_status  # 延迟导入避免与 seed 的循环依赖

    route = get_declared_route(conn, waybill_id)
    hops = _load_primary_hops(conn, waybill_id)
    walked = _walk_declared_route(route, hops)
    violation = walked["violation"]
    legal_ids = walked["legal_ids"]
    quarantined_ids = walked["quarantined_ids"]
    quar_set = set(quarantined_ids)
    now = _now()

    by_id = {h["id"]: h for h in hops}

    # 正式有效轴 = 合法前缀末 hop；投影只由它派生。绝不由隔离点推进。
    legal_last = by_id[legal_ids[-1]] if legal_ids else None
    axis_id = legal_last["id"] if legal_last else None
    mapped_code = legal_last["event_code"] if legal_last else None
    proj = map_status(mapped_code) if mapped_code else "未知"

    # 硬约束：合法轴不得落在隔离成员上（发现即整批回滚）。
    if axis_id is not None and axis_id in quar_set:
        raise RuntimeError(
            f"跳站撕裂核证失败：{waybill_id} 正式轴落点 {axis_id} 属于隔离成员"
        )

    # 若把跳站点也当正式轴会得到的投影（仅作核证留痕，绝不写回运单）
    full_last = hops[-1] if hops else None
    proj_before = map_status(full_last["event_code"]) if full_last else "未知"
    proj_before_id = full_last["id"] if full_last else None

    old_proof = conn.execute(
        "SELECT detected_batch_id, created_at, reason FROM route_skip_proofs WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    old_tear = conn.execute(
        "SELECT created_at FROM skip_tear_verifications WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()

    # 清旧产物：证明 / 隔离 / 核证永远由同一次重放同时重写，刷新后不会漂移
    conn.execute("DELETE FROM route_skip_proofs WHERE waybill_id=?", (waybill_id,))
    conn.execute("DELETE FROM skip_tear_verifications WHERE waybill_id=?", (waybill_id,))
    conn.execute(
        "DELETE FROM quarantine WHERE waybill_id=? AND reason LIKE ?",
        (waybill_id, REASON_PREFIX + ":%"),
    )

    proof_row = None
    tear_row = None
    if violation is not None:
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
        detected_batch = old_proof["detected_batch_id"] if old_proof else (batch_id or SEED_BATCH)
        created_at = old_proof["created_at"] if old_proof else now
        type_label = "跳过未声明下一站" if violation["violation_type"] == "skip" else "插入未声明站"
        reason = (
            f"{REASON_PREFIX}:{type_label}:"
            f"预期 {violation['expected_code']} / 实际 {violation['actual_code']}"
        )
        member_json = json.dumps(members, ensure_ascii=False)
        declared_json = json.dumps(route, ensure_ascii=False)

        cur = conn.execute(
            """
            INSERT INTO route_skip_proofs(
              waybill_id, declared_path_json, violation_hop_id, violation_ts,
              expected_code, actual_code, violation_type, reason, member_json,
              detected_batch_id, created_at, refreshed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                waybill_id,
                declared_json,
                violation["violation_hop_id"],
                violation["event_ts"],
                violation["expected_code"],
                violation["actual_code"],
                violation["violation_type"],
                reason,
                member_json,
                detected_batch,
                created_at,
                now,
            ),
        )
        proof_id = cur.lastrowid
        # 隔离原因与预期/实际站同源：原因字符串由本次重放的 violation 生成，
        # 行上 proof_id 回指同一份证明，刷新后不会对不上。
        conn.execute(
            """
            INSERT INTO quarantine(waybill_id, reason, member_json, created_at, proof_id)
            VALUES (?,?,?,?,?)
            """,
            (waybill_id, reason, member_json, now, proof_id),
        )

        legal_prefix_json = json.dumps(
            [
                {
                    "hop_id": h["id"],
                    "location_code": h["location_code"],
                    "event_code": h["event_code"],
                }
                for h in hops
                if h["id"] in set(legal_ids)
            ],
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO skip_tear_verifications(
              waybill_id, batch_id, declared_path_json, violation_hop_id, violation_ts,
              expected_code, actual_code, violation_type, legal_prefix_json,
              legal_prefix_last_hop_id, projection_before, projection_before_hop_id,
              projection_after, projection_after_hop_id, member_json,
              created_at, refreshed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                waybill_id,
                detected_batch,
                declared_json,
                violation["violation_hop_id"],
                violation["event_ts"],
                violation["expected_code"],
                violation["actual_code"],
                violation["violation_type"],
                legal_prefix_json,
                axis_id,
                proj_before,
                proj_before_id,
                proj,
                axis_id,
                member_json,
                old_tear["created_at"] if old_tear else now,
                now,
            ),
        )
        proof_row = get_proof(conn, proof_id)
        tear_row = get_tear(conn, waybill_id)

    # 状态投影只由合法前缀末 hop 派生（隔离点绝不推进投影）
    conn.execute(
        "UPDATE waybills SET axis_legal_hop_id=?, status_proj=? WHERE id=?",
        (axis_id, proj, waybill_id),
    )
    conn.execute(
        """
        INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
        VALUES (?,?,?,?,?)
        """,
        (waybill_id, axis_id, mapped_code, proj, now),
    )

    return {
        "waybill_id": waybill_id,
        "declared_route": route,
        "legal_hop_ids": legal_ids,
        "quarantined_hop_ids": quarantined_ids,
        "axis_legal_hop_id": axis_id,
        "projection": proj,
        "projection_before": proj_before,
        "violation": violation,
        "proof": proof_row,
        "tear_verification": tear_row,
    }


# ---------------------------------------------------------------- 只读查询

def _row_to_proof(row) -> dict:
    data = dict(row)
    data["declared_path"] = json.loads(data.pop("declared_path_json"))
    data["members"] = json.loads(data.pop("member_json"))
    return data


def _row_to_tear(row) -> dict:
    data = dict(row)
    data["declared_path"] = json.loads(data.pop("declared_path_json"))
    data["legal_prefix"] = json.loads(data.pop("legal_prefix_json"))
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


def list_tears(conn, waybill_id: str | None = None, batch_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM skip_tear_verifications WHERE 1=1"
    params: list = []
    if waybill_id:
        sql += " AND waybill_id=?"
        params.append(waybill_id)
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    sql += " ORDER BY id ASC"
    return [_row_to_tear(r) for r in conn.execute(sql, params).fetchall()]


def get_tear(conn, waybill_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM skip_tear_verifications WHERE waybill_id=?", (waybill_id,)
    ).fetchone()
    return _row_to_tear(row) if row else None


def list_legal_hops(conn, waybill_id: str) -> list[dict]:
    """正式轴：只返回合法前缀（违规前最后合法点为止，隔离成员绝不上轴）。"""
    wb = conn.execute(
        "SELECT axis_legal_hop_id FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    if not wb or wb["axis_legal_hop_id"] is None:
        return []  # 失败闭：无合法前缀时正式轴为空，隔离点不得上轴
    stop_id = wb["axis_legal_hop_id"]

    proof = get_proof_by_waybill(conn, waybill_id)
    member_ids = {m["hop_id"] for m in proof["members"]} if proof else set()
    if stop_id in member_ids:
        # 读侧兜底：即使轴停点列被旧产物污染，也绝不让隔离点露出正式轴
        return []

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
        if r["id"] in member_ids:
            break  # 违规点及之后整段隔离，永不进正式轴
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


# ---------------------------------------------------------------- 互证

def verify_tear(conn, waybill_id: str) -> dict:
    """跳站撕裂互证：把核证实体与运单轴/投影/隔离原因逐项对齐。

    返回只读核对结果，供检查器与接口同时展示；任何一项为 False 即说明
    写路径与读路径出现分叉。
    """
    from app.seed import map_status

    wb = conn.execute(
        "SELECT axis_legal_hop_id, status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    if not wb:
        return {"waybill_id": waybill_id, "exists": False, "consistent": False}

    tear = get_tear(conn, waybill_id)
    proof = get_proof_by_waybill(conn, waybill_id)
    declared = get_declared_route(conn, waybill_id)
    axis_id = wb["axis_legal_hop_id"]
    proj = wb["status_proj"]

    axis_hop = None
    if axis_id:
        axis_hop = conn.execute(
            "SELECT id, event_code FROM hops WHERE id=? AND waybill_id=?",
            (axis_id, waybill_id),
        ).fetchone()

    checks = {
        "axis_hop_exists": axis_id is None or axis_hop is not None,
        "projection_from_axis": (
            (axis_id is None and proj == "未知")
            or (axis_hop is not None and proj == map_status(axis_hop["event_code"]))
        ),
    }

    if tear is None:
        # 无跳站：正式轴必须等于全量正式点末 hop，且不存在隔离成员
        fulllast = conn.execute(
            """
            SELECT id FROM hops
            WHERE waybill_id=? AND channel='primary' AND retracted=0
            ORDER BY event_ts DESC, seq IS NULL DESC, seq DESC, id DESC LIMIT 1
            """,
            (waybill_id,),
        ).fetchone()
        checks["legal_axis_is_full_axis"] = (
            (fulllast is None and axis_id is None)
            or (fulllast is not None and axis_id == fulllast["id"])
        )
        checks["no_quarantine_members"] = proof is None
        checks["declared_path_matches"] = True if proof is None else (
            proof["declared_path"] == declared
        )
        return {
            "waybill_id": waybill_id,
            "exists": True,
            "has_tear": False,
            "consistent": all(checks.values()),
            "checks": checks,
            "axis_legal_hop_id": axis_id,
            "projection": proj,
            "tear": None,
        }

    member_ids = {m["hop_id"] for m in tear["members"]}
    before_hop = None
    if tear["projection_before_hop_id"]:
        before_hop = conn.execute(
            "SELECT id, event_code FROM hops WHERE id=? AND waybill_id=?",
            (tear["projection_before_hop_id"], waybill_id),
        ).fetchone()
    # 隔离区行必须与本次证明同源：原因文本、成员快照、proof_id 回指三处对齐
    quar = conn.execute(
        """
        SELECT reason, member_json, proof_id FROM quarantine
        WHERE waybill_id=? AND reason LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (waybill_id, REASON_PREFIX + ":%"),
    ).fetchone()
    checks.update(
        {
            "axis_is_legal_prefix_last": tear["legal_prefix_last_hop_id"] == axis_id,
            "axis_not_quarantined": axis_id is None or axis_id not in member_ids,
            "projection_matches_after": tear["projection_after"] == proj,
            "projection_before_matches_hop": (
                (tear["projection_before_hop_id"] is None and tear["projection_before"] == "未知")
                or (
                    before_hop is not None
                    and tear["projection_before"] == map_status(before_hop["event_code"])
                )
            ),
            "declared_path_matches": proof is not None and proof["declared_path"] == declared,
            "members_match_proof": proof is not None and proof["members"] == tear["members"],
            "reason_matches_expected_actual": (
                proof is not None
                and f"预期 {proof['expected_code']}" in proof["reason"]
                and f"实际 {proof['actual_code']}" in proof["reason"]
            ),
            "quarantine_row_present": quar is not None,
            "quarantine_reason_matches_proof": (
                quar is not None
                and proof is not None
                and quar["reason"] == proof["reason"]
            ),
            "quarantine_members_match_proof": (
                quar is not None
                and proof is not None
                and quar["member_json"] == json.dumps(proof["members"], ensure_ascii=False)
            ),
            "quarantine_points_to_proof": (
                quar is not None
                and proof is not None
                and quar["proof_id"] == proof["id"]
            ),
        }
    )
    return {
        "waybill_id": waybill_id,
        "exists": True,
        "has_tear": True,
        "consistent": all(checks.values()),
        "checks": checks,
        "axis_legal_hop_id": axis_id,
        "projection": proj,
        "tear": tear,
    }
