from datetime import datetime, timedelta, timezone
import math

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

# 约 1.1km 见方的围栏；中央孔洞 (lon 0.004~0.006, lat 0.004~0.006)
FENCE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]],
        [[0.004, 0.004], [0.006, 0.004], [0.006, 0.006], [0.004, 0.006], [0.004, 0.004]],
    ],
}
PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

# 南北走向门线（GeoJSON [lon, lat]：沿 lon=0.005，自南 lat=0.002 到北 lat=0.008）。
# 面向坐标顺序（北）时右=东(lon 大)、左=西：东 -> 西 = 正向“进”，西 -> 东 = 反向“出”。
GATE_NS = {
    "type": "LineString",
    "coordinates": [[0.005, 0.002], [0.005, 0.008]],
}
GATE_NS_SHORT = {
    "type": "LineString",
    "coordinates": [[0.005, 0.003], [0.005, 0.007]],
}
# 折角门线：南段向北到折点 (lon=0.005,lat=0.005)，北段斜向东北
GATE_BENT = {
    "type": "LineString",
    "coordinates": [[0.005, 0.002], [0.005, 0.005], [0.006, 0.008]],
}
# V 形门线（折点朝东）：折点两邻点都在西侧，供“折点同侧擦碰”用例
GATE_V_GRAZE = {
    "type": "LineString",
    "coordinates": [[0.003, 0.003], [0.005, 0.005], [0.003, 0.007]],
}
# Z 形门线：竖直轨迹 lon=0.005 的单个轨迹段会横穿其 3 个子段
GATE_ZIG = {
    "type": "LineString",
    "coordinates": [
        [0.006, 0.002], [0.004, 0.004], [0.006, 0.006], [0.004, 0.008],
    ],
}


def pt(device, sec, lat, lon):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def ts(sec):
    return BASE + timedelta(seconds=sec)


def create_fence(client, geometry=PLAIN_FENCE):
    r = client.post("/fences", json={"name": "门线测试围栏", "geometry": geometry})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_gate(client, fence_id, geometry=GATE_NS, min_disp=0.0,
                forward="进", reverse="出"):
    r = client.post(
        f"/fences/{fence_id}/gates",
        json={
            "geometry": geometry,
            "forward_name": forward,
            "reverse_name": reverse,
            "min_displacement_m": min_disp,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def crossings(client, **params):
    r = client.get("/crossings", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def approx_lon_meters(dlon, lat=0.005):
    return math.radians(dlon) * 6_371_000.0 * math.cos(math.radians(lat))


def test_gate_crud_and_validation(client):
    fence_id = create_fence(client)

    # 围栏不存在
    r = client.post("/fences/999/gates", json={
        "geometry": GATE_NS, "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 404
    assert client.get("/fences/999/gates").status_code == 404

    gate_id = create_gate(client, fence_id, min_disp=12.5)
    gates = client.get(f"/fences/{fence_id}/gates").json()
    assert len(gates) == 1
    assert gates[0]["id"] == gate_id
    assert gates[0]["forward_name"] == "进"
    assert gates[0]["reverse_name"] == "出"
    assert gates[0]["min_displacement_m"] == 12.5
    assert gates[0]["geometry"]["coordinates"] == GATE_NS["coordinates"]

    # 不是 LineString
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "Point", "coordinates": [0.005, 0.005]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422

    # 非法 GeoJSON
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString", "coordinates": [[0.005]]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422

    # 越出围栏（南端 lat=-0.001 在外环外）
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.005, -0.001], [0.005, 0.005]]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422

    # 自相交折线（蝴蝶结）
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString", "coordinates": [
            [0.002, 0.002], [0.008, 0.008], [0.002, 0.008], [0.008, 0.002]]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422

    # 闭合线
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString", "coordinates": [
            [0.002, 0.002], [0.008, 0.002], [0.008, 0.008], [0.002, 0.002]]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422

    # 最小位移不允许负数
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": GATE_NS, "forward_name": "进", "reverse_name": "出",
        "min_displacement_m": -1,
    })
    assert r.status_code == 422

    # 删除
    assert client.delete(f"/fences/{fence_id}/gates/{gate_id}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/gates/{gate_id}").status_code == 404
    assert client.get(f"/fences/{fence_id}/gates").json() == []


def test_gate_cannot_cross_hole_or_lie_inside_it(client):
    fence_id = create_fence(client, FENCE_WITH_HOLE)
    # 南北直线贯穿中央孔洞
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": GATE_NS, "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422
    # 完全在孔洞内部
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.0045, 0.0045], [0.0055, 0.0055]]},
        "forward_name": "进", "reverse_name": "出",
    })
    assert r.status_code == 422
    # 孔洞下方有效区域内的横线 -> 通过
    create_gate(client, fence_id, {
        "type": "LineString",
        "coordinates": [[0.002, 0.002], [0.008, 0.002]],
    })
    # 沿外环边界铺设（贴边允许）-> 通过
    create_gate(client, fence_id, {
        "type": "LineString",
        "coordinates": [[0.002, 0.0], [0.008, 0.0]],
    })


def test_crossing_direction_and_interpolated_time(client):
    fence_id = create_fence(client)
    gate_id = create_gate(client, fence_id)

    # t=0 东(右) lon=0.008 -> t=10 西(左) lon=0.004：右->左正向，
    # 交点 lon=0.005 位于 (0.008->0.004) 的 3/4 处 -> t=7.5
    submit(client, [
        pt("dev1", 0, 0.005, 0.008),
        pt("dev1", 10, 0.005, 0.004),
        # t=20 西 lon=0.002 -> t=30 东 lon=0.008：左->右反向，交点中点 -> t=25
        pt("dev1", 20, 0.005, 0.002),
        pt("dev1", 30, 0.005, 0.008),
    ])

    cs = crossings(client, device_id="dev1")
    assert [(c["gate_id"], c["direction"], c["direction_name"]) for c in cs] == [
        (gate_id, "positive", "进"),
        (gate_id, "negative", "出"),
    ]
    assert datetime.fromisoformat(cs[0]["ts"]) == ts(7.5)
    assert datetime.fromisoformat(cs[1]["ts"]) == ts(25)
    assert cs[0]["fence_id"] == fence_id


def test_out_of_order_and_duplicate_points_give_same_crossings(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id)
    track = [
        pt("dev1", 0, 0.005, 0.008),    # 东
        pt("dev1", 10, 0.005, 0.002),   # 西：正向 @5
        pt("dev1", 20, 0.005, 0.008),   # 东：反向 @15
    ]
    # 乱序提交 + 批内/跨批重复
    submit(client, [track[2], track[0], track[0]])
    submit(client, [track[1], track[2], track[1]])

    cs = crossings(client, device_id="dev1")
    assert [c["direction"] for c in cs] == ["positive", "negative"]
    assert [datetime.fromisoformat(c["ts"]) for c in cs] == [ts(5), ts(15)]
    # 再整体重复提交，结果不变（不重复产出）
    submit(client, track)
    assert crossings(client, device_id="dev1") == cs


def test_endpoint_contact_along_line_and_parallel_graze_ignored(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id, GATE_NS)

    # 轨迹终点恰好落在门线南端 (lon=0.005,lat=0.002)，随后从该端点离开
    submit(client, [
        pt("dev1", 0, 0.0005, 0.008),
        pt("dev1", 10, 0.002, 0.005),   # 终点=门线南端 -> 端点接触
        pt("dev1", 20, 0.0005, 0.002),  # 自门线南端离开 -> 端点接触
    ])
    assert crossings(client, device_id="dev1") == []

    # 起点落在门线内部 (lon=0.005,lat=0.005) 向东离开：端点接触 -> 忽略
    submit(client, [
        pt("dev2", 0, 0.005, 0.005),
        pt("dev2", 10, 0.005, 0.009),
    ])
    assert crossings(client, device_id="dev2") == []

    # 与门线共线，沿门线自南向北：沿线 -> 忽略
    submit(client, [
        pt("dev3", 0, 0.003, 0.005),
        pt("dev3", 10, 0.007, 0.005),
    ])
    assert crossings(client, device_id="dev3") == []

    # 门线东侧平行北上：两端同侧 -> 忽略
    submit(client, [
        pt("dev4", 0, 0.003, 0.006),
        pt("dev4", 10, 0.007, 0.006),
    ])
    assert crossings(client, device_id="dev4") == []


def test_min_displacement_threshold(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id, min_disp=100.0)

    # 穿越但段长仅约 67m < 100m -> 位移不足，忽略
    assert approx_lon_meters(0.0006) < 100
    submit(client, [
        pt("dev1", 0, 0.005, 0.0053),
        pt("dev1", 10, 0.005, 0.0047),
    ])
    assert crossings(client, device_id="dev1") == []

    # 另一设备段长约 668m >= 100m -> 产生正向穿越（东 -> 西，中点 @25）
    assert approx_lon_meters(0.006) > 100
    submit(client, [
        pt("dev2", 20, 0.005, 0.008),
        pt("dev2", 30, 0.005, 0.002),
    ])
    cs = crossings(client, device_id="dev2")
    assert len(cs) == 1 and cs[0]["direction"] == "positive"
    assert datetime.fromisoformat(cs[0]["ts"]) == ts(25)


def test_bent_gate_vertex_crossing(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id, GATE_BENT)

    # 水平轨迹 lat=0.005 自西向东穿过折点 (lon=0.005,lat=0.005)：
    # 南段（折点以南）与北段（折点东北）分居轨迹两侧 -> 真横穿；
    # 西(左) -> 东(右) = 反向，折点在轨迹段中点 -> t=5
    submit(client, [
        pt("dev1", 0, 0.005, 0.002),
        pt("dev1", 10, 0.005, 0.008),
    ])
    cs = crossings(client, device_id="dev1")
    assert len(cs) == 1
    assert cs[0]["direction"] == "negative"
    assert datetime.fromisoformat(cs[0]["ts"]) == ts(5)


def test_bent_gate_vertex_same_side_graze_ignored(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id, GATE_V_GRAZE)

    # 竖直轨迹 lon=0.005 自南向北穿过折点，但 V 形门线两邻点都在轨迹西侧
    # （折点两侧邻点同处一侧）-> 同侧擦碰，忽略
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 10, 0.008, 0.005),
    ])
    assert crossings(client, device_id="dev1") == []


def test_segment_crossing_gate_twice_is_ignored(client):
    fence_id = create_fence(client)
    create_gate(client, fence_id, GATE_ZIG)

    # 竖直轨迹 lon=0.005 的单个轨迹段横穿 Z 形门线的 3 个子段
    # （单段内交点不止一个）-> 整段忽略
    submit(client, [
        pt("dev1", 0, 0.002, 0.005),
        pt("dev1", 10, 0.008, 0.005),
    ])
    assert crossings(client, device_id="dev1") == []


def test_multiple_gates_sorted_by_time_then_id(client):
    fence_id = create_fence(client)
    # 两条并列南北门线：西门 lon=0.004（id 小），东门 lon=0.006（id 大）
    g_west = create_gate(client, fence_id, {
        "type": "LineString", "coordinates": [[0.004, 0.002], [0.004, 0.008]]})
    g_east = create_gate(client, fence_id, {
        "type": "LineString", "coordinates": [[0.006, 0.002], [0.006, 0.008]]})
    assert g_west < g_east

    # 东 -> 西水平行进：先过东门（t=10/3），再过西门（t=20/3），均正向
    submit(client, [
        pt("dev1", 0, 0.005, 0.008),
        pt("dev1", 10, 0.005, 0.002),
    ])
    cs = crossings(client, device_id="dev1")
    assert [c["gate_id"] for c in cs] == [g_east, g_west]
    assert [c["direction"] for c in cs] == ["positive", "positive"]
    assert datetime.fromisoformat(cs[0]["ts"]) == ts(10 / 3)
    assert datetime.fromisoformat(cs[1]["ts"]) == ts(20 / 3)

    # 同一插值时刻命中多条门线：在独立围栏放两条相交短门线（一横一竖），
    # 对角轨迹穿过二者交点；交点须同时位于两条门线的端点之间
    fence2 = create_fence(client)
    g_h = create_gate(client, fence2, {
        "type": "LineString", "coordinates": [[0.002, 0.0025], [0.008, 0.0025]]})
    g_v = create_gate(client, fence2, {
        "type": "LineString", "coordinates": [[0.005, 0.002], [0.005, 0.0035]]})
    submit(client, [
        pt("dev2", 20, 0.002, 0.008),   # 东南
        pt("dev2", 30, 0.003, 0.002),   # 西北，穿过交点 (lon=0.005,lat=0.0025)
    ])
    cs2 = crossings(client, device_id="dev2", fence_id=fence2)
    assert {c["gate_id"] for c in cs2} == {g_h, g_v}
    assert len({c["ts"] for c in cs2}) == 1  # 同一插值时刻
    assert [c["gate_id"] for c in cs2] == sorted(c["gate_id"] for c in cs2)  # 按门线 ID


def test_crossing_query_filters_and_half_open_window(client):
    f1 = create_fence(client)
    f2 = create_fence(client)
    g1 = create_gate(client, f1)
    g2 = create_gate(client, f2, GATE_NS_SHORT)

    submit(client, [
        # dev1 东->西->东：正向 @5、反向 @15（长短门线均命中）
        pt("dev1", 0, 0.005, 0.008),
        pt("dev1", 10, 0.005, 0.002),
        pt("dev1", 20, 0.005, 0.008),
        # dev2 东->西一次 @10；两门线均命中
        pt("dev2", 0, 0.005, 0.008),
        pt("dev2", 20, 0.005, 0.002),
    ])

    # 按门线
    assert {c["device_id"] for c in crossings(client, gate_id=g1)} == {"dev1", "dev2"}
    assert {c["device_id"] for c in crossings(client, gate_id=g2)} == {"dev1", "dev2"}
    # 按围栏
    assert {c["gate_id"] for c in crossings(client, fence_id=f2)} == {g2}
    # 按设备：两扇门线各 2 次
    assert len(crossings(client, device_id="dev1")) == 4
    # 按方向
    assert all(c["direction"] == "positive"
               for c in crossings(client, direction="positive"))
    neg = crossings(client, device_id="dev1", direction="negative")
    assert len(neg) == 2
    assert all(datetime.fromisoformat(c["ts"]) == ts(15) for c in neg)

    # 半开窗口 [5, 15)：dev1 在 g1 上 t=5 的正向计入，t=15 的反向不计
    win = crossings(client, device_id="dev1", gate_id=g1, start=iso(5), end=iso(15))
    assert [c["direction"] for c in win] == ["positive"]
    # start == end / start > end -> 422
    assert client.get("/crossings", params={"start": iso(10), "end": iso(10)}).status_code == 422
    assert client.get("/crossings", params={"start": iso(20), "end": iso(10)}).status_code == 422


def test_gate_applies_to_existing_points_and_backfill_recomputes(client):
    fence_id = create_fence(client)

    # dev1 两个点都在门线东侧 -> 初始无穿越；dev2 已有一次东->西穿越
    submit(client, [
        pt("dev1", 0, 0.005, 0.008),
        pt("dev1", 100, 0.005, 0.007),
        pt("dev2", 0, 0.005, 0.008),
        pt("dev2", 10, 0.005, 0.002),
    ])
    gate_id = create_gate(client, fence_id)  # 建门线后对存量轨迹重算
    assert crossings(client, device_id="dev1") == []
    cs2 = crossings(client, device_id="dev2")
    assert len(cs2) == 1
    assert datetime.fromisoformat(cs2[0]["ts"]) == ts(5)

    # 补录乱序点 t=50 在西侧：dev1 重算为 正向@25、反向@80，旧的“无结果”被替换且不重复
    submit(client, [pt("dev1", 50, 0.005, 0.002)])
    cs = crossings(client, device_id="dev1")
    assert len(cs) == 2
    assert [(c["direction"], datetime.fromisoformat(c["ts"])) for c in cs] == [
        ("positive", ts(25)),
        ("negative", ts(80)),
    ]
    assert all(c["gate_id"] == gate_id for c in cs)
    # dev2 不受影响
    assert len(crossings(client, device_id="dev2")) == 1


def test_delete_gate_and_fence_removes_crossings(client):
    fence_id = create_fence(client)
    gate_id = create_gate(client, fence_id)
    submit(client, [
        pt("dev1", 0, 0.005, 0.008),
        pt("dev1", 10, 0.005, 0.002),
    ])
    assert len(crossings(client, gate_id=gate_id, device_id="dev1")) == 1

    assert client.delete(f"/fences/{fence_id}/gates/{gate_id}").status_code == 204
    assert crossings(client) == []

    # 再建围栏与门线，验证删围栏级联删除门线与穿越
    fence2 = create_fence(client)
    gate2 = create_gate(client, fence2)
    submit(client, [
        pt("dev2", 0, 0.005, 0.008),
        pt("dev2", 10, 0.005, 0.002),
    ])
    assert len(crossings(client, gate_id=gate2, device_id="dev2")) == 1
    assert client.delete(f"/fences/{fence2}").status_code == 204
    assert crossings(client) == []
