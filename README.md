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
- 失败闭路径（择一）：**发现即隔离，已合法前缀不动**；装载/物化在同一事务内确定性重放，路径或点序一处变化即驱动正式轴、投影、证明、隔离同成同败；首点即违规时投影闭为「未知」。
- 跳站证明（运单、声明路径快照、违规点、预期站、实际站、隔离成员快照）只读接口：`GET /api/route-skip-proofs?waybill_id=&batch_id=`。
- 测绘板正式轴只渲染合法前缀，隔离段红色单列；隔离页展示预期/实际站与成员明细。
- 夹具：`WB-2001` 跳站单（声明 SHA→NJ→HF→WH，实际 NJ 后直跳 WH）与 `WB-2002` 合规单。

后端测试：`cd backend && python -m pytest tests/`。
