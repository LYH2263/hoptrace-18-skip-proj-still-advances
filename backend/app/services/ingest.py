import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import route_skip_quarantine as rsq


def run_batch() -> dict:
    """装载批次：写点与跳站物化在同一事务内完成，失败整体回滚（失败闭）。"""
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        affected: set[str] = set()
        for hop in hops:
            wid = hop["waybill_id"]
            exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
            affected.add(wid)

        # 本批携带的声明路径同样落库（路径一处变化即驱动物化重放）
        for route in payload.get("declared_routes", []):
            rsq.upsert_route(conn, route["waybill_id"], route["location_codes"], batch_id)
            affected.add(route["waybill_id"])

        # 全序校验 + 正式轴/投影/证明/隔离/撕裂核证原子物化
        quarantined: dict[str, int] = {}
        for wid in sorted(affected):
            result = rsq.evaluate_waybill(conn, wid, batch_id)
            if result["violation"] is not None:
                quarantined[wid] = len(result["quarantined_hop_ids"])

        # 提交前互证闸：轴/投影/隔离原因任一项与核证实体分叉即整批回滚
        inconsistent: dict[str, dict] = {}
        for wid in sorted(affected):
            check = rsq.verify_tear(conn, wid)
            if not check["consistent"]:
                inconsistent[wid] = check["checks"]
        if inconsistent:
            raise RuntimeError(f"跳站撕裂互证失败，整批回滚: {inconsistent}")

        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": written,
            "input": len(hops),
            "waybills_materialized": len(affected),
            "route_skip_quarantine": quarantined,
            "skip_tear_verifications": sum(
                1 for wid in affected if rsq.get_tear(conn, wid) is not None
            ),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

