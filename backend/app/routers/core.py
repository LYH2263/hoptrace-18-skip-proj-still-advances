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
            "skip_tear_verifications": conn.execute(
                "SELECT COUNT(*) AS c FROM skip_tear_verifications"
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
            "skip_tear_verification": rsq.get_tear(conn, waybill_id),
            "tear_check": rsq.verify_tear(conn, waybill_id),
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
                   q.proof_id AS quarantine_proof_id,
                   p.id AS route_skip_proof_id,
                   p.expected_code AS expected_location,
                   p.actual_code AS actual_location,
                   p.violation_hop_id,
                   p.violation_type,
                   p.reason AS proof_reason,
                   p.declared_path_json
            FROM quarantine q
            LEFT JOIN route_skip_proofs p
              ON p.waybill_id = q.waybill_id
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
            # 隔离原因必须与本次证明同源；漂移即显式标记，供隔离页警示
            item["reason_matches_proof"] = item.get("proof_reason") == item.get("reason")
            item["proof_id_matches"] = (
                item.get("quarantine_proof_id") == item.get("route_skip_proof_id")
            )
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


@router.get("/skip-tear-verifications")
def skip_tear_verifications(waybill_id: str | None = None, batch_id: str | None = None):
    """跳站撕裂核证只读接口（基线所无的新实体）。

    每行记录一次跳站处置的完整证据链：路径声明、违规点、合法前缀末 hop、
    投影前后、隔离成员。用于证明「轴停在违规前、投影未由跳站点推进」。
    """
    conn = connect()
    try:
        return rsq.list_tears(conn, waybill_id=waybill_id, batch_id=batch_id)
    finally:
        conn.close()


@router.get("/skip-tear-verifications/verify")
def skip_tear_verify(waybill_id: str):
    """把核证实体与运单轴/投影/隔离原因逐项对齐，返回互证结论。"""
    conn = connect()
    try:
        return rsq.verify_tear(conn, waybill_id)
    finally:
        conn.close()


@router.get("/skip-tear-verifications/{waybill_id}")
def skip_tear_one(waybill_id: str):
    conn = connect()
    try:
        tear = rsq.get_tear(conn, waybill_id)
        if tear is None:
            raise HTTPException(404, "该运单无跳站撕裂核证")
        return tear
    finally:
        conn.close()

