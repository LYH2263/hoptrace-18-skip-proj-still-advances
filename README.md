# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 跳站隔离（route_skip_quarantine）

运单声明路径与实际正式点序做全序校验：跳过未声明下一站或插入未声明站时，
从违规点起本趟点整段隔离，正式有效轴停在违规前最后合法点，状态投影只由合法前缀派生。

- 落库形态（择一）：**路径表 `waybill_routes` + 跳站证明表 `route_skip_proofs` + 通用隔离表 `quarantine` 按运单关联**；运单行以 `axis_legal_hop_id` 记录轴停点。
- 提交形态（择一）：**发现跳站即同事务隔离后续点，并重算合法前缀投影**；装载/物化在同一事务内确定性重放，路径或点序一处变化即驱动正式轴、投影、证明、隔离、核证同成同败；首点即违规时投影闭为「未知」。装载提交前逐单跑撕裂互证闸，任一单分叉即整批回滚。
- 跳站证明（运单、声明路径快照、违规点、预期站、实际站、隔离成员快照）只读接口：`GET /api/route-skip-proofs?waybill_id=&batch_id=`。
- **跳站撕裂核证**（基线所无的新实体，表 `skip_tear_verifications`）：每行固化一次处置的完整证据链——路径声明、违规点、合法前缀末 hop、投影前后、隔离成员。只读接口：
  - `GET /api/skip-tear-verifications?waybill_id=&batch_id=` 列表
  - `GET /api/skip-tear-verifications/{waybill_id}` 单运单（无核证返回 404）
  - `GET /api/skip-tear-verifications/verify?waybill_id=` 与运单轴/投影/隔离原因逐项互证
- 硬约束由写路径与读路径共同兜底：轴停点绝不落在隔离成员上；正式轴永不露出违规点；隔离原因与证明同源（`quarantine.proof_id` 回指、原因文本同一次重放生成），刷新不漂移。
- 测绘板正式轴只渲染合法前缀，隔离段红色单列；隔离页展示预期/实际站、成员明细与「原因同源」状态；证明页展示跳站证明与撕裂核证。
- 夹具：`WB-2001` 跳站单（声明 SHA→NJ→HF→WH，实际 NJ 后直跳 WH，跳站点 DELIVERED 与合法前缀到站形成投影撕裂）与 `WB-2002` 合规单，覆盖「跳站发生」与「路径合法」两对照。

后端测试：`cd backend && python -m pytest tests/`。
