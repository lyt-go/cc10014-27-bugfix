"""表级比对 时间线服务：按差异分区生成左右表差异记录并串联补数调度。

修复要点：
- 比对不再只记录差异总数，而是按差异分区（分区字段 + 差异键）生成左右表差异记录，
  并持久化「差异分区清单」（store._diff_partitions）。
- 触发补数时严格取差异分区的日期作为起止区间，历史差异不会再被错误补成当天。
- 差异分区清单与补数任务在同一写入边界内落库，避免清单已写入但任务仍用当天日期。
- 非法分区值（非空但无法解析为日期等）按 Bugfix 失败处理：保留既有持久化数据，
  不写半成品清单、不触发补数。
- 差异分区清单围绕表级比对按顺序推进，遇到冲突阻塞不得跳级。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "table_compare"
REQUIRED_FIELDS = ["任务编号", "左表名称", "右表名称"]
STATUS_ORDER = ["草稿", "比对中", "已完成", "已取消"]
ACTION_RULES = {"启动比对": "比对中", "确认完成": "已完成", "取消比对": "已取消"}
NEGATIVE_ACTIONS = []

# 边界模式：严格边界保留左右两组日期/补数范围；放宽边界忽略其中一侧日期
MODE_STRICT = "strict"
MODE_RELAXED = "relaxed"
BOUNDARY_MODES = (MODE_STRICT, MODE_RELAXED)
DEFAULT_MODE = MODE_STRICT

# 差异分区清单行状态：围绕表级比对按顺序推进
ROW_PENDING = "待补数"
ROW_TRIGGERED = "已触发"
ROW_DONE = "已补数"
ROW_BLOCKED = "冲突阻塞"
ROW_RESOLVED = "已裁决"

# 差异类型
DIFF_LEFT_ONLY = "仅左表"
DIFF_RIGHT_ONLY = "仅右表"
DIFF_DATE_MISMATCH = "日期不一致"
DIFF_VALUE_MISMATCH = "取值不一致"


class CompareBugfixError(ValueError):
    """比对输入存在非法值，按 Bugfix 失败处理，不产生任何新的持久化写入。"""


def _parse_partition_date(raw: Any) -> str | None:
    """分区值校验：空值允许（记为 None），非空但无法解析为日期即非法。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CompareBugfixError(f"非法分区日期值：{text!r}，无法按差异分区比对") from exc


def _fallback_partition_date(entry: dict[str, Any]) -> str:
    """无分区字段的表（行内没有分区列）时的确定性回退日期。

    优先从比对批次中解析日期（如 RAW-20261101），保证历史任务可复现；
    解析不到才回退当天，且仅作用于真正没有分区信息的表。
    """
    batch = str(entry.get("比对批次", "") or "")
    digits = "".join(ch for ch in batch if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date.fromisoformat(digits[:8]).isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


class TableCompareService:
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
                "diff_count": int(row.get("diff_count", 0) or 0),
                "partition_count": len(store.get_diff_partitions(int(row.get("id", i + 1)))),
                "abnormal": bool(row.get("abnormal")),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_diff_partitions(self, entry_id: int) -> list[dict[str, Any]]:
        return store.get_diff_partitions(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        # 只统计当前仍在表内的记录，避免清理后 id 从 1 重来、与历史清单键冲突
        entry = {"id": max((int(r.get("id", 0)) for r in rows if r.get("id") is not None), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["error_message"] = ""
        for k in ["比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]:
            if k in values:
                entry[k] = values[k]
        rows.append(entry)
        # 清理历史残留：同编号的旧清单与草稿态自动补数任务不再有效
        stale_jobs = [r for r in store.rows("backfill_job")
                      if r.get("source_compare_no") == entry["任务编号"]
                      and r.get("status") == "草稿"]
        for job in stale_jobs:
            store.rows("backfill_job").remove(job)
        return entry, []

    def run_action(self, entry_id: int, action: str, mode: str | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"比对任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表级比对可执行范围"
        if mode is not None and mode not in BOUNDARY_MODES:
            return None, f"边界模式「{mode}」非法，仅支持 {MODE_STRICT}/{MODE_RELAXED}"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["error_message"] = ""
        store.save_version(MODULE, entry_id, entry)
        if action == "启动比对":
            try:
                result, manifest = self._do_compare(entry, mode or DEFAULT_MODE)
            except CompareBugfixError as exc:
                # Bugfix 失败：保留既有清单与补数记录，不写半成品
                entry["abnormal"] = True
                entry["status"] = "比对中"
                entry["error_message"] = str(exc)
                entry["last_run_failed"] = True
                store.save_version(MODULE, entry_id, entry)
                return entry, f"比对失败（Bugfix）：{exc}"
            self._persist_compare_result(entry, result, manifest)
        return entry, f"比对任务已{action}"

    # ------------------------------------------------------------------
    # 比对核心：按差异分区生成左右表差异记录
    # ------------------------------------------------------------------
    def _load_table(self, table_name: str) -> list[dict[str, Any]]:
        rows = store.table_rows(table_name)
        if rows:
            return rows
        from app.services.ods_layer import OdsLayerService
        return OdsLayerService().get_table_data(table_name) or []

    def _resolve_partition_field(self, table_left: str, table_right: str,
                                 left_rows: list[dict[str, Any]],
                                 right_rows: list[dict[str, Any]]) -> str | None:
        # 1) 以行数据里真实存在的列为准，避免拿别的表的分区字段误判
        actual_columns: set[str] = set()
        for table_rows in (left_rows, right_rows):
            if table_rows:
                actual_columns |= set(table_rows[0].keys())
        for candidate in ("dt", "分区日期", "partition_date", "date"):
            if candidate in actual_columns:
                return candidate
        # 2) 行内没有时，再查与本次比对表名匹配的分区/分层元数据
        for module in ("partition_mgr", "ods_layer", "dwd_layer"):
            for row in store.rows(module):
                field = row.get("分区字段")
                table_name = row.get("目标表名") or row.get("表名称")
                if field and table_name in (table_left, table_right):
                    return str(field)
        # 3) 两张表确实都没有分区信息：按无分区表处理，走确定性回退日期
        return None

    def _do_compare(self, entry: dict[str, Any], mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        left = entry.get("左表名称", "")
        right = entry.get("右表名称", "")
        fields_str = entry.get("比对字段", "")
        fields = [f.strip() for f in fields_str.split(",") if f.strip()] if fields_str else []
        left_rows = self._load_table(left)
        right_rows = self._load_table(right)
        if not fields:
            fields = [f for f in (list(left_rows[0].keys()) if left_rows else []) if f != "dt"]
        key_field = fields[0] if fields else "_id"
        value_fields = [f for f in fields[1:] if f]
        partition_field = self._resolve_partition_field(left, right, left_rows, right_rows)

        # 无分区列：使用确定性回退日期，保证重新进入/刷新结果一致
        fallback_date: str | None = None
        if partition_field is None:
            fallback_date = _fallback_partition_date(entry)

        def row_date(row: dict[str, Any]) -> str | None:
            if partition_field is None:
                return fallback_date
            return _parse_partition_date(row.get(partition_field))

        # 按键分组（同一键可能存在多个分区日期）
        def group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                key = str(row.get(key_field, ""))
                grouped.setdefault(key, []).append(row)
            return grouped

        left_group = group(left_rows)
        right_group = group(right_rows)

        raw_records: list[dict[str, Any]] = []
        only_left_keys: list[str] = []
        only_right_keys: list[str] = []

        for key in sorted(set(left_group) | set(right_group)):
            l_rows = left_group.get(key, [])
            r_rows = right_group.get(key, [])
            l_dates = {row_date(r) for r in l_rows}
            r_dates = {row_date(r) for r in r_rows}
            if l_rows and not r_rows:
                only_left_keys.append(key)
                for dt_value in sorted(d for d in l_dates if d is not None) or [None]:
                    raw_records.append(self._build_record(
                        DIFF_LEFT_ONLY, key, dt_value, None, mode, key_field,
                        partition_field, l_rows[0] if l_rows else {}, {},
                    ))
                continue
            if r_rows and not l_rows:
                only_right_keys.append(key)
                for dt_value in sorted(d for d in r_dates if d is not None) or [None]:
                    raw_records.append(self._build_record(
                        DIFF_RIGHT_ONLY, key, None, dt_value, mode, key_field,
                        partition_field, {}, r_rows[0] if r_rows else {},
                    ))
                continue
            # 两侧都有该键：比较分区日期
            for dt_value in sorted(l_dates | r_dates, key=lambda d: d or ""):
                l_match = [r for r in l_rows if row_date(r) == dt_value]
                r_match = [r for r in r_rows if row_date(r) == dt_value]
                if dt_value in l_dates and dt_value not in r_dates:
                    raw_records.append(self._build_record(
                        DIFF_DATE_MISMATCH, key, dt_value,
                        next((row_date(r) for r in r_rows if row_date(r) is not None), None),
                        mode, key_field, partition_field,
                        l_match[0] if l_match else l_rows[0],
                        r_rows[0],
                    ))
                elif dt_value in r_dates and dt_value not in l_dates:
                    raw_records.append(self._build_record(
                        DIFF_DATE_MISMATCH, key,
                        next((row_date(r) for r in l_rows if row_date(r) is not None), None),
                        dt_value, mode, key_field, partition_field,
                        l_rows[0], r_match[0] if r_match else r_rows[0],
                    ))
                elif l_match and r_match and value_fields:
                    sample_l, sample_r = l_match[0], r_match[0]
                    if any(str(sample_l.get(f, "")) != str(sample_r.get(f, "")) for f in value_fields):
                        raw_records.append(self._build_record(
                            DIFF_VALUE_MISMATCH, key, dt_value, dt_value, mode,
                            key_field, partition_field, sample_l, sample_r,
                        ))

        # 差异分区清单：按差异日期、差异键排序，顺序推进、不跳级
        raw_records.sort(key=lambda r: (r["diff_date"] or "9999-12-31", r["diff_type"], r["diff_key"]))
        manifest: list[dict[str, Any]] = []
        for seq, record in enumerate(raw_records, start=1):
            record["seq"] = seq
            record["compare_id"] = int(entry["id"])
            record["compare_no"] = str(entry.get("任务编号", ""))
            record["target_table"] = str(right)
            record["boundary_mode"] = mode
            record["status"] = ROW_PENDING
            record["backfill_job_no"] = ""
            record["conflict_with"] = ""
            record["priority"] = ""
            manifest.append(record)

        result = {
            "left_count": len(left_rows),
            "right_count": len(right_rows),
            "diff_count": len(manifest),
            "only_left": only_left_keys[:10],
            "only_right": only_right_keys[:10],
            "key_field": key_field,
            "partition_field": partition_field or "",
            "boundary_mode": mode,
            "partition_count": len({r["diff_date"] for r in manifest if r["diff_date"]}),
            "manifest": manifest,
        }
        return result, manifest

    def _build_record(self, diff_type: str, key: str, left_date: str | None,
                      right_date: str | None, mode: str, key_field: str,
                      partition_field: str | None,
                      left_row: dict[str, Any], right_row: dict[str, Any]) -> dict[str, Any]:
        """生成一条左右表差异记录，并按边界模式决定保留/忽略哪一侧日期。"""
        ignored_side = ""
        ld, rd = left_date, right_date
        if mode == MODE_RELAXED:
            # 放宽边界：以有数据来源的一侧日期为准，另一侧标记为忽略
            if diff_type == DIFF_LEFT_ONLY:
                rd, ignored_side = None, "right"
            elif diff_type == DIFF_RIGHT_ONLY:
                ld, ignored_side = None, "left"
            elif diff_type == DIFF_DATE_MISMATCH:
                if ld is not None:
                    rd, ignored_side = None, "right"
                else:
                    ld, ignored_side = None, "left"

        # 两组补数范围：严格边界两侧各一组；放宽边界只保留生效侧
        left_range = {"start": ld, "end": ld} if ld else {"start": "", "end": ""}
        right_range = {"start": rd, "end": rd} if rd else {"start": "", "end": ""}
        if mode == MODE_RELAXED and ignored_side == "right":
            right_range = {"start": "", "end": ""}
        if mode == MODE_RELAXED and ignored_side == "left":
            left_range = {"start": "", "end": ""}

        # 补数落在右表（下游表），目标区间取右表分区日期；
        # 仅左表差异时右表缺该分区，使用左表日期作为需要补齐的目标分区
        target_date = rd or ld
        backfill_range = {"start": target_date, "end": target_date} if target_date else {"start": "", "end": ""}
        return {
            "diff_type": diff_type,
            "diff_key": key,
            "key_field": key_field,
            "partition_field": partition_field or "",
            "left_date": ld or "",
            "right_date": rd or "",
            "ignored_side": ignored_side,
            "left_backfill_range": left_range,
            "right_backfill_range": right_range,
            "backfill_range": backfill_range,
            # 放宽边界下目标日期跟随生效侧，被忽略日期不参与补数分区聚合
            "diff_date": target_date or "",
            "left_value": {k: left_row.get(k) for k in left_row.keys() & {"order_amount", "order_status", "pay_amount", "user_level"}} if left_row else {},
            "right_value": {k: right_row.get(k) for k in right_row.keys() & {"order_amount", "order_status", "pay_amount", "user_level"}} if right_row else {},
        }

    # ------------------------------------------------------------------
    # 差异分区清单 + 补数任务：同一写入边界落库
    # ------------------------------------------------------------------
    def _persist_compare_result(self, entry: dict[str, Any], result: dict[str, Any],
                                manifest: list[dict[str, Any]]) -> None:
        from app.services.backfill_job import BackfillJobService
        bf = BackfillJobService()
        compare_id = int(entry["id"])

        # 清理上一轮本任务自动产生、但仍处草稿态的补数任务（运行中/已完成不动）
        stale = [r for r in store.rows("backfill_job")
                 if r.get("source_compare_id") == compare_id and r.get("status") == "草稿"]
        for row in stale:
            store.rows("backfill_job").remove(row)
        # 同一 id 重新比对：先清掉历史清单，避免上一运行环境的残留串号
        store.replace_diff_partitions(compare_id, [])

        # 按差异分区日期生成补数任务：每个目标分区一个任务，日期严格来自清单
        date_to_job: dict[str, str] = {}
        job_diff_keys: dict[str, list[str]] = {}
        for record in manifest:
            target_date = record["diff_date"]
            if not target_date:
                continue
            if target_date not in date_to_job:
                job_no = f"BF-{entry.get('任务编号', '')}-{target_date}"
                existing = next((r for r in store.rows("backfill_job")
                                 if r.get("任务编号") == job_no), None)
                if existing is None:
                    bf.create_entry({
                        "任务编号": job_no,
                        "目标表名": record["target_table"],
                        "起始日期": target_date,
                        "截止日期": target_date,
                        "并行度": "2",
                        "覆盖策略": "覆盖写入",
                        "完成水位": "",
                        "失败区间": "",
                        "source_compare_id": compare_id,
                        "source_compare_no": str(entry.get("任务编号", "")),
                        "diff_keys": [],
                        "boundary_mode": record["boundary_mode"],
                    })
                date_to_job[target_date] = job_no
                job_diff_keys[job_no] = []
            job_diff_keys[date_to_job[target_date]].append(record["diff_key"])

        # 回填任务上的差异键与清单上的任务编号
        for job_no, keys in job_diff_keys.items():
            job = next((r for r in store.rows("backfill_job") if r.get("任务编号") == job_no), None)
            if job is not None:
                job["diff_keys"] = sorted(set(keys))
        for record in manifest:
            record["backfill_job_no"] = date_to_job.get(record["diff_date"], "")

        # 冲突裁决：与其他比对任务在同一右表、同一分区上的未完成补数任务冲突
        self._mark_conflicts(manifest, compare_id)

        # 顺序推进、不得跳级：首个被阻塞的行之后的行保持待补数，已建任务保留关联
        blocked = False
        for record in manifest:
            if blocked:
                record["status"] = ROW_PENDING
                continue
            if record["status"] == ROW_BLOCKED:
                blocked = True
                continue
            record["status"] = ROW_TRIGGERED if record["backfill_job_no"] else ROW_PENDING

        # 单一写入边界：清单与补数记录一致落库
        store.replace_diff_partitions(compare_id, manifest)
        entry["_compare_result"] = result
        entry["diff_count"] = result["diff_count"]
        entry["partition_count"] = result["partition_count"]
        entry["boundary_mode"] = result["boundary_mode"]
        entry["backfill_jobs"] = sorted(set(date_to_job.values()))
        entry["last_run_failed"] = False
        store.save_version(MODULE, compare_id, entry)

    def _mark_conflicts(self, manifest: list[dict[str, Any]], compare_id: int) -> None:
        for record in manifest:
            target_date = record["diff_date"]
            if not target_date:
                continue
            for other_id, other_rows in store.all_diff_partitions().items():
                if int(other_id) == compare_id:
                    continue
                for other in other_rows:
                    if other.get("target_table") != record["target_table"]:
                        continue
                    if other.get("diff_date") != target_date:
                        continue
                    other_job = other.get("backfill_job_no", "")
                    job_row = next((r for r in store.rows("backfill_job")
                                    if r.get("任务编号") == other_job), None)
                    if job_row is None or job_row.get("status") in ("已完成", "已取消"):
                        continue
                    # 优先级：比对任务编号（id）小者优先，同任务内按清单序号
                    winner = compare_id <= int(other_id)
                    record["conflict_with"] = str(other_job)
                    record["priority"] = "优先" if winner else "让位"
                    if not winner:
                        record["status"] = ROW_BLOCKED
                    break
                if record["status"] == ROW_BLOCKED:
                    break

    def resolve_conflict(self, entry_id: int, seq: int, decision: str) -> tuple[dict[str, Any] | None, str]:
        """裁决差异分区冲突：accept_priority 接受优先级，take_over 本任务接管。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"比对任务 {entry_id} 不存在"
        manifest = store.get_diff_partitions(entry_id)
        record = next((r for r in manifest if int(r.get("seq", -1)) == seq), None)
        if record is None:
            return None, f"差异分区序号 {seq} 不存在"
        if record.get("status") != ROW_BLOCKED:
            return None, "该差异分区不存在待裁决冲突"
        if decision == "accept_priority":
            # 让位方放弃该分区：移除/取消关联补数任务，行标记为已裁决
            job_no = record.get("backfill_job_no", "")
            if job_no:
                job = next((r for r in store.rows("backfill_job") if r.get("任务编号") == job_no), None)
                if job is not None and job.get("status") == "草稿":
                    store.rows("backfill_job").remove(job)
            record["backfill_job_no"] = ""
            record["status"] = ROW_RESOLVED
        elif decision == "take_over":
            # 接管：取消对方未完成任务，本行恢复触发
            other_job_no = record.get("conflict_with", "")
            other_job = next((r for r in store.rows("backfill_job")
                              if r.get("任务编号") == other_job_no), None)
            if other_job is not None and other_job.get("status") not in ("已完成", "已取消"):
                other_job["status"] = "已取消"
                other_job["pending"] = False
            record["status"] = ROW_TRIGGERED
            record["priority"] = "接管"
        else:
            return None, f"裁决动作「{decision}」非法"
        # 裁决后继续顺序推进后续行
        for later in manifest:
            if int(later["seq"]) > seq and later.get("status") == ROW_PENDING and later.get("backfill_job_no"):
                later["status"] = ROW_TRIGGERED
        store.replace_diff_partitions(entry_id, manifest)
        store.save_version(MODULE, entry_id, entry)
        return record, "冲突已裁决"
