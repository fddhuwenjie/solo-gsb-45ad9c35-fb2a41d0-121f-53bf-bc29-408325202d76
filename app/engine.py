"""围栏事件判定引擎。

将经纬度投影到以围栏质心为原点的局部平面坐标系（等距圆柱近似），
在米制坐标下用 Shapely 做距离计算，配合进入/离开缓冲距离形成迟滞区间，
再叠加停留时长（dwell）做抖动去抖，产出 enter / leave / stay 事件。

另含容量核算：由 enter/leave 事件还原设备半开在场区间，结合容量规则
把查询窗口切分为状态恒定的占用分段并标注超限明细。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6_371_000.0

EVENT_ENTER = "enter"
EVENT_LEAVE = "leave"
EVENT_STAY = "stay"


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """经纬度 -> 以 (lat0, lon0) 为原点的局部米制坐标（等距圆柱近似）。"""
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return (x, y)


@dataclass
class FenceConfig:
    enter_buffer_m: float = 0.0   # 距多边形 <= 该距离即视为“进入”（提前触发）
    exit_buffer_m: float = 0.0    # 距多边形 > 该距离才视为“离开”（迟滞带）
    enter_dwell_s: float = 0.0    # 持续在内部这么久才确认 enter
    exit_dwell_s: float = 0.0     # 持续在外部这么久才确认 leave
    stay_min_s: float = 300.0     # 单次访问内连续停留达到该时长产生一次 stay


@dataclass
class TrackPoint:
    ts: float   # epoch 秒
    lat: float
    lon: float


@dataclass
class FenceEvent:
    fence_id: int
    type: str   # enter | leave | stay
    ts: float   # 事件发生的轨迹时间（非确认时间）


class ProjectedFence:
    """投影到局部米制坐标系的围栏多边形，支持带孔多边形。"""

    def __init__(self, fence_id: int, polygon: Polygon, config: FenceConfig):
        self.fence_id = fence_id
        self.config = config
        centroid = polygon.centroid
        self._lat0, self._lon0 = centroid.y, centroid.x
        shell = [_project(lat, lon, self._lat0, self._lon0)
                 for lon, lat in polygon.exterior.coords]
        holes = [
            [_project(lat, lon, self._lat0, self._lon0) for lon, lat in ring.coords]
            for ring in polygon.interiors
        ]
        self._poly = Polygon(shell, holes)

    def classify(self, lat: float, lon: float, prev_inside: bool) -> bool:
        """判定点是否在围栏内。

        点到多边形的距离 d（内部为 0，孔洞内 > 0，因此孔洞天然视为外部）：
        - d <= enter_buffer_m  -> 内部
        - d >  exit_buffer_m   -> 外部
        - 两者之间为迟滞灰区   -> 维持上一状态，抑制边界抖动
        """
        x, y = _project(lat, lon, self._lat0, self._lon0)
        d = self._poly.distance(Point(x, y))
        if d <= self.config.enter_buffer_m:
            return True
        if d > self.config.exit_buffer_m:
            return False
        return prev_inside


def compute_events(
    fence: ProjectedFence, points: list[TrackPoint]
) -> tuple[list[FenceEvent], bool, float | None]:
    """对单个 (设备, 围栏) 按时间有序轨迹计算事件。

    points 必须已按 ts 升序排列。返回 (事件列表, 最终是否在围栏内, 当前状态起始时间)。
    状态机：
    - 分类结果与当前状态一致 -> 取消未确认的待定迁移；
    - 分类结果不同 -> 记录待定迁移起点，持续满足 dwell 时长后才确认迁移并产出事件，
      事件时间取条件首次满足的时刻；期间条件被打破则待定迁移作废（去抖）。
    - 进入围栏后连续停留达到 stay_min_s，产出一次 stay（每次访问最多一次）。
    """
    cfg = fence.config
    events: list[FenceEvent] = []
    inside = False
    state_since: float | None = None
    pending_since: float | None = None
    stay_emitted = False

    for pt in points:
        now_inside = fence.classify(pt.lat, pt.lon, inside)
        if now_inside == inside:
            pending_since = None
        else:
            if pending_since is None:
                pending_since = pt.ts
            dwell = cfg.enter_dwell_s if now_inside else cfg.exit_dwell_s
            if pt.ts - pending_since >= dwell:
                inside = now_inside
                state_since = pending_since
                events.append(FenceEvent(
                    fence.fence_id,
                    EVENT_ENTER if inside else EVENT_LEAVE,
                    pending_since,
                ))
                pending_since = None
                stay_emitted = False
        if inside and not stay_emitted and pt.ts - state_since >= cfg.stay_min_s:
            events.append(FenceEvent(fence.fence_id, EVENT_STAY, state_since + cfg.stay_min_s))
            stay_emitted = True

    return events, inside, state_since


# ---------- 容量核算 ----------


@dataclass
class OccupancySegment:
    """[start, end) 内状态恒定（在场设备集合与生效上限均不变）的占用分段。"""

    start: float
    end: float
    devices: tuple[str, ...]   # 在场设备 ID，已排序
    limit: int | None          # 该分段生效的容量上限；无规则时为 None

    @property
    def count(self) -> int:
        return len(self.devices)

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.count > self.limit

    @property
    def excess(self) -> int | None:
        return self.count - self.limit if self.exceeded else None


def compute_occupancy_segments(
    events: list[tuple[str, str, float]],
    rules: list[tuple[float, int]],
    start: float,
    end: float,
) -> list[OccupancySegment]:
    """由 enter/leave 事件与容量规则还原 [start, end) 内的占用分段。

    - 重放事件流，把每台设备的在场时间还原为半开区间 [enter, leave)；
      查询前已进入、查询结束仍未离开的区间被截断到查询窗口。
    - 边界取窗口端点、区间端点与窗口内的规则生效时刻；同一时刻的
      设备进出与规则切换在边界集合中天然合并，按一个原子边界处理。
    - 相邻状态（在场设备集合 + 生效上限）相同的分段被合并，保证分段尽可能长。
    """
    # 1) 还原每台设备的半开在场区间 [enter, leave)
    by_device: dict[str, list[tuple[float, str]]] = {}
    for device_id, etype, ts in events:
        if etype in (EVENT_ENTER, EVENT_LEAVE):
            by_device.setdefault(device_id, []).append((ts, etype))

    intervals: list[tuple[float, float, str]] = []  # (起, 止, 设备)
    for device_id, evs in by_device.items():
        evs.sort()
        open_since: float | None = None
        for ts, etype in evs:
            if etype == EVENT_ENTER:
                if open_since is None:
                    open_since = ts
            elif open_since is not None:
                intervals.append((open_since, ts, device_id))
                open_since = None
        if open_since is not None:  # 仍未离开：延伸到 +inf，交由查询窗口截断
            intervals.append((open_since, math.inf, device_id))

    # 2) 截断到查询窗口 [start, end)，丢弃窗口外的零长区间
    clipped = [
        (max(s, start), min(e, end), dev)
        for s, e, dev in intervals
        if max(s, start) < min(e, end)
    ]

    # 3) 边界集合：同一时刻的进出与规则切换合并为一个原子边界
    boundaries = {start, end}
    for cs, ce, _ in clipped:
        boundaries.add(cs)
        boundaries.add(ce)
    ordered_rules = sorted(rules)
    for effective_from, _ in ordered_rules:
        if start < effective_from < end:
            boundaries.add(effective_from)
    ordered = sorted(boundaries)

    # 4) 逐段求恒定状态，相邻同状态分段合并
    segments: list[OccupancySegment] = []
    for a, b in zip(ordered, ordered[1:]):
        devices = tuple(sorted(dev for cs, ce, dev in clipped if cs <= a < ce))
        limit: int | None = None
        for effective_from, cap in ordered_rules:
            if effective_from > a:
                break
            limit = cap
        if segments and segments[-1].devices == devices and segments[-1].limit == limit:
            segments[-1].end = b
        else:
            segments.append(OccupancySegment(a, b, devices, limit))
    return segments
