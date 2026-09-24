"""Conservative, offline extraction of recruiting notifications.

Results are proposals, never instructions to mutate application state. In particular
missing dates, conflicting statuses and an ATS sender are not invented facts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import ipaddress
import re
from urllib.parse import urlsplit

MAX_MESSAGE_LENGTH = 20_000
_ATS = re.compile(r"北森|北森测评|Moka|飞书招聘|招聘系统|测评系统|通知|短信|邮件|验证码", re.I)
_STATUSES = {
    "rejected": r"未通过.{0,12}(?:筛选|面试|笔试|测评)|未能通过|很遗憾|暂不录用|不予录用|不符合.{0,8}(?:要求|条件)|申请.{0,8}(?:未通过|被拒绝)",
    "offered": r"录用通知|正式录用|决定录用|录用意向书|offer\s*(?:通知|letter)|向您发出\s*offer",
    "interview_scheduled": r"面试邀请|邀请.{0,20}面试|面试(?:时间|安排|通知)|参加.{0,12}面试",
    "oa_pending": r"(?:笔试|测评|在线考试|在线测试)(?:邀请|通知|时间|截止)|(?:邀请|请您|请于|请在|参加).{0,60}(?:笔试|测评|在线考试)|(?<!未)(?<!不)完成.{0,12}(?:笔试|测评)|\bOA\b.{0,20}(?:邀请|截止)",
}
_DATE = re.compile(
    r"(?:(?P<year>20\d{2})[年/.-])?(?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})(?:日|号)?"
    r"|(?P<relative>今天|今日|明天|明日|后天)"
)
_CLOCK = re.compile(r"(?P<period>凌晨|上午|中午|下午|晚上|晚间)?\s*(?P<hour>\d{1,2})(?::|：|点|时)(?P<minute>\d{1,2}|半)?(?:分)?(?::(?P<second>\d{1,2}))?")


def safe_public_url(value: str) -> bool:
    """Syntactic public URL filter; never resolves or fetches a supplied address."""
    try:
        u = urlsplit(value)
        host = (u.hostname or "").lower().rstrip(".")
        if u.scheme not in {"http", "https"} or not host or u.username or u.password:
            return False
        if u.port not in (None, 80, 443) or re.search(r"[\s\\\x00-\x1f]", value):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            # Numeric/hex browser IP aliases must not evade the literal IP check.
            if "." not in host or re.fullmatch(r"[0-9a-fx.]+", host, re.I):
                return False
            return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host))
    except (ValueError, UnicodeError):
        return False


def _companies(text: str) -> list[str]:
    candidates = re.findall(r"[【\[]([^】\]\n]{2,60})[】\]]", text)
    candidates += re.findall(r"(?:公司(?:名称)?|招聘企业|雇主)\s*[:：]\s*([^\n，,。；;]{2,60})", text)
    candidates += re.findall(r"(?:^|\n)\s*([^\n，,。；;：:]{2,40}?)(?:招聘团队|招聘组|人力资源部)\s*(?:$|\n)", text)
    # Known brand names are not candidate facts: only extract when present verbatim.
    candidates += re.findall(r"(?:美团|腾讯)(?=校园招聘|校招|招聘|向您|邀请|面试|笔试)", text)
    result = []
    for candidate in candidates:
        if _ATS.search(candidate):
            continue
        candidate = re.sub(r"(?:校园招聘|校招|招聘|面试邀请|笔试邀请|测评邀请|录用通知|面试通知|笔试通知)$", "", candidate.strip()).strip()
        if len(candidate) >= 2 and not _ATS.search(candidate) and candidate not in result:
            result.append(candidate)
    return result


def _status(text: str, warnings: list[str]) -> tuple[str | None, str | None]:
    found = {}
    for clause in re.split(r"[。！？!?\n；;]", text):
        # Do not turn hypotheticals, negated receipt or old forwarded text into facts.
        if re.search(r"如果|若|如未|如您|一旦|可能|预计|尚未收到|未收到|尚未发出|尚未安排|未安排|取消", clause):
            continue
        for status, pattern in _STATUSES.items():
            match = re.search(pattern, clause, re.I)
            if match:
                found[status] = match.group(0)
    if len(found) > 1:
        warnings.append("识别到多个流转状态，请选择本次通知对应的状态。")
        return None, None
    if not found:
        warnings.append("未识别到明确的流转状态，请人工确认。")
        return None, None
    return next(iter(found.items()))


def _time(text: str, kind: str, now: datetime, warnings: list[str]) -> tuple[str | None, str | None]:
    # Time-zone labels may also appear without an offset. Do not silently
    # interpret bare "UTC"/"GMT" as the computer's local time zone.
    notice_text = re.sub(r"https?://[^\s<>\"']+", " ", text, flags=re.I)
    if re.search(r"(?<![A-Za-z])(?:UTC|GMT)(?![A-Za-z])|北京时间|中国标准时间|东八区|美东|美西|纽约时间|伦敦时间|\b(?:EST|EDT|PST|PDT)\b", notice_text, re.I):
        warnings.append("通知注明时区，请按本机时间人工换算并确认。")
        return None, None
    times = []
    for match in _DATE.finditer(text):
        # Do not mistake dates in links for the notice's schedule.
        if re.search(r"https?://[^\s]*$", text[:match.start()], re.I):
            continue
        if kind == "oa" and re.match(r"\s*[-—–~～至到]\s*(?=\d|今天|明天|后天)", text[match.end():]):
            warnings.append("测评包含日期范围，请人工确认截止时间；不会把开始时间当截止时间。")
            return None, None
        tail = text[match.end():match.end() + 30]
        clock = _CLOCK.search(tail)
        if not clock or re.search(r"[。；;\n]|\d{1,2}[月/.-]\d{1,2}", tail[:clock.start()]):
            continue
        if len(tail[:clock.start()].strip()) > 10:
            continue
        if (":" in clock.group(0) or "：" in clock.group(0)) and clock.group("minute") is None:
            warnings.append("时刻格式不完整，请人工填写。")
            continue
        if kind == "oa" and re.match(r"\s*[-—–~～至到]\s*(?=\d|上午|下午|晚上|今天|明天|后天)", text[match.end() + clock.end():]):
            warnings.append("测评包含时间范围，请人工确认截止时间；不会把开始时间当截止时间。")
            return None, None
        relative = match.group("relative")
        implicit = not relative and not match.group("year")
        try:
            if relative:
                warnings.append("相对日期按当前本机日期解析；若是旧通知，请人工修正日期。")
                offset = 2 if relative == "后天" else 1 if relative in {"明天", "明日"} else 0
                date = (now + timedelta(days=offset)).date()
            else:
                date = datetime(int(match.group("year") or now.year), int(match.group("month")), int(match.group("day"))).date()
            hour = int(clock.group("hour"))
            minute = 30 if clock.group("minute") == "半" else int(clock.group("minute") or 0)
            period = clock.group("period")
            if period and hour > 12:
                raise ValueError("ambiguous period")
            if period == "上午" and hour == 12:
                raise ValueError("ambiguous noon")
            if period in {"下午", "晚上", "晚间"} and hour < 12:
                hour += 12
            elif period in {"凌晨", "上午"} and hour == 12:
                hour = 0
            elif period == "中午" and hour < 10:
                hour += 12
            value = datetime(
                date.year, date.month, date.day, hour, minute,
                int(clock.group("second") or 0),
            ).astimezone()
        except ValueError:
            warnings.append("日期或时间无效或有歧义，请人工填写。")
            continue
        end = match.end() + clock.end()
        before = text[max(0, match.start()-28):match.start()]
        after = text[end:end+12]
        relevant = bool(re.search(r"截止|截至|最晚|不晚于|完成时间", before) or re.match(r"\s*(?:前|之前|截止)", after)) if kind == "oa" else bool(re.search(r"面试|开始|举行|参加", before))
        if kind == "oa" and not relevant and re.search(r"开始|开放|发信|发送|发布时间", before):
            continue
        times.append((value.isoformat(), text[match.start():end], relevant, implicit))
    choices = [entry for entry in times if entry[2]] or times
    unique = {entry[0] for entry in choices}
    if len(unique) != 1:
        warnings.append("存在多个时间，请确认截止或面试开始时间。" if unique else "未找到完整日期和时刻；不会自动假定当天截止。")
        return None, None
    selected = choices[0]
    if selected[3]:
        warnings.append("通知未注明年份，暂按当前本机年份解析，请确认；不会自动跨年。")
    if datetime.fromisoformat(selected[0]) < now:
        warnings.append("识别时间已过去，请核对年份和通知时效。")
    return selected[0], selected[1]


def parse_message(text: str, now: datetime | None = None) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError("请提供 1–20000 字的通知文本。")
    now = now or datetime.now().astimezone()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须带时区。")
    now = now.astimezone()
    warnings: list[str] = []
    companies = _companies(text)
    company = companies[0] if len(companies) == 1 else None
    if not company:
        warnings.append("识别到多个企业，请人工选择。" if companies else "未确认招聘企业；招聘平台名称不等于雇主。")
    status, status_evidence = _status(text, warnings)
    links = []
    for raw in re.findall(r"https?://[^\s<>\"'，。；、！？（）【】]+", text, re.I):
        url = raw.rstrip(".,;!?)]}")
        if safe_public_url(url) and url not in links:
            links.append(url)
        elif not safe_public_url(url):
            warnings.append("已排除非公开或不安全的链接。")
    event = None
    time_evidence = None
    if status in {"oa_pending", "interview_scheduled"}:
        kind = "oa" if status == "oa_pending" else "interview"
        at, time_evidence = _time(text, kind, now, warnings)
        if len(links) > 1:
            warnings.append("存在多个链接，请人工选择测评或会议链接。")
        event = {"kind": kind, "at": at, "url": links[0] if len(links) == 1 else None}
    return {"company": company, "status": status, "event": event, "links": links,
            "warnings": list(dict.fromkeys(warnings)),
            "evidence": {"company": company, "status": status_evidence, "time": time_evidence}}
