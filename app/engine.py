"""围栏事件判定引擎。

将经纬度投影到以围栏质心为原点的局部平面坐标系（等距圆柱近似），
在米制坐标下用 Shapely 做距离计算，配合进入/离开缓冲距离形成迟滞区间，
再叠加停留时长（dwell）做抖动去抖，产出 enter / leave / stay 事件。

另含容量核算：由 enter/leave 事件还原设备半开在场区间，结合容量规则
把查询窗口切分为状态恒定的占用分段并标注超限明细。

门线核算：门线为完全落在围栏有效区域内（不越出外环、不穿孔洞）的简单
LineString。相邻轨迹点组成的线段与门线严格横穿（交点同时位于两条线段
内部、两端点分居门线两侧）才产生 crossing；沿门线、仅接触端点、同侧
擦碰、位移不足均忽略。方向按门线坐标顺序判定：右->左为正向，左->右为
反向，穿越时刻按交点在轨迹段上的比例插值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

EARTH_RADIUS_M = 6_371_000.0

EVENT_ENTER = "enter"
EVENT_LEAVE = "leave"
EVENT_STAY = "stay"

DIRECTION_POSITIVE = "positive"  # 按门线坐标顺序从右侧穿到左侧（右 -> 左）
DIRECTION_NEGATIVE = "negative"  # 左 -> 右


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

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """经纬度 -> 该围栏局部米制坐标。"""
        return _project(lat, lon, self._lat0, self._lon0)

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


# ---------- 门线穿越核算 ----------


@dataclass
class GateSpec:
    """门线定义。line 为 lon/lat 坐标的 LineString，方向名按门线坐标顺序配置。"""

    gate_id: int
    line: LineString
    forward_name: str    # 右 -> 左 方向名称
    reverse_name: str    # 左 -> 右 方向名称
    min_displacement_m: float


@dataclass
class GateCrossing:
    gate_id: int
    direction: str   # DIRECTION_POSITIVE | DIRECTION_NEGATIVE
    ts: float        # 交点在轨迹段上的插值时刻


def validate_gate_line(polygon: Polygon, geom: BaseGeometry) -> str | None:
    """校验门线几何：必须是非空简单 LineString（>=2 点、非闭合、非自交），
    且整条线被围栏有效区域覆盖（不得越出外环，也不得进入任何孔洞）。

    通过返回 None，否则返回错误信息。
    """
    if geom.geom_type != "LineString" or geom.is_empty:
        return "geometry 必须是 GeoJSON LineString"
    if len(geom.coords) < 2:
        return "LineString 至少需要 2 个坐标点"
    if not geom.is_valid or not geom.is_simple:
        return "LineString 无效或存在自相交"
    if geom.is_closed:
        return "LineString 不得闭合（首尾点必须不同）"
    if geom.length <= 0:
        return "LineString 长度必须大于 0"
    # polygon.covers(line)：线可贴外环（有效区域边界），但不得落入孔洞内部
    if not polygon.covers(geom):
        return "整条门线必须位于围栏有效区域内（不得越出外环或穿过孔洞）"
    return None


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _strict_segment_cross(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float | None:
    """线段 pq 与 ab 的内部相交参数。

    仅当交点同时落在两条线段内部时返回交点在 pq 上的参数 t∈(0,1)；
    交于任一端点（t/u 为 0 或 1）、平行/共线均返回 None。
    由此“仅接触端点”和“沿线”都不会被记为穿越。
    """
    r = (q[0] - p[0], q[1] - p[1])
    s = (b[0] - a[0], b[1] - a[1])
    rxs = _cross(r[0], r[1], s[0], s[1])
    if rxs == 0.0:
        return None  # 平行或共线：沿线/重叠不算横穿
    ap = (a[0] - p[0], a[1] - p[1])
    t = _cross(ap[0], ap[1], s[0], s[1]) / rxs
    u = _cross(ap[0], ap[1], r[0], r[1]) / rxs
    eps = 1e-12
    if eps < t < 1.0 - eps and eps < u < 1.0 - eps:
        return t
    return None


def _direction_for_sides(side0: float, side1: float) -> str | None:
    """按门线有向子段两侧的 cross 符号判定穿越方向。

    投影坐标 x 东 y 北，沿门线坐标顺序行进时 cross(g, p-g0)>0 为左侧。
    右 -> 左（负 -> 正）取正向，左 -> 右取反向；同侧/含零返回 None。
    """
    if side0 < 0.0 and side1 > 0.0:
        return DIRECTION_POSITIVE
    if side0 > 0.0 and side1 < 0.0:
        return DIRECTION_NEGATIVE
    return None


def compute_crossings(
    fence: ProjectedFence,
    points: list[TrackPoint],
    gates: list[GateSpec],
) -> list[GateCrossing]:
    """对单个 (设备, 围栏) 计算各门线的穿越记录。

    points 必须已按 ts 升序。对每条相邻轨迹段与每扇门线：
    - 轨迹段投影长度 < 门线 min_displacement_m 视为位移不足，忽略；
    - 仅当与门线恰好发生一个横穿点（交点在轨迹段内部、门线两端分居两侧）
      时产出一条 crossing：
      * 与子段内部横截：交点严格位于子段内部；
      * 经过门线内部折点：折点两侧邻点分居轨迹段两侧才算横穿（同侧为擦碰，
        忽略），且两条邻接子段给出的方向一致，否则折点处方向存疑忽略；
      * 经过门线首尾端点：属于“仅接触端点”，一律忽略；
    - 一个轨迹段与同一门线存在两个及以上横穿点（如跨过 Z 形门线）时整段忽略；
    - 穿越时刻 = t0 + ratio * (t1 - t0)，ratio 为交点在轨迹段上的参数。
    返回结果按 (插值时刻, 门线 ID) 排序。
    """
    if not gates or len(points) < 2:
        return []

    projected_gates: list[tuple[GateSpec, list[tuple[float, float]]]] = []
    for gate in gates:
        coords = [fence.project(lat, lon) for lon, lat in gate.line.coords]
        projected_gates.append((gate, coords))

    crossings: list[GateCrossing] = []
    for p0, p1 in zip(points, points[1:]):
        a = fence.project(p0.lat, p0.lon)
        b = fence.project(p1.lat, p1.lon)
        r = (b[0] - a[0], b[1] - a[1])
        seg_len = math.hypot(r[0], r[1])

        for gate, coords in projected_gates:
            if seg_len < gate.min_displacement_m:
                continue  # 位移不足

            # 候选横穿点：key=(量化交点)，value=(轨迹段参数 t, 方向)；
            # 同一交点被重复报告且方向矛盾时剔除该点
            hits: dict[tuple[float, float], tuple[float, str]] = {}

            def add_hit(t: float, direction: str | None) -> None:
                if direction is None:
                    return
                x = a[0] + t * r[0]
                y = a[1] + t * r[1]
                key = (round(x, 6), round(y, 6))
                if key in hits:
                    if hits[key][1] != direction:
                        hits.pop(key)  # 折点处方向判定不一致 -> 丢弃
                    return
                hits[key] = (t, direction)

            # 1) 与各子段的严格内部横穿
            for gs0, gs1 in zip(coords, coords[1:]):
                t = _strict_segment_cross(a, b, gs0, gs1)
                if t is None:
                    continue
                g = (gs1[0] - gs0[0], gs1[1] - gs0[1])
                side0 = _cross(g[0], g[1], a[0] - gs0[0], a[1] - gs0[1])
                side1 = _cross(g[0], g[1], b[0] - gs0[0], b[1] - gs0[1])
                add_hit(t, _direction_for_sides(side0, side1))

            # 2) 轨迹段穿过门线内部折点（不含首尾端点）
            if seg_len > 0:
                r_len2 = r[0] ** 2 + r[1] ** 2
                for i in range(1, len(coords) - 1):
                    v = coords[i]
                    vp = (v[0] - a[0], v[1] - a[1])
                    t = (vp[0] * r[0] + vp[1] * r[1]) / r_len2
                    if not (1e-12 < t < 1.0 - 1e-12):
                        continue
                    # 折点到轨迹段的垂直距离须近似为 0（约 1e-6 m）
                    if abs(_cross(r[0], r[1], vp[0], vp[1])) / seg_len > 1e-6:
                        continue
                    prev_v, next_v = coords[i - 1], coords[i + 1]
                    s_prev = _cross(r[0], r[1], prev_v[0] - a[0], prev_v[1] - a[1])
                    s_next = _cross(r[0], r[1], next_v[0] - a[0], next_v[1] - a[1])
                    if s_prev == 0.0 or s_next == 0.0 or s_prev * s_next > 0.0:
                        continue  # 折点两邻点同侧（或落在轨迹线上）-> 擦碰，忽略
                    # 真横穿：两条相邻子段给出的方向须一致
                    g_prev = (v[0] - prev_v[0], v[1] - prev_v[1])
                    g_next = (next_v[0] - v[0], next_v[1] - v[1])
                    d_prev = _direction_for_sides(
                        _cross(g_prev[0], g_prev[1], a[0] - prev_v[0], a[1] - prev_v[1]),
                        _cross(g_prev[0], g_prev[1], b[0] - prev_v[0], b[1] - prev_v[1]),
                    )
                    d_next = _direction_for_sides(
                        _cross(g_next[0], g_next[1], a[0] - v[0], a[1] - v[1]),
                        _cross(g_next[0], g_next[1], b[0] - v[0], b[1] - v[1]),
                    )
                    if d_prev is not None and d_prev == d_next:
                        add_hit(t, d_prev)

            if len(hits) != 1:
                continue  # 无横穿、多点横穿或方向存疑 -> 整段忽略

            (t, direction), = hits.values()
            ts = p0.ts + t * (p1.ts - p0.ts)
            crossings.append(GateCrossing(gate.gate_id, direction, ts))

    crossings.sort(key=lambda c: (c.ts, c.gate_id))
    return crossings
