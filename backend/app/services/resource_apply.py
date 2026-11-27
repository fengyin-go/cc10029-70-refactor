"""教学资源申请领域服务：申请登记、审批、归还，占用判定统一走调度判定。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.dispatch import (
    dispatch_lock,
    ensure_resource_available,
    evaluate_resource,
    row_status,
)


class ResourceApplyService(BaseModuleService):
    MODULE = "resource_apply"
    ENTITY = "资源申请"
    OPERATION_TERM = "审批领用"
    LIST_FIELDS = ["申请资源", "用途说明", "使用时间", "归还时间", "申请教师", "审批状态", "领取地点", "备注说明"]
    REQUIRED_FIELDS = ["申请资源", "用途说明", "使用时间"]
    STATUS_ORDER = ["草稿", "待审批", "已批准", "已归还"]
    ACTION_RULES = {"审批领用": "已批准", "退回申请": "草稿", "确认归还": "已归还"}
    NEGATIVE_ACTIONS = []

    def entry_status(self, entry: dict[str, Any]) -> str:
        """读取领用状态，兼容历史已归还领用只写在审批状态里的记录。"""
        return row_status(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建资源申请：检测同一资源同时间段的冲突。

        待审批和已批准都算占用、已归还释放的口径留在调度判定里；
        检查与登记在同一把业务锁内完成，避免并发登记绕开冲突检查。
        """
        with dispatch_lock:
            ensure_resource_available(values)
            return super().create_entry(values)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含状态约束和占用复核。"""
        # 读取、占用观察、状态落库是同一次业务处理，不能被另一次审批插队。
        with dispatch_lock:
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

            # 审批领用：必须是待审批
            if action == "审批领用":
                if self.entry_status(entry) not in ("待审批", "草稿"):
                    return None, f"{self.ENTITY} {entry_id} 当前状态为'{self.entry_status(entry)}'，不可审批"
                # 两份使用申请争同一资源同一时段时，只有先完成这次业务处理的一份能通过；
                # 后一份在锁内重新观察占用，拿到否定的判定结果。办理时只认已批准的占用，
                # 否则两份待审批会互相等待，变成两份都办不了。
                decision = evaluate_resource(
                    entry, exclude_id=entry_id, committed_only=True)
                if not decision.allowed:
                    return None, decision.message

            # 确认归还：必须是已批准状态
            if action == "确认归还":
                if self.entry_status(entry) != "已批准":
                    return None, f"{self.ENTITY} {entry_id} 必须已批准，才能确认归还"

            result_entry, message = super().run_action(entry_id, action)
            if result_entry is not None and action in ("审批领用", "确认归还"):
                # 同步审批字段，历史数据里只读审批状态的查询也能看到最新结果。
                result_entry["审批状态"] = result_entry.get("status", "")
            return result_entry, message
