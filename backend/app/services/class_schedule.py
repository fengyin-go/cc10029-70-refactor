"""排课编排领域服务：排课单登记、确认、调整，占用判定统一走调度判定。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.dispatch import (
    dispatch_lock,
    ensure_schedule_available,
    ensure_schedule_committable,
    inspect_schedule_change,
    row_status,
    update_resource_todo,
)


class ClassScheduleService(BaseModuleService):
    MODULE = "class_schedule"
    ENTITY = "排课单"
    OPERATION_TERM = "确认排课"
    LIST_FIELDS = ["课程名称", "授课教师", "上课时间", "上课地点", "授课班级", "起止周次", "课时数量", "备注说明"]
    REQUIRED_FIELDS = ["课程名称", "授课教师", "上课时间"]
    STATUS_ORDER = ["草稿", "待确认", "已确认", "已调整"]
    ACTION_RULES = {"确认排课": "已确认", "申请调整": "已调整", "执行调整": "已确认"}
    NEGATIVE_ACTIONS = []

    def entry_status(self, entry: dict[str, Any]) -> str:
        """读取排课状态，兼容历史已确认排课只落在状态字段上的记录。"""
        return row_status(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建排课单：检测教师和教室在同一时段的冲突。

        冲突检查统一调用调度判定，教师和教室仍是两条独立的占用声明。
        检查与登记在同一把业务锁内完成，避免同一份占用被观察两次后都放行。
        """
        with dispatch_lock:
            ensure_schedule_available(values)
            return super().create_entry(values)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，确认类动作在落库前重新判定占用。"""
        # 整段动作（读取、判定、状态落库、待办更新）属于同一次业务处理。
        with dispatch_lock:
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

            # 确认排课：必须是待确认
            if action == "确认排课":
                if self.entry_status(entry) not in ("待确认", "草稿", "已调整"):
                    return None, f"{self.ENTITY} {entry_id} 当前状态为'{self.entry_status(entry)}'，不可确认"
                # 两位值班人可能同时确认同一时段的两份请求：落库前以已确认排课为准重新判定，
                # 只有先拿到确认的一份能继续办理；设备占用还要读取资源领用。
                try:
                    ensure_schedule_committable(entry, exclude_id=entry_id)
                except ValueError as error:
                    return None, str(error)

            # 执行调整（改课）：设备资格与场次确认必须在同一次业务处理内读取，
            # 判定通过后再落库，并把结果写入资源领用待办。
            if action == "执行调整":
                inspection = inspect_schedule_change(entry, exclude_id=entry_id)
                if not inspection.allowed:
                    return None, inspection.message
                result_entry, message = super().run_action(entry_id, action)
                if result_entry is not None:
                    update_resource_todo(result_entry)
                return result_entry, message

            return super().run_action(entry_id, action)
