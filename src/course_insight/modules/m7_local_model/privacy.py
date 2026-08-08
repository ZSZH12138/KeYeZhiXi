"""Fail-closed privacy governance for student answers sent off platform."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Literal


PrivacyDecision = Literal["allowed", "redacted", "blocked"]


@dataclass(frozen=True, slots=True)
class M7OutboundPrivacyPolicy:
    """Version and thresholds for deterministic outbound answer handling."""

    policy_version: str = "m7-outbound-privacy-v1"
    minimum_remaining_semantic_characters: int = 8
    minimum_remaining_ratio: float = 0.25

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("M7 privacy policy version must not be blank")
        if self.minimum_remaining_semantic_characters < 1:
            raise ValueError("M7 privacy minimum content must be positive")
        if not 0.0 <= self.minimum_remaining_ratio <= 1.0:
            raise ValueError("M7 privacy remaining ratio must be a probability")


DEFAULT_M7_OUTBOUND_PRIVACY_POLICY = M7OutboundPrivacyPolicy()


@dataclass(frozen=True, slots=True)
class OutboundPrivacyResult:
    """Ephemeral governed text plus metadata that is safe to persist."""

    decision: PrivacyDecision
    policy_version: str
    input_checksum: str
    outbound_checksum: str
    flags: tuple[str, ...]
    redaction_count: int
    outbound_text: str | None

    def __post_init__(self) -> None:
        if self.decision not in {"allowed", "redacted", "blocked"}:
            raise ValueError("invalid outbound privacy decision")
        if not self.policy_version.strip():
            raise ValueError("privacy policy version must not be blank")
        for checksum in (self.input_checksum, self.outbound_checksum):
            if not _is_sha256(checksum):
                raise ValueError("privacy checksums must be lowercase SHA-256")
        if (
            type(self.flags) is not tuple
            or len(self.flags) != len(set(self.flags))
            or any(not flag.strip() for flag in self.flags)
            or type(self.redaction_count) is not int
            or self.redaction_count < 0
        ):
            raise ValueError("privacy flags and counters are invalid")
        if self.decision == "blocked" and self.outbound_text is not None:
            raise ValueError("blocked privacy results must not expose outbound text")
        if self.decision != "blocked" and self.outbound_text is None:
            raise ValueError("allowed privacy results require governed text")
        if self.decision == "allowed" and self.redaction_count != 0:
            raise ValueError("allowed privacy results cannot report redactions")
        if self.decision == "redacted" and self.redaction_count == 0:
            raise ValueError("redacted privacy results require a redaction")

    def safe_record(self) -> dict[str, Any]:
        """Return metadata only; never return matched values or answer text."""

        return {
            "privacy_decision": self.decision,
            "privacy_policy_version": self.policy_version,
            "student_answer_checksum": self.input_checksum,
            "outbound_answer_checksum": self.outbound_checksum,
            "privacy_flags": list(self.flags),
            "redaction_count": self.redaction_count,
        }


_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}(?![\w-])",
    re.IGNORECASE,
)
_STUDENT_NUMBER = re.compile(
    r"(?P<label>(?:学号|学生编号|student\s*(?:id|number|no\.?))"
    r"\s*[:：#]?\s*)(?P<value>[A-Z0-9][A-Z0-9_-]{3,31})",
    re.IGNORECASE,
)
_PRC_ID_CANDIDATE = re.compile(
    r"(?<!\d)\d{6}[ -]?\d{8}[ -]?\d{3}[0-9Xx](?!\d)"
)
_PHONE = re.compile(
    r"(?<!\d)(?:(?:\+?86)[ -]?)?1[3-9]\d(?:[ -]?\d{4}){2}(?!\d)"
)
_LANDLINE = re.compile(
    r"(?<!\d)(?:(?:\+?86)[ -]?)?0\d{2,3}[ -]?\d{7,8}"
    r"(?:[ -]?(?:转|ext\.?)?[ -]?\d{1,6})?(?!\d)",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\[[A-Z_]+\]")
_SEMANTIC_CHARACTER = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")

_HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "high_risk_name",
        re.compile(
            r"(?:^|[\s,，;；])(?:(?:我的|本人)\s*)?"
            r"(?:姓名|名字)\s*(?::|：|是|叫|为)\s*"
            r"[\u3400-\u9fff·]{2,20}"
            r"|(?:^|[\s,，;；])(?:我|本人)叫[\u3400-\u9fff·]{2,10}"
            r"|(?:^|[\s,，;；])我姓[\u3400-\u9fff·]{1,3}"
            r"|\bmy\s+name\s+is\s+[A-Z][A-Z .'-]{1,60}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "high_risk_address",
        re.compile(
            r"(?:家庭住址|详细地址|住址|地址)\s*"
            r"(?::|：|是|为)\s*[^\n。；;]{4,}"
            r"|(?:我|本人|我家|家)住(?:在)?[^\n。；;]{3,}"
            r"(?:省|市|区|县|路|街|道|号|小区|村|镇)"
            r"|\b(?:home\s+address|i\s+live\s+at)\s*[:：]?\s*"
            r"[^\n.;]{4,}",
            re.IGNORECASE,
        ),
    ),
    (
        "high_risk_health",
        re.compile(
            r"(?:我|本人)[^\n。；;]{0,8}"
            r"(?:患有|得了|被诊断为|确诊|正在服用|长期服用|对.+过敏)"
            r"|(?:过敏史|病史|健康状况|用药情况|medical\s+history)"
            r"\s*(?::|：|是|为)",
            re.IGNORECASE,
        ),
    ),
    (
        "high_risk_family",
        re.compile(
            r"(?:我的|我家)(?:爸爸|妈妈|父亲|母亲|家人|家庭成员|监护人)"
            r"|(?:家庭情况|家庭成员|监护人信息)\s*(?::|：|是|为)"
            r"|(?:监护人|家长)(?:姓名|电话|联系方式|信息)?"
            r"\s*(?::|：|是|为)"
            r"|\bmy\s+(?:father|mother|parent|guardian|family)\b",
            re.IGNORECASE,
        ),
    ),
)


def govern_student_answer(
    answer: str,
    policy: M7OutboundPrivacyPolicy = DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
) -> OutboundPrivacyResult:
    """Redact reliable identifiers and block ambiguous sensitive prose.

    Detection outcomes contain only stable flag names and counts. Matches are
    deliberately never returned or logged. High-risk prose is blocked because
    deterministic replacement cannot preserve its scoring meaning reliably.
    """

    if type(answer) is not str:
        raise TypeError("student answer must be text")

    input_checksum = _checksum(answer)
    high_risk_flags = tuple(
        flag for flag, pattern in _HIGH_RISK_PATTERNS if pattern.search(answer)
    )

    governed = answer
    flags: list[str] = []
    redaction_count = 0
    governed, count = _EMAIL.subn("[EMAIL_REDACTED]", governed)
    if count:
        flags.append("pii_email_redacted")
        redaction_count += count
    governed, count = _STUDENT_NUMBER.subn(
        lambda match: f"{match.group('label')}[STUDENT_ID_REDACTED]",
        governed,
    )
    if count:
        flags.append("pii_student_number_redacted")
        redaction_count += count
    governed, count = _redact_valid_prc_ids(governed)
    if count:
        flags.append("pii_prc_id_redacted")
        redaction_count += count
    governed, mobile_count = _PHONE.subn("[PHONE_REDACTED]", governed)
    governed, landline_count = _LANDLINE.subn("[PHONE_REDACTED]", governed)
    phone_count = mobile_count + landline_count
    if phone_count:
        flags.append("pii_phone_redacted")
        redaction_count += phone_count

    block_flags = list(high_risk_flags)
    if redaction_count and _redaction_destroyed_meaning(answer, governed, policy):
        block_flags.append("redaction_meaning_loss")

    outbound_checksum = _checksum(governed)
    if block_flags:
        return OutboundPrivacyResult(
            decision="blocked",
            policy_version=policy.policy_version,
            input_checksum=input_checksum,
            outbound_checksum=outbound_checksum,
            flags=tuple(dict.fromkeys([*flags, *block_flags])),
            redaction_count=redaction_count,
            outbound_text=None,
        )
    if redaction_count:
        return OutboundPrivacyResult(
            decision="redacted",
            policy_version=policy.policy_version,
            input_checksum=input_checksum,
            outbound_checksum=outbound_checksum,
            flags=tuple(flags),
            redaction_count=redaction_count,
            outbound_text=governed,
        )
    return OutboundPrivacyResult(
        decision="allowed",
        policy_version=policy.policy_version,
        input_checksum=input_checksum,
        outbound_checksum=outbound_checksum,
        flags=(),
        redaction_count=0,
        outbound_text=answer,
    )


def _redact_valid_prc_ids(value: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        candidate = re.sub(r"[ -]", "", match.group(0)).upper()
        if not _valid_prc_id(candidate):
            return match.group(0)
        count += 1
        return "[PRC_ID_REDACTED]"

    return _PRC_ID_CANDIDATE.sub(replace, value), count


def _valid_prc_id(value: str) -> bool:
    if len(value) != 18 or not value[:17].isdigit():
        return False
    year = int(value[6:10])
    month = int(value[10:12])
    day = int(value[12:14])
    if not 1900 <= year <= 2099 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    expected = checks[sum(int(digit) * weight for digit, weight in zip(value[:17], weights)) % 11]
    return value[-1] == expected


def _redaction_destroyed_meaning(
    original: str,
    governed: str,
    policy: M7OutboundPrivacyPolicy,
) -> bool:
    original_count = len(_SEMANTIC_CHARACTER.findall(original))
    without_placeholders = _PLACEHOLDER.sub("", governed)
    remaining_count = len(_SEMANTIC_CHARACTER.findall(without_placeholders))
    if remaining_count < policy.minimum_remaining_semantic_characters:
        return True
    return original_count > 0 and (
        remaining_count / original_count < policy.minimum_remaining_ratio
    )


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "DEFAULT_M7_OUTBOUND_PRIVACY_POLICY",
    "M7OutboundPrivacyPolicy",
    "OutboundPrivacyResult",
    "PrivacyDecision",
    "govern_student_answer",
]
