"""表级比对 时间线接口：返回作业时间序列结构。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.table_compare import TableCompareService

router = APIRouter(prefix="/api/table_compare", tags=["表级比对"])
service = TableCompareService()
LIST_FIELDS = ["任务编号", "左表名称", "右表名称", "比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]
STATUSES = ["草稿", "比对中", "已完成", "已取消"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=table_compare.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="比对任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action, payload.mode)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    if entry_or_none.get("last_run_failed"):
        return ActionResult(ok=False, message=message, entry=entry_or_none)
    return ActionResult(ok=True, message=message, entry=entry_or_none)


@router.get("/{entry_id}/result")
def get_compare_result(entry_id: int) -> dict[str, Any]:
    """返回比对结果，包含差异数量、差异分区清单和补数任务。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 不存在")
    result = dict(entry.get("_compare_result", {}))
    result["diff_partitions"] = service.get_diff_partitions(entry_id)
    result["backfill_jobs"] = entry.get("backfill_jobs", [])
    result["error_message"] = entry.get("error_message", "")
    return result


@router.get("/{entry_id}/diff-partitions")
def get_diff_partitions(entry_id: int) -> dict[str, Any]:
    """差异分区清单：保留差异日期、差异键和触发任务数据结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"比对任务 {entry_id} 不存在")
    rows = service.get_diff_partitions(entry_id)
    return {
        "items": rows,
        "total": len(rows),
        "compare_id": entry_id,
        "compare_no": entry.get("任务编号", ""),
        "target_table": entry.get("右表名称", ""),
    }


@router.post("/{entry_id}/diff-partitions/{seq}/resolve")
def resolve_conflict(entry_id: int, seq: int, payload: ActionPayload) -> ActionResult:
    """裁决差异分区冲突：accept_priority（接受优先级）或 take_over（接管）。"""
    row_or_none, message = service.resolve_conflict(entry_id, seq, payload.action)
    if row_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=row_or_none)
