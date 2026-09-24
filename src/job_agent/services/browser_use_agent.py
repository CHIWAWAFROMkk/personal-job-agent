"""Browser-Use integration for Personal Job Agent.

Automates form-filling and resume uploading for job applications
using the open-source `browser-use` framework while strictly enforcing
human-in-the-loop safety boundaries (Hard Stop).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from job_agent.models.base import StrictModel
from job_agent.models.job_record import JobDetail
from job_agent.models.profile import Profile
from job_agent.services.runtime_config import RuntimeConfig, RuntimeConfigError
from job_agent.services.safe_job_fetch import UnsafeJobURL, resolve_public

logger = logging.getLogger(__name__)


class BrowserUseError(RuntimeError):
    pass


class HardStopSubmissionAttemptError(BrowserUseError):
    """Raised when an action attempts to click a final submit or application button."""
    pass


FORBIDDEN_SUBMIT_KEYWORDS: tuple[str, ...] = (
    "立即投递",
    "确认投递",
    "确认申请",
    "提交申请",
    "提交简历",
    "确认提交",
    "立即申请",
    "直接投递",
    "完成投递",
    "确认报名",
    "投递职位",
    "投递简历",
    "立即应聘",
    "确认应聘",
    "马上投递",
    "一键投递",
    "发送申请",
    "投简历",
    "投个简历",
)

FORBIDDEN_SUBMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*提交\s*$"),
    re.compile(r"^\s*submit(\s+application|\s+resume)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*apply\s+now\s*$", re.IGNORECASE),
    re.compile(r"^\s*confirm\s+application\s*$", re.IGNORECASE),
    re.compile(r"^\s*complete\s+application\s*$", re.IGNORECASE),
    re.compile(r"submit[-_]?(btn|button|job|app)", re.IGNORECASE),
    re.compile(r"apply[-_]?(btn|button|job|now)", re.IGNORECASE),
)


def is_submission_element(node: Any) -> tuple[bool, str]:
    """Inspect a DOM node and its immediate parents to detect forbidden submission elements."""
    if node is None:
        return True, "无法识别目标元素"

    curr = node
    depth = 0
    while curr is not None and depth < 3:
        attrs = getattr(curr, "attributes", None) or {}
        input_type = (attrs.get("type") or "").strip().lower()
        if input_type == "submit":
            val = attrs.get("value", "")
            return True, f"input[type='submit'] (value='{val}')"

        text = ""
        if hasattr(curr, "get_all_children_text"):
            try:
                text = (curr.get_all_children_text() or "").strip()
            except Exception:
                return True, "无法读取目标文字"
        elif hasattr(curr, "node_value") and curr.node_value:
            text = str(curr.node_value).strip()

        for kw in FORBIDDEN_SUBMIT_KEYWORDS:
            if kw in text:
                return True, f"包含投递关键词 '{kw}'"

        for p in FORBIDDEN_SUBMIT_PATTERNS:
            if p.search(text):
                return True, f"匹配提交模式 '{p.pattern}'"

        for attr_name in ("value", "aria-label", "title", "id", "name", "class", "data-action"):
            val = (attrs.get(attr_name) or "").strip()
            if not val:
                continue
            for kw in FORBIDDEN_SUBMIT_KEYWORDS:
                if kw in val:
                    return True, f"属性 {attr_name} 包含 '{kw}'"
            for p in FORBIDDEN_SUBMIT_PATTERNS:
                if p.search(val):
                    return True, f"属性 {attr_name} 匹配 '{p.pattern}'"

        curr = getattr(curr, "parent", None)
        depth += 1

    return False, ""


_FORBIDDEN_INPUT_LABEL = re.compile(
    r"验证码|校验码|短信码|动态口令|密码|支付|身份证|"
    r"captcha|verification[ _-]?code|one[ _-]?time|\botp\b|password|payment|identity[ _-]?number",
    re.IGNORECASE,
)


def is_forbidden_input_element(node: Any) -> tuple[bool, str]:
    """Stop before typing into a verification or sensitive control."""
    if node is None:
        return True, "无法核验输入目标"
    current = node
    for depth in range(3):
        if current is None:
            break
        attrs = getattr(current, "attributes", None) or {}
        if not isinstance(attrs, dict):
            return True, "输入目标属性无法核验"
        field_type = str(attrs.get("type") or "").casefold()
        if field_type in {"password", "hidden", "submit", "button", "file"}:
            return True, "输入目标属于敏感或非文字控件"
        labels = " ".join(
            str(attrs.get(name) or "")
            for name in ("id", "name", "aria-label", "placeholder", "autocomplete", "title")
        )
        # A form ancestor can contain unrelated CAPTCHA text. Only inspect the
        # control itself or an actual wrapping label, not the entire form.
        tag_name = str(getattr(current, "tag_name", "") or "").casefold()
        if hasattr(current, "get_all_children_text") and (depth == 0 or tag_name == "label"):
            try:
                labels += " " + str(current.get_all_children_text() or "")[:300]
            except Exception:
                return True, "输入目标文字无法核验"
        if _FORBIDDEN_INPUT_LABEL.search(labels):
            return True, "验证码或敏感字段必须由本人填写"
        current = getattr(current, "parent", None)
    return False, ""


def _approved_job_navigation(job: JobDetail) -> tuple[str, str]:
    url = next((item.source_url for item in job.sources if item.source_url), "")
    if not url:
        raise BrowserUseError("岗位缺少可核验的招聘页链接，请先补充官方岗位网址。")
    try:
        if urlsplit(url).scheme.casefold() != "https":
            raise BrowserUseError("浏览器辅助只接受 HTTPS 招聘页。")
    except ValueError as exc:
        raise BrowserUseError("岗位链接格式无效，浏览器辅助已停止。") from exc
    try:
        parts, hostname, _port, _address = resolve_public(url, timeout=4)
    except UnsafeJobURL as exc:
        raise BrowserUseError("岗位链接未通过公开网址检查，浏览器辅助已停止。") from exc
    if parts.scheme != "https":
        raise BrowserUseError("浏览器辅助只接受 HTTPS 招聘页。")
    return url, hostname


def validate_local_cdp_url(url: str) -> str:
    """Never connect an authenticated browser session to a remote debugger."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BrowserUseError("浏览器调试地址格式无效。") from exc
    if (
        parts.scheme != "http"
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise BrowserUseError("浏览器调试地址只允许本机 HTTP 端口。")
    return url


def get_safe_controller_class() -> type:
    """Return the SafeJobController class extending browser_use.Controller."""
    if not is_browser_use_available():
        raise BrowserUseError("未安装 browser-use 依赖。")

    from browser_use import ActionResult, Controller

    class SafeJobController(Controller):
        """Browser-Use controller strictly enforcing hard-stop safety policies."""

        def __init__(
            self, *args: Any, approved_first_url: str | None = None,
            approved_resume_path: str | None = None, **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self.blocked_attempts: list[str] = []
            self.approved_first_url = approved_first_url
            self.approved_resume_path = approved_resume_path
            self.initial_navigation_used = False
            for act_name in ("evaluate", "write_file", "replace_file", "send_keys"):
                if act_name in self.registry.registry.actions:
                    self.exclude_action(act_name)

        def _stop(self, reason: str) -> ActionResult:
            self.blocked_attempts.append(reason)
            return ActionResult(error=f"Hard Stop：{reason}。请停止自动操作并由本人处理；这不表示表单或附件已完成。", include_in_memory=True)

        async def act(
            self,
            action: Any,
            browser_session: Any,
            **kwargs: Any,
        ) -> ActionResult:
            # Deny new/unreviewed dependency actions, even if registered later.
            allowed = {"done", "extract", "scroll", "wait", "search_page", "find_text", "input", "upload_file", "click", "navigate"}
            for action_name, params in action.model_dump(exclude_unset=True).items():
                if params is None:
                    continue
                if self.blocked_attempts and action_name != "done":
                    return self._stop("本轮已停止，不能继续执行其他动作")
                if action_name not in allowed:
                    return self._stop("该动作未经安全审查")
                if action_name == "navigate":
                    if (
                        not isinstance(params, dict)
                        or self.initial_navigation_used
                        or not self.approved_first_url
                        or params.get("url") != self.approved_first_url
                        or params.get("new_tab") is not True
                    ):
                        return self._stop("仅允许首次打开已核验的岗位页面")
                    self.initial_navigation_used = True
                if action_name == "input":
                    if not isinstance(params, dict) or any(ch in str(params.get("text", "")) for ch in ("\r", "\n", "\t")):
                        return self._stop("包含可能触发表单操作的控制字符")
                    idx = params.get("index")
                    if type(idx) is not int or browser_session is None:
                        return self._stop("无法核验输入目标")
                    try:
                        node = (await browser_session.get_selector_map()).get(idx)
                        forbidden, reason = is_forbidden_input_element(node)
                    except Exception:
                        return self._stop("输入目标检查失败")
                    if forbidden:
                        return self._stop(reason)
                if action_name == "upload_file":
                    if (
                        not isinstance(params, dict)
                        or not self.approved_resume_path
                        or params.get("path") != self.approved_resume_path
                    ):
                        return self._stop("仅允许上传已审核的本岗位简历 PDF")
                if action_name == "click" and isinstance(params, dict):
                    idx = params.get("index")
                    if type(idx) is not int or browser_session is None or params.get("coordinate_x") is not None or params.get("coordinate_y") is not None:
                        return self._stop("无法核验点击目标")
                    if idx is not None and browser_session is not None:
                        try:
                            node = await browser_session.get_element_by_index(idx)
                            is_forbidden, reason = is_submission_element(node)
                            if is_forbidden:
                                logger.warning(
                                    "Hard Stop interceptor blocked submission click on index %s: %s",
                                    idx,
                                    reason,
                                )
                                return self._stop(reason)
                        except Exception:
                            return self._stop("目标检查失败")
                    # A harmless-looking custom button may submit via JavaScript.
                    # Until site-specific adapters are verified, clicks stay manual.
                    return self._stop("通用页面点击需本人执行")
                if action_name == "click":
                    return self._stop("点击参数无效")

            return await super().act(action, browser_session, **kwargs)

    return SafeJobController


def is_browser_use_available() -> bool:
    """Check if the `browser_use` package is installed and importable."""
    return importlib.util.find_spec("browser_use") is not None


if is_browser_use_available():
    try:
        SafeJobController = get_safe_controller_class()
    except Exception:
        SafeJobController = None  # type: ignore[assignment, misc]
else:
    SafeJobController = None  # type: ignore[assignment, misc]


def check_cdp_available(cdp_url: str = "http://localhost:9222") -> bool:
    """Check if Chrome DevTools Protocol port is open and responding."""
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        req = urllib.request.Request(version_url, headers={"User-Agent": "JobAgent/1.0"})
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_chrome_launch_instructions(port: int = 9222) -> dict[str, str]:
    """Provide executable path and launch commands for the user's Chrome."""
    exe_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    try:
        from browser_use.browser.chrome import find_chrome_executable
        found = find_chrome_executable()
        if found:
            exe_path = found
    except Exception:
        pass

    cmd_pwsh = f'& "{exe_path}" --remote-debugging-port={port}'
    cmd_cmd = f'"{exe_path}" --remote-debugging-port={port}'

    return {
        "executable_path": exe_path,
        "port": str(port),
        "cdp_url": f"http://localhost:{port}",
        "command_powershell": cmd_pwsh,
        "command_cmd": cmd_cmd,
        "instructions": (
            f"运行上述命令启动 Chrome 并开启远程调试端口 {port}。"
            "在该浏览器窗口中登录你的求职平台（如 Boss直聘/拉勾/各大企业招聘网），"
            "Agent 即可复用你的登录状态和 Cookie，安全代填表单。"
        ),
    }


def create_browser_use_llm(config: RuntimeConfig) -> Any:
    """Create a LangChain/browser-use compatible LLM based on RuntimeConfig."""
    if not is_browser_use_available():
        raise BrowserUseError("未安装 browser-use 依赖。")

    from browser_use import ChatOpenAI

    ai = config.ai
    provider = ai.provider.lower()

    if provider == "local":
        raise RuntimeConfigError(
            "浏览器智能体（browser-use）需要连接云端大模型以理解网页控件。"
            "请前往系统【设置】配置 DeepSeek 或 OpenAI 兼容 API Key。"
        )

    if not ai.api_key:
        raise RuntimeConfigError(f"未配置 {ai.provider} API Key，请在系统设置中保存密钥后再使用。")

    if provider == "deepseek":
        # DeepSeek V3/R1 via ChatOpenAI or ChatDeepSeek
        base_url = ai.base_url or "https://api.deepseek.com/v1"
        if not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        return ChatOpenAI(
            model=ai.model or "deepseek-chat",
            api_key=ai.api_key,
            base_url=base_url,
            temperature=0.1,
        )
    elif provider == "openai":
        return ChatOpenAI(
            model=ai.model or "gpt-4o",
            api_key=ai.api_key,
            temperature=0.1,
        )
    elif provider == "openai_compatible":
        if not ai.base_url or not ai.model.strip():
            raise RuntimeConfigError("OpenAI 兼容服务必须提供服务地址和模型名称。")
        return ChatOpenAI(
            model=ai.model,
            api_key=ai.api_key,
            base_url=ai.base_url,
            temperature=0.1,
        )
    else:
        raise RuntimeConfigError(f"不支持的 AI 供应商类型: {ai.provider}")


def build_browser_use_task_prompt(
    job: JobDetail,
    profile: Profile,
    resume_path: Path | None,
) -> str:
    """Build the high-precision form-filling task prompt with Hard Stop safety rules."""
    person = profile.person
    contact = person.contact

    edu = next((e for e in profile.education if profile.is_application_ready(e.status)), None)
    school_name = (edu.institution if edu else None) or "未填写"
    major_name = (edu.major if edu else None) or "未填写"
    degree_name = (edu.degree if edu else None) or "未填写"
    grad_date = (edu.end if edu else None) or "未填写"
    gpa_info = (edu.gpa if edu else None) or "未填写"
    experience_lines = []
    for experience in profile.experiences:
        for fact in experience.facts:
            if profile.is_application_ready(fact.status):
                experience_lines.append(f"- {experience.role}（{experience.organization or '机构未填写'}）：{fact.statement}")
    if not experience_lines:
        experience_lines = ["求职者尚未录入已确认经历，请优先填充基本联系方式。"]

    target_url = (
        job.sources[0].source_url
        if job.sources and job.sources[0].source_url
        else "当前打开的招聘页面"
    )

    prompt_lines = [
        "### 任务目标：帮助求职者完成招聘网站表单代填与定向简历上传",
        f"目标岗位：{job.title}（{job.company}）",
        f"目标网页链接：{target_url}",
        "",
        "### 求职者核验档案事实（严禁编造任何虚假信息）：",
        f"- 姓名：{person.legal_name or person.display_name or '未填写'}",
        f"- 电话 / 手机号：{contact.phone or '未填写'}",
        f"- 电子邮箱：{contact.email or '未填写'}",
        f"- 毕业院校：{school_name}",
        f"- 专业：{major_name}",
        f"- 学历 / 学位：{degree_name}",
        f"- 毕业年份 / 时间：{grad_date}",
        f"- 成绩 / 排名：{gpa_info}",
        f"- 现居城市：{person.current_city or '未填写'}",
        "",
        "### 核心经历摘要（仅已确认事实）：",
        *experience_lines,
        "",
    ]

    if resume_path and resume_path.is_file():
        prompt_lines.extend([
            "### 简历附件：",
            f"- 专属定制简历 PDF 绝对路径：{resume_path.resolve()}",
            "如果页面有上传简历附件、投递附件简历的按钮或文件输入框（<input type='file'>），请将上述 PDF 文件上传。",
            "",
        ])

    prompt_lines.extend([
        "### 🔴 铁律安全红线（CRITICAL HARD STOP）：",
        "1. 【严禁最终提交】：你绝对不能点击任何带有‘立即投递’、‘确认申请’、‘确认提交’、‘提交简历’、‘Submit Application’、‘Apply Now’等含义的最终提交按钮！",
        "2. 【完成即停】：在将上述求职者个人信息填入对应的表单项，并完成简历附件上传后，请立即调用 `done` 结束任务！",
        "3. 【人机验证与遇到验证码】：遇到任何验证码、短信验证码、登录确认、测评或重要筛选问题，请立即调用 done，交由本人处理；不得请求或自动填写验证码。",
        "4. 【汇报审计】：在 `done` 的总结中，必须列出：",
        "   - 成功填写的字段（如姓名、手机、邮箱、学校等）；",
        "   - 简历附件是否成功上传；",
        "   - 提醒求职者：‘所有信息已就绪，请本人检查页面无误后，手动点击最终提交按钮完成投递’。",
    ])

    return "\n".join(prompt_lines)


class BrowserUseRunResult(StrictModel):
    success: bool
    job_id: int
    status: str  # "completed", "stopped_for_review", "captcha_detected", "failed", "simulated"
    cdp_connected: bool
    steps_count: int = 0
    actions_taken: list[str] = []
    filled_fields: list[str] = []
    summary: str = ""
    error: str | None = None
    started_at: datetime = datetime.now(UTC)
    finished_at: datetime = datetime.now(UTC)


async def _run_agent_async(
    job: JobDetail,
    profile: Profile,
    resume_path: Path | None,
    config: RuntimeConfig,
    cdp_url: str = "http://localhost:9222",
    headless: bool = False,
    max_steps: int = 15,
) -> BrowserUseRunResult:
    """Async execution of browser-use agent."""
    from browser_use import Agent, Browser

    validate_local_cdp_url(cdp_url)
    target_url, hostname = _approved_job_navigation(job)
    approved_resume_path: str | None = None
    if resume_path is not None:
        resolved = resume_path.expanduser().resolve()
        if not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
            raise BrowserUseError("定向简历 PDF 未通过文件检查，已停止上传。")
        approved_resume_path = str(resolved)
    task_prompt = build_browser_use_task_prompt(job, profile, resume_path)
    llm = create_browser_use_llm(config)

    cdp_connected = check_cdp_available(cdp_url)
    if cdp_connected:
        browser = Browser(cdp_url=cdp_url, allowed_domains=[hostname], captcha_solver=False)
    else:
        # Fall back to local browser instance
        browser = Browser(headless=headless, channel="chrome", allowed_domains=[hostname], captcha_solver=False)

    # Use vision if model supports multimodal (e.g. gpt-4o), disable if deepseek
    is_deepseek = config.ai.provider.lower() == "deepseek" or "deepseek" in config.ai.model.lower()
    use_vision = not is_deepseek

    SafeControllerCls = get_safe_controller_class()
    controller = SafeControllerCls(
        approved_first_url=target_url,
        approved_resume_path=approved_resume_path,
    )

    agent = Agent(
        task=task_prompt,
        llm=llm,
        browser=browser,
        controller=controller,
        use_vision=use_vision,
        max_actions_per_step=3,
        max_failures=3,
        use_judge=False,
        directly_open_url=False,
        initial_actions=[{"navigate": {"url": target_url, "new_tab": True}}],
        available_file_paths=[approved_resume_path] if approved_resume_path else [],
        extend_system_message=(
            "CRITICAL SAFETY DIRECTIVE:\n"
            "You are an automated assistant helping the candidate fill out application forms.\n"
            "Under NO circumstances should you click any button that submits the application "
            "(e.g., '提交', '确认申请', '立即投递', 'Submit', 'Apply Now').\n"
            "Once fields are filled and resume uploaded, you MUST call `done` and prompt the candidate to review."
        ),
    )

    started_at = datetime.now(UTC)
    history = await agent.run(max_steps=max_steps)
    finished_at = datetime.now(UTC)

    actions = []
    try:
        actions = list(history.action_names())
    except Exception:
        pass

    if controller.blocked_attempts:
        if "hard_stop_intercepted" not in actions:
            actions.append("hard_stop_intercepted")

    final_res = ""
    try:
        final_res = history.final_result() or ""
    except Exception:
        pass

    if controller.blocked_attempts:
        final_res = (
            "自动操作已停止，请本人检查当前页面。"
            "尚未核实字段填写、附件上传或是否到达确认页。"
        )

    # Detect filled fields from action history / model thoughts
    filled_fields = []
    # Model narration is not evidence that any field was actually filled.

    return BrowserUseRunResult(
        success=True,
        job_id=job.job_id,
        status="stopped_for_review",
        cdp_connected=cdp_connected,
        steps_count=len(history.agent_steps) if hasattr(history, "agent_steps") else len(actions),
        actions_taken=actions[:20],
        filled_fields=filled_fields,
        summary=final_res or "运行已结束，请本人核实填写和附件状态；系统未确认已完成。",
        started_at=started_at,
        finished_at=finished_at,
    )


def run_browser_use_assist(
    job: JobDetail,
    profile: Profile,
    resume_path: Path | None,
    config: RuntimeConfig,
    cdp_url: str = "http://localhost:9222",
    dry_run: bool = False,
    headless: bool = False,
    max_steps: int = 15,
) -> BrowserUseRunResult:
    """Run browser-use assistant synchronously with full error handling and dry-run support."""
    started_at = datetime.now(UTC)
    validate_local_cdp_url(cdp_url)

    if dry_run:
        available_fields = []
        if profile.person.display_name.strip():
            available_fields.append("姓名")
        if profile.person.contact.phone:
            available_fields.append("手机号")
        if profile.person.contact.email:
            available_fields.append("邮箱")
        if any(item.institution and item.status.value in ("documented", "user_confirmed") for item in profile.education):
            available_fields.append("已确认教育信息")
        return BrowserUseRunResult(
            success=True,
            job_id=job.job_id,
            status="simulated",
            cdp_connected=False,
            steps_count=0,
            actions_taken=[],
            filled_fields=available_fields,
            summary=(
                "【模拟代填预览】仅列出当前档案中可供核对的字段；"
                "未检查招聘网页表单，也未核验或上传定向简历附件。"
            ),
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    cdp_connected = check_cdp_available(cdp_url)
    try:
        return asyncio.run(
            _run_agent_async(
                job=job,
                profile=profile,
                resume_path=resume_path,
                config=config,
                cdp_url=cdp_url,
                headless=headless,
                max_steps=max_steps,
            )
        )
    except Exception:
        logger.warning("Browser-use execution failed; private error details suppressed.")
        return BrowserUseRunResult(
            success=False,
            job_id=job.job_id,
            status="failed",
            cdp_connected=cdp_connected,
            steps_count=0,
            actions_taken=[],
            filled_fields=[],
            summary="自动化代填执行异常，已安全退出。",
            error="浏览器辅助未完成，请检查服务配置和浏览器连接后重试。",
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
