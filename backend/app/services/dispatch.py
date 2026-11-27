"""调度判定：集中排课与教学资源申请的占用读取、冲突检查和设备/场次确认。

在此之前，排课和资源申请各自在 ``create_entry`` 里直接遍历 ``store`` 判断占用，
两套口径容易漂移：什么状态算占用、哪个字段代表同一资源、历史数据怎么读，
都散落在各自模块里。本模块把这些读取收拢成唯一入口：

* ``OccupancyClaim`` 把业务字段翻译成一条“某模块的某字段在某时段被谁占用”；
* ``read_occupancy`` 负责唯一的占用读取口径；
* ``check_claims`` / ``ensure_claims_available`` 是排课与设备申请共用的冲突检查入口；
* ``dispatch_lock`` 保证“检查 + 确认”属于同一次业务处理，避免两次不一致的占用观察
  各给出一次确认。

原有口径保持不变：排课仍区分授课教师和教室，资源申请仍区分待审批与已归还。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from app.store import store

# 各模块的时段字段：占用只在同一时段内成立。
TIME_FIELDS: dict[str, str] = {
    "class_schedule": "上课时间",
    "resource_apply": "使用时间",
}

# 各模块“仍占用资源”的状态。排课只看已确认；资源领用待审批/已批准占用，已归还释放。
# “归还还在途中”在数据上就是那条领用仍是已批准、还没走到已归还，
# 因此它照常算占用。这里就是唯一的占用口径，两个领域服务不再各自维护一份。
OCCUPANCY_STATUSES: dict[str, tuple[str, ...]] = {
    "class_schedule": ("已确认",),
    "resource_apply": ("待审批", "已批准"),
}

# 办理（确认排课 / 审批领用）时只认已经落定的占用：已确认的排课、已批准的领用。
# 两份互斥的使用申请里，先办理的一份落定为已批准，后一份在锁内重新观察就会拿到
# 否定的判定结果；如果待审批也参与办理判定，两份会互相等待，谁都办不成。
COMMITTED_STATUSES: dict[str, tuple[str, ...]] = {
    "class_schedule": ("已确认",),
    "resource_apply": ("已批准",),
}

# 状态字段：统一状态 + 历史数据沿用的审批状态。
STATUS_FIELD = "status"
APPROVAL_STATUS_FIELD = "审批状态"

# 判定的业务处理锁：检查、占用读取、状态落库、待办更新必须在同一临界区完成。
# 使用 RLock 是为了让领域服务在外层持锁时，判定函数内部还能再次进入。
dispatch_lock = threading.RLock()

_DEVICE_TEXT = re.compile(r"^(?:设备)?[-_ ]?([A-Za-z]{0,4})[-_ ]?(\d{1,6})$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_device_reference(value: Any) -> bool:
    """判断一个地点值是否是可领用设备编号，例如 ``A-05``、``设备A-05``。

    普通教室（如“教1-101”）也会参与排课占用判定，但只有设备编号才需要
    读取设备资格并生成资源领用待办。
    """
    text = _text(value)
    if not text:
        return False
    match = _DEVICE_TEXT.fullmatch(text)
    if match is None:
        return False
    prefix, number = match.groups()
    # 纯数字的教室编号不当作设备；至少要有设备字母前缀或“设备”字样。
    return bool(prefix) or text.startswith("设备")


def row_status(row: Mapping[str, Any]) -> str:
    """读取记录的业务状态，兼容历史数据只写“审批状态”的情况。

    * 任一字段已经写明“已归还”，归还结果优先，记录不再占用资源；
    * 否则以统一的 ``status`` 为准；
    * 历史已归还领用可能没有 ``status``，此时退回读 ``审批状态``。

    排课的“已确认”和历史沿用直接写在 ``status`` 上，两条读法走同一个入口。
    """
    status = _text(row.get(STATUS_FIELD))
    approval = _text(row.get(APPROVAL_STATUS_FIELD))
    if status == "已归还" or approval == "已归还":
        return "已归还"
    if status:
        return status
    if approval in {"待审批", "已批准", "已归还"}:
        return approval
    return status or approval


@dataclass(frozen=True)
class OccupancyClaim:
    """一条占用声明：某模块的某字段在当前时段被某个资源名占用。"""

    module: str
    field: str
    value: str
    label: str
    message: str

    def normalized(self) -> "OccupancyClaim | None":
        value = _text(self.value)
        if not value:
            return None
        return replace(self, value=value)


@dataclass(frozen=True)
class OccupancyConflict:
    module: str
    field: str
    value: str
    time_slot: str
    message: str
    row_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "field": self.field,
            "value": self.value,
            "time_slot": self.time_slot,
            "message": self.message,
            "row_id": self.row_id,
        }


@dataclass(frozen=True)
class OccupancyDecision:
    """能否继续办理的判定结果，供两个页面的值班人核对。"""

    allowed: bool
    message: str
    conflicts: tuple[OccupancyConflict, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "message": self.message,
            "conflicts": [item.as_dict() for item in self.conflicts],
        }


@dataclass(frozen=True)
class DeviceQualification:
    """排课改课时读取的设备资格。"""

    device: str
    required: bool
    qualified: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "required": self.required,
            "qualified": self.qualified,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SessionConfirmation:
    """排课改课时对目标场次的确认结果。"""

    time_slot: str
    confirmed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "time_slot": self.time_slot,
            "confirmed": self.confirmed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScheduleInspection:
    """排课改课的一次完整判定：资格、场次和冲突结论。"""

    decision: OccupancyDecision
    qualification: DeviceQualification
    session: SessionConfirmation

    @property
    def allowed(self) -> bool:
        return self.decision.allowed and self.qualification.qualified and self.session.confirmed

    @property
    def message(self) -> str:
        if not self.qualification.qualified and self.qualification.required:
            return self.qualification.reason
        if not self.session.confirmed:
            return self.session.reason
        return self.decision.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "message": self.message,
            "conflicts": [item.as_dict() for item in self.decision.conflicts],
            "qualification": self.qualification.as_dict(),
            "session": self.session.as_dict(),
        }


class OccupancyConflictError(ValueError):
    """共用的冲突异常：保留各模块原有的提示语，便于页面直接展示。"""

    def __init__(self, conflicts: Iterable[OccupancyConflict]):
        self.conflicts = tuple(conflicts)
        message = self.conflicts[0].message if self.conflicts else "占用冲突"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 唯一的占用读取入口
# ---------------------------------------------------------------------------

def read_occupancy(
    module: str,
    field: str,
    value: Any,
    time_slot: Any,
    *,
    exclude_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """读取指定模块中仍占用该资源、该时段的记录。

    ``exclude_id`` 用于确认动作排除自身记录，避免把“自己占用自己”当成冲突。
    ``statuses`` 供确认动作收窄口径（例如审批领用时只看已批准），默认取模块口径。
    """
    value_text = _text(value)
    slot_text = _text(time_slot)
    if not value_text or not slot_text:
        return []
    active_statuses = statuses if statuses is not None else OCCUPANCY_STATUSES.get(module, ())
    time_field = TIME_FIELDS.get(module)
    if not active_statuses or time_field is None:
        return []

    rows: list[dict[str, Any]] = []
    for row in store.rows(module):
        if exclude_id is not None and int(row.get("id", 0)) == int(exclude_id):
            continue
        if row_status(row) not in active_statuses:
            # 已归还的领用、草稿/待确认/已调整的排课都不算占用。
            continue
        if _text(row.get(field)) != value_text:
            continue
        if _text(row.get(time_field)) != slot_text:
            continue
        rows.append(row)
    return rows


def check_claims(
    claims: Iterable[OccupancyClaim],
    time_slot: Any,
    *,
    exclude_ids: Mapping[str, int] | None = None,
    status_overrides: Mapping[str, tuple[str, ...]] | None = None,
) -> list[OccupancyConflict]:
    """排课和设备申请共用的冲突检查入口。

    每个模块只贡献自己的 claim（排课的教师/教室、资源申请的资源），
    占用读取和冲突提示统一在这里完成。
    """
    slot_text = _text(time_slot)
    if not slot_text:
        return []
    excluded = exclude_ids or {}
    overrides = status_overrides or {}
    conflicts: list[OccupancyConflict] = []
    seen: set[tuple[str, str, str]] = set()

    for claim in claims:
        item = claim.normalized()
        if item is None:
            continue
        key = (item.module, item.field, item.value)
        if key in seen:
            continue
        rows = read_occupancy(
            item.module,
            item.field,
            item.value,
            slot_text,
            exclude_id=excluded.get(item.module),
            statuses=overrides.get(item.module),
        )
        if not rows:
            continue
        seen.add(key)
        conflicts.append(OccupancyConflict(
            module=item.module,
            field=item.field,
            value=item.value,
            time_slot=slot_text,
            message=item.message.format(
                label=item.label, value=item.value, time_slot=slot_text),
            row_id=int(rows[0].get("id", 0)) or None,
        ))
    return conflicts


def evaluate_claims(
    claims: Iterable[OccupancyClaim],
    time_slot: Any,
    *,
    exclude_ids: Mapping[str, int] | None = None,
    status_overrides: Mapping[str, tuple[str, ...]] | None = None,
) -> OccupancyDecision:
    conflicts = check_claims(
        claims, time_slot,
        exclude_ids=exclude_ids, status_overrides=status_overrides)
    if conflicts:
        return OccupancyDecision(
            allowed=False,
            message=conflicts[0].message,
            conflicts=tuple(conflicts),
        )
    return OccupancyDecision(allowed=True, message="该次请求可以继续办理")


def ensure_claims_available(
    claims: Iterable[OccupancyClaim],
    time_slot: Any,
    *,
    exclude_ids: Mapping[str, int] | None = None,
    status_overrides: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """冲突检查入口的抛错版本：原有登记路径继续用异常表达冲突。"""
    conflicts = check_claims(
        claims, time_slot,
        exclude_ids=exclude_ids, status_overrides=status_overrides)
    if conflicts:
        raise OccupancyConflictError(conflicts)


# ---------------------------------------------------------------------------
# 业务字段 -> 占用声明
# ---------------------------------------------------------------------------

def schedule_claims(
    values: Mapping[str, Any], *, include_shared: bool = False
) -> list[OccupancyClaim]:
    """排课单的占用声明：教师和教室各算一条，保持原有区分。

    ``include_shared`` 用于办理/核对场景：设备编号（如 A-05）还要读取资源领用里
    尚未归还的占用，让排课页面和领用页面看到同一份设备占用。登记时不开共享读取，
    保持原有的登记口径（只看排课自身）。
    """
    teacher = _text(values.get("授课教师"))
    classroom = _text(values.get("上课地点"))
    claims: list[OccupancyClaim] = []

    if teacher:
        claims.append(OccupancyClaim(
            module="class_schedule",
            field="授课教师",
            value=teacher,
            label="教师",
            message="排课冲突：教师 {value} 在 {time_slot} 时段已有已确认的排课",
        ))
    if classroom:
        claims.append(OccupancyClaim(
            module="class_schedule",
            field="上课地点",
            value=classroom,
            label="教室",
            message="教室冲突：{value} 在 {time_slot} 时段已被占用",
        ))
        # 普通教室不是可领用资源，只有设备编号才跨模块读取，保持排课原有的教室口径。
        if include_shared and is_device_reference(classroom):
            claims.append(OccupancyClaim(
                module="resource_apply",
                field="申请资源",
                value=classroom,
                label="教室",
                message="教室冲突：{value} 在 {time_slot} 时段仍被领用（归还尚未完成）",
            ))
    return claims


def resource_claims(
    values: Mapping[str, Any], *, include_shared: bool = False
) -> list[OccupancyClaim]:
    """资源申请的占用声明：只按申请资源一条，保持原有口径。

    ``include_shared`` 用于办理/核对场景：设备编号（如 A-05）还要读取排课里
    已确认的占用。登记时不开共享读取，保持原有的登记口径（只看资源申请自身）。
    """
    resource = _text(values.get("申请资源"))
    claims: list[OccupancyClaim] = []
    if resource:
        claims.append(OccupancyClaim(
            module="resource_apply",
            field="申请资源",
            value=resource,
            label="资源",
            message="资源冲突：{value} 在 {time_slot} 时段已被申请或批准",
        ))
        if include_shared and is_device_reference(resource):
            claims.append(OccupancyClaim(
                module="class_schedule",
                field="上课地点",
                value=resource,
                label="资源",
                message="资源冲突：{value} 在 {time_slot} 时段已被已确认的排课占用",
            ))
    return claims


def _exclude_map(module: str, exclude_id: int | None) -> dict[str, int]:
    return {module: exclude_id} if exclude_id is not None else {}


def evaluate_schedule(
    values: Mapping[str, Any],
    *,
    exclude_id: int | None = None,
    include_shared: bool = False,
) -> OccupancyDecision:
    return evaluate_claims(
        schedule_claims(values, include_shared=include_shared),
        values.get("上课时间"),
        exclude_ids=_exclude_map("class_schedule", exclude_id),
    )


def evaluate_resource(
    values: Mapping[str, Any],
    *,
    exclude_id: int | None = None,
    committed_only: bool = False,
) -> OccupancyDecision:
    """资源占用判定。

    ``committed_only`` 用于办理与核对动作：只把已批准的领用当作占用，
    让两份互斥的使用申请中先办理的一份通过、后一份得到否定结果，
    而不是两份待审批互相等待、谁都无法办理。
    """
    exclude_ids = _exclude_map("resource_apply", exclude_id)
    claims = resource_claims(values, include_shared=True)
    if committed_only:
        conflicts = committed_conflicts(
            claims, values.get("使用时间"), exclude_ids=exclude_ids)
        if conflicts:
            return OccupancyDecision(
                allowed=False, message=conflicts[0].message, conflicts=tuple(conflicts))
        return OccupancyDecision(allowed=True, message="该次请求可以继续办理")
    return evaluate_claims(
        claims,
        values.get("使用时间"),
        exclude_ids=exclude_ids,
    )


def committed_conflicts(
    claims: Iterable[OccupancyClaim],
    time_slot: Any,
    *,
    exclude_ids: Mapping[str, int] | None = None,
    status_overrides: Mapping[str, tuple[str, ...]] | None = None,
) -> list[OccupancyConflict]:
    """办理时使用的冲突结论：只看已经落定的占用。

    默认两个模块都取 ``COMMITTED_STATUSES``；需要保留某模块原有占用口径时，
    用 ``status_overrides`` 单独指定（例如资源申请登记仍按待审批/已批准判定）。
    """
    overrides = dict(COMMITTED_STATUSES)
    overrides.update(status_overrides or {})
    return check_claims(
        claims, time_slot, exclude_ids=exclude_ids, status_overrides=overrides)


def ensure_schedule_available(
    values: Mapping[str, Any],
    *,
    exclude_id: int | None = None,
    include_shared: bool = False,
) -> None:
    ensure_claims_available(
        schedule_claims(values, include_shared=include_shared),
        values.get("上课时间"),
        exclude_ids=_exclude_map("class_schedule", exclude_id),
    )


def ensure_schedule_committable(
    values: Mapping[str, Any], *, exclude_id: int | None = None
) -> None:
    """确认排课前的占用复核：与审批领用一样只认已落定占用，并读取设备共享占用。

    只认已确认排课 / 已批准领用，避免两份待确认请求互相等待；
    设备编号还要读取资源领用的已批准占用（归还尚未完成的领用会挡住排课确认）。
    """
    conflicts = committed_conflicts(
        schedule_claims(values, include_shared=True),
        values.get("上课时间"),
        exclude_ids=_exclude_map("class_schedule", exclude_id),
    )
    if conflicts:
        raise OccupancyConflictError(conflicts)


def ensure_resource_available(
    values: Mapping[str, Any], *, exclude_id: int | None = None
) -> None:
    ensure_claims_available(
        resource_claims(values),
        values.get("使用时间"),
        exclude_ids=_exclude_map("resource_apply", exclude_id),
    )


# ---------------------------------------------------------------------------
# 排课改课：设备资格 + 场次确认一次读取
# ---------------------------------------------------------------------------

def read_device_qualification(
    device: Any,
    time_slot: Any,
    *,
    exclude_ids: Mapping[str, int] | None = None,
    statuses: tuple[str, ...] | None = None,
) -> DeviceQualification:
    """读取设备资格：设备在同一时段没有仍被领用/未归还的记录。

    默认按办理口径只看已批准的领用：已批准意味着设备已经借出、归还尚未完成。
    """
    device_text = _text(device)
    slot_text = _text(time_slot)
    if not is_device_reference(device_text):
        return DeviceQualification(
            device=device_text,
            required=False,
            qualified=True,
            reason="未指定设备，无需读取设备资格",
        )
    if not slot_text:
        return DeviceQualification(
            device=device_text,
            required=True,
            qualified=False,
            reason=f"设备 {device_text} 缺少场次时间，无法确认资格",
        )
    excluded = exclude_ids or {}
    occupied = read_occupancy(
        "resource_apply", "申请资源", device_text, slot_text,
        exclude_id=excluded.get("resource_apply"),
        statuses=statuses if statuses is not None else COMMITTED_STATUSES["resource_apply"],
    )
    if occupied:
        return DeviceQualification(
            device=device_text,
            required=True,
            qualified=False,
            reason=f"设备 {device_text} 在 {slot_text} 时段仍被领用（归还尚未完成）",
        )
    return DeviceQualification(
        device=device_text,
        required=True,
        qualified=True,
        reason=f"设备 {device_text} 资格有效，场次可确认",
    )


def inspect_schedule_change(
    values: Mapping[str, Any],
    *,
    exclude_id: int | None = None,
    time_slot: Any | None = None,
) -> ScheduleInspection:
    """排课改课的统一判定：设备资格与场次确认在同一次业务处理内读取。

    外层调用方（确认/执行调整动作）会持有 ``dispatch_lock``；这里再次进入 RLock，
    保证资格读取、场次读取和冲突结论来自同一份占用观察，不会拿两次不一致的
    观察结果各给一次确认。
    """
    with dispatch_lock:
        slot_text = _text(time_slot if time_slot is not None else values.get("上课时间"))
        exclude_ids = _exclude_map("class_schedule", exclude_id)
        claims = schedule_claims(values, include_shared=True)
        # 核对与确认同一口径：只看已经落定的占用，避免两份待办互相挡住对方，
        # 也避免核对说“不能办”而确认动作却说“能办”。
        decision_conflicts = committed_conflicts(
            claims, slot_text, exclude_ids=exclude_ids)
        decision = (
            OccupancyDecision(
                allowed=False,
                message=decision_conflicts[0].message,
                conflicts=tuple(decision_conflicts),
            )
            if decision_conflicts
            else OccupancyDecision(allowed=True, message="该次请求可以继续办理")
        )

        qualification = read_device_qualification(
            values.get("上课地点"), slot_text, exclude_ids=exclude_ids)

        # 场次确认只看“教室/设备”这条占用，不把教师冲突误当成场次占用。
        session_claims = [
            claim for claim in claims
            if claim.field in ("上课地点", "申请资源")
        ]
        session_conflicts = committed_conflicts(
            session_claims, slot_text, exclude_ids=exclude_ids)
        if session_conflicts:
            session = SessionConfirmation(
                time_slot=slot_text,
                confirmed=False,
                reason=session_conflicts[0].message,
            )
        elif qualification.required and not qualification.qualified:
            session = SessionConfirmation(
                time_slot=slot_text,
                confirmed=False,
                reason=qualification.reason,
            )
        elif not slot_text:
            session = SessionConfirmation(
                time_slot=slot_text,
                confirmed=False,
                reason="未指定场次时间，无法确认场次",
            )
        else:
            session = SessionConfirmation(
                time_slot=slot_text,
                confirmed=True,
                reason=f"场次 {slot_text} 可以确认",
            )

        return ScheduleInspection(
            decision=decision,
            qualification=qualification,
            session=session,
        )


# ---------------------------------------------------------------------------
# 排课改课结果 -> 资源领用待办
# ---------------------------------------------------------------------------

def update_resource_todo(schedule_entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """把排课改课的结果写入资源领用待办。

    只有设备编号（如 A-05）才需要领用待办。已存在同一排课生成的待办时更新，
    已有同设备同时段的草稿领用单时复用，否则新建一条草稿待办。
    """
    with dispatch_lock:
        device = _text(schedule_entry.get("上课地点"))
        slot_text = _text(schedule_entry.get("上课时间"))
        if not slot_text or not is_device_reference(device):
            return None

        schedule_id = int(schedule_entry.get("id", 0)) or 0
        marker = f"[class_schedule:{schedule_id}]"
        rows = store.rows("resource_apply")
        note = f"排课单 #{schedule_id} 改课已确认，请核对设备领用 {marker}"

        for row in rows:
            if marker in _text(row.get("备注说明")):
                row["备注说明"] = _merge_note(row.get("备注说明"), note)
                row["pending"] = True
                return row

        for row in rows:
            if (row_status(row) == "草稿"
                    and _text(row.get("申请资源")) == device
                    and _text(row.get("使用时间")) == slot_text):
                row["备注说明"] = _merge_note(row.get("备注说明"), note)
                row["pending"] = True
                return row

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        row = {
            "id": new_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "申请资源": device,
            "用途说明": f"排课单 #{schedule_id} 改课设备领用待办",
            "使用时间": slot_text,
            "归还时间": "",
            "申请教师": _text(schedule_entry.get("授课教师")),
            APPROVAL_STATUS_FIELD: "待办",
            "领取地点": _text(schedule_entry.get("上课地点")),
            "备注说明": note,
        }
        rows.append(row)
        return row


def _merge_note(existing: Any, note: str) -> str:
    text = _text(existing)
    if not text:
        return note
    if note in text:
        return text
    return f"{text}；{note}"
