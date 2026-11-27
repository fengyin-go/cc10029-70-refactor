"""调度判定测试：排课与设备申请共用冲突检查入口、一次业务处理只给一个结论。

场景一：原来的冲突场次（排课教师/教室冲突、资源待审批/已归还口径、历史已确认排课）。
场景二：设备 A-05 归还过程中，排课与领用页面分别重试，核对结果必须一致且互斥。
"""
import threading

import pytest

from app.services.class_schedule import ClassScheduleService
from app.services.dispatch import (
    OccupancyClaim,
    check_claims,
    dispatch_lock,
    evaluate_resource,
    evaluate_schedule,
    inspect_schedule_change,
    is_device_reference,
    read_occupancy,
    row_status,
)
from app.services.resource_apply import ResourceApplyService
from app.store import store


def clear_dispatch_modules():
    for name in ("class_schedule", "resource_apply"):
        store.rows(name).clear()


@pytest.fixture(autouse=True)
def _clean_dispatch_modules():
    clear_dispatch_modules()
    yield
    clear_dispatch_modules()


class TestSharedConflictEntry:
    """排课与设备申请共用同一个冲突检查入口。"""

    def test_schedule_still_separates_teacher_and_classroom(self):
        schedule = ClassScheduleService()
        schedule.create_entry({
            "课程名称": "高等数学", "授课教师": "周老师",
            "上课时间": "周一1-2节", "上课地点": "教1-101",
        })
        store.rows("class_schedule")[-1]["status"] = "已确认"

        with pytest.raises(ValueError, match="排课冲突：教师 周老师"):
            schedule.create_entry({
                "课程名称": "线性代数", "授课教师": "周老师",
                "上课时间": "周一1-2节", "上课地点": "教1-201",
            })
        with pytest.raises(ValueError, match="教室冲突：教1-101"):
            schedule.create_entry({
                "课程名称": "线性代数", "授课教师": "吴老师",
                "上课时间": "周一1-2节", "上课地点": "教1-101",
            })
        # 换教师换教室就能登记
        entry, missing = schedule.create_entry({
            "课程名称": "线性代数", "授课教师": "吴老师",
            "上课时间": "周一1-2节", "上课地点": "教1-201",
        })
        assert missing == []
        assert entry is not None

    def test_resource_still_separates_pending_and_returned(self):
        resource = ResourceApplyService()
        entry, _ = resource.create_entry({
            "申请资源": "多媒体教室A", "用途说明": "公开课", "使用时间": "2025-10-15上午"})
        # 待审批算占用（登记后进入待审批）
        store.rows("resource_apply")[-1]["status"] = "待审批"
        with pytest.raises(ValueError, match="资源冲突"):
            resource.create_entry({
                "申请资源": "多媒体教室A", "用途说明": "补课", "使用时间": "2025-10-15上午"})
        # 已归还释放
        resource.run_action(entry["id"], "审批领用")
        resource.run_action(entry["id"], "确认归还")
        assert store.find("resource_apply", entry["id"])["status"] == "已归还"
        again, missing = resource.create_entry({
            "申请资源": "多媒体教室A", "用途说明": "补课", "使用时间": "2025-10-15上午"})
        assert missing == [] and again is not None

    def test_check_claims_is_the_single_entry(self):
        store.rows("class_schedule").append({
            "id": 1, "status": "已确认", "授课教师": "周老师",
            "上课时间": "周一1-2节", "上课地点": "教1-101",
        })
        claims = [
            OccupancyClaim("class_schedule", "授课教师", "周老师", "教师",
                           "排课冲突：教师 {value} 在 {time_slot} 时段已有已确认的排课"),
            OccupancyClaim("class_schedule", "上课地点", "教1-999", "教室",
                           "教室冲突：{value} 在 {time_slot} 时段已被占用"),
        ]
        conflicts = check_claims(claims, "周一1-2节")
        assert [item.field for item in conflicts] == ["授课教师"]
        assert conflicts[0].row_id == 1
        assert read_occupancy("class_schedule", "上课地点", "教1-101", "周一1-2节")


class TestOriginalConflictScenario:
    """复查场景一：原来的冲突场次。"""

    def test_confirmed_schedule_blocks_same_teacher_slot(self):
        schedule = ClassScheduleService()
        schedule.create_entry({
            "课程名称": "高等数学", "授课教师": "周老师", "上课时间": "周一1-2节"})
        store.rows("class_schedule")[-1]["status"] = "已确认"
        decision = evaluate_schedule({"授课教师": "周老师", "上课时间": "周一1-2节"})
        assert not decision.allowed
        assert decision.conflicts[0].module == "class_schedule"

    def test_draft_schedule_does_not_block(self):
        schedule = ClassScheduleService()
        schedule.create_entry({
            "课程名称": "高等数学", "授课教师": "周老师", "上课时间": "周一1-2节"})
        # 草稿不是已确认，不算占用
        assert evaluate_schedule({"授课教师": "周老师", "上课时间": "周一1-2节"}).allowed

    def test_historical_confirmed_schedule_is_queryable(self):
        """兼容历史已确认排课：只写了状态字段的记录仍参与冲突判定和查询。"""
        store.rows("class_schedule").append({
            "id": 9, "status": "已确认", "课程名称": "历史排课",
            "授课教师": "老教师", "上课时间": "周六1-2节", "上课地点": "教9-101",
        })
        schedule = ClassScheduleService()
        assert row_status(store.find("class_schedule", 9)) == "已确认"
        rows, total = schedule.list_entries(status="已确认")
        assert total == 1 and rows[0]["id"] == 9
        with pytest.raises(ValueError, match="排课冲突：教师 老教师"):
            schedule.create_entry({
                "课程名称": "新课", "授课教师": "老教师", "上课时间": "周六1-2节"})

    def test_historical_returned_loan_is_released(self):
        """兼容历史已归还领用：只在审批状态里写已归还的记录不再占用。"""
        store.rows("resource_apply").append({
            "id": 9, "pending": False, "abnormal": False,
            "申请资源": "投影仪B", "用途说明": "历史领用",
            "使用时间": "周日1-2节", "审批状态": "已归还",
        })
        resource = ResourceApplyService()
        assert row_status(store.find("resource_apply", 9)) == "已归还"
        rows, total = resource.list_entries(status="已归还")
        assert total == 1 and rows[0]["id"] == 9
        entry, missing = resource.create_entry({
            "申请资源": "投影仪B", "用途说明": "新领用", "使用时间": "周日1-2节"})
        assert missing == [] and entry is not None


class TestDeviceReturnInProgress:
    """复查场景二：设备 A-05 的归还还在途中。"""

    def _approved_loan(self) -> dict:
        resource = ResourceApplyService()
        loan, _ = resource.create_entry({
            "申请资源": "A-05", "用途说明": "实验课", "使用时间": "周三5-6节"})
        resource.run_action(loan["id"], "审批领用")
        assert loan["status"] == "已批准"
        return loan

    def test_schedule_page_cannot_confirm_while_return_in_progress(self):
        self._approved_loan()
        schedule = ClassScheduleService()
        entry, _ = schedule.create_entry({
            "课程名称": "物理实验", "授课教师": "李老师",
            "上课时间": "周三5-6节", "上课地点": "A-05"})

        inspection = inspect_schedule_change(entry, exclude_id=entry["id"])
        assert not inspection.allowed
        assert inspection.qualification.required
        assert not inspection.qualification.qualified
        assert "归还尚未完成" in inspection.qualification.reason
        # 场次确认与设备资格来自同一次读取
        assert not inspection.session.confirmed

        result, message = schedule.run_action(entry["id"], "确认排课")
        assert result is None
        assert "归还尚未完成" in message

    def test_resource_page_also_sees_the_same_occupancy(self):
        self._approved_loan()
        decision = evaluate_resource({"申请资源": "A-05", "使用时间": "周三5-6节"})
        assert not decision.allowed
        assert decision.conflicts[0].value == "A-05"

    def test_both_duty_pages_can_review_the_same_request(self):
        """两位值班人分别从排课和领用页面核对同一次请求，结论一致。"""
        self._approved_loan()
        schedule = ClassScheduleService()
        entry, _ = schedule.create_entry({
            "课程名称": "物理实验", "授课教师": "李老师",
            "上课时间": "周三5-6节", "上课地点": "A-05"})

        schedule_view = evaluate_schedule(
            entry, exclude_id=entry["id"], include_shared=True)
        resource_view = evaluate_resource(
            {"申请资源": "A-05", "使用时间": "周三5-6节"})
        assert schedule_view.allowed is False
        assert resource_view.allowed is False
        assert "A-05" in schedule_view.message
        assert "A-05" in resource_view.message

    def test_retry_after_return_is_allowed(self):
        """归还完成后重试同一请求，两个页面都放行。"""
        loan = self._approved_loan()
        resource = ResourceApplyService()
        resource.run_action(loan["id"], "确认归还")
        assert store.find("resource_apply", loan["id"])["status"] == "已归还"

        schedule = ClassScheduleService()
        entry, _ = schedule.create_entry({
            "课程名称": "物理实验", "授课教师": "李老师",
            "上课时间": "周三5-6节", "上课地点": "A-05"})
        inspection = inspect_schedule_change(entry, exclude_id=entry["id"])
        assert inspection.allowed
        result, _ = schedule.run_action(entry["id"], "确认排课")
        assert result is not None and result["status"] == "已确认"
        assert evaluate_resource({"申请资源": "A-05", "使用时间": "周三5-6节"}).allowed is False

    def test_schedule_change_writes_resource_todo(self):
        schedule = ClassScheduleService()
        entry, _ = schedule.create_entry({
            "课程名称": "物理实验", "授课教师": "李老师",
            "上课时间": "周三5-6节", "上课地点": "A-05"})
        result, _ = schedule.run_action(entry["id"], "执行调整")
        assert result is not None and result["status"] == "已确认"
        todos = [
            row for row in store.rows("resource_apply")
            if f"[class_schedule:{entry['id']}]" in str(row.get("备注说明", ""))
        ]
        assert len(todos) == 1
        assert todos[0]["申请资源"] == "A-05"
        assert todos[0]["pending"] is True
        # 重复改课不会重复生成待办
        schedule.run_action(entry["id"], "执行调整")
        todos = [
            row for row in store.rows("resource_apply")
            if f"[class_schedule:{entry['id']}]" in str(row.get("备注说明", ""))
        ]
        assert len(todos) == 1

    def test_plain_classroom_does_not_cross_read_as_device(self):
        """普通教室不是可领用设备，排课仍按原来的教室口径判定。"""
        assert is_device_reference("A-05")
        assert is_device_reference("设备A-05")
        assert not is_device_reference("教1-101")
        assert not is_device_reference("101")

        resource = ResourceApplyService()
        resource.create_entry({
            "申请资源": "教1-101", "用途说明": "公开课", "使用时间": "周四1-2节"})
        store.rows("resource_apply")[-1]["status"] = "已批准"
        # 排课登记不跨模块读取普通教室占用，保持原有口径
        entry, missing = ClassScheduleService().create_entry({
            "课程名称": "语文", "授课教师": "王老师",
            "上课时间": "周四1-2节", "上课地点": "教1-101"})
        assert missing == [] and entry is not None


class TestSingleDecisionForConflictingClaims:
    """两份使用申请冲突时只能有一份可办结果。"""

    def test_two_pending_claims_only_one_can_be_approved(self):
        resource = ResourceApplyService()
        first, _ = resource.create_entry({
            "申请资源": "B-01", "用途说明": "甲", "使用时间": "周四1-2节"})
        second, _ = resource.create_entry({
            "申请资源": "B-01", "用途说明": "乙", "使用时间": "周四1-2节"})

        approved, first_msg = resource.run_action(first["id"], "审批领用")
        assert approved is not None and approved["status"] == "已批准"

        rejected, second_msg = resource.run_action(second["id"], "审批领用")
        assert rejected is None
        assert "资源冲突" in second_msg
        assert store.find("resource_apply", second["id"])["status"] != "已批准"

    def test_conflicting_schedule_only_one_can_be_confirmed(self):
        schedule = ClassScheduleService()
        first, _ = schedule.create_entry({
            "课程名称": "甲", "授课教师": "赵老师",
            "上课时间": "周五1-2节", "上课地点": "教2-101"})
        second, _ = schedule.create_entry({
            "课程名称": "乙", "授课教师": "赵老师",
            "上课时间": "周五1-2节", "上课地点": "教2-102"})

        ok_one, _ = schedule.run_action(first["id"], "确认排课")
        assert ok_one is not None and ok_one["status"] == "已确认"
        ok_two, message = schedule.run_action(second["id"], "确认排课")
        assert ok_two is None
        assert "排课冲突" in message

    def test_no_confirmation_from_two_inconsistent_observations(self):
        """处理中不能根据两次不一致的占用观察都给出确认。

        并发确认两份互斥申请时，只有一个线程能拿到确认；其余线程在锁内重新观察
        到已落定的占用后得到否定结论。
        """
        schedule = ClassScheduleService()
        entries = [
            schedule.create_entry({
                "课程名称": f"并发{i}", "授课教师": "钱老师",
                "上课时间": "周六3-4节", "上课地点": f"教3-{100 + i}"})[0]
            for i in range(4)
        ]
        results: list[bool] = []
        barrier = threading.Barrier(len(entries))

        def confirm(entry_id: int) -> None:
            barrier.wait()
            result, _ = schedule.run_action(entry_id, "确认排课")
            results.append(result is not None)

        threads = [threading.Thread(target=confirm, args=(entry["id"],)) for entry in entries]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in results if item) == 1
        confirmed = [row for row in store.rows("class_schedule") if row["status"] == "已确认"]
        assert len(confirmed) == 1

    def test_decision_reports_all_conflicts(self):
        store.rows("class_schedule").extend([
            {"id": 1, "status": "已确认", "授课教师": "孙老师",
             "上课时间": "周一3-4节", "上课地点": "教4-101"},
            {"id": 2, "status": "已确认", "授课教师": "李老师",
             "上课时间": "周一3-4节", "上课地点": "教4-102"},
        ])
        decision = evaluate_schedule({
            "授课教师": "孙老师", "上课时间": "周一3-4节", "上课地点": "教4-102"})
        assert not decision.allowed
        assert len(decision.conflicts) == 2
        assert {item.field for item in decision.conflicts} == {"授课教师", "上课地点"}
        payload = decision.as_dict()
        assert payload["allowed"] is False
        assert len(payload["conflicts"]) == 2


class TestBusinessLockScope:
    """检查、读取、落库必须在同一次业务处理内。"""

    def test_lock_is_reentrant(self):
        with dispatch_lock:
            with dispatch_lock:
                assert dispatch_lock is not None


class TestConcurrentConflictingApplications:
    """并发办理：两份互斥的使用申请只有一个可办结果。"""

    def test_concurrent_approvals_yield_single_winner(self):
        resource = ResourceApplyService()
        entries = [
            resource.create_entry({
                "申请资源": "C-05", "用途说明": f"并发{i}",
                "使用时间": "周日3-4节"})[0]
            for i in range(4)
        ]
        results: list[bool] = []
        barrier = threading.Barrier(len(entries))

        def approve(entry_id: int) -> None:
            barrier.wait()
            result, _ = resource.run_action(entry_id, "审批领用")
            results.append(result is not None)

        threads = [threading.Thread(target=approve, args=(entry["id"],)) for entry in entries]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in results if item) == 1
        approved = [
            row for row in store.rows("resource_apply")
            if str(row.get("申请资源")) == "C-05" and row["status"] == "已批准"
        ]
        assert len(approved) == 1
