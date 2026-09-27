import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from shapely.geometry import shape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import engine, get_db
from .engine import (
    EVENT_ENTER,
    EVENT_LEAVE,
    FenceConfig,
    ProjectedFence,
    TrackPoint,
    compute_events,
    compute_occupancy_segments,
)
from .models import Base, CapacityRule, DeviceState, Event, Fence, TrackPointRow
from .schemas import (
    CapacityRuleCreate,
    CapacityRuleOut,
    DeviceStatusOut,
    EventOut,
    FenceConfigSchema,
    FenceCreate,
    FenceOut,
    FenceStateOut,
    IngestResult,
    OccupancyOut,
    OccupancySegmentOut,
    PointBatch,
)

# 摄入与重算在进程内串行化，保证同一设备的重算基于一致快照
INGEST_LOCK = threading.Lock()


def to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_iso(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="轨迹围栏事件 API",
    description="带孔多边形围栏 + 乱序轨迹重排 + 缓冲/停留去抖的 enter/leave/stay 事件服务，支持围栏容量核算",
    version="1.1.0",
    lifespan=lifespan,
)


# ---------- 内部逻辑 ----------

def recompute_device(db: Session, device_id: str) -> None:
    """按设备全量轨迹（按时间升序）重算该设备在所有围栏下的事件与当前状态。

    乱序点到达后重算可得到与按序到达一致的结果；重复提交因去重不产生新点，
    重算结果不变，因此不会重复产出事件。
    """
    rows = (
        db.query(TrackPointRow)
        .filter_by(device_id=device_id)
        .order_by(TrackPointRow.ts)
        .all()
    )
    points = [TrackPoint(ts=r.ts, lat=r.lat, lon=r.lon) for r in rows]
    fences = db.query(Fence).all()

    db.query(Event).filter_by(device_id=device_id).delete()
    db.query(DeviceState).filter_by(device_id=device_id).delete()

    for f in fences:
        polygon = shape(json.loads(f.geometry))
        cfg = FenceConfig(
            enter_buffer_m=f.enter_buffer_m,
            exit_buffer_m=f.exit_buffer_m,
            enter_dwell_s=f.enter_dwell_s,
            exit_dwell_s=f.exit_dwell_s,
            stay_min_s=f.stay_min_s,
        )
        pf = ProjectedFence(f.id, polygon, cfg)
        events, inside, since = compute_events(pf, points)
        for e in events:
            db.add(Event(device_id=device_id, fence_id=e.fence_id, type=e.type, ts=e.ts))
        db.add(DeviceState(
            device_id=device_id,
            fence_id=f.id,
            state="inside" if inside else "outside",
            since=since,
        ))
    db.commit()


def fence_out(f: Fence) -> FenceOut:
    return FenceOut(
        id=f.id,
        name=f.name,
        geometry=json.loads(f.geometry),
        config=FenceConfigSchema(
            enter_buffer_m=f.enter_buffer_m,
            exit_buffer_m=f.exit_buffer_m,
            enter_dwell_s=f.enter_dwell_s,
            exit_dwell_s=f.exit_dwell_s,
            stay_min_s=f.stay_min_s,
        ),
        created_at=to_iso(f.created_at),
    )


# ---------- 围栏管理 ----------

@app.post("/fences", response_model=FenceOut, status_code=201)
def create_fence(body: FenceCreate, db: Session = Depends(get_db)):
    """创建带孔多边形围栏。geometry 为 GeoJSON Polygon，coordinates[0] 为外环，后续为孔洞。"""
    try:
        geom = shape(body.geometry)
    except Exception:
        raise HTTPException(status_code=422, detail="geometry 不是合法的 GeoJSON")
    if geom.geom_type != "Polygon" or geom.is_empty:
        raise HTTPException(status_code=422, detail="geometry 必须是 Polygon 类型")
    if not geom.is_valid:
        raise HTTPException(status_code=422, detail="多边形无效（自相交或孔洞越出外环）")

    fence = Fence(
        name=body.name,
        geometry=json.dumps(body.geometry),
        enter_buffer_m=body.config.enter_buffer_m,
        exit_buffer_m=body.config.exit_buffer_m,
        enter_dwell_s=body.config.enter_dwell_s,
        exit_dwell_s=body.config.exit_dwell_s,
        stay_min_s=body.config.stay_min_s,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(fence)
    db.commit()
    db.refresh(fence)

    # 新围栏对存量设备轨迹生效
    with INGEST_LOCK:
        device_ids = [r[0] for r in db.query(TrackPointRow.device_id).distinct()]
        for device_id in device_ids:
            recompute_device(db, device_id)

    return fence_out(fence)


@app.get("/fences", response_model=list[FenceOut])
def list_fences(db: Session = Depends(get_db)):
    return [fence_out(f) for f in db.query(Fence).order_by(Fence.id).all()]


@app.get("/fences/{fence_id}", response_model=FenceOut)
def get_fence(fence_id: int, db: Session = Depends(get_db)):
    fence = db.get(Fence, fence_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    return fence_out(fence)


@app.delete("/fences/{fence_id}", status_code=204)
def delete_fence(fence_id: int, db: Session = Depends(get_db)):
    fence = db.get(Fence, fence_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    db.query(Event).filter_by(fence_id=fence_id).delete()
    db.query(DeviceState).filter_by(fence_id=fence_id).delete()
    db.query(CapacityRule).filter_by(fence_id=fence_id).delete()
    db.delete(fence)
    db.commit()


# ---------- 轨迹点摄入 ----------

@app.post("/points", response_model=IngestResult)
def submit_points(batch: PointBatch, db: Session = Depends(get_db)):
    """批量提交轨迹点。乱序点按设备时间重排；(device_id, ts) 重复的点被幂等忽略。"""
    accepted = 0
    duplicates = 0
    devices: set[str] = set()
    seen: set[tuple[str, float]] = set()  # 本批次内已接收的 (device_id, ts)

    with INGEST_LOCK:
        for p in batch.points:
            ts = to_epoch(p.ts)
            key = (p.device_id, ts)
            if key in seen:
                duplicates += 1
                continue
            exists = (
                db.query(TrackPointRow.id)
                .filter_by(device_id=p.device_id, ts=ts)
                .first()
            )
            if exists:
                duplicates += 1
                continue
            seen.add(key)
            db.add(TrackPointRow(device_id=p.device_id, ts=ts, lat=p.lat, lon=p.lon))
            accepted += 1
            devices.add(p.device_id)
        db.commit()

        for device_id in devices:
            recompute_device(db, device_id)

    return IngestResult(accepted=accepted, duplicates=duplicates, devices_updated=len(devices))


# ---------- 事件与状态查询 ----------

@app.get("/events", response_model=list[EventOut])
def list_events(
    device_id: str | None = None,
    fence_id: int | None = None,
    type: Literal["enter", "leave", "stay"] | None = None,
    start: datetime | None = Query(None, description="起始时间（ISO 8601，含）"),
    end: datetime | None = Query(None, description="结束时间（ISO 8601，含）"),
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """按设备/围栏/事件类型/时间范围查询事件，按事件时间升序返回。"""
    q = db.query(Event)
    if device_id is not None:
        q = q.filter(Event.device_id == device_id)
    if fence_id is not None:
        q = q.filter(Event.fence_id == fence_id)
    if type is not None:
        q = q.filter(Event.type == type)
    if start is not None:
        q = q.filter(Event.ts >= to_epoch(start))
    if end is not None:
        q = q.filter(Event.ts <= to_epoch(end))
    rows = q.order_by(Event.ts, Event.id).limit(limit).offset(offset).all()
    return [
        EventOut(id=e.id, device_id=e.device_id, fence_id=e.fence_id, type=e.type, ts=to_iso(e.ts))
        for e in rows
    ]


@app.get("/devices/{device_id}/status", response_model=DeviceStatusOut)
def device_status(device_id: str, db: Session = Depends(get_db)):
    """查询设备当前相对各围栏的状态（inside/outside 及进入该状态的时间）。"""
    states = db.query(DeviceState).filter_by(device_id=device_id).all()
    fence_names = {f.id: f.name for f in db.query(Fence).all()}
    return DeviceStatusOut(
        device_id=device_id,
        fences=[
            FenceStateOut(
                fence_id=s.fence_id,
                fence_name=fence_names.get(s.fence_id, ""),
                state=s.state,
                since=to_iso(s.since),
            )
            for s in sorted(states, key=lambda s: s.fence_id)
        ],
    )


# ---------- 容量规则与占用核算 ----------

def capacity_rule_out(r: CapacityRule) -> CapacityRuleOut:
    return CapacityRuleOut(
        id=r.id,
        fence_id=r.fence_id,
        effective_from=to_iso(r.effective_from),
        max_occupancy=r.max_occupancy,
        created_at=to_iso(r.created_at),
    )


@app.post("/fences/{fence_id}/capacity-rules", response_model=CapacityRuleOut, status_code=201)
def create_capacity_rule(fence_id: int, body: CapacityRuleCreate, db: Session = Depends(get_db)):
    """新增容量规则：自 effective_from（含）起生效的正整数上限。

    同一围栏同一生效时刻只允许一条规则，重复提交返回 409。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rule = CapacityRule(
        fence_id=fence_id,
        effective_from=to_epoch(body.effective_from),
        max_occupancy=body.max_occupancy,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同一围栏同一生效时刻的容量规则已存在")
    db.refresh(rule)
    return capacity_rule_out(rule)


@app.get("/fences/{fence_id}/capacity-rules", response_model=list[CapacityRuleOut])
def list_capacity_rules(fence_id: int, db: Session = Depends(get_db)):
    """按生效时刻升序列出围栏的容量规则。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rules = (
        db.query(CapacityRule)
        .filter_by(fence_id=fence_id)
        .order_by(CapacityRule.effective_from)
        .all()
    )
    return [capacity_rule_out(r) for r in rules]


@app.delete("/fences/{fence_id}/capacity-rules/{rule_id}", status_code=204)
def delete_capacity_rule(fence_id: int, rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(CapacityRule, rule_id)
    if rule is None or rule.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="容量规则不存在")
    db.delete(rule)
    db.commit()


@app.get("/fences/{fence_id}/occupancy", response_model=OccupancyOut)
def fence_occupancy(
    fence_id: int,
    start: datetime = Query(description="起始时间（ISO 8601，含）"),
    end: datetime = Query(description="结束时间（ISO 8601，不含）"),
    db: Session = Depends(get_db),
):
    """查询 [start, end) 内的占用核算：状态恒定的分段序列 + 超限明细。

    由 enter/leave 事件还原各设备的半开在场区间（越界部分截断到查询窗口）；
    同一时刻的设备进出与容量规则切换按一个原子边界处理；超限分段给出上限、
    实际人数、超额数与按 ID 排序的在场设备。占用在查询时由事件现算，
    补录乱序轨迹点触发事件重算后，再次查询即得到最新结果。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    start_ts, end_ts = to_epoch(start), to_epoch(end)
    if not start_ts < end_ts:
        raise HTTPException(status_code=422, detail="start 必须早于 end")

    events = (
        db.query(Event)
        .filter(Event.fence_id == fence_id, Event.type.in_([EVENT_ENTER, EVENT_LEAVE]))
        .all()
    )
    rules = db.query(CapacityRule).filter_by(fence_id=fence_id).all()
    segments = compute_occupancy_segments(
        [(e.device_id, e.type, e.ts) for e in events],
        [(r.effective_from, r.max_occupancy) for r in rules],
        start_ts,
        end_ts,
    )
    return OccupancyOut(
        fence_id=fence_id,
        start=to_iso(start_ts),
        end=to_iso(end_ts),
        segments=[
            OccupancySegmentOut(
                start=to_iso(s.start),
                end=to_iso(s.end),
                count=s.count,
                limit=s.limit,
                exceeded=s.exceeded,
                excess=s.excess,
                devices=list(s.devices) if s.exceeded else None,
            )
            for s in segments
        ],
    )


@app.get("/health")
def health():
    return {"status": "ok"}
