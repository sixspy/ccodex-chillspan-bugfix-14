"""冷链血缘边登记（列表代滤纪要·甲路径：只许当前代正式边，跨代边拒收）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import LineageFilterIn, LineageIn

router = APIRouter(tags=["lineage"])


def _edge_item(row) -> dict:
    it = dict(row)
    # 代滤如实计算：钉代 == 批次当前代（查询已钉死，此处只做如实呈现）
    it["generation_filter_ok"] = bool(
        it["from_gen"] == it["from_gen_current"]
        and (it["to_batch_id"] is None or it["to_gen"] == it["to_gen_current"])
    )
    return it


@router.get("/api/lineage")
def list_edges(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        # 甲路径：列表只许当前代正式边，邻代边读时滤除，零增展示
        rows = conn.execute(
            """SELECT e.*, a.batch_code from_code, b.batch_code to_code,
                      a.generation from_gen_current, b.generation to_gen_current
               FROM lineage_edges e
               JOIN temp_batches a ON a.id=e.from_batch_id
               LEFT JOIN temp_batches b ON b.id=e.to_batch_id
               WHERE e.from_gen = a.generation
                 AND (e.to_batch_id IS NULL OR e.to_gen = b.generation)
               ORDER BY e.id DESC"""
        ).fetchall()
        return {"items": [_edge_item(r) for r in rows]}


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        src = conn.execute(
            "SELECT id, generation FROM temp_batches WHERE id=?",
            (body.from_batch_id,),
        ).fetchone()
        if not src:
            raise HTTPException(404, "来源批次不存在")
        dst = None
        if body.to_batch_id:
            dst = conn.execute(
                "SELECT id, generation FROM temp_batches WHERE id=?",
                (body.to_batch_id,),
            ).fetchone()
            if not dst:
                raise HTTPException(404, "目标批次不存在")
        from_gen = int(src["generation"])
        to_gen = int(dst["generation"]) if dst else None
        if to_gen is not None and to_gen != from_gen:
            # 甲路径：跨代边拒收，零增（不落库）
            raise HTTPException(
                409,
                f"跨代边拒收：来源 G{from_gen} 与目标 G{to_gen} 代次不一致",
            )
        cur = conn.execute(
            """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,
               from_gen,to_gen,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                body.from_batch_id,
                body.to_batch_id,
                body.edge_type,
                body.detail,
                from_gen,
                to_gen,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_add",
            str(body.from_batch_id),
            {
                "edge_id": cur.lastrowid,
                "type": body.edge_type,
                "from_gen": from_gen,
                "to_gen": to_gen,
            },
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "edge_type": body.edge_type,
            "from_gen": from_gen,
            "to_gen": to_gen,
        }


@router.get("/api/lineage/batch/{batch_id}")
def batch_lineage(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT id, generation FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        out_edges = conn.execute(
            """SELECT e.* FROM lineage_edges e
               JOIN temp_batches a ON a.id=e.from_batch_id
               LEFT JOIN temp_batches b ON b.id=e.to_batch_id
               WHERE e.from_batch_id=? AND e.from_gen = a.generation
                 AND (e.to_batch_id IS NULL OR e.to_gen = b.generation)""",
            (batch_id,),
        ).fetchall()
        in_edges = conn.execute(
            """SELECT e.* FROM lineage_edges e
               JOIN temp_batches b ON b.id=e.to_batch_id
               JOIN temp_batches a ON a.id=e.from_batch_id
               WHERE e.to_batch_id=? AND e.to_gen = b.generation
                 AND e.from_gen = a.generation""",
            (batch_id,),
        ).fetchall()
        pin = conn.execute(
            "SELECT * FROM lineage_filter_pins WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return {
            "batch_id": batch_id,
            "generation": int(batch["generation"]),
            "filter_pin": dict(pin) if pin else None,
            "outbound": [dict(e) for e in out_edges],
            "inbound": [dict(e) for e in in_edges],
        }


@router.post("/api/lineage/filter")
def pin_generation_filter(body: LineageFilterIn, authorization: AuthHeader = None):
    """手改代滤：须抢血缘锁 lineage:{batch_id}；同时争用仅存痕；只许钉当前代。"""
    user = require_writer(authorization)
    lock_key = f"lineage:{body.batch_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 争用存痕落库后再拒，后到不写业务
            raise HTTPException(409, "争用：该批次血缘代滤被他人占用，本次仅存痕")
        try:
            batch = conn.execute(
                "SELECT id, generation FROM temp_batches WHERE id=?",
                (body.batch_id,),
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            cur_gen = int(batch["generation"])
            if body.generation != cur_gen:
                raise HTTPException(
                    409, f"跨代拒收：代滤只能钉当前代 G{cur_gen}"
                )
            now = now_iso()
            conn.execute(
                """INSERT INTO lineage_filter_pins(batch_id,generation,pinned_by,pinned_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     generation=excluded.generation,
                     pinned_by=excluded.pinned_by,
                     pinned_at=excluded.pinned_at""",
                (body.batch_id, cur_gen, user, now),
            )
            write_audit(
                conn,
                user,
                "lineage_filter_pin",
                str(body.batch_id),
                {"generation": cur_gen},
            )
            conn.commit()
            return {
                "batch_id": body.batch_id,
                "generation": cur_gen,
                "pinned_by": user,
                "pinned_at": now,
            }
        finally:
            release(conn, lock_key, user)
