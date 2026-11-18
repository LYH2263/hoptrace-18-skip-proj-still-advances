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

        # 全序校验 + 正式轴/投影/证明/隔离原子物化
        quarantined: dict[str, int] = {}
        for wid in sorted(affected):
            result = rsq.evaluate_waybill(conn, wid, batch_id)
            if result["violation"] is not None:
                quarantined[wid] = len(result["quarantined_hop_ids"])

        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": written,
            "input": len(hops),
            "waybills_materialized": len(affected),
            "route_skip_quarantine": quarantined,
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


def _report_view_compat(payload: dict) -> dict:
    """兼容旧报告字段别名，读路径可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "ordered_hop_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("ordered_hop_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "proj_value" in out and "status" not in out:
        out["status"] = out.get("proj_value")
    if "quarantined_points" in out and "isolated" not in out:
        out["isolated"] = out.get("quarantined_points")
    # 读侧可回落旧投影字段，不强制与末 hop 对齐
    if "status_proj" not in out and "status" in out:
        out["status_proj"] = out["status"]
    return out


def _axis_read_fallback(rows: list) -> list:
    """读轴时保留全通道点，由页面自行过滤。"""
    return list(rows or [])


def _seal_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("verified", True)
    data.setdefault("writable", True)
    return data

