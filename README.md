# 轨迹围栏事件 API（Geofence Trajectory Events）

基于 **FastAPI + SQLite + Shapely** 的地理围栏事件服务：

- 创建**带孔多边形**围栏（GeoJSON Polygon，支持 holes），每围栏可配置判定参数
- 批量提交轨迹点（设备 ID + 时间戳 + 经纬度），**乱序点按设备时间自动重排**
- 边界抖动去抖：可配置的**进入/离开缓冲距离**（迟滞带）+ **停留时长**（dwell）
- 产出 `enter` / `leave` / `stay` 事件；**重复提交轨迹点不会重复产出事件**（幂等）
- **围栏容量核算**：按生效时刻维护正整数容量上限规则，查询任意 `[start, end)` 窗口的占用分段与超限明细
- **门线定向穿越核算**：围栏内保存简单 GeoJSON LineString 门线，严格横穿才产出 `crossing`，方向按门线坐标顺序判定、时刻按交点比例插值
- 按时间范围/设备/围栏/类型查询事件，查询设备当前状态

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

- 服务端口：**8000**（http://localhost:8000）
- 交互式 API 文档：http://localhost:8000/docs
- SQLite 数据持久化在名为 `geofence-data` 的卷中

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）

## 判定逻辑说明

1. **投影**：以围栏质心为原点做等距圆柱投影，把经纬度转为米制坐标后用 Shapely 计算点到多边形的距离 `d`（点在多边形内 `d=0`；**孔洞内 `d>0`，天然视为外部**）。
2. **缓冲迟滞**：
   - `d <= enter_buffer_m` → 判定为内部（可略提前触发进入）
   - `d > exit_buffer_m` → 判定为外部
   - 两者之间为迟滞灰区 → **维持上一状态**，抑制边界来回抖动
3. **停留去抖（dwell）**：状态迁移条件首次满足时开始计时，持续满足 `enter_dwell_s` / `exit_dwell_s` 才确认迁移并产出事件；期间条件被打破则作废。事件时间取**条件首次满足的轨迹时刻**。
4. **stay**：进入围栏后连续停留达到 `stay_min_s` 产出一次 `stay`（每次访问最多一次）。
5. **乱序与幂等**：轨迹点按 `(device_id, ts)` 唯一约束去重；每次摄入后按设备全量轨迹（时间升序）重算事件，因此乱序到达与按序到达结果一致，重复提交结果不变。

## 示例请求

### 1. 创建带孔围栏

```bash
curl -X POST http://localhost:8000/fences \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "园区A（中央湖泊为孔洞）",
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]],
        [[116.308, 39.908], [116.312, 39.908], [116.312, 39.912], [116.308, 39.912], [116.308, 39.908]]
      ]
    },
    "config": {
      "enter_buffer_m": 0,
      "exit_buffer_m": 50,
      "enter_dwell_s": 10,
      "exit_dwell_s": 10,
      "stay_min_s": 600
    }
  }'
```

返回 `201` 及围栏对象（含 `id`，下文假设为 `1`）。

### 2. 提交轨迹点（乱序、含重复）

```bash
curl -X POST http://localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.900, "lon": 116.300},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:10Z", "lat": 39.905, "lon": 116.302},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305}
    ]
  }'
```

返回：`{"accepted": 3, "duplicates": 1, "devices_updated": 1}`

### 3. 按时间范围查询事件

```bash
curl 'http://localhost:8000/events?device_id=dev-001&start=2026-09-22T00:00:00Z&end=2026-09-23T00:00:00Z'
# 可选过滤：fence_id=1&type=enter&limit=100&offset=0
```

### 4. 查询设备当前状态

```bash
curl http://localhost:8000/devices/dev-001/status
```

返回示例：

```json
{
  "device_id": "dev-001",
  "fences": [
    {"fence_id": 1, "fence_name": "园区A（中央湖泊为孔洞）", "state": "inside", "since": "2026-09-22T08:00:10Z"}
  ]
}
```

### 5. 新增容量规则

```bash
curl -X POST http://localhost:8000/fences/1/capacity-rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_from": "2026-09-22T08:00:00Z", "max_occupancy": 2}'
```

- `max_occupancy` 必须是**正整数**（否则 `422`）
- 同一围栏同一 `effective_from` 重复提交返回 `409`
- 某时刻生效的上限 = 生效时刻 ≤ 该时刻的最近一条规则；此前无规则则该时段不限容（`limit` 为 `null`）
- 管理接口：`GET /fences/1/capacity-rules`（按生效时刻升序）、`DELETE /fences/1/capacity-rules/{rule_id}`

### 6. 查询占用分段（容量核算）

```bash
curl 'http://localhost:8000/fences/1/occupancy?start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回示例：

```json
{
  "fence_id": 1,
  "start": "2026-09-22T08:00:00Z",
  "end": "2026-09-22T09:00:00Z",
  "segments": [
    {"start": "2026-09-22T08:00:00Z", "end": "2026-09-22T08:10:00Z", "count": 1, "limit": 2, "exceeded": false, "excess": null, "devices": null},
    {"start": "2026-09-22T08:10:00Z", "end": "2026-09-22T08:20:00Z", "count": 3, "limit": 2, "exceeded": true, "excess": 1, "devices": ["dev-001", "dev-002", "dev-003"]},
    {"start": "2026-09-22T08:20:00Z", "end": "2026-09-22T09:00:00Z", "count": 2, "limit": 2, "exceeded": false, "excess": null, "devices": null}
  ]
}
```

**语义说明**：

1. **在场区间还原**：由该围栏的 `enter`/`leave` 事件重放出每台设备的半开区间 `[enter, leave)`；查询开始前已进入、查询结束仍未离开的区间被正确截断到 `[start, end)`。
2. **原子边界**：同一时刻的设备进/出与容量规则切换合并为一个边界，分段在该时刻一次性切换，不产生中间态或零长分段。
3. **恒定状态分段**：分段内在场设备集合与生效上限均不变；相邻同状态分段已合并，返回尽可能长的分段。
4. **超限明细**：`exceeded=true` 的分段给出 `limit`（上限）、`count`（实际人数）、`excess`（超额数）与按 ID 排序的 `devices`；其余分段 `excess`/`devices` 为 `null`。
5. **重算一致**：占用在查询时由事件现算；补录乱序轨迹点触发事件重算后，再次查询即反映最新结果，不残留旧的占用或超限片段。

## 门线定向穿越核算

### 1. 新增门线

```bash
curl -X POST http://localhost:8000/fences/1/gates \
  -H 'Content-Type: application/json' \
  -d '{
    "geometry": {
      "type": "LineString",
      "coordinates": [[116.310, 39.902], [116.310, 39.918]]
    },
    "forward_name": "东向西进入",
    "reverse_name": "西向东离开",
    "min_displacement_m": 5
  }'
```

- `geometry`：**简单 GeoJSON LineString**（至少 2 点、不闭合、不自交）。整条线必须位于围栏**有效区域**内——不得越出外环，也不得进入或穿过任何孔洞（贴外环允许），否则 `422`。
- `forward_name` / `reverse_name`：正/反两个方向的名称；`min_displacement_m`：产生穿越所需的最小轨迹段位移（米，默认 0）。
- 围栏不存在返回 `404`；新建门线会立即对全部已有设备轨迹重算穿越。
- 管理接口：`GET /fences/1/gates`（按 ID 升序）、`DELETE /fences/1/gates/{gate_id}`（同时删除其穿越记录）。

### 2. 穿越判定规则

相邻轨迹点（按设备时间重排后）组成的线段与门线做几何判定，**只有“单点严格横穿、两端分居两侧”才产生一条 crossing**：

| 情形 | 是否产出 crossing |
| --- | --- |
| 横穿门线内部，交点在轨迹段与门线两侧 | ✅ 产出，时刻按交点在轨迹段上的比例插值 |
| 沿门线行走 / 与门线共线重叠 | ❌ 忽略 |
| 仅接触门线的首/尾端点 | ❌ 忽略 |
| 门线折点处两侧邻点同侧（擦碰） | ❌ 忽略；分居两侧才按折点横穿计 |
| 单个轨迹段横穿同一门线两次以上（如跨过 Z 形门线） | ❌ 整段忽略 |
| 轨迹段投影长度 < `min_displacement_m` | ❌ 位移不足，忽略 |

- **方向**：按门线坐标顺序判定。沿线坐标顺序行进时左侧为正侧：轨迹**从右侧穿到左侧**为 `positive`（取 `forward_name`），**从左侧穿到右侧**为 `negative`（取 `reverse_name`）。
- **时刻**：交点位于轨迹段 `p0→p1` 的参数 `t∈(0,1)`，穿越时刻 = `t0 + t·(t1 - t0)`。
- 同一轨迹段命中多条门线时，结果按 **(插值时刻, 门线 ID)** 排序。
- 与围栏事件一致：轨迹点按 `(device_id, ts)` 去重，每次摄入按设备全量重算，**补录乱序点后穿越结果自动重算，重复提交不产生重复 crossing**。

### 3. 查询穿越记录

```bash
curl 'http://localhost:8000/crossings?device_id=dev-001&gate_id=1&direction=positive&start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回示例（时间窗口为半开 `[start, end)`；`direction_name` 取自门线当前配置的方向名称）：

```json
[
  {
    "id": 12,
    "device_id": "dev-001",
    "fence_id": 1,
    "gate_id": 1,
    "direction": "positive",
    "direction_name": "东向西进入",
    "ts": "2026-09-22T08:12:34.500000Z"
  }
]
```

可选过滤参数：`device_id`、`fence_id`、`gate_id`、`direction`（`positive`/`negative`）、`start`、`end`（半开）、`limit`、`offset`；默认按 `(ts, gate_id)` 升序。`start >= end` 返回 `422`。

### 4. 边界示例（坐标为 `[lon, lat]`）

以南北走向门线 `[[0.005,0.002],[0.005,0.008]]`（沿 lon=0.005 自南向北；右=东、左=西）为例：

- 轨迹 `(lat 0.005, lon 0.008)@t0 → (lat 0.005, lon 0.002)@t0+10s`：东→西横穿，`positive`，交点在中点，时刻 `t0+5s`。
- 轨迹终点恰好落在门线端点 `(0.005, 0.002)`：仅接触端点，**无** crossing。
- 轨迹点 `(lat 0.003→0.007, lon 恒为 0.005)`：与门线共线（沿线），**无** crossing。
- 轨迹段两端都在门线东侧（如 lon 恒为 0.006）：同侧，**无** crossing。
- 单段 `(lat 0.002, lon 0.005) → (lat 0.008, lon 0.005)` 穿过 Z 形折线门线多个子段：交点不止一个，**整段忽略**。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/fences` | 创建围栏（GeoJSON Polygon，支持孔洞，附判定配置） |
| GET | `/fences` / `/fences/{id}` | 围栏列表 / 详情 |
| DELETE | `/fences/{id}` | 删除围栏及其事件、状态、容量规则 |
| POST | `/points` | 批量提交轨迹点（乱序重排、重复幂等） |
| GET | `/events` | 事件查询（device_id / fence_id / type / start / end / limit / offset） |
| GET | `/devices/{device_id}/status` | 设备当前相对各围栏的状态 |
| POST | `/fences/{id}/capacity-rules` | 新增容量规则（生效时刻 + 正整数上限，同时刻唯一） |
| GET | `/fences/{id}/capacity-rules` | 容量规则列表（按生效时刻升序） |
| DELETE | `/fences/{id}/capacity-rules/{rule_id}` | 删除容量规则 |
| GET | `/fences/{id}/occupancy` | 查询 `[start, end)` 占用分段与超限明细（必填 start/end） |
| POST | `/fences/{id}/gates` | 新增门线（简单 LineString，须完全在围栏有效区域内 + 正反方向名 + 最小位移） |
| GET | `/fences/{id}/gates` | 门线列表（按 ID 升序） |
| DELETE | `/fences/{id}/gates/{gate_id}` | 删除门线及其穿越记录 |
| GET | `/crossings` | 门线穿越查询（device_id / fence_id / gate_id / direction / `[start,end)` / limit / offset） |
| GET | `/health` | 健康检查 |

## 围栏配置参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enter_buffer_m` | 0 | 进入缓冲距离（米），距多边形 ≤ 该值即视为内部 |
| `exit_buffer_m` | 0 | 离开缓冲距离（米），距多边形 > 该值才视为外部；须 ≥ `enter_buffer_m` |
| `enter_dwell_s` | 0 | 进入确认所需持续时长（秒） |
| `exit_dwell_s` | 0 | 离开确认所需持续时长（秒） |
| `stay_min_s` | 300 | 产生 `stay` 事件的最小连续停留时长（秒） |

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

pytest                      # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地启动
```

数据库位置由环境变量 `DATABASE_URL` 控制，默认 `sqlite:///./geofence.db`。

## 项目结构

```
app/
  main.py      # FastAPI 路由与摄入/重算流程
  engine.py    # 投影、缓冲迟滞 + dwell 状态机、容量占用分段、门线严格横穿判定
  models.py    # SQLAlchemy 模型（fences / points / events / device_state / capacity_rules / gates / crossings）
  schemas.py   # Pydantic 请求/响应模型
  database.py  # SQLite 连接与会话
tests/         # pytest 端到端测试（孔洞、乱序、幂等、抖动、停留、查询、容量核算、门线穿越）
Dockerfile / docker-compose.yml
```

## 说明与取舍

- 事件与门线穿越均按设备全量轨迹重算并整体替换，保证乱序/重复提交下结果一致；记录 `id` 可能随重算变化，请以业务字段组合识别。
- 同一设备同一时间戳的轨迹点视为重复（保留首条）。
- 距离计算采用以围栏质心为原点的局部等距圆柱投影近似，围栏尺度（公里级）下误差可忽略。
