"""已驗證身分對照家庭角色；權限不取自表單或文件文字。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import AUDIT_EVENTS, ROLE_LABELS
from src.store import Store
from web.access import Identity


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    label: str

    @property
    def permissions(self) -> dict[str, bool]:
        family = self.role == "family"
        member = self.role in ("family", "elder")
        return dict(can_confirm=family, can_correct=family, can_manage=family,
                    can_reject=member, can_remind=member, can_upload=member)

    @property
    def view(self) -> dict[str, str]:
        return dict(label=self.label, role=self.role,
                    role_label=ROLE_LABELS.get(self.role, "僅供查看"))


def principal_for(identity: Identity, store: Store) -> Principal:
    if identity.local:
        return Principal("local", "family", "這台電腦")
    member = store.get_member(identity.email)
    return Principal(identity.email or "", member["role"] if member else "viewer",
                     (member.get("display_name") if member else None) or identity.email or "已登入使用者")


def member_values(email: str, role: str, name: str) -> tuple[dict[str, str], str | None]:
    values = dict(email=email.strip().casefold(), role=role, display_name=name.strip())
    if len(values["email"]) > 254 or not re.fullmatch(r"[^@\s\x00-\x1f\x7f]{1,64}@[^@\s\x00-\x1f\x7f]{1,189}", values["email"]):
        return values, "請填入登入時使用的電子郵件。"
    if role not in ROLE_LABELS:
        return values, "請選擇「家人」或「長輩」。"
    if len(values["display_name"]) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in values["display_name"]):
        return values, "稱呼請使用 40 字以內的文字。"
    return values, None


def audit_rows(events: list[dict]) -> list[dict]:
    states = {"pending": "待確認", "done": "已確認", "rejected": "已退回", "completed": "已完成",
              "cancelled": "已取消", "archive": "已存檔", "review": "待複核", "failed": "讀不出來"}
    rows = []
    for event in events:
        detail = event.get("detail") or {}
        before, after = detail.get("from"), detail.get("to")
        if event["event"] in ("cancel_reminder", "restore_reminder"):
            labels = {"pending": "提醒生效", "rejected": "已取消"}
        elif event["event"] in ("complete_reminder", "undo_complete"):
            labels = {**states, "pending": "提醒生效", "completed": "已完成"}
        else:
            labels = states
        note = f"{labels.get(before, before)} → {labels.get(after, after)}" if before and after else ""
        actor = event.get("actor")
        rows.append(dict(when=event["created_at"].replace("T", " ")[:19],
                         actor="這台電腦" if actor == "local" else actor or "未記錄身分",
                         role_label=ROLE_LABELS.get(event.get("role"), ""),
                         event_label=AUDIT_EVENTS.get(event["event"], "操作"),
                         detail=note))
    return rows
