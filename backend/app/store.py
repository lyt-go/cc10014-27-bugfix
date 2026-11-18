"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # 差异分区清单: "compare_id" -> [差异分区记录, ...]
        self._diff_partitions: dict[str, list[dict[str, Any]]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        # 注意：不能用 setdefault——测试或清理逻辑可能直接给 _tables[module] 赋新列表，
        # 此前取到旧引用的调用方再 append 会写到已脱管的列表里，持久化对不上。
        if module not in self._tables:
            self._tables[module] = []
        return self._tables[module]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))

    # ---- 差异分区清单 ----

    def replace_diff_partitions(self, compare_id: int, rows: list[dict[str, Any]]) -> None:
        """整单替换差异分区清单：先构建完整清单再落库，保证与补数记录一致。"""
        self._diff_partitions[str(compare_id)] = [dict(row) for row in rows]

    def get_diff_partitions(self, compare_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self._diff_partitions.get(str(compare_id), [])]

    def all_diff_partitions(self) -> dict[str, list[dict[str, Any]]]:
        return {key: [dict(row) for row in rows] for key, rows in self._diff_partitions.items()}

    def find_diff_partition(self, compare_id: int, seq: int) -> dict[str, Any] | None:
        for row in self._diff_partitions.get(str(compare_id), []):
            if int(row.get("seq", -1)) == seq:
                return row
        return None

    def clear_diff_partitions(self) -> None:
        self._diff_partitions.clear()


store = Store()
