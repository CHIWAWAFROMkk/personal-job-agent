from __future__ import annotations

import re
from pathlib import Path

from job_agent.models.profile import Profile, SourceDocument


class ContactImportError(RuntimeError):
    pass


_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?86[\s-]?)?(1[3-9](?:[\s-]?\d){9})(?!\d)"
)


def extract_contact_candidates(text: str) -> tuple[list[str], list[str]]:
    emails = sorted({match.group(0).casefold() for match in _EMAIL_PATTERN.finditer(text)})
    phones = sorted(
        {
            re.sub(r"\D", "", match.group(1))
            for match in _PHONE_PATTERN.finditer(text)
        }
    )
    return emails, phones


def _confirmed_resume_sources(profile: Profile) -> list[SourceDocument]:
    return [
        source
        for source in profile.source_documents
        if source.kind == "resume"
        and source.sha256
        and "用户已确认" in source.notes
    ]


def import_contact_from_confirmed_resume(
    profile: Profile,
    private_dir: Path,
) -> tuple[Profile, list[str]]:
    sources = _confirmed_resume_sources(profile)
    if not sources:
        raise ContactImportError("没有已由本人确认真实性的简历来源，不能自动导入联系方式。")

    all_emails: set[str] = set()
    all_phones: set[str] = set()
    files_read = 0
    for source in sources:
        assert source.sha256 is not None
        text_path = private_dir / f"resume-{source.sha256[:12]}.txt"
        if not text_path.is_file():
            continue
        text = text_path.read_text(encoding="utf-8")
        emails, phones = extract_contact_candidates(text)
        all_emails.update(emails)
        all_phones.update(phones)
        files_read += 1
    if files_read == 0:
        raise ContactImportError("已确认简历的本地文字副本不存在，无法安全提取联系方式。")
    if len(all_emails) > 1:
        raise ContactImportError("已确认简历中识别到多个邮箱，请本人选择后再写入 Profile。")
    if len(all_phones) > 1:
        raise ContactImportError("已确认简历中识别到多个手机号，请本人选择后再写入 Profile。")

    imported: list[str] = []
    email = next(iter(all_emails), None)
    phone = next(iter(all_phones), None)
    if email:
        if profile.person.contact.email and profile.person.contact.email.casefold() != email:
            raise ContactImportError("Profile 邮箱与已确认简历不一致，拒绝自动覆盖。")
        if not profile.person.contact.email:
            profile.person.contact.email = email
            imported.append("邮箱")
    if phone:
        normalized_existing = re.sub(r"\D", "", profile.person.contact.phone or "")
        if normalized_existing and normalized_existing[-11:] != phone:
            raise ContactImportError("Profile 手机号与已确认简历不一致，拒绝自动覆盖。")
        if not profile.person.contact.phone:
            profile.person.contact.phone = phone
            imported.append("手机号")
    if not email and not phone:
        raise ContactImportError("已确认简历中没有识别到邮箱或中国大陆手机号。")
    return profile, imported

