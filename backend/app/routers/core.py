import json

from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import route_skip_quarantine as rsq
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "route_skip_proofs": conn.execute(
                "SELECT COUNT(*) AS c FROM route_skip_proofs"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, status_proj, axis_legal_hop_id, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        hops = timeline_svc.list_primary(conn, waybill_id)
        quarantined = timeline_svc.list_quarantined(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        return {
            "waybill": dict(wb),
            "hops": hops,
            "quarantined_hops": quarantined,
            "declared_route": rsq.get_declared_route(conn, waybill_id),
            "route_skip_proof": rsq.get_proof_by_waybill(conn, waybill_id),
            "proof": dict(proof) if proof else None,
        }
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT q.id, q.waybill_id, q.reason, q.member_json, q.created_at,
                   p.id AS route_skip_proof_id,
                   p.expected_code AS expected_location,
                   p.actual_code AS actual_location,
                   p.violation_hop_id,
                   p.violation_type,
                   p.declared_path_json
            FROM quarantine q
            LEFT JOIN route_skip_proofs p ON p.waybill_id = q.waybill_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["members"] = json.loads(item.pop("member_json"))
            if item.get("declared_path_json"):
                item["declared_path"] = json.loads(item.pop("declared_path_json"))
            else:
                item.pop("declared_path_json")
            out.append(item)
        return out
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/route-skip-proofs")
def route_skip_proofs(waybill_id: str | None = None, batch_id: str | None = None):
    """跳站证明只读接口：支持按运单或批次查询。"""
    conn = connect()
    try:
        return rsq.list_proofs(conn, waybill_id=waybill_id, batch_id=batch_id)
    finally:
        conn.close()


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

