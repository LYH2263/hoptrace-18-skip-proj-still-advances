from app.modules import route_skip_quarantine as rsq


def list_primary(conn, waybill_id: str) -> list[dict]:
    """测绘板正式轴：只显示合法前缀，隔离点不进正式轴、不参与投影。"""
    return rsq.list_legal_hops(conn, waybill_id)


def list_quarantined(conn, waybill_id: str) -> list[dict]:
    """本运单被跳站隔离的整段点（违规点起），供隔离页/检查器查看。"""
    return rsq.list_quarantined_hops(conn, waybill_id)

