"""补数调度 时间线服务：作用于具体表，修改分区水位，记录补数区间。

补数任务的起止日期必须来自差异分区清单，执行时只覆盖目标区间内的分区，
不会清掉无关分区；非法日期按失败处理并记入失败区间。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "backfill_job"
REQUIRED_FIELDS = ["任务编号", "目标表名", "起始日期"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已取消"]
ACTION_RULES = {"启动任务": "运行中", "确认完成": "已完成", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []
EXTRA_FIELDS = ["source_compare_id", "source_compare_no", "diff_keys", "boundary_mode"]


class BackfillJobService:
    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["截止日期", "并行度", "覆盖策略", "完成水位", "失败区间"]:
            if k in values:
                entry[k] = values[k]
        # 表级比对差异分区清单的串联信息
        for k in EXTRA_FIELDS:
            if k in values:
                entry[k] = values[k]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于补数调度可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["error_message"] = ""
        store.save_version(MODULE, entry_id, entry)
        if action == "启动任务":
            error = self._execute_backfill(entry)
            if error:
                entry["abnormal"] = True
                entry["失败区间"] = f"{entry.get('起始日期', '')}~{entry.get('截止日期', '')}"
                entry["error_message"] = error
                store.save_version(MODULE, entry_id, entry)
                return entry, f"补数失败（Bugfix）：{error}"
            entry["失败区间"] = ""
            self._sync_manifest(entry, "已触发")
        if action == "确认完成":
            entry["pending"] = False
            self._sync_manifest(entry, "已完成")
        return entry, f"补数任务已{action}"

    def get_linkage(self, entry_id: int) -> dict[str, Any]:
        """回查补数任务对应的差异键与左右表分区。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {}
        compare_id = entry.get("source_compare_id")
        partitions = store.get_backfill_partitions(str(entry.get("任务编号", "")))
        diff_rows: list[dict[str, Any]] = []
        if compare_id is not None:
            diff_rows = [
                row for row in store.get_diff_partitions(int(compare_id))
                if row.get("backfill_job_no") == entry.get("任务编号")
            ]
        return {
            "job_no": entry.get("任务编号", ""),
            "source_compare_id": compare_id,
            "source_compare_no": entry.get("source_compare_no", ""),
            "diff_keys": entry.get("diff_keys", []),
            "boundary_mode": entry.get("boundary_mode", ""),
            "partitions": partitions,
            "diff_rows": diff_rows,
        }

    def _execute_backfill(self, entry: dict[str, Any]) -> str:
        table = entry.get("目标表名", "")
        start = str(entry.get("起始日期", "") or "").strip()
        end = str(entry.get("截止日期", "") or "").strip() or start
        strategy = entry.get("覆盖策略", "覆盖写入")
        # 非法日期按 Bugfix 失败处理：不静默回退、不写无关分区
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
        except ValueError as exc:
            return f"非法补数区间：起始={start!r} 截止={end!r}（{exc}）"
        if e < s:
            return f"非法补数区间：截止日期 {end} 早于起始日期 {start}"

        new_partitions: list[dict[str, Any]] = []
        target_dates: set[str] = set()
        already_recorded = {p.get("dt") for p in store.get_backfill_partitions(entry.get("任务编号", ""))}
        d = s
        while d <= e:
            day = d.isoformat()
            target_dates.add(day)
            new_partitions.append({"dt": day, "_backfilled": True})
            d += timedelta(days=1)

        if strategy == "覆盖写入":
            # 覆盖写入：只重写目标区间内的分区，区间之外的无关历史分区原样保留
            kept = [dict(row) for row in store.table_rows(table)
                    if str(row.get("dt", "")) not in target_dates]
            store.set_table_rows(table, kept + new_partitions)
        else:
            store.append_table_rows(table, new_partitions)
        # 记录补数分区（同一任务同一分区只记一次，重复执行不产生重复区间）
        for p in new_partitions:
            if p["dt"] not in already_recorded:
                store.save_backfill_partition(entry.get("任务编号", ""), p)
        entry["完成水位"] = end
        # 更新关联DWD/ODS的水位
        for row in store.rows("dwd_layer"):
            if row.get("表名称") == table:
                row["刷新水位"] = end
                store.save_version("dwd_layer", row["id"], row)
        for row in store.rows("ods_layer"):
            if row.get("表名称") == table:
                store.save_version("ods_layer", row["id"], row)
        return ""

    def _sync_manifest(self, entry: dict[str, Any], job_status: str) -> None:
        """补数状态回写差异分区清单，保持两侧持久化一致。"""
        compare_id = entry.get("source_compare_id")
        if compare_id is None:
            return
        manifest = store.get_diff_partitions(int(compare_id))
        from app.services.table_compare import ROW_DONE, ROW_TRIGGERED
        changed = False
        for row in manifest:
            if row.get("backfill_job_no") == entry.get("任务编号"):
                row["job_status"] = job_status
                if job_status == "已完成" and row.get("status") == ROW_TRIGGERED:
                    row["status"] = ROW_DONE
                changed = True
        if changed:
            store.replace_diff_partitions(int(compare_id), manifest)
