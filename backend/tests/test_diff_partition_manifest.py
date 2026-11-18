"""表级比对按差异分区生成清单并串联补数调度的缺陷修复验证。

覆盖：
- 历史差异按真实差异日期生成补数区间，不再总取当天
- 左右表同一键不同差异日期/空值：严格边界两组范围，放宽边界单侧+忽略日期
- 非法分区值按 Bugfix 失败处理，保留持久化与一致性边界
- 差异分区清单与补数记录一致落库，重新进入/刷新结果可复现
- 顺序推进不跳级、冲突裁决
- 补数只覆盖目标分区，不覆盖无关分区
"""
from __future__ import annotations

from datetime import date, timedelta

from app.store import store
from app.services.table_compare import TableCompareService
from app.services.backfill_job import BackfillJobService


def reset_store():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store.clear_diff_partitions()


class TestDiffPartitionManifest:
    def setup_method(self):
        reset_store()

    def test_backfill_uses_diff_partition_date_not_today(self):
        """历史分区差异：补数任务起止日期必须来自差异分区，而不是当天。"""
        historical = (date.today() - timedelta(days=10)).isoformat()
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "order_amount": "100", "dt": historical},
            {"order_id": "O002", "order_amount": "200", "dt": historical},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100", "dt": historical},
        ])
        tc = TableCompareService()
        updated, msg = tc.run_action(1, "启动比对")
        assert updated is not None
        result = updated["_compare_result"]
        assert result["diff_count"] == 1
        manifest = store.get_diff_partitions(1)
        assert len(manifest) == 1
        row = manifest[0]
        assert row["diff_key"] == "O002"
        assert row["diff_date"] == historical
        # 触发的补数任务日期严格等于差异分区日期
        jobs = [r for r in store.rows("backfill_job")
                if r.get("source_compare_id") == 1]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["起始日期"] == historical
        assert job["截止日期"] == historical
        assert historical != date.today().isoformat()
        assert job["目标表名"] == "dwd_order_detail"
        assert row["backfill_job_no"] == job["任务编号"]

    def test_manifest_persisted_and_reloadable(self):
        """清单必须落库：重新进入服务/刷新页面仍能回查差异键与左右分区。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
            {"order_id": "O002", "dt": "2026-11-02"},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        tc = TableCompareService()
        tc.run_action(1, "启动比对")
        # 新建服务实例模拟页面重新进入
        reloaded = TableCompareService().get_diff_partitions(1)
        assert [r["diff_key"] for r in reloaded] == ["O002"]
        assert reloaded[0]["left_date"] == "2026-11-02"
        assert reloaded[0]["right_date"] == ""
        result = TableCompareService().get_entry(1)["_compare_result"]
        assert result["partition_count"] == 1

    def test_same_key_different_dates_strict_mode_two_ranges(self):
        """同键不同差异日期：严格边界显示左右日期和两组补数范围。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "order_amount": "100", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100", "dt": "2026-11-05"},
        ])
        tc = TableCompareService()
        updated, _ = tc.run_action(1, "启动比对", "strict")
        manifest = updated["_compare_result"]["manifest"]
        mismatch = [r for r in manifest if r["diff_type"] == "日期不一致"]
        assert mismatch, "应识别出日期不一致差异"
        row = mismatch[0]
        assert row["left_date"] == "2026-11-01"
        assert row["right_date"] == "2026-11-05"
        assert row["left_backfill_range"] == {"start": "2026-11-01", "end": "2026-11-01"}
        assert row["right_backfill_range"] == {"start": "2026-11-05", "end": "2026-11-05"}
        # 补数落右表，目标分区取右表日期
        assert row["backfill_range"] == {"start": "2026-11-05", "end": "2026-11-05"}

    def test_same_key_different_dates_relaxed_mode_single_side(self):
        """放宽边界：显示单侧日期、被忽略日期和一组补数范围。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "dt": "2026-11-05"},
        ])
        tc = TableCompareService()
        updated, _ = tc.run_action(1, "启动比对", "relaxed")
        manifest = updated["_compare_result"]["manifest"]
        mismatch = [r for r in manifest if r["diff_type"] == "日期不一致"]
        row = mismatch[0]
        # 左表日期生效，右表日期被忽略
        assert row["left_date"] == "2026-11-01"
        assert row["right_date"] == ""
        assert row["ignored_side"] == "right"
        assert row["right_backfill_range"] == {"start": "", "end": ""}
        assert row["left_backfill_range"] == {"start": "2026-11-01", "end": "2026-11-01"}

    def test_null_partition_value_allowed_but_recorded(self):
        """空分区值不判非法：清单保留空日期，且不产生无日期补数任务。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": ""},
        ])
        store.set_table_rows("dwd_order_detail", [])
        tc = TableCompareService()
        updated, _ = tc.run_action(1, "启动比对")
        row = updated["_compare_result"]["manifest"][0]
        assert row["diff_key"] == "O001"
        assert row["left_date"] == ""
        assert row["backfill_range"] == {"start": "", "end": ""}
        jobs = [r for r in store.rows("backfill_job") if r.get("source_compare_id") == 1]
        assert jobs == []

    def test_illegal_partition_value_bugfix_failure_keeps_persistence(self):
        """非法分区值按 Bugfix 失败：保留旧清单与补数记录，不写半成品。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [])
        tc = TableCompareService()
        first, _ = tc.run_action(1, "启动比对")
        assert first["diff_count"] == 1
        persisted_manifest = store.get_diff_partitions(1)
        persisted_jobs = [r["任务编号"] for r in store.rows("backfill_job")]
        # 第二次比对出现非法分区值
        store.set_table_rows("ods_orders", [
            {"order_id": "O009", "dt": "not-a-date"},
        ])
        updated, msg = tc.run_action(1, "启动比对")
        assert "Bugfix" in msg
        assert updated["abnormal"] is True
        assert updated["last_run_failed"] is True
        # 旧清单与补数记录原样保留
        assert store.get_diff_partitions(1) == persisted_manifest
        assert [r["任务编号"] for r in store.rows("backfill_job")] == persisted_jobs

    def test_sequential_manifest_and_no_skip_on_block(self):
        """清单按序号推进；冲突阻塞后后续行保持待补数，不得跳级。"""
        # 先跑 id=1 的比对，占用 dwd_order_detail 的 2026-11-01 分区
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [])
        TableCompareService().run_action(1, "启动比对")
        # 再登记 id=4 的新比对，同表同分区 + 另一个更晚分区
        store.set_table_rows("ods_payments", [
            {"pay_id": "P001", "dt": "2026-11-01"},
            {"pay_id": "P002", "dt": "2026-11-02"},
        ])
        tc = TableCompareService()
        created, _ = tc.create_entry({
            "任务编号": "CMP-NEW-001",
            "左表名称": "ods_payments",
            "右表名称": "dwd_order_detail",
            "比对字段": "pay_id",
            "比对批次": "RAW-20261102",
        })
        # 构造同表同分区：让新比对也落到 dwd_order_detail
        store.set_table_rows("ods_payments", [
            {"pay_id": "P001", "dt": "2026-11-01"},
            {"pay_id": "P002", "dt": "2026-11-02"},
        ])
        updated, _ = tc.run_action(created["id"], "启动比对")
        manifest = updated["_compare_result"]["manifest"]
        assert [r["seq"] for r in manifest] == [1, 2]
        blocked = next(r for r in manifest if r["diff_date"] == "2026-11-01")
        later = next(r for r in manifest if r["diff_date"] == "2026-11-02")
        assert blocked["status"] == "冲突阻塞"
        assert blocked["priority"] == "让位"
        # 被阻塞行之后不得跳级触发
        assert later["status"] == "待补数"

    def test_conflict_resolution_take_over_resumes_sequence(self):
        """裁决接管后，后续待补数行顺序恢复触发。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [])
        TableCompareService().run_action(1, "启动比对")
        tc = TableCompareService()
        created, _ = tc.create_entry({
            "任务编号": "CMP-NEW-002",
            "左表名称": "ods_payments",
            "右表名称": "dwd_order_detail",
            "比对字段": "pay_id",
            "比对批次": "RAW-20261102",
        })
        store.set_table_rows("ods_payments", [
            {"pay_id": "P001", "dt": "2026-11-01"},
            {"pay_id": "P002", "dt": "2026-11-02"},
        ])
        updated, _ = tc.run_action(created["id"], "启动比对")
        manifest = updated["_compare_result"]["manifest"]
        blocked_seq = next(r["seq"] for r in manifest if r["status"] == "冲突阻塞")
        row, msg = tc.resolve_conflict(created["id"], blocked_seq, "take_over")
        assert row is not None
        assert row["status"] == "已触发"
        after = store.get_diff_partitions(created["id"])
        assert all(r["status"] in ("已触发", "已补数") for r in after)


class TestBackfillPartitionScope:
    def setup_method(self):
        reset_store()

    def test_backfill_overwrites_only_target_partition(self):
        """覆盖写入只替换目标区间分区，无关分区保留，不被当天日期覆盖。"""
        historical = (date.today() - timedelta(days=10)).isoformat()
        other_day = (date.today() - timedelta(days=20)).isoformat()
        target = "dwd_backfill_scope_demo"  # 专用表，避免种子补数残留干扰
        store.set_table_rows("ods_scope_demo", [
            {"order_id": "O001", "dt": historical},
            {"order_id": "O002", "dt": historical},
            {"order_id": "KEEP", "dt": other_day},
        ])
        # 目标表：缺 O002，另有一个无关分区必须保留
        store.set_table_rows(target, [
            {"order_id": "O001", "dt": historical},
            {"order_id": "KEEP", "dt": other_day},
        ])
        tc = TableCompareService()
        created, _ = tc.create_entry({
            "任务编号": "CMP-SCOPE-UNIQUE-9527",
            "左表名称": "ods_scope_demo",
            "右表名称": target,
            "比对字段": "order_id",
            "比对批次": "RAW-20261101",
        })
        tc.run_action(created["id"], "启动比对")
        manifest = store.get_diff_partitions(created["id"])
        o002 = next(r for r in manifest if r["diff_key"] == "O002")
        assert o002["diff_date"] == historical
        job = next(r for r in store.rows("backfill_job")
                   if r.get("任务编号") == o002["backfill_job_no"])
        assert job["起始日期"] == historical and job["截止日期"] == historical
        BackfillJobService().run_action(job["id"], "启动任务")
        rows = store.table_rows(target)
        kept = [r for r in rows if r.get("dt") == other_day]
        assert kept and kept[0].get("order_id") == "KEEP"
        covered = [r for r in rows if r.get("dt") == historical]
        assert covered and all(r.get("_backfilled") for r in covered)
        # 补数分区与清单一致，且不包含无关日期
        partitions = store.get_backfill_partitions(job["任务编号"])
        assert {p["dt"] for p in partitions} == {historical}

    def test_backfill_invalid_date_fails_without_writes(self):
        """非法补数区间按 Bugfix 失败，不写任何分区。"""
        bf = BackfillJobService()
        entry, _ = bf.create_entry({
            "任务编号": "BF-BAD-001",
            "目标表名": "ods_orders",
            "起始日期": "2026-13-99",
            "截止日期": "2026-13-99",
        })
        before = list(store.table_rows("ods_orders"))
        updated, msg = bf.run_action(entry["id"], "启动任务")
        assert "Bugfix" in msg
        assert updated["abnormal"] is True
        assert updated["失败区间"]
        assert store.table_rows("ods_orders") == before

    def test_linkage_returns_diff_keys_and_partitions(self):
        """补数面板可回查差异键与对应左右表分区。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
            {"order_id": "O002", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        TableCompareService().run_action(1, "启动比对")
        job = next(r for r in store.rows("backfill_job") if r.get("source_compare_id") == 1)
        linkage = BackfillJobService().get_linkage(job["id"])
        assert linkage["diff_keys"] == ["O002"]
        assert linkage["source_compare_no"] == "CMP-ORD-001"
        assert linkage["diff_rows"][0]["left_date"] == "2026-11-01"


class TestConflictPriorityAndIdempotency:
    def setup_method(self):
        reset_store()

    def test_lower_compare_id_wins_priority(self):
        """同表同分区冲突：比对任务 id 小者优先，不被阻塞。"""
        # 先跑较大 id 的比对，占用同表同分区
        tc = TableCompareService()
        big, _ = tc.create_entry({
            "任务编号": "CMP-BIG-001",
            "左表名称": "ods_payments",
            "右表名称": "dwd_order_detail",
            "比对字段": "pay_id",
            "比对批次": "RAW-20261102",
        })
        store.set_table_rows("ods_payments", [{"pay_id": "P001", "dt": "2026-11-01"}])
        tc.run_action(big["id"], "启动比对")
        # 再跑种子里 id=1 的比对（id 更小），同表同分区
        store.set_table_rows("ods_orders", [{"order_id": "O001", "dt": "2026-11-01"}])
        store.set_table_rows("dwd_order_detail", [])
        updated, _ = TableCompareService().run_action(1, "启动比对")
        row = updated["_compare_result"]["manifest"][0]
        assert row["status"] == "已触发"
        assert row["priority"] == "优先"
        assert row["conflict_with"].startswith("BF-CMP-BIG-001")

    def test_rerun_compare_is_idempotent_on_draft_jobs(self):
        """重复比对不产生重复补数任务：草稿态任务重建，运行中任务保留。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "dt": "2026-11-01"},
            {"order_id": "O002", "dt": "2026-11-01"},
        ])
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "dt": "2026-11-01"},
        ])
        tc = TableCompareService()
        tc.run_action(1, "启动比对")
        first_jobs = [r["任务编号"] for r in store.rows("backfill_job")
                      if r.get("source_compare_id") == 1]
        assert len(first_jobs) == 1
        # 再次比对（数据未变）
        tc.run_action(1, "启动比对")
        second_jobs = [r["任务编号"] for r in store.rows("backfill_job")
                       if r.get("source_compare_id") == 1]
        assert second_jobs == first_jobs
        # 清单仍只有一条且与任务关联一致
        manifest = store.get_diff_partitions(1)
        assert len(manifest) == 1
        assert manifest[0]["backfill_job_no"] == first_jobs[0]

    def test_accept_priority_cancels_loser_and_resumes(self):
        """让位裁决：移除让位方草稿任务，后续行恢复顺序触发。"""
        store.set_table_rows("ods_orders", [{"order_id": "O001", "dt": "2026-11-01"}])
        store.set_table_rows("dwd_order_detail", [])
        TableCompareService().run_action(1, "启动比对")
        tc = TableCompareService()
        created, _ = tc.create_entry({
            "任务编号": "CMP-YIELD-001",
            "左表名称": "ods_payments",
            "右表名称": "dwd_order_detail",
            "比对字段": "pay_id",
            "比对批次": "RAW-20261102",
        })
        store.set_table_rows("ods_payments", [
            {"pay_id": "P001", "dt": "2026-11-01"},
            {"pay_id": "P002", "dt": "2026-11-02"},
        ])
        updated, _ = tc.run_action(created["id"], "启动比对")
        manifest = updated["_compare_result"]["manifest"]
        blocked_seq = next(r["seq"] for r in manifest if r["status"] == "冲突阻塞")
        blocked_job = next(r["backfill_job_no"] for r in manifest if r["seq"] == blocked_seq)
        row, msg = tc.resolve_conflict(created["id"], blocked_seq, "accept_priority")
        assert row["status"] == "已裁决"
        # 让位方草稿任务已移除
        assert all(r["任务编号"] != blocked_job for r in store.rows("backfill_job"))
        after = store.get_diff_partitions(created["id"])
        later = next(r for r in after if r["seq"] > blocked_seq)
        assert later["status"] == "已触发"
