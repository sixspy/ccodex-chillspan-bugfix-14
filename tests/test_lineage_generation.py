"""邻代边三断言：列表代滤复现夹具（口径见 docs/列表代滤纪要.md，当班封死甲路径）。

- 测甲：跨代边未滤 —— 邻代边必须滤出当前代列表，跨代登记必须拒收（409）。
- 测乙：影子浏览未晋升却展示 —— 未对齐当前代的边一律不得展示，影子参数后门无效。
- 争用测：双人同批抢刷列表 —— 手改代滤须抢血缘锁 lineage:{batch_id}，争用仅存痕。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _hn_chamber_and_probe(c, h):
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"]
    return ch, probes[0]


def _mk_batch(c, h, code, ch, probe) -> int:
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "代滤夹具货载",
            "temp_points": [{"t": "10:00", "c": 3.0}, {"t": "11:00", "c": 4.0}],
            "batch_code": code,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_path_a_cross_generation_edges_rejected_and_filtered():
    """测甲：邻代边滤出当前列表 + 跨代登记拒收 + 五处代一致。"""
    c = TestClient(app)
    h = _auth(c)
    ch, probe = _hn_chamber_and_probe(c, h)
    a_id = _mk_batch(c, h, "TB-LG-A01", ch, probe)
    b_id = _mk_batch(c, h, "TB-LG-B01", ch, probe)

    # 同代登记 G1→G1：合法，入当前代列表
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": a_id,
            "to_batch_id": b_id,
            "edge_type": "分装衍生",
            "detail": "同代分装",
        },
    )
    assert r.status_code == 200, r.text
    edge_id = r.json()["id"]
    items = c.get("/api/lineage", headers=h).json()["items"]
    mine = [e for e in items if e["id"] == edge_id]
    assert len(mine) == 1
    assert mine[0]["from_gen"] == 1 and mine[0]["to_gen"] == 1

    # 封批升代：B → G2，旧边沦为邻代边
    seal = c.post("/api/seals", headers=h, json={"batch_id": b_id})
    assert seal.status_code == 200 and seal.json()["generation"] == 2

    # 断言一：邻代边不得混入当前代列表（血统视图）
    items = c.get("/api/lineage", headers=h).json()["items"]
    assert all(e["id"] != edge_id for e in items), "邻代边仍混入当前代列表"

    # 断言二：批次维度的出/入边同样钉代过滤
    out_edges = c.get(f"/api/lineage/batch/{a_id}", headers=h).json()["outbound"]
    in_edges = c.get(f"/api/lineage/batch/{b_id}", headers=h).json()["inbound"]
    assert all(e["id"] != edge_id for e in out_edges + in_edges)

    # 断言三：跨代登记拒收（A 仍 G1，B 已 G2），且零增（不得落库）
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": a_id,
            "to_batch_id": b_id,
            "edge_type": "复冻转运",
            "detail": "跨代登记应拒收",
        },
    )
    assert r.status_code == 409, r.text
    items = c.get("/api/lineage", headers=h).json()["items"]
    assert all(e["detail"] != "跨代登记应拒收" for e in items), "拒收边竟落库"

    # 五处代一致：代次 / 封存 / 曲线 / 占用 / 血统
    batch_b = c.get(f"/api/batches/{b_id}", headers=h).json()["batch"]
    assert batch_b["generation"] == 2, "代次视图代不一致"
    seals = c.get("/api/seals", headers=h).json()["items"]
    assert any(
        s["batch_code"] == "TB-LG-B01" and s["batch_generation"] == 2 for s in seals
    ), "封存视图代不一致"
    sp = c.post("/api/spectrum/export", headers=h, json={"batch_id": b_id})
    assert sp.status_code == 200
    assert sp.json()["export_ref"].endswith("-G2"), "曲线视图代不一致"
    chamber = c.get(f"/api/chambers/{ch['id']}", headers=h).json()
    recent = {b["id"]: b for b in chamber["recent_batches"]}
    assert recent[b_id]["generation"] == 2, "占用视图代不一致"
    for e in c.get("/api/lineage", headers=h).json()["items"]:
        assert e["generation_filter_ok"] is True, "血统视图展示了未过代滤的边"


def test_path_b_unpromoted_shadow_never_displayed():
    """测乙：未晋升（未对齐当前代）的边一律不得展示，影子参数后门无效。"""
    c = TestClient(app)
    h = _auth(c)
    ch, probe = _hn_chamber_and_probe(c, h)
    a_id = _mk_batch(c, h, "TB-LG-C01", ch, probe)
    b_id = _mk_batch(c, h, "TB-LG-D01", ch, probe)
    r = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": a_id,
            "to_batch_id": b_id,
            "edge_type": "分装衍生",
            "detail": "待晋升边",
        },
    )
    assert r.status_code == 200, r.text
    edge_id = r.json()["id"]

    seal = c.post("/api/seals", headers=h, json={"batch_id": b_id})
    assert seal.status_code == 200 and seal.json()["generation"] == 2

    # 未晋升却展示 = 缺陷；任何影子/回显参数都不得放出未晋升边
    for path in (
        "/api/lineage",
        "/api/lineage?shadow=1",
        "/api/lineage?include_stale=1",
        "/api/lineage?promoted=0",
    ):
        items = c.get(path, headers=h).json()["items"]
        assert all(e["id"] != edge_id for e in items), f"{path} 展示了未晋升边"
        for e in items:
            assert e["generation_filter_ok"] is True
            assert e["from_gen"] == e["from_gen_current"], "展示了钉代≠当前代的边"
            if e["to_batch_id"] is not None:
                assert e["to_gen"] == e["to_gen_current"], "展示了钉代≠当前代的边"


def test_lineage_filter_contention_two_users():
    """争用测：双人同批抢刷列表/手改代滤 —— 须抢血缘锁，后到仅存痕。"""
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "ops")
    ch, probe = _hn_chamber_and_probe(c, h1)
    b_id = _mk_batch(c, h1, "TB-LG-E01", ch, probe)
    lock_key = f"lineage:{b_id}"

    # 窗口一（admin）持有该批次血缘锁：模拟手改代滤进行中
    r = c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key})
    assert r.status_code == 200, r.text

    # 窗口二（ops）同批手改代滤：抢锁失败，仅存痕，不写业务
    r = c.post(
        "/api/lineage/filter",
        headers=h2,
        json={"batch_id": b_id, "generation": 1},
    )
    assert r.status_code == 409, r.text
    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont
    ), "争用未存痕"
    with connect() as conn:
        pins = conn.execute(
            "SELECT * FROM lineage_filter_pins WHERE batch_id=?", (b_id,)
        ).fetchall()
    assert len(pins) == 0, "争用方不得写入代滤"

    # 窗口一放锁后，窗口二钉当前代成功
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})
    r = c.post(
        "/api/lineage/filter",
        headers=h2,
        json={"batch_id": b_id, "generation": 1},
    )
    assert r.status_code == 200 and r.json()["generation"] == 1, r.text

    # 跨代钉滤拒收（即便抢到锁，也只能钉当前代）
    r = c.post(
        "/api/lineage/filter",
        headers=h2,
        json={"batch_id": b_id, "generation": 7},
    )
    assert r.status_code == 409, r.text
