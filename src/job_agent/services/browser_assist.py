from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from job_agent.models.application import ApplicationPack
from job_agent.models.browser_assist import (
    BrowserApplicationSession,
    BrowserApplicationVerification,
    BrowserAuditAction,
    BrowserHardStop,
    BrowserRunReport,
    ControlDecision,
    FormControlDescriptor,
    IdentityField,
    PlannedFormField,
)
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import Profile
from job_agent.services.profile_store import write_json_atomic


class BrowserAssistError(RuntimeError):
    pass


_SAFE_FIELD_LABELS = {
    "full_name": "姓名",
    "email": "邮箱",
    "phone": "手机号",
    "school": "学校",
    "major": "专业",
    "degree": "学历",
    "graduation_date": "毕业时间",
    "resume": "定向简历",
}

_SAFE_PATTERNS = {
    "full_name": ("姓名", "真实姓名", "应聘者姓名", "fullname", "full name", "name"),
    "email": ("邮箱", "电子邮箱", "电子邮件", "email", "e-mail"),
    "phone": ("手机号", "手机号码", "联系电话", "电话", "mobile", "phone", "tel"),
    "school": ("学校", "院校", "毕业院校", "大学", "school", "university", "institution"),
    "major": ("专业", "major", "fieldofstudy", "field of study"),
    "degree": ("学历", "学位", "degree", "educationlevel", "education level"),
    "graduation_date": (
        "毕业时间",
        "毕业日期",
        "毕业年份",
        "graduationdate",
        "graduation date",
        "graduationyear",
        "graduation year",
    ),
    "resume": ("简历", "附件", "resume", "cv", "curriculumvitae"),
}

_MANUAL_PATTERNS = (
    "验证码",
    "captcha",
    "verificationcode",
    "期望薪资",
    "薪资要求",
    "salary",
    "compensation",
    "工作地点",
    "意向地点",
    "意向城市",
    "locationpreference",
    "preferredlocation",
    "调剂",
    "服从安排",
    "relocation",
    "到岗",
    "每周到岗",
    "实习时长",
    "availability",
    "为什么",
    "why",
    "个人优势",
    "自我评价",
    "开放题",
    "身份证",
    "证件",
    "政治面貌",
    "婚姻",
    "性别",
    "民族",
    "户籍",
    "签证",
    "visa",
    "残疾",
    "disability",
    "背景调查",
    "backgroundcheck",
    "是否同意",
    "consent",
)

_SUBMIT_PATTERNS = (
    "提交",
    "投递",
    "投个简历",
    "投简历",
    "申请",
    "确认申请",
    "submit",
    "apply",
    "deliver",
    "delivery",
    "sendapplication",
)

_CONTROL_SELECTOR = (
    'input, textarea, select, button, [role="button"], [onclick], '
    '[class*="apply" i], [class*="submit" i], [class*="deliver" i], '
    'a[href*="apply" i], a[href*="submit" i], a[href*="deliver" i]'
)

_CONTROL_ENUMERATION_SCRIPT = r"""
() => Array.from(document.querySelectorAll(__CONTROL_SELECTOR__)).map((el, index) => {
  const labels = [];
  const pushLabel = value => {
    const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    if (text && !labels.includes(text)) labels.push(text);
  };
  if (el.labels) Array.from(el.labels).forEach(item => pushLabel(item.innerText || item.textContent));
  const closest = el.closest('label');
  if (closest) pushLabel(closest.innerText || closest.textContent);
  const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  labelledBy.forEach(id => {
    const item = document.getElementById(id);
    if (item) pushLabel(item.innerText || item.textContent);
  });
  ['data-field', 'data-name', 'data-prop', 'data-key', 'data-label', 'data-lable', 'title']
    .forEach(attribute => pushLabel(el.getAttribute(attribute)));
  const fieldContainer = el.closest([
    '.el-form-item', '.ant-form-item', '.van-field', '.layui-form-item',
    '.arco-form-item', '.nut-form-item', '.t-form-item', '.form-group',
    '[class*="form-item"]', '[class*="form_item"]'
  ].join(','));
  if (fieldContainer) {
    const labelNode = fieldContainer.querySelector([
      'label', '.el-form-item__label', '.ant-form-item-label', '.van-field__label',
      '.layui-form-label', '.arco-form-item-label', '.nut-form-item__label',
      '.t-form__label', '[class*="field-label"]', '[class*="field_label"]'
    ].join(','));
    if (labelNode) pushLabel(labelNode.innerText || labelNode.textContent);
  }
  const cell = el.closest('td, dd');
  if (cell && cell.previousElementSibling) {
    pushLabel(cell.previousElementSibling.innerText || cell.previousElementSibling.textContent);
  }
  const parent = el.parentElement;
  if (parent && parent.previousElementSibling) {
    const sibling = parent.previousElementSibling;
    const siblingHasControl = sibling.matches('input, textarea, select, button') ||
      Boolean(sibling.querySelector('input, textarea, select, button'));
    const siblingText = (sibling.innerText || sibling.textContent || '').trim();
    if (!siblingHasControl && siblingText.length <= 80) pushLabel(siblingText);
  }
  const tag = (el.tagName || '').toLowerCase();
  const inputType = (el.getAttribute('type') || '').toLowerCase();
  const className = typeof el.className === 'string' ? el.className : '';
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  const visible = rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  const actionClass = className.split(/\s+/).some(token => /(apply|submit|deliver)/i.test(token));
  const actionLike = tag === 'button' || tag === 'a' || inputType === 'submit' || inputType === 'button' ||
    el.getAttribute('role') === 'button' || el.hasAttribute('onclick') || actionClass;
  if (actionLike) {
    labels.length = 0;
    pushLabel(el.innerText || el.textContent);
  }
  return {
    index,
    tag,
    input_type: inputType,
    name: el.getAttribute('name') || '',
    element_id: el.id || '',
    class_name: className,
    label: labels.join(' ').trim(),
    placeholder: el.getAttribute('placeholder') || '',
    aria_label: el.getAttribute('aria-label') || '',
    autocomplete: el.getAttribute('autocomplete') || '',
    accept: el.getAttribute('accept') || '',
    action_like: actionLike,
    visible,
    required: Boolean(el.required),
    disabled: Boolean(el.disabled),
    readonly: Boolean(el.readOnly),
  };
})
""".replace("__CONTROL_SELECTOR__", json.dumps(_CONTROL_SELECTOR))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _normalize_hint(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def _public_hint(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:120]


def _contains_pattern(normalized_hint: str, patterns: tuple[str, ...]) -> bool:
    return any(_normalize_hint(pattern) in normalized_hint for pattern in patterns)


def classify_form_control(control: FormControlDescriptor) -> ControlDecision:
    hint = _normalize_hint(control.hint_text)
    input_type = control.input_type.casefold()
    tag = control.tag.casefold()

    if control.disabled or control.readonly:
        return ControlDecision(category="ignored", reason="字段不可编辑。")
    is_submit_control = _contains_pattern(hint, _SUBMIT_PATTERNS) or input_type == "submit"
    if is_submit_control:
        return ControlDecision(
            category="blocked",
            reason="检测到提交/投递控件，触发最终提交硬暂停。",
        )
    if input_type in {"button", "image", "reset"} or tag == "button":
        return ControlDecision(category="blocked", reason="按钮不会由 Agent 点击。")
    if control.action_like:
        return ControlDecision(category="blocked", reason="自定义交互控件不会由 Agent 点击。")
    if input_type == "password":
        return ControlDecision(category="manual", reason="登录密码必须由本人输入。")
    if _contains_pattern(hint, _MANUAL_PATTERNS):
        return ControlDecision(category="manual", reason="该字段需要本人判断或安全确认。")
    if tag == "textarea":
        return ControlDecision(category="manual", reason="开放文本题默认由本人复核后填写。")
    if input_type in {"checkbox", "radio"} or tag == "select":
        return ControlDecision(category="manual", reason="选择类字段默认由本人确认。")
    if input_type == "file":
        return ControlDecision(
            category="safe_objective",
            field_key="resume",
            reason="文件上传框可使用已通过质检的定向简历。",
        )
    if input_type == "email" or control.autocomplete.casefold() == "email":
        return ControlDecision(category="safe_objective", field_key="email", reason="客观联系方式。")
    if input_type == "tel" or control.autocomplete.casefold() == "tel":
        return ControlDecision(category="safe_objective", field_key="phone", reason="客观联系方式。")

    exact_full_name = hint in {
        _normalize_hint(value) for value in _SAFE_PATTERNS["full_name"]
    }
    if exact_full_name:
        return ControlDecision(category="safe_objective", field_key="full_name", reason="客观身份字段。")

    for key in (
        "email",
        "phone",
        "school",
        "major",
        "degree",
        "graduation_date",
        "resume",
    ):
        if _contains_pattern(hint, _SAFE_PATTERNS[key]):
            return ControlDecision(
                category="safe_objective",
                field_key=key,
                reason="可由已确认 Profile 客观信息填写。",
            )
    return ControlDecision(category="ignored", reason="未识别字段，不进行猜测或填写。")


def detect_page_hard_stop(
    controls: list[FormControlDescriptor],
    page_title: str,
) -> str | None:
    if any(item.visible and item.input_type.casefold() == "password" for item in controls):
        return "检测到登录密码框，必须由本人先完成登录。"
    if any(
        item.visible
        and (
            "验证码" in _normalize_hint(item.hint_text)
            or "captcha" in _normalize_hint(item.hint_text)
        )
        for item in controls
    ):
        return "检测到验证码/CAPTCHA，已暂停全部自动填写。"
    normalized_title = _normalize_hint(page_title)
    if any(
        marker in normalized_title
        for marker in ("在线测评", "笔试", "assessment", "testcenter")
    ):
        return "检测到测评或笔试页面，禁止自动作答。"
    return None


def build_application_identity(profile: Profile) -> list[IdentityField]:
    education = next(
        (item for item in profile.education if profile.is_application_ready(item.status)),
        None,
    )
    name = profile.person.legal_name or profile.person.display_name or None
    return [
        IdentityField(
            key="full_name",
            label="姓名",
            value=name,
            source="profile.person",
            confirmed=bool(name),
            sensitive=True,
        ),
        IdentityField(
            key="email",
            label="邮箱",
            value=profile.person.contact.email,
            source="profile.person.contact",
            confirmed=bool(profile.person.contact.email),
            sensitive=True,
        ),
        IdentityField(
            key="phone",
            label="手机号",
            value=profile.person.contact.phone,
            source="profile.person.contact",
            confirmed=bool(profile.person.contact.phone),
            sensitive=True,
        ),
        IdentityField(
            key="school",
            label="学校",
            value=education.institution if education else None,
            source=f"profile.education.{education.id}" if education else "profile.education",
            confirmed=bool(education and education.institution),
        ),
        IdentityField(
            key="major",
            label="专业",
            value=education.major if education else None,
            source=f"profile.education.{education.id}" if education else "profile.education",
            confirmed=bool(education and education.major),
        ),
        IdentityField(
            key="degree",
            label="学历",
            value=education.degree if education else None,
            source=f"profile.education.{education.id}" if education else "profile.education",
            confirmed=bool(education and education.degree),
        ),
        IdentityField(
            key="graduation_date",
            label="毕业时间",
            value=education.end if education else None,
            source=f"profile.education.{education.id}" if education else "profile.education",
            confirmed=bool(education and education.end),
        ),
    ]


def load_application_pack(path: Path) -> ApplicationPack:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise BrowserAssistError(f"投递材料包不存在: {path}")
    try:
        return ApplicationPack.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrowserAssistError(f"投递材料包无法读取: {path}: {exc}") from exc


def find_application_pack(
    application_packs_dir: Path,
    job_id: int,
) -> tuple[Path, ApplicationPack]:
    candidates: list[tuple[int, float, Path, ApplicationPack]] = []
    if application_packs_dir.is_dir():
        for path in application_packs_dir.rglob("application-pack.json"):
            try:
                pack = load_application_pack(path)
            except BrowserAssistError:
                continue
            if pack.job.job_id != job_id:
                continue
            ready_priority = 1 if pack.resume.status == "ready" and pack.resume.qa_verified else 0
            candidates.append((ready_priority, path.stat().st_mtime, path, pack))
    if not candidates:
        raise BrowserAssistError(
            f"岗位 #{job_id} 尚无投递材料包；请先运行 applications prepare。"
        )
    _, _, path, pack = max(candidates, key=lambda item: (item[0], item[1], str(item[2])))
    return path.resolve(), pack


def _validate_resume(pack: ApplicationPack) -> tuple[Path | None, str | None, list[str]]:
    blockers: list[str] = []
    if pack.resume.status != "ready" or not pack.resume.qa_verified:
        blockers.append("定向简历尚未通过完整质检，禁止自动上传。")
        return None, None, blockers
    if not pack.resume.pdf_path:
        blockers.append("投递材料包没有可上传的 PDF 简历路径。")
        return None, None, blockers
    resume_path = Path(pack.resume.pdf_path).expanduser().resolve()
    if not resume_path.is_file():
        blockers.append("投递材料包关联的 PDF 简历不存在。")
        return None, None, blockers
    actual_hash = _sha256(resume_path)
    if not pack.resume.manifest_path:
        blockers.append("定向简历缺少质检清单，禁止自动上传。")
        return None, None, blockers
    manifest_path = Path(pack.resume.manifest_path).expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        blockers.append("定向简历质检清单无法读取，禁止自动上传。")
        return None, None, blockers
    expected_hash = str(
        manifest.get("artifacts", {}).get("pdf", {}).get("sha256", "")
    ).upper()
    if not expected_hash or expected_hash != actual_hash:
        blockers.append("定向简历 PDF 与质检清单哈希不一致，禁止自动上传。")
        return None, None, blockers
    return resume_path, actual_hash, blockers


def _validate_target_url(url: str, *, local_demo: bool) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    if local_demo and parsed.scheme == "file":
        return "local-fixture", ["local-fixture"]
    if parsed.scheme not in {"https", "http"}:
        raise BrowserAssistError("岗位链接必须使用 HTTPS；本地演示页面除外。")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        raise BrowserAssistError("岗位链接缺少有效域名。")
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1"}:
        raise BrowserAssistError("真实岗位页面必须使用 HTTPS。")
    return hostname, [hostname]


def create_application_session(
    profile: Profile,
    job: JobDetail,
    pack: ApplicationPack,
    pack_path: Path,
    *,
    sessions_dir: Path,
    target_url: str | None = None,
    mode: str = "safe_fill",
    local_demo: bool = False,
) -> tuple[BrowserApplicationSession, Path]:
    if pack.job.job_id != job.job_id:
        raise BrowserAssistError("投递材料包与岗位编号不一致。")
    if mode not in {"open_only", "safe_fill"}:
        raise BrowserAssistError("浏览器模式必须是 open_only 或 safe_fill。")
    url = (target_url or pack.job.source_url or "").strip()
    if not url:
        raise BrowserAssistError("岗位没有可打开的链接，请使用 --url 提供真实岗位页面。")
    hostname, allowed_domains = _validate_target_url(url, local_demo=local_demo)
    identity = build_application_identity(profile)
    resume_path, resume_hash, resume_blockers = _validate_resume(pack)
    planned_fields = [
        PlannedFormField(
            key=item.key,
            label=item.label,
            category="safe_objective",
            status="ready" if item.confirmed and item.value else "missing",
            value=item.value if item.confirmed else None,
            source=item.source,
            sensitive=item.sensitive,
            detail=(
                "仅在域名白名单内填写。"
                if item.confirmed and item.value
                else "Profile 尚未提供已确认值。"
            ),
        )
        for item in identity
    ]
    planned_fields.append(
        PlannedFormField(
            key="resume",
            label="定向简历",
            category="safe_objective" if resume_path else "blocked",
            status="ready" if resume_path else "blocked",
            value=str(resume_path) if resume_path else None,
            source=pack.resume.manifest_path or "application-pack",
            sensitive=True,
            detail=(
                "PDF 哈希已与质检清单核对。"
                if resume_path
                else "简历未通过上传门槛。"
            ),
        )
    )
    for key, label, detail in (
        ("salary", "薪资要求", "需要本人判断。"),
        ("location_choice", "地点选择", "需要本人确认。"),
        ("adjustment", "是否接受调剂", "需要本人确认。"),
        ("availability_question", "到岗/实习时长筛选题", "需核对题目原文后回答。"),
        ("subjective_questions", "开放题/主观筛选题", "需本人复核生成内容。"),
        ("captcha", "验证码/CAPTCHA", "禁止绕过，只能本人完成。"),
        ("assessment", "测评/笔试", "禁止代答。"),
        ("final_submit", "最终提交申请", "Agent 永远不会自动点击。"),
    ):
        planned_fields.append(
            PlannedFormField(
                key=key,
                label=label,
                category="blocked" if key in {"captcha", "assessment", "final_submit"} else "manual",
                status="blocked" if key in {"captcha", "assessment", "final_submit"} else "manual",
                detail=detail,
            )
        )

    blockers = list(resume_blockers)
    missing_labels = [
        item.label
        for item in planned_fields
        if item.category == "safe_objective" and item.status == "missing"
    ]
    if missing_labels:
        blockers.append("Profile 缺少客观字段：" + "、".join(missing_labels))
    blockers.extend(
        f"提交前确认：{item.item}"
        for item in pack.review_checklist
        if item.blocks_submission
    )
    hard_stops = [
        BrowserHardStop(rule="domain_allowlist", detail=f"只向 {hostname} 顶层页面填写信息。"),
        BrowserHardStop(rule="no_submit", detail="不提供、也不执行最终提交动作。"),
        BrowserHardStop(rule="no_captcha_bypass", detail="验证码和反自动化检查必须本人完成。"),
        BrowserHardStop(rule="manual_subjective_answers", detail="薪资、地点、调剂和主观题不自动填写。"),
        BrowserHardStop(rule="action_budget", detail="单次最多执行有限数量的安全填写动作。"),
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"job-{job.job_id}-{stamp}-{uuid.uuid4().hex[:8]}"
    session = BrowserApplicationSession(
        session_id=session_id,
        job_id=job.job_id,
        company=job.company,
        title=job.title,
        platform=job.sources[0].platform if job.sources else "unknown",
        target_url=url,
        allowed_domains=allowed_domains,
        mode=mode,
        application_pack_path=str(pack_path.expanduser().resolve()),
        resume_path=str(resume_path) if resume_path else None,
        resume_sha256=resume_hash,
        fields=planned_fields,
        hard_stops=hard_stops,
        blockers=blockers,
        status="planned",
        local_demo=local_demo,
    )
    session_path = sessions_dir.expanduser().resolve() / session_id / "session.json"
    write_json_atomic(session.model_dump(mode="json"), session_path)
    return session, session_path


def load_application_session(path: Path) -> BrowserApplicationSession:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise BrowserAssistError(f"投递会话不存在: {path}")
    try:
        session = BrowserApplicationSession.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise BrowserAssistError(f"投递会话无法读取: {path}: {exc}") from exc
    if session.resume_path and session.resume_sha256:
        resume_path = Path(session.resume_path)
        if not resume_path.is_file() or _sha256(resume_path) != session.resume_sha256:
            raise BrowserAssistError("投递会话中的简历文件已变化，拒绝继续自动上传。")
    return session


def playwright_environment_status() -> tuple[bool, bool, str]:
    if importlib.util.find_spec("playwright") is None:
        return False, False, "Playwright Python 包未安装。"
    try:
        from playwright.sync_api import sync_playwright

        manager = sync_playwright().start()
        try:
            browser = manager.chromium.launch(headless=True, channel="chromium")
            browser.close()
            return True, True, "Chromium 新版无头模式启动成功。"
        finally:
            manager.stop()
    except Exception as exc:  # pragma: no cover - depends on local browser runtime
        return True, False, f"Playwright 浏览器检查失败: {exc}"


def verify_application_page(
    session_path: Path,
    *,
    browser_profile_dir: Path,
    browser_channel: str = "chromium",
    headless: bool = True,
) -> tuple[BrowserApplicationVerification, Path]:
    """Open a job page read-only and detect explicit visible application state."""

    session_path = session_path.expanduser().resolve()
    session = load_application_session(session_path)
    if importlib.util.find_spec("playwright") is None:
        raise BrowserAssistError("Playwright 尚未安装，无法核验投递状态。")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserAssistError("Playwright 无法导入，无法核验投递状态。") from exc

    browser_profile_dir = browser_profile_dir.expanduser().resolve()
    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    verification_path = session_path.parent / "application-verification.json"
    screenshot_path = session_path.parent / "投递状态核验.png"
    context = None
    browser = None
    final_url = session.target_url
    observed_status = "unknown"
    explicit_markers: list[str] = []
    blocker: str | None = None

    try:
        with sync_playwright() as playwright:
            if session.local_demo:
                browser = playwright.chromium.launch(
                    headless=True,
                    channel=browser_channel,
                )
                context = browser.new_context(locale="zh-CN")
            else:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(browser_profile_dir),
                    headless=headless,
                    channel=browser_channel,
                    locale="zh-CN",
                )
            context.set_default_timeout(5000)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                session.target_url,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(1500)
            final_url = page.url
            if not _domain_allowed(
                final_url,
                session.allowed_domains,
                local_demo=session.local_demo,
            ):
                observed_status = "blocked"
                blocker = "核验页面跳转到域名白名单之外。"
            else:
                observation = page.evaluate(
                    r"""
() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility !== 'hidden';
  };
  const texts = Array.from(document.querySelectorAll('body *'))
    .filter(visible)
    .map(element => (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(text => text.length > 0 && text.length <= 40);
  const unique = Array.from(new Set(texts));
  const appliedMarkers = ['已投递', '投递成功', '简历投递成功', '申请成功'];
  const entryMarkers = ['投个简历', '投递简历', '立即申请'];
  const loginMarkers = ['登录', '请先登录', '手机号登录'];
  return {
    applied: appliedMarkers.filter(marker => unique.includes(marker)),
    entry: entryMarkers.filter(marker => unique.includes(marker)),
    login: loginMarkers.filter(marker => unique.includes(marker)),
    visiblePassword: Array.from(document.querySelectorAll('input[type="password"]')).some(visible),
  };
}
"""
                )
                if observation.get("applied"):
                    observed_status = "applied"
                    explicit_markers = list(observation["applied"])
                elif observation.get("visiblePassword") or observation.get("login"):
                    observed_status = "login_required"
                    explicit_markers = list(observation.get("login", []))
                    blocker = "页面要求本人登录后才能核验。"
                elif observation.get("entry"):
                    observed_status = "not_applied"
                    explicit_markers = list(observation["entry"])
                else:
                    observed_status = "unknown"
                    blocker = "页面没有出现可确认的已投递或待投递标记。"
            page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as exc:
        observed_status = "blocked"
        blocker = f"只读核验失败：{type(exc).__name__}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    verification = BrowserApplicationVerification(
        session_id=session.session_id,
        requested_url=session.target_url,
        final_url=final_url,
        observed_status=observed_status,
        explicit_markers=explicit_markers,
        screenshot_path=str(screenshot_path) if screenshot_path.is_file() else None,
        blocker=blocker,
    )
    write_json_atomic(verification.model_dump(mode="json"), verification_path)
    return verification, verification_path


def _domain_allowed(url: str, allowed_domains: list[str], *, local_demo: bool) -> bool:
    parsed = urlsplit(url)
    if local_demo and parsed.scheme == "file":
        return True
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return hostname in {item.casefold().rstrip(".") for item in allowed_domains}


def _field_index(session: BrowserApplicationSession) -> dict[str, PlannedFormField]:
    return {
        item.key: item
        for item in session.fields
        if item.category == "safe_objective" and item.status == "ready" and item.value
    }


_SHIXISENG_ANSWER_OPTIONS = {
    "arrival": {"1周内", "2周内", "1个月内", "3个月内"},
    "duration": {"3个月以内", "3-6个月", "6个月以上"},
    "days": {f"{item}天" for item in range(1, 7)},
}


def build_shixiseng_availability_answers(
    profile: Profile,
    *,
    today: date | None = None,
) -> dict[str, str]:
    """Map confirmed Profile availability to Shixiseng's exact choices."""

    availability = profile.job_search.availability
    missing: list[str] = []
    if not availability.earliest_start:
        missing.append("最早到岗日期")
    if availability.duration_months is None:
        missing.append("最短连续实习月数")
    if availability.days_per_week is None:
        missing.append("每周稳定到岗天数")
    if missing:
        raise BrowserAssistError(
            "Profile 缺少已确认的实习可用性：" + "、".join(missing)
        )

    try:
        start_date = date.fromisoformat(availability.earliest_start or "")
    except ValueError as exc:
        raise BrowserAssistError("Profile 的最早到岗日期不是 YYYY-MM-DD。") from exc

    days_until_start = (start_date - (today or date.today())).days
    if days_until_start <= 7:
        arrival = "1周内"
    elif days_until_start <= 14:
        arrival = "2周内"
    elif days_until_start <= 31:
        arrival = "1个月内"
    elif days_until_start <= 92:
        arrival = "3个月内"
    else:
        raise BrowserAssistError(
            "最早到岗日期超过实习僧现有选项范围，必须由本人选择。"
        )

    minimum_months = availability.duration_months or 0
    if minimum_months >= 6:
        duration = "6个月以上"
    elif minimum_months >= 3:
        duration = "3-6个月"
    else:
        duration = "3个月以内"

    days = f"{availability.days_per_week}天"
    if days not in _SHIXISENG_ANSWER_OPTIONS["days"]:
        raise BrowserAssistError(
            "每周到岗天数超出实习僧现有选项范围，必须由本人选择。"
        )
    return {"arrival": arrival, "duration": duration, "days": days}


def _validate_confirmed_availability_answers(values: dict[str, str]) -> None:
    if set(values) != set(_SHIXISENG_ANSWER_OPTIONS):
        raise BrowserAssistError("投递弹窗答案必须完整包含到岗、时长和出勤三项。")
    for key, value in values.items():
        if value not in _SHIXISENG_ANSWER_OPTIONS[key]:
            raise BrowserAssistError(f"投递弹窗包含不受支持的已确认选项：{key}。")


def _exact_text_locator(locator, expected_text: str):
    matches = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
        if text == expected_text and candidate.is_visible():
            matches.append(candidate)
    return matches


def _public_page_location(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    return {
        "host": (parsed.hostname or "").casefold(),
        "path": parsed.path,
    }


def _resume_upload_inputs(page) -> list:
    """Return only unambiguous document upload inputs, excluding image inputs."""

    candidates = []
    inputs = page.locator('input[type="file"]')
    for index in range(inputs.count()):
        candidate = inputs.nth(index)
        accept = (candidate.get_attribute("accept") or "").casefold()
        name = " ".join(
            filter(
                None,
                (
                    candidate.get_attribute("name"),
                    candidate.get_attribute("id"),
                    candidate.get_attribute("class"),
                    candidate.get_attribute("aria-label"),
                ),
            )
        ).casefold()
        allows_document = any(
            marker in accept
            for marker in ("pdf", "doc", "msword", "officedocument")
        )
        looks_like_resume = any(
            marker in name for marker in ("resume", "attachment", "attach", "简历", "附件")
        )
        rejects_images_only = "image" in accept and not allows_document
        if (allows_document or looks_like_resume) and not rejects_images_only:
            candidates.append(candidate)
    return candidates


def _pdf_page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _viewer_page_total(page) -> int | None:
    for frame in page.frames:
        try:
            text = frame.locator("body").inner_text(timeout=1000)
        except Exception:
            continue
        matches = re.findall(r"(?<!\d)\d+\s*/\s*(\d+)(?!\d)", text)
        if matches:
            return int(matches[0])
    return None


def _viewer_source_fingerprint(page) -> str | None:
    sources = [frame.url for frame in page.frames if frame.url and frame.url != page.url]
    try:
        embedded_sources = page.locator("iframe, embed, object").evaluate_all(
            """
elements => elements
  .map(element => element.getAttribute('src') || element.getAttribute('data') || '')
  .filter(Boolean)
"""
        )
    except Exception:
        embedded_sources = []
    sources.extend(str(item) for item in embedded_sources)
    if not sources:
        return None
    payload = "\n".join(sorted(str(item) for item in sources))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()[:16]


def _is_trusted_shixiseng_resume_editor(
    url: str,
    session: BrowserApplicationSession,
) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    allowed = {item.casefold().rstrip(".") for item in session.allowed_domains}
    return hostname == "resume.shixiseng.com" and "www.shixiseng.com" in allowed


def _upload_through_shixiseng_editor(
    editor_page,
    resume_path: Path,
    *,
    audit_dir: Path,
    audit: dict[str, object],
) -> str | None:
    """Upload through the official editor and require preview-page evidence."""

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    expected_pages = _pdf_page_count(resume_path)
    viewer_before = _viewer_page_total(editor_page)
    source_before = _viewer_source_fingerprint(editor_page)
    audit["expected_resume_pages"] = expected_pages
    audit["viewer_pages_before"] = viewer_before
    upload_locator = editor_page.get_by_text("上传", exact=True)
    upload_controls = [
        upload_locator.nth(index)
        for index in range(upload_locator.count())
        if upload_locator.nth(index).is_visible()
    ]
    if len(upload_controls) != 1:
        return "附件修改页没有唯一可见的“上传”入口。"

    file_selected = False
    try:
        with editor_page.expect_file_chooser(timeout=5000) as chooser_info:
            upload_controls[0].click()
        chooser_info.value.set_files(str(resume_path))
        file_selected = True
    except PlaywrightTimeoutError:
        editor_page.wait_for_timeout(300)
        upload_dialogs = []
        dialogs = editor_page.locator(".el-dialog:visible, [role='dialog']:visible")
        for index in range(dialogs.count()):
            candidate = dialogs.nth(index)
            text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
            if "请选择中英文附件" in text and "确定" in text:
                upload_dialogs.append(candidate)
        if len(upload_dialogs) == 1:
            upload_dialog = upload_dialogs[0]
            chinese_controls = upload_dialog.get_by_text("中文", exact=True)
            visible_chinese_controls = [
                chinese_controls.nth(index)
                for index in range(chinese_controls.count())
                if chinese_controls.nth(index).is_visible()
            ]
            if len(visible_chinese_controls) != 1:
                return "附件语言弹窗没有唯一可见的“中文”选项。"
            chinese_control = visible_chinese_controls[0]
            chinese_selected = chinese_control.evaluate(
                """
element => {
  const label = element.closest('label') || element.parentElement;
  const input = label ? label.querySelector('input[type="radio"]') : null;
  return Boolean(
    (input && input.checked) ||
    (label && /(^|\\s)is-checked(\\s|$)/.test(label.className || ''))
  );
}
"""
            )
            if not chinese_selected:
                chinese_control.click()
            confirm_controls = _exact_text_locator(
                upload_dialog.locator("button, .el-button, [role='button']"),
                "确定",
            )
            if len(confirm_controls) != 1:
                return "附件语言弹窗没有唯一的“确定”按钮。"
            try:
                with editor_page.expect_file_chooser(timeout=5000) as chooser_info:
                    confirm_controls[0].click()
                chooser_info.value.set_files(str(resume_path))
                file_selected = True
            except PlaywrightTimeoutError:
                editor_page.wait_for_timeout(300)

        if not file_selected:
            safe_inputs = _resume_upload_inputs(editor_page)
            if len(safe_inputs) == 1:
                safe_inputs[0].set_input_files(str(resume_path))
                file_selected = True
            elif len(safe_inputs) > 1:
                return "点击上传后出现多个文档文件框，拒绝猜测。"

        if not file_selected:
            all_file_inputs = editor_page.locator('input[type="file"]')
            audit["upload_file_inputs"] = [
                {
                    "accept": all_file_inputs.nth(index).get_attribute("accept") or "",
                    "name": all_file_inputs.nth(index).get_attribute("name") or "",
                    "id": all_file_inputs.nth(index).get_attribute("id") or "",
                    "class": all_file_inputs.nth(index).get_attribute("class") or "",
                }
                for index in range(all_file_inputs.count())
            ]
            editor_page.screenshot(
                path=str(audit_dir / "附件简历上传入口点击后.png"),
                full_page=True,
            )
            if all_file_inputs.count() == 1:
                all_file_inputs.first.set_input_files(str(resume_path))
                file_selected = True
            elif all_file_inputs.count() > 1:
                return "点击上传后出现多个未标注文件框，拒绝猜测。"
            else:
                return "点击上传后没有出现文件框。"
    if not file_selected:
        return "附件文件未被文件选择器接受。"

    audit["resume_file_selected"] = True
    viewer_after = _viewer_page_total(editor_page)
    source_after = _viewer_source_fingerprint(editor_page)
    for _ in range(30):
        visible_errors = editor_page.locator(
            ".el-message--error:visible, .el-form-item__error:visible"
        ).all_inner_texts()
        if visible_errors:
            return "实习僧附件页显示上传错误。"
        viewer_after = _viewer_page_total(editor_page)
        source_after = _viewer_source_fingerprint(editor_page)
        if (
            (
                expected_pages is not None
                and viewer_after == expected_pages
                and viewer_before != expected_pages
            )
            or (
                source_before is not None
                and source_after is not None
                and source_before != source_after
            )
        ):
            break
        editor_page.wait_for_timeout(500)

    audit["viewer_pages_after"] = viewer_after
    audit["viewer_source_changed"] = (
        source_before is not None
        and source_after is not None
        and source_before != source_after
    )
    editor_page.screenshot(
        path=str(audit_dir / "附件简历修改页-上传后.png"),
        full_page=True,
    )
    body_text = editor_page.locator("body").inner_text()
    explicit_success = any(
        marker in body_text for marker in ("上传成功", "上传完成", resume_path.name)
    )
    preview_changed = (
        (
            expected_pages is not None
            and viewer_after == expected_pages
            and viewer_before != expected_pages
        )
        or bool(audit["viewer_source_changed"])
    )
    audit["upload_evidence"] = {
        "explicit_success_marker": explicit_success,
        "preview_page_count_changed": preview_changed,
    }
    if not explicit_success and not preview_changed:
        return "文件已选择，但网站没有提供可验证的上传成功证据。"
    return None


def _prepare_shixiseng_submission_modal(
    page,
    session: BrowserApplicationSession,
    answers: dict[str, str],
    *,
    action_budget: int,
    audit_dir: Path,
) -> tuple[list[BrowserAuditAction], list[str], list[str], bool, bool, dict[str, object]]:
    """Prepare the known Shixiseng modal and never click its final button."""

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    _validate_confirmed_availability_answers(answers)
    actions: list[BrowserAuditAction] = []
    blockers: list[str] = []
    filled_fields: list[str] = []
    uploaded_resume = False
    final_confirmation_detected = False
    logical_actions = 0
    audit: dict[str, object] = {
        "schema_version": "1.0",
        "session_id": session.session_id,
        "site": "shixiseng",
        "status": "blocked",
        "resume_file": Path(session.resume_path).name if session.resume_path else None,
        "resume_selected": False,
        "resume_upload_accepted": False,
        "answers": dict(answers),
        "verified_answers": {},
        "final_confirmation_detected": False,
        "final_submit_clicked": False,
        "blockers": blockers,
    }

    def spend_action() -> bool:
        nonlocal logical_actions
        if logical_actions >= action_budget:
            blockers.append("达到站点弹窗安全动作上限，已停止继续填写。")
            return False
        logical_actions += 1
        return True

    modal_candidates = page.locator(".common-deliver:visible")
    if modal_candidates.count() != 1:
        blockers.append("未找到唯一的实习僧投递确认弹窗。")
        return (
            actions,
            blockers,
            filled_fields,
            uploaded_resume,
            final_confirmation_detected,
            audit,
        )
    modal = modal_candidates.first
    if "选择投递简历" not in modal.inner_text() or "确认投递" not in modal.inner_text():
        blockers.append("弹窗文字与已验证的实习僧投递结构不一致。")
        return actions, blockers, filled_fields, uploaded_resume, False, audit

    resume_path = Path(session.resume_path).expanduser().resolve() if session.resume_path else None
    if resume_path is None or not resume_path.is_file():
        blockers.append("没有通过哈希门槛的定向 PDF，拒绝上传。")
        return actions, blockers, filled_fields, uploaded_resume, False, audit

    rows = modal.locator(".user-resume")
    attachment_rows = []
    for index in range(rows.count()):
        row = rows.nth(index)
        title = row.locator(".user-resume__left").first
        if title.count() and re.sub(r"\s+", " ", title.inner_text()).strip() == "附件简历":
            attachment_rows.append(row)
    if len(attachment_rows) != 1:
        blockers.append("未找到唯一的“附件简历”区域，拒绝猜测上传位置。")
        return actions, blockers, filled_fields, uploaded_resume, False, audit

    attachment_row = attachment_rows[0]
    if not spend_action():
        return actions, blockers, filled_fields, uploaded_resume, False, audit
    upload_error: str | None = None
    try:
        row_inputs = attachment_row.locator('input[type="file"]')
        if row_inputs.count() == 1:
            row_inputs.first.set_input_files(str(resume_path))
        elif row_inputs.count() > 1:
            upload_error = "附件简历区域存在多个文件框，拒绝猜测。"
        else:
            modify_candidates = _exact_text_locator(
                attachment_row.locator("span, a, button, [role='button']"),
                "修改",
            )
            if len(modify_candidates) != 1:
                upload_error = "附件简历区域没有唯一的“修改”入口。"
            else:
                pages_before_click = list(page.context.pages)
                try:
                    with page.expect_file_chooser(timeout=2500) as chooser_info:
                        modify_candidates[0].click()
                    chooser_info.value.set_files(str(resume_path))
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(300)
                    new_pages = [
                        candidate
                        for candidate in page.context.pages
                        if candidate not in pages_before_click
                    ]
                    upload_surface = page
                    if len(new_pages) == 1:
                        upload_surface = new_pages[0]
                        try:
                            upload_surface.wait_for_load_state(
                                "domcontentloaded",
                                timeout=10000,
                            )
                        except PlaywrightTimeoutError:
                            pass
                        audit["resume_edit_page"] = {
                            **_public_page_location(upload_surface.url),
                            "title": _public_hint(upload_surface.title()),
                        }
                        upload_surface.screenshot(
                            path=str(audit_dir / "附件简历修改页.png"),
                            full_page=True,
                        )
                    elif len(new_pages) > 1:
                        upload_error = "“修改”同时打开多个页面，拒绝猜测上传位置。"

                    trusted_editor = _is_trusted_shixiseng_resume_editor(
                        upload_surface.url,
                        session,
                    )
                    if upload_error is None and not (
                        _domain_allowed(
                            upload_surface.url,
                            session.allowed_domains,
                            local_demo=session.local_demo,
                        )
                        or trusted_editor
                    ):
                        upload_error = "附件修改页离开域名白名单，拒绝上传。"

                    if upload_error is None and trusted_editor:
                        upload_error = _upload_through_shixiseng_editor(
                            upload_surface,
                            resume_path,
                            audit_dir=audit_dir,
                            audit=audit,
                        )
                    elif upload_error is None:
                        safe_inputs = _resume_upload_inputs(upload_surface)
                        if len(safe_inputs) == 1:
                            safe_inputs[0].set_input_files(str(resume_path))
                        elif len(safe_inputs) > 1:
                            upload_error = "附件修改页存在多个文档上传框，拒绝猜测。"
                        else:
                            upload_error = (
                                "附件修改页没有唯一且明确接受 PDF/DOC 的简历文件框；"
                                "已停止，未点任何确认按钮。"
                            )
        if upload_error is None:
            page.bring_to_front()
            page.wait_for_timeout(1200)
            visible_upload_errors = page.locator(
                ".el-message--error:visible, .el-form-item__error:visible"
            ).all_inner_texts()
            if visible_upload_errors:
                upload_error = "网站显示附件上传错误。"
            else:
                uploaded_resume = True
                audit["resume_upload_accepted"] = True
                filled_fields.append("resume")
                actions.append(
                    BrowserAuditAction(
                        action="upload",
                        result="completed",
                        field_key="resume",
                        control_hint="附件简历",
                        detail="已向附件文件框提供哈希校验通过的岗位定向 PDF；审计不保存完整路径。",
                    )
                )
    except Exception as exc:
        upload_error = f"附件上传控件处理失败：{type(exc).__name__}"
    if upload_error:
        blockers.append(upload_error)
        actions.append(
            BrowserAuditAction(
                action="upload",
                result="failed",
                field_key="resume",
                control_hint="附件简历",
                detail=upload_error,
            )
        )

    # The row may have been re-rendered by the upload, so locate it again.
    rows = modal.locator(".user-resume")
    attachment_row = None
    for index in range(rows.count()):
        candidate = rows.nth(index)
        title = candidate.locator(".user-resume__left").first
        if title.count() and re.sub(r"\s+", " ", title.inner_text()).strip() == "附件简历":
            attachment_row = candidate
            break
    if attachment_row is None:
        blockers.append("上传后附件简历区域消失，无法安全选择。")
    elif spend_action():
        selector = attachment_row.locator(".right-item__img").first
        if selector.count() != 1 or not selector.is_visible():
            blockers.append("附件简历选择控件不可见。")
        else:
            try:
                selector.click()
                page.wait_for_timeout(250)
                audit["resume_selected"] = True
                filled_fields.append("resume_source")
                actions.append(
                    BrowserAuditAction(
                        action="select",
                        result="completed",
                        field_key="resume_source",
                        control_hint="附件简历",
                        detail="已选择附件简历作为本次投递版本。",
                    )
                )
            except Exception as exc:
                blockers.append(f"附件简历选择失败：{type(exc).__name__}")

    expected_by_title = {
        "到岗时间": ("arrival", answers["arrival"]),
        "实习时长": ("duration", answers["duration"]),
        "每周出勤": ("days", answers["days"]),
    }
    verified_answers: dict[str, str] = {}
    for title_text, (field_key, target_text) in expected_by_title.items():
        if not spend_action():
            break
        items = modal.locator(".complete-info__item")
        matching_items = []
        for index in range(items.count()):
            item = items.nth(index)
            title = item.locator(".complete-info__item-title").first
            if not title.count():
                continue
            normalized_title = re.sub(r"[\s：:]+", "", title.inner_text())
            if normalized_title == title_text:
                matching_items.append(item)
        if len(matching_items) != 1:
            blockers.append(f"未找到唯一的“{title_text}”选项。")
            continue
        select = matching_items[0].locator(".el-select").first
        input_control = select.locator("input").first
        try:
            current_value = input_control.input_value() if input_control.count() else ""
            if current_value != target_text:
                option_matches = []
                for _ in range(2):
                    page.bring_to_front()
                    input_control.click()
                    visible_options = page.locator(
                        ".el-select-dropdown__item:visible"
                    )
                    try:
                        visible_options.first.wait_for(
                            state="visible",
                            timeout=2500,
                        )
                    except Exception:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(150)
                        continue
                    option_matches = _exact_text_locator(
                        visible_options,
                        target_text,
                    )
                    if option_matches:
                        break
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                if len(option_matches) != 1:
                    page.keyboard.press("Escape")
                    blockers.append(f"网站未提供唯一的“{target_text}”选项。")
                    continue
                option_matches[0].click()
                page.wait_for_timeout(250)
            verified_value = input_control.input_value() if input_control.count() else ""
            if verified_value != target_text:
                blockers.append(f"“{title_text}”写入后回读不一致。")
                continue
            verified_answers[field_key] = verified_value
            filled_fields.append(f"availability_{field_key}")
            actions.append(
                BrowserAuditAction(
                    action="select",
                    result="completed",
                    field_key=f"availability_{field_key}",
                    control_hint=title_text,
                    detail="已按本人确认的 Profile 选择，并完成页面回读。",
                )
            )
        except Exception as exc:
            blockers.append(f"“{title_text}”填写失败：{type(exc).__name__}")

    audit["verified_answers"] = verified_answers
    footer_controls = _exact_text_locator(
        modal.locator(".common-deliver__footer .btn, .common-deliver__footer button"),
        "确认投递",
    )
    final_confirmation_detected = len(footer_controls) == 1 and footer_controls[0].is_visible()
    audit["final_confirmation_detected"] = final_confirmation_detected
    if not final_confirmation_detected:
        blockers.append("没有检测到唯一的最终“确认投递”按钮，页面结构可能已变化。")

    ready = (
        uploaded_resume
        and bool(audit["resume_selected"])
        and len(verified_answers) == 3
        and final_confirmation_detected
        and not blockers
    )
    audit["status"] = "ready_for_user_confirmation" if ready else "blocked"
    return (
        actions,
        blockers,
        filled_fields,
        uploaded_resume,
        final_confirmation_detected,
        audit,
    )


def run_browser_session(
    session_path: Path,
    *,
    browser_profile_dir: Path,
    headless: bool = False,
    keep_open: bool = True,
    browser_channel: str = "chromium",
    max_actions: int = 12,
    confirmed_entry_text: str | None = None,
    confirmation_token: str | None = None,
    confirmed_availability_answers: dict[str, str] | None = None,
    dashboard_wait_seconds: int | None = None,
) -> tuple[BrowserRunReport, Path]:
    if max_actions < 1 or max_actions > 32:
        raise BrowserAssistError("浏览器安全动作上限必须在 1 到 32 之间。")
    if dashboard_wait_seconds is not None and not 30 <= dashboard_wait_seconds <= 1800:
        raise BrowserAssistError("Dashboard 浏览器等待时间必须在 30 到 1800 秒之间。")
    session_path = session_path.expanduser().resolve()
    session = load_application_session(session_path)
    if confirmed_entry_text is not None:
        expected_token = f"SUBMIT_JOB_{session.job_id}"
        if confirmation_token != expected_token:
            raise BrowserAssistError(
                "最终投递入口必须提供与岗位编号一致的一次性确认令牌。"
            )
    elif confirmation_token is not None:
        raise BrowserAssistError("未指定投递入口文字，拒绝使用确认令牌。")
    if confirmed_availability_answers is not None:
        if confirmed_entry_text is None:
            raise BrowserAssistError("只有经确认打开投递弹窗后，才能填写可用性选项。")
        _validate_confirmed_availability_answers(confirmed_availability_answers)
    if importlib.util.find_spec("playwright") is None:
        raise BrowserAssistError(
            "Playwright 尚未安装。请先运行 scripts/setup.ps1 安装浏览器组件。"
        )
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - guarded above
        raise BrowserAssistError("Playwright 无法导入，请重新运行安装脚本。") from exc

    started_at = datetime.now(UTC)
    actions: list[BrowserAuditAction] = []
    blockers: list[str] = []
    filled_fields: list[str] = []
    uploaded_resume = False
    manual_fields = 0
    submit_controls = 0
    submit_attempted = False
    submission_prepared = False
    final_confirmation_detected = False
    submission_outcome = "not_attempted"
    final_url = session.target_url
    screenshot_path: Path | None = None
    observed_demo_submit_count: int | None = None
    observed_demo_entry_click_count: int | None = None
    report_status = "failed"
    fields = _field_index(session)
    browser_profile_dir = browser_profile_dir.expanduser().resolve()
    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    report_path = session_path.parent / "browser-run-report.json"
    context = None
    browser = None

    try:
        with sync_playwright() as playwright:
            channel = browser_channel
            if headless or session.local_demo:
                browser = playwright.chromium.launch(headless=headless, channel=channel)
                context = browser.new_context(locale="zh-CN")
            else:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(browser_profile_dir),
                    headless=False,
                    channel=channel,
                    locale="zh-CN",
                )
            context.set_default_timeout(5000)
            page = context.pages[0] if context.pages else context.new_page()
            actions.append(
                BrowserAuditAction(
                    action="navigate",
                    result="completed",
                    detail="打开岗位页面；未读取浏览器密码、Cookie 或本地存储。",
                )
            )
            page.goto(session.target_url, wait_until="domcontentloaded", timeout=45000)
            final_url = page.url
            if not _domain_allowed(
                final_url,
                session.allowed_domains,
                local_demo=session.local_demo,
            ):
                blockers.append("页面跳转到域名白名单之外，已停止填写。")
                actions.append(
                    BrowserAuditAction(
                        action="hard_stop",
                        result="blocked",
                        detail="顶层页面域名不在本会话白名单中。",
                    )
                )
                report_status = "blocked"
            elif session.mode == "open_only":
                actions.append(
                    BrowserAuditAction(
                        action="skip",
                        result="skipped",
                        detail="当前会话为只打开模式，未填写任何字段。",
                    )
                )
                report_status = "completed"
            else:
                if confirmed_entry_text is not None:
                    pre_click_controls = [
                        control
                        for item in page.evaluate(_CONTROL_ENUMERATION_SCRIPT)
                        if (
                            control := FormControlDescriptor.model_validate(item)
                        ).visible
                    ]
                    pre_click_hard_stop = detect_page_hard_stop(
                        pre_click_controls,
                        page.title(),
                    )
                    entry_label = _normalize_hint(confirmed_entry_text)
                    entry_candidates = [
                        control
                        for control in pre_click_controls
                        if _normalize_hint(control.label) == entry_label
                        and classify_form_control(control).category == "blocked"
                        and (
                            "提交" in classify_form_control(control).reason
                            or "投递" in classify_form_control(control).reason
                        )
                    ]
                    submit_controls += len(entry_candidates)
                    selected_entry = None
                    if pre_click_hard_stop:
                        blockers.append(pre_click_hard_stop)
                        actions.append(
                            BrowserAuditAction(
                                action="hard_stop",
                                result="blocked",
                                detail=pre_click_hard_stop,
                            )
                        )
                        report_status = "blocked"
                    else:
                        for candidate in entry_candidates:
                            candidate_locator = page.locator(_CONTROL_SELECTOR).nth(
                                candidate.index
                            )
                            if (
                                candidate_locator.is_visible()
                                and candidate_locator.is_enabled()
                            ):
                                selected_entry = (candidate, candidate_locator)
                                break
                        if selected_entry is None:
                            blockers.append(
                                "没有找到可见且文字完全匹配的投递入口，未执行点击。"
                            )
                            actions.append(
                                BrowserAuditAction(
                                    action="hard_stop",
                                    result="blocked",
                                    detail="一次性确认的投递入口不存在或不可用。",
                                )
                            )
                            report_status = "blocked"
                        else:
                            before_submit_path = session_path.parent / "提交入口点击前.png"
                            page.screenshot(
                                path=str(before_submit_path),
                                full_page=True,
                            )
                            selected_control, selected_locator = selected_entry
                            selected_locator.click(timeout=5000)
                            submit_attempted = True
                            actions.append(
                                BrowserAuditAction(
                                    action="submit",
                                    result="completed",
                                    control_hint=_public_hint(
                                        selected_control.hint_text
                                    ),
                                    detail="已按本人一次性确认点击投递入口 1 次。",
                                )
                            )
                            page.wait_for_timeout(2000)
                            final_url = page.url
                            if not _domain_allowed(
                                final_url,
                                session.allowed_domains,
                                local_demo=session.local_demo,
                            ):
                                blockers.append(
                                    "点击后跳转到域名白名单之外，已停止后续填写。"
                                )
                                actions.append(
                                    BrowserAuditAction(
                                        action="hard_stop",
                                        result="blocked",
                                        detail="投递入口点击后离开域名白名单。",
                                    )
                                )
                                report_status = "blocked"

                if report_status == "blocked":
                    controls = []
                else:
                    raw_controls = page.evaluate(_CONTROL_ENUMERATION_SCRIPT)
                    controls = [
                        control
                        for item in raw_controls
                        if (
                            control := FormControlDescriptor.model_validate(item)
                        ).visible
                    ]
                    page_hard_stop = detect_page_hard_stop(controls, page.title())
                    if page_hard_stop:
                        reason = page_hard_stop
                        blockers.append(reason)
                        actions.append(
                            BrowserAuditAction(
                                action="hard_stop",
                                result="blocked",
                                detail=reason,
                            )
                        )
                        controls = []
                        report_status = "blocked"
                used_keys: set[str] = set()
                executed_actions = 0
                for control in controls:
                    decision = classify_form_control(control)
                    hint = _public_hint(control.hint_text)
                    if decision.category == "blocked":
                        if "提交" in decision.reason or "投递" in decision.reason:
                            submit_controls += 1
                        actions.append(
                            BrowserAuditAction(
                                action="hard_stop",
                                result="blocked",
                                control_hint=hint,
                                detail=decision.reason,
                            )
                        )
                        continue
                    if decision.category == "manual":
                        manual_fields += 1
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="skipped",
                                control_hint=hint,
                                detail=decision.reason,
                            )
                        )
                        continue
                    if decision.category != "safe_objective" or not decision.field_key:
                        continue
                    field = fields.get(decision.field_key)
                    if field is None:
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="skipped",
                                field_key=decision.field_key,
                                control_hint=hint,
                                detail="Profile 或简历审核门没有提供可用值。",
                            )
                        )
                        continue
                    if decision.field_key in used_keys:
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="skipped",
                                field_key=decision.field_key,
                                control_hint=hint,
                                detail="同一字段出现多个候选控件，为避免误填只处理第一个。",
                            )
                        )
                        continue
                    if executed_actions >= max_actions:
                        blockers.append("达到本次安全动作上限，剩余字段未填写。")
                        actions.append(
                            BrowserAuditAction(
                                action="hard_stop",
                                result="blocked",
                                detail="达到动作预算上限。",
                            )
                        )
                        break
                    locator = page.locator(_CONTROL_SELECTOR).nth(control.index)
                    try:
                        if not locator.is_visible() or not locator.is_enabled():
                            actions.append(
                                BrowserAuditAction(
                                    action="skip",
                                    result="skipped",
                                    field_key=decision.field_key,
                                    control_hint=hint,
                                    detail="控件当前不可见或不可用。",
                                )
                            )
                            continue
                        if decision.field_key == "resume":
                            locator.set_input_files(str(field.value))
                            action_name = "upload"
                            uploaded_resume = True
                        else:
                            locator.fill(str(field.value))
                            action_name = "fill"
                        executed_actions += 1
                        used_keys.add(decision.field_key)
                        filled_fields.append(decision.field_key)
                        actions.append(
                            BrowserAuditAction(
                                action=action_name,
                                result="completed",
                                field_key=decision.field_key,
                                control_hint=hint,
                                detail="已填写；审计记录不保存字段值。",
                            )
                        )
                    except Exception as exc:
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="failed",
                                field_key=decision.field_key,
                                control_hint=hint,
                                detail=f"控件填写失败，已跳过：{type(exc).__name__}",
                            )
                        )
                if report_status != "blocked":
                    report_status = "completed"

                if (
                    report_status != "blocked"
                    and submit_attempted
                    and confirmed_availability_answers is not None
                ):
                    current_hostname = (urlsplit(page.url).hostname or "").casefold()
                    has_known_modal = page.locator(".common-deliver:visible").count() > 0
                    if current_hostname == "www.shixiseng.com" or (
                        session.local_demo and has_known_modal
                    ):
                        (
                            preparation_actions,
                            preparation_blockers,
                            preparation_fields,
                            preparation_uploaded,
                            final_confirmation_detected,
                            preparation_audit,
                        ) = _prepare_shixiseng_submission_modal(
                            page,
                            session,
                            confirmed_availability_answers,
                            action_budget=max(0, max_actions - executed_actions),
                            audit_dir=session_path.parent,
                        )
                        actions.extend(preparation_actions)
                        blockers.extend(preparation_blockers)
                        for field_key in preparation_fields:
                            if field_key not in filled_fields:
                                filled_fields.append(field_key)
                        uploaded_resume = uploaded_resume or preparation_uploaded
                        submission_prepared = preparation_audit.get("status") == (
                            "ready_for_user_confirmation"
                        )
                        write_json_atomic(
                            preparation_audit,
                            session_path.parent / "submission-preparation.json",
                        )
                        if preparation_blockers:
                            report_status = "blocked"
                    else:
                        blockers.append("当前网站没有经过验证的投递弹窗适配器，未自动填写。")
                        report_status = "blocked"

                if submit_attempted:
                    outcome = page.evaluate(
                        r"""
() => {
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const successMarkers = ['投递成功', '已投递', '申请成功', '简历投递成功'];
  const submitted = successMarkers.some(marker => bodyText.includes(marker));
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const formOpened = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, select'))
    .some(visible);
  const secondaryConfirmation = Array.from(document.querySelectorAll('button, [role="button"], [class*="submit" i], [class*="apply" i]'))
    .filter(visible)
    .some(el => /确认|提交|投递|申请/.test((el.innerText || el.textContent || '').trim()));
  return { submitted, formOpened, secondaryConfirmation };
}
"""
                    )
                    if outcome.get("submitted"):
                        submission_outcome = "submitted"
                    elif outcome.get("formOpened"):
                        submission_outcome = "form_opened"
                    elif outcome.get("secondaryConfirmation"):
                        submission_outcome = "secondary_confirmation"
                    else:
                        submission_outcome = "unknown"

                    modal_controls = page.evaluate(
                        r"""
() => {
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const roots = Array.from(document.querySelectorAll('[class*="deliver" i], [role="dialog"]')).filter(visible);
  const root = roots.find(el => {
    const text = el.innerText || el.textContent || '';
    return text.includes('选择投递简历') && text.includes('确认投递');
  });
  if (!root) return [];
  return Array.from(root.querySelectorAll('*')).filter(visible).slice(0, 180).map((el, index) => {
    const ownText = Array.from(el.childNodes)
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent || '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
    const fallbackText = ['BUTTON', 'A', 'LABEL'].includes(el.tagName)
      ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
      : '';
    return {
      index,
      tag: (el.tagName || '').toLowerCase(),
      class_name: typeof el.className === 'string' ? el.className.slice(0, 240) : '',
      element_id: (el.id || '').slice(0, 120),
      role: (el.getAttribute('role') || '').slice(0, 80),
      input_type: (el.getAttribute('type') || '').slice(0, 80),
      name: (el.getAttribute('name') || '').slice(0, 120),
      aria_label: (el.getAttribute('aria-label') || '').slice(0, 160),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 160),
      text: (ownText || fallbackText).slice(0, 200),
    };
  });
}
"""
                    )
                    modal_audit_path = session_path.parent / "submission-modal-controls.json"
                    write_json_atomic(
                        {
                            "schema_version": "1.0",
                            "session_id": session.session_id,
                            "controls": modal_controls,
                        },
                        modal_audit_path,
                    )

                    submission_options: list[dict[str, object]] = []
                    option_selects = page.locator(
                        ".common-deliver .complete-info__item .el-select"
                    )
                    for option_index in range(option_selects.count()):
                        option_select = option_selects.nth(option_index)
                        current_input = option_select.locator("input").first
                        current_value = (
                            current_input.input_value()
                            if current_input.count() and current_input.is_visible()
                            else ""
                        )
                        option_select.click()
                        page.wait_for_timeout(150)
                        option_items = page.locator(
                            ".el-select-dropdown__item:visible"
                        )
                        option_texts = [
                            re.sub(r"\s+", " ", text).strip()
                            for text in option_items.all_inner_texts()
                            if re.sub(r"\s+", " ", text).strip()
                        ]
                        submission_options.append(
                            {
                                "index": option_index,
                                "current": current_value,
                                "options": option_texts,
                            }
                        )
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(100)
                    write_json_atomic(
                        {
                            "schema_version": "1.0",
                            "session_id": session.session_id,
                            "selects": submission_options,
                        },
                        session_path.parent / "submission-options.json",
                    )

            screenshot_path = session_path.parent / "填写后页面.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            actions.append(
                BrowserAuditAction(
                    action="screenshot",
                    result="completed",
                    detail="已保存填写后的页面截图供本人复核。",
                )
            )
            if session.local_demo:
                observed_demo_submit_count = int(
                    page.evaluate("() => Number(window.__jobAgentSubmitCount || 0)")
                )
                observed_demo_entry_click_count = int(
                    page.evaluate(
                        "() => Number(window.__jobAgentEntryClickCount || 0)"
                    )
                )
            if keep_open and not headless:
                if submit_attempted:
                    print("已执行经确认的一次投递入口点击；页面已暂停，请本人检查后按 Enter 关闭。")
                else:
                    print("浏览器已停在提交前。请本人检查页面；完成后回到此窗口按 Enter 关闭。")
                if dashboard_wait_seconds is not None:
                    try:
                        page.wait_for_event("close", timeout=dashboard_wait_seconds * 1000)
                    except PlaywrightTimeoutError:
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="skipped",
                                detail="达到 Dashboard 等待上限，专用浏览器窗口已安全关闭。",
                            )
                        )
                else:
                    try:
                        input()
                    except EOFError:
                        actions.append(
                            BrowserAuditAction(
                                action="skip",
                                result="skipped",
                                detail="当前终端没有交互输入，浏览器按安全规则关闭。",
                            )
                        )
    except Exception as exc:
        blockers.append(f"浏览器运行失败：{type(exc).__name__}")
        actions.append(
            BrowserAuditAction(
                action="hard_stop",
                result="failed",
                detail=f"运行失败并停止：{type(exc).__name__}",
            )
        )
        report_status = "failed"
        report = BrowserRunReport(
            session_id=session.session_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            requested_url=session.target_url,
            final_url=final_url,
            allowed_domain=session.allowed_domains[0],
            status=report_status,
            actions=actions,
            filled_fields=filled_fields,
            uploaded_resume=uploaded_resume,
            manual_fields_detected=manual_fields,
            submit_controls_detected=submit_controls,
            submit_attempted=submit_attempted,
            submission_prepared=submission_prepared,
            final_confirmation_detected=final_confirmation_detected,
            submission_outcome=(
                "unknown" if submit_attempted and submission_outcome == "not_attempted"
                else submission_outcome
            ),
            observed_demo_submit_count=observed_demo_submit_count,
            observed_demo_entry_click_count=observed_demo_entry_click_count,
            screenshot_path=str(screenshot_path) if screenshot_path and screenshot_path.is_file() else None,
            blockers=blockers,
        )
        write_json_atomic(report.model_dump(mode="json"), report_path)
        session.status = "failed"
        write_json_atomic(session.model_dump(mode="json"), session_path)
        raise BrowserAssistError(
            f"浏览器辅助运行失败，审计报告已保存: {report_path}: {exc}"
        ) from exc
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    report = BrowserRunReport(
        session_id=session.session_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        requested_url=session.target_url,
        final_url=final_url,
        allowed_domain=session.allowed_domains[0],
        status=report_status,
        actions=actions,
        filled_fields=filled_fields,
        uploaded_resume=uploaded_resume,
        manual_fields_detected=manual_fields,
        submit_controls_detected=submit_controls,
        submit_attempted=submit_attempted,
        submission_prepared=submission_prepared,
        final_confirmation_detected=final_confirmation_detected,
        submission_outcome=submission_outcome,
        observed_demo_submit_count=observed_demo_submit_count,
        observed_demo_entry_click_count=observed_demo_entry_click_count,
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        blockers=blockers,
    )
    write_json_atomic(report.model_dump(mode="json"), report_path)
    session.status = "completed" if report_status == "completed" else "blocked"
    write_json_atomic(session.model_dump(mode="json"), session_path)
    return report, report_path
