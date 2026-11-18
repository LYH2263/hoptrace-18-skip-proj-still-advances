import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="hoptrace-test-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("FIXTURES_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "fixtures"))

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import FIXTURES_DIR, connect  # noqa: E402
from app.main import app  # noqa: E402
from app.modules import route_skip_quarantine as rsq  # noqa: E402


@pytest.fixture()
def conn(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="hoptrace-case-")
    monkeypatch.setattr("app.db.DB_PATH", os.path.join(tmp, "case.db"))
    c = connect()
    c.executescript(seed.SCHEMA)
    rsq.init_schema(c)
    yield c
    c.close()


def _add_waybill(conn, wid):
    conn.execute(
        "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
        (wid, wid, "未知", "2026-11-17T08:00:00Z"),
    )


def _add_hop(conn, hid, wid, ts, loc, code, seq):
    conn.execute(
        "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
        "VALUES (?,?,?,?,?,?, 'primary')",
        (hid, wid, ts, loc, code, seq),
    )


# ---------------------------------------------------------------- 跳站主路径

def test_skip_quarantines_from_violation_and_keeps_legal_prefix(conn):
    # 声明 A→B→C，实际 A 后直接 C
    _add_waybill(conn, "WB-T2")
    rsq.upsert_route(conn, "WB-T2", ["A", "B", "C"])
    _add_hop(conn, "k1", "WB-T2", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "k2", "WB-T2", "2026-11-17T10:00:00Z", "C", "ARRIVED_HUB", 2)
    _add_hop(conn, "k3", "WB-T2", "2026-11-17T11:00:00Z", "C", "OUT_FOR_DELIVERY", 3)

    result = rsq.evaluate_waybill(conn, "WB-T2", "B-T")
    conn.commit()

    assert result["legal_hop_ids"] == ["k1"]                      # 正式轴停在违规前最后合法点
    assert result["quarantined_hop_ids"] == ["k2", "k3"]         # 违规点起整段隔离
    v = result["violation"]
    assert v["violation_hop_id"] == "k2"
    assert v["expected_code"] == "B"                              # 预期下一站
    assert v["actual_code"] == "C"                                # 实际站
    assert v["violation_type"] == "skip"

    legal = rsq.list_legal_hops(conn, "WB-T2")
    assert [h["id"] for h in legal] == ["k1"]                    # 测绘板只显示合法前缀
    quar = rsq.list_quarantined_hops(conn, "WB-T2")
    assert [h["id"] for h in quar] == ["k2", "k3"]

    wb = conn.execute("SELECT * FROM waybills WHERE id='WB-T2'").fetchone()
    assert wb["axis_legal_hop_id"] == "k1"
    assert wb["status_proj"] == "已揽收"                          # 投影只由合法前缀（A/PICKED）派生

    proof = rsq.get_proof_by_waybill(conn, "WB-T2")
    assert proof["declared_path"] == ["A", "B", "C"]
    assert proof["expected_code"] == "B" and proof["actual_code"] == "C"
    assert [m["hop_id"] for m in proof["members"]] == ["k2", "k3"]
    assert proof["detected_batch_id"] == "B-T"

    q = conn.execute("SELECT * FROM quarantine WHERE waybill_id='WB-T2'").fetchone()
    assert "预期 B" in q["reason"] and "实际 C" in q["reason"]   # 隔离原因与预期/实际对得上


def test_undeclared_insertion_is_quarantined(conn):
    _add_waybill(conn, "WB-T3")
    rsq.upsert_route(conn, "WB-T3", ["A", "B", "C"])
    _add_hop(conn, "u1", "WB-T3", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "u2", "WB-T3", "2026-11-17T09:00:00Z", "X", "IN_TRANSIT", 2)  # 插入未声明站
    _add_hop(conn, "u3", "WB-T3", "2026-11-17T10:00:00Z", "B", "ARRIVED_HUB", 3)

    result = rsq.evaluate_waybill(conn, "WB-T3", "B-T")
    conn.commit()

    assert result["violation"]["violation_type"] == "undeclared"
    assert result["legal_hop_ids"] == ["u1"]
    assert result["quarantined_hop_ids"] == ["u2", "u3"]


def test_first_hop_violation_fails_closed_to_unknown(conn):
    _add_waybill(conn, "WB-T4")
    rsq.upsert_route(conn, "WB-T4", ["A", "B"])
    _add_hop(conn, "f1", "WB-T4", "2026-11-17T08:00:00Z", "B", "PICKED", 1)

    rsq.evaluate_waybill(conn, "WB-T4", "B-T")
    conn.commit()

    wb = conn.execute("SELECT * FROM waybills WHERE id='WB-T4'").fetchone()
    assert wb["axis_legal_hop_id"] is None                        # 无合法前缀
    assert wb["status_proj"] == "未知"                            # 禁止用隔离点派生投影
    assert rsq.list_legal_hops(conn, "WB-T4") == []


def test_compliant_waybill_has_full_axis_and_no_proof(conn):
    _add_waybill(conn, "WB-T5")
    rsq.upsert_route(conn, "WB-T5", ["A", "B", "C"])
    _add_hop(conn, "g1", "WB-T5", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "g2", "WB-T5", "2026-11-17T09:00:00Z", "B", "IN_TRANSIT", 2)
    _add_hop(conn, "g3", "WB-T5", "2026-11-17T10:00:00Z", "C", "DELIVERED", 3)

    result = rsq.evaluate_waybill(conn, "WB-T5", "B-T")
    conn.commit()

    assert result["violation"] is None
    assert result["legal_hop_ids"] == ["g1", "g2", "g3"]
    assert rsq.get_proof_by_waybill(conn, "WB-T5") is None
    wb = conn.execute("SELECT * FROM waybills WHERE id='WB-T5'").fetchone()
    assert wb["axis_legal_hop_id"] == "g3"
    assert wb["status_proj"] == "已签收"


def test_replay_is_idempotent_and_repairs_when_violation_clears(conn):
    _add_waybill(conn, "WB-T6")
    rsq.upsert_route(conn, "WB-T6", ["A", "B", "C"])
    _add_hop(conn, "r1", "WB-T6", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "r2", "WB-T6", "2026-11-17T09:00:00Z", "C", "ARRIVED_HUB", 2)

    rsq.evaluate_waybill(conn, "WB-T6", "B-1")
    rsq.evaluate_waybill(conn, "WB-T6", "B-2")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM route_skip_proofs WHERE waybill_id='WB-T6'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM quarantine WHERE waybill_id='WB-T6'").fetchone()["c"] == 1

    # 修复：违规点撤回并补齐 B，重放后证明/隔离须消失，正式轴恢复
    conn.execute("UPDATE hops SET retracted=1 WHERE id='r2'")
    _add_hop(conn, "r3", "WB-T6", "2026-11-17T09:30:00Z", "B", "IN_TRANSIT", 2)
    _add_hop(conn, "r4", "WB-T6", "2026-11-17T10:30:00Z", "C", "ARRIVED_HUB", 3)
    rsq.evaluate_waybill(conn, "WB-T6", "B-3")
    conn.commit()

    assert rsq.get_proof_by_waybill(conn, "WB-T6") is None
    assert rsq.get_tear(conn, "WB-T6") is None
    assert conn.execute("SELECT COUNT(*) c FROM quarantine WHERE waybill_id='WB-T6'").fetchone()["c"] == 0
    assert [h["id"] for h in rsq.list_legal_hops(conn, "WB-T6")] == ["r1", "r3", "r4"]


# ---------------------------------------------------------------- 跳站撕裂核证

def test_tear_verification_records_before_after_projection_and_prefix(conn):
    _add_waybill(conn, "WB-V1")
    rsq.upsert_route(conn, "WB-V1", ["A", "B", "C"])
    _add_hop(conn, "v1", "WB-V1", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "v2", "WB-V1", "2026-11-17T10:00:00Z", "C", "DELIVERED", 2)

    rsq.evaluate_waybill(conn, "WB-V1", "B-V")
    conn.commit()

    tear = rsq.get_tear(conn, "WB-V1")
    assert tear["declared_path"] == ["A", "B", "C"]
    assert tear["violation_hop_id"] == "v2"
    assert tear["legal_prefix_last_hop_id"] == "v1"          # 合法前缀末 hop
    assert tear["projection_before_hop_id"] == "v2"          # 若含跳站点会得到「已签收」
    assert tear["projection_before"] == "已签收"
    assert tear["projection_after_hop_id"] == "v1"
    assert tear["projection_after"] == "已揽收"               # 正式投影停在合法前缀
    assert [m["hop_id"] for m in tear["members"]] == ["v2"]
    assert [h["hop_id"] for h in tear["legal_prefix"]] == ["v1"]

    check = rsq.verify_tear(conn, "WB-V1")
    assert check["has_tear"] is True
    assert check["consistent"] is True, check["checks"]


def test_hard_constraint_axis_never_lands_on_quarantined_member(conn):
    _add_waybill(conn, "WB-V2")
    rsq.upsert_route(conn, "WB-V2", ["A", "B", "C"])
    _add_hop(conn, "w1", "WB-V2", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "w2", "WB-V2", "2026-11-17T09:00:00Z", "C", "ARRIVED_HUB", 2)

    rsq.evaluate_waybill(conn, "WB-V2", "B-V")
    conn.commit()

    axis = conn.execute(
        "SELECT axis_legal_hop_id FROM waybills WHERE id='WB-V2'"
    ).fetchone()["axis_legal_hop_id"]
    tear = rsq.get_tear(conn, "WB-V2")
    member_ids = {m["hop_id"] for m in tear["members"]}
    assert axis not in member_ids                            # 轴绝不落在隔离成员上

    legal_ids = [h["id"] for h in rsq.list_legal_hops(conn, "WB-V2")]
    assert not (set(legal_ids) & member_ids)                 # 正式轴与隔离成员互斥


def test_legacy_polluted_axis_column_never_leaks_skip_point(conn):
    """读侧硬约束：即使轴停点列被旧产物写成违规点，正式轴也不得露出它。"""
    _add_waybill(conn, "WB-V3")
    rsq.upsert_route(conn, "WB-V3", ["A", "B", "C"])
    _add_hop(conn, "x1", "WB-V3", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "x2", "WB-V3", "2026-11-17T09:00:00Z", "C", "ARRIVED_HUB", 2)
    rsq.evaluate_waybill(conn, "WB-V3", "B-V")
    conn.execute("UPDATE waybills SET axis_legal_hop_id='x2' WHERE id='WB-V3'")  # 模拟旧产物

    legal_ids = [h["id"] for h in rsq.list_legal_hops(conn, "WB-V3")]
    assert legal_ids == []                                   # 隔离点不上轴

    check = rsq.verify_tear(conn, "WB-V3")
    assert check["consistent"] is False                      # 互证必须报警


def test_quarantine_reason_is_same_source_as_proof_and_survives_replay(conn):
    _add_waybill(conn, "WB-V4")
    rsq.upsert_route(conn, "WB-V4", ["A", "B", "C"])
    _add_hop(conn, "y1", "WB-V4", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "y2", "WB-V4", "2026-11-17T09:00:00Z", "C", "IN_TRANSIT", 2)

    rsq.evaluate_waybill(conn, "WB-V4", "B-1")
    conn.commit()
    first = conn.execute(
        "SELECT q.reason, q.proof_id, p.id AS proof_id2, p.reason AS proof_reason, q.created_at "
        "FROM quarantine q JOIN route_skip_proofs p ON p.waybill_id=q.waybill_id "
        "WHERE q.waybill_id='WB-V4'"
    ).fetchone()

    # 路径刷新（声明路径整体重写）后重放：原因与预期/实际不得漂移
    rsq.upsert_route(conn, "WB-V4", ["A", "B", "C"])
    rsq.evaluate_waybill(conn, "WB-V4", "B-2")
    conn.commit()
    second = conn.execute(
        "SELECT q.reason, q.proof_id, p.id AS proof_id2, p.reason AS proof_reason, q.created_at "
        "FROM quarantine q JOIN route_skip_proofs p ON p.waybill_id=q.waybill_id "
        "WHERE q.waybill_id='WB-V4'"
    ).fetchone()

    assert first["reason"] == second["reason"] == second["proof_reason"]
    assert "预期 B" in second["reason"] and "实际 C" in second["reason"]
    assert first["proof_id"] == first["proof_id2"]           # 隔离行与证明同源回指
    assert first["created_at"] == second["created_at"]       # 首次发现时间稳定
    assert rsq.verify_tear(conn, "WB-V4")["consistent"] is True


def test_verify_tear_flags_reason_drift(conn):
    _add_waybill(conn, "WB-V5")
    rsq.upsert_route(conn, "WB-V5", ["A", "B", "C"])
    _add_hop(conn, "z1", "WB-V5", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "z2", "WB-V5", "2026-11-17T09:00:00Z", "C", "IN_TRANSIT", 2)
    rsq.evaluate_waybill(conn, "WB-V5", "B-V")
    conn.commit()

    assert rsq.verify_tear(conn, "WB-V5")["consistent"] is True
    conn.execute(
        "UPDATE quarantine SET reason='跳站隔离:漂移:预期 X / 实际 Y' WHERE waybill_id='WB-V5'"
    )
    # 隔离原因被篡改后与证明不再同源，互证必须报警（禁止原因漂移）
    drift = rsq.verify_tear(conn, "WB-V5")
    assert drift["consistent"] is False
    assert drift["checks"]["quarantine_reason_matches_proof"] is False


def test_compliant_waybill_has_no_tear_entity(conn):
    _add_waybill(conn, "WB-V6")
    rsq.upsert_route(conn, "WB-V6", ["A", "B"])
    _add_hop(conn, "c1", "WB-V6", "2026-11-17T08:00:00Z", "A", "PICKED", 1)
    _add_hop(conn, "c2", "WB-V6", "2026-11-17T09:00:00Z", "B", "DELIVERED", 2)

    rsq.evaluate_waybill(conn, "WB-V6", "B-V")
    conn.commit()

    assert rsq.get_tear(conn, "WB-V6") is None
    check = rsq.verify_tear(conn, "WB-V6")
    assert check["has_tear"] is False
    assert check["consistent"] is True, check["checks"]


def test_proof_queries_by_waybill_and_batch(conn):
    for wid, batch in [("WB-Q1", "B-A"), ("WB-Q2", "B-B")]:
        _add_waybill(conn, wid)
        rsq.upsert_route(conn, wid, ["A", "B"])
        _add_hop(conn, wid + "-1", wid, "2026-11-17T08:00:00Z", "A", "PICKED", 1)
        _add_hop(conn, wid + "-2", wid, "2026-11-17T09:00:00Z", "B", "PICKED", 2)
        conn.execute("UPDATE hops SET location_code='Z' WHERE id=?", (wid + "-2",))
        rsq.evaluate_waybill(conn, wid, batch)
    conn.commit()

    assert len(rsq.list_proofs(conn, batch_id="B-A")) == 1
    assert len(rsq.list_proofs(conn, waybill_id="WB-Q2")) == 1
    assert len(rsq.list_proofs(conn)) == 2


# ---------------------------------------------------------------- 夹具与接口

def test_seed_fixtures_contain_skip_and_compliant_waybills(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="hoptrace-seed-")
    monkeypatch.setattr("app.db.DB_PATH", os.path.join(tmp, "seed.db"))
    monkeypatch.setattr("app.seed.FIXTURES_DIR", FIXTURES_DIR)
    seed.init_db()
    c = connect()
    try:
        proof = rsq.get_proof_by_waybill(c, "WB-2001")
        assert proof is not None                                     # 跳站单
        assert proof["expected_code"] == "HF-HUB"
        assert proof["actual_code"] == "WH-HUB"
        assert [h["id"] for h in rsq.list_legal_hops(c, "WB-2001")] == ["H-2001-1", "H-2001-2"]

        # 跳站撕裂核证：轴停点/投影/隔离成员同源可查
        tear = rsq.get_tear(c, "WB-2001")
        assert tear["legal_prefix_last_hop_id"] == "H-2001-2"
        assert tear["projection_before_hop_id"] == "H-2001-3"
        assert tear["projection_before"] == "已签收"                 # 若含跳站点会得到的投影
        assert tear["projection_after"] == "到站"                    # 合法前缀末 hop 派生
        assert [m["hop_id"] for m in tear["members"]] == ["H-2001-3"]
        wb2001 = c.execute("SELECT * FROM waybills WHERE id='WB-2001'").fetchone()
        assert wb2001["axis_legal_hop_id"] == "H-2001-2"
        assert rsq.verify_tear(c, "WB-2001")["consistent"] is True

        assert rsq.get_proof_by_waybill(c, "WB-2002") is None       # 合规单
        assert rsq.get_tear(c, "WB-2002") is None
        assert [h["id"] for h in rsq.list_legal_hops(c, "WB-2002")] == [
            "H-2002-1", "H-2002-2", "H-2002-3"
        ]
        assert rsq.verify_tear(c, "WB-2002")["consistent"] is True

        routes = {
            r["waybill_id"]: rsq.get_declared_route(c, r["waybill_id"])
            for r in c.execute("SELECT DISTINCT waybill_id FROM waybill_routes").fetchall()
        }
        assert routes["WB-2001"] == ["SHA-HUB", "NJ-HUB", "HF-HUB", "WH-HUB"]
    finally:
        c.close()


def test_api_endpoints_and_ingest_flow(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="hoptrace-api-")
    monkeypatch.setattr("app.db.DB_PATH", os.path.join(tmp, "api.db"))
    monkeypatch.setattr("app.seed.FIXTURES_DIR", FIXTURES_DIR)
    monkeypatch.setattr("app.services.ingest.FIXTURES_DIR", FIXTURES_DIR)
    seed.init_db()
    client = TestClient(app)

    tl = client.get("/api/waybills/WB-2001/timeline").json()
    assert [h["id"] for h in tl["hops"]] == ["H-2001-1", "H-2001-2"]   # 正式轴仅合法前缀
    assert tl["declared_route"] == ["SHA-HUB", "NJ-HUB", "HF-HUB", "WH-HUB"]
    assert tl["route_skip_proof"]["expected_code"] == "HF-HUB"

    proofs = client.get("/api/route-skip-proofs").json()
    assert any(p["waybill_id"] == "WB-2001" for p in proofs)
    by_wb = client.get("/api/route-skip-proofs?waybill_id=WB-2001").json()
    assert len(by_wb) == 1 and by_wb[0]["actual_code"] == "WH-HUB"

    # 跳站撕裂核证只读接口
    tears = client.get("/api/skip-tear-verifications").json()
    assert any(t["waybill_id"] == "WB-2001" for t in tears)
    assert all(t["waybill_id"] != "WB-2002" for t in tears)      # 合规单无核证
    one = client.get("/api/skip-tear-verifications/WB-2001").json()
    assert one["legal_prefix_last_hop_id"] == "H-2001-2"
    assert one["projection_before"] == "已签收" and one["projection_after"] == "到站"
    verdict = client.get("/api/skip-tear-verifications/verify?waybill_id=WB-2001").json()
    assert verdict["consistent"] is True and verdict["has_tear"] is True
    ok_verdict = client.get("/api/skip-tear-verifications/verify?waybill_id=WB-2002").json()
    assert ok_verdict["consistent"] is True and ok_verdict["has_tear"] is False
    assert client.get("/api/skip-tear-verifications/WB-2002").status_code == 404

    q = client.get("/api/quarantine").json()
    row = next(r for r in q if r["waybill_id"] == "WB-2001")
    assert row["expected_location"] == "HF-HUB"
    assert row["actual_location"] == "WH-HUB"
    assert row["reason_matches_proof"] is True and row["proof_id_matches"] is True

    # 跑装载批次：跳站单后续点继续整段隔离，合规单走完全程
    rep = client.post("/api/ingest/run").json()
    assert rep["ok"] is True
    assert rep["report"]["route_skip_quarantine"]["WB-2001"] == 2

    c = connect()
    try:
        assert [h["id"] for h in rsq.list_legal_hops(c, "WB-2001")] == ["H-2001-1", "H-2001-2"]
        quar = [h["id"] for h in rsq.list_quarantined_hops(c, "WB-2001")]
        assert quar == ["H-2001-3", "H-2001-4"]
        assert [h["id"] for h in rsq.list_legal_hops(c, "WB-2002")] == [
            "H-2002-1", "H-2002-2", "H-2002-3", "H-2002-4"
        ]
        by_batch = client.get(f"/api/route-skip-proofs?batch_id={rep['report']['batch_id']}").json()
        # WB-2001 在 SEED 已发现，detected_batch 保持 SEED；按批次过滤接口本身可用
        assert isinstance(by_batch, list)
    finally:
        c.close()
