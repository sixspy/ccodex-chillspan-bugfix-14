"""解冻事件空桩：只写任务表与审计。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_writer
from db import connect, now_iso, write_audit
from models import ThawStubIn

router = APIRouter(tags=["stubs"])


@router.get("/api/thaw/jobs")
def list_thaw_jobs(authorization: AuthHeader = None):
    require_writer(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM thaw_event_jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/thaw-stub")
def stub_thaw(body: ThawStubIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        before = None
        if body.batch_id:
            before = conn.execute(
                "SELECT status FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
        cur = conn.execute(
            """INSERT INTO thaw_event_jobs(batch_id,thaw_minutes,mode,status,detail,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                body.batch_id,
                body.thaw_minutes,
                body.mode,
                "空桩登记",
                "未改写批次状态",
                now_iso(),
            ),
        )
        after = None
        if body.batch_id:
            after = conn.execute(
                "SELECT status FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
        unchanged = (
            before is None
            or after is None
            or before["status"] == after["status"]
        )
        write_audit(
            conn,
            user,
            "thaw_stub",
            str(body.batch_id or "none"),
            {"job_id": cur.lastrowid},
        )
        conn.commit()
        return {
            "job_id": cur.lastrowid,
            "status": "空桩登记",
            "batch_unchanged": unchanged,
        }