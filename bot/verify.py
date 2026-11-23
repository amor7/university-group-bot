"""Pure student-code verification (no Telegram calls; the clock is passed in as `now`)."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol

from bot.importer import CODE_MAX_LEN, CODE_MIN_LEN
from bot.normalize import normalize_code
from bot.repo import CodeAlreadyBound, Repo, StudentNotFound


class VerifyStatus(enum.Enum):
    OK = "ok"
    ALREADY_YOURS = "already_yours"      # the user already holds this very code
    HAS_OTHER_CODE = "has_other_code"    # the user is registered with a different code
    BOUND_TO_OTHER = "bound_to_other"    # someone else holds the code (counted as a failure)
    NOT_FOUND = "not_found"              # code not in the list (counted as a failure)
    INVALID_FORMAT = "invalid_format"    # not a digits-only code (NOT counted)
    LOCKED = "locked"                    # too many failures; try later


@dataclass(frozen=True)
class GroupSettings:
    deadline_hours: int = 0  # 0 = never remove; admins opt in with /set_deadline
    max_attempts: int = 5
    lock_minutes: int = 60
    contact_text: str | None = None
    report_chat_id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> GroupSettings:
        if row is None:
            return cls()
        return cls(
            deadline_hours=row["deadline_hours"], max_attempts=row["max_attempts"],
            lock_minutes=row["lock_minutes"], contact_text=row["contact_text"],
            report_chat_id=row["report_chat_id"],
        )


def load_settings(repo: Repo, chat_id: int) -> GroupSettings:
    return GroupSettings.from_row(repo.get_group(chat_id))


def pending_deadline(deadline_hours: int, now: int) -> int | None:
    """Deadline for a new pending member; deadline_hours <= 0 means kicking is off (None)."""
    return now + deadline_hours * 3600 if deadline_hours > 0 else None


@dataclass(frozen=True)
class VerifyResult:
    status: VerifyStatus
    code: str | None = None             # the normalized code (or the user's existing code)
    locked_until: int | None = None
    seconds_left: int = 0               # remaining lock time
    just_locked: bool = False           # this attempt triggered the lock (notify admins once)
    fails: int = 0
    student_name: str | None = None     # list name, for the admin report on OK


class UserLike(Protocol):
    id: int
    username: str | None
    full_name: str


def looks_like_code(text: str | None) -> bool:
    """Digits only after normalization (Persian/Arabic digits, spaces, dashes allowed),
    with the same length bounds the importer accepts."""
    code = normalize_code(text or "")
    return code.isascii() and code.isdigit() and CODE_MIN_LEN <= len(code) <= CODE_MAX_LEN


def verify_code(
    repo: Repo, chat_id: int, user: UserLike, code_text: str, now: int,
    settings: GroupSettings,
) -> VerifyResult:
    locked_until = repo.is_locked(chat_id, user.id, now)
    if locked_until is not None:
        return VerifyResult(VerifyStatus.LOCKED, locked_until=locked_until,
                            seconds_left=locked_until - now)
    if not looks_like_code(code_text):
        return VerifyResult(VerifyStatus.INVALID_FORMAT)
    code = normalize_code(code_text)

    mine = repo.get_binding_by_user(chat_id, user.id)
    if mine is not None:
        if mine["code"] == code:
            member = repo.get_member(chat_id, user.id)
            if member is None or member["status"] != "verified":
                repo.set_status(chat_id, user.id, "verified", now)
            return VerifyResult(VerifyStatus.ALREADY_YOURS, code=code)
        return VerifyResult(VerifyStatus.HAS_OTHER_CODE, code=mine["code"])

    def failure(status: VerifyStatus) -> VerifyResult:
        fails, until = repo.record_failure(
            chat_id, user.id, now, settings.max_attempts, settings.lock_minutes * 60)
        return VerifyResult(status, code=code, fails=fails, locked_until=until,
                            seconds_left=(until - now) if until else 0,
                            just_locked=until is not None)

    student = repo.find_student(chat_id, code)
    if student is None:
        return failure(VerifyStatus.NOT_FOUND)
    if repo.get_binding_by_code(chat_id, code) is not None:
        return failure(VerifyStatus.BOUND_TO_OTHER)
    try:
        repo.bind_and_verify(chat_id, user.id, code, user.username, user.full_name, now)
    except CodeAlreadyBound:
        return failure(VerifyStatus.BOUND_TO_OTHER)
    except StudentNotFound:
        return failure(VerifyStatus.NOT_FOUND)
    return VerifyResult(VerifyStatus.OK, code=code, student_name=student["full_name"])
