"""Synthetic notification fixtures; no real candidate communications."""
from datetime import datetime, timezone, timedelta

import pytest

from job_agent.services.tracking_parser import parse_message, safe_public_url

NOW = datetime(2026, 9, 20, 10, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize("text,company,status,at", [
    ("【美团】笔试邀请：请在2026年9月22日18:30前完成笔试。https://assessment.example.com/exam", "美团", "oa_pending", "2026-09-22T18:30:00+08:00"),
    ("【腾讯招聘】面试邀请：面试时间2026-09-23 下午3点30分。", "腾讯", "interview_scheduled", "2026-09-23T15:30:00+08:00"),
    ("【北森】公司：示例科技\n测评通知：截止时间2026/9/24 23:59:30。", "示例科技", "oa_pending", "2026-09-24T23:59:30+08:00"),
    ("【示例科技】面试安排：明天上午10点。", "示例科技", "interview_scheduled", "2026-09-21T10:00:00+08:00"),
    ("【示例科技】测评邀请：请于后天晚上8点半前完成测评。", "示例科技", "oa_pending", "2026-09-22T20:30:00+08:00"),
    ("【示例科技】面试邀请：面试时间今日14:00。", "示例科技", "interview_scheduled", "2026-09-20T14:00:00+08:00"),
    ("【示例科技】很遗憾，您未通过本轮面试。", "示例科技", "rejected", None),
    ("【示例科技】正式录用，录用通知请查收。", "示例科技", "offered", None),
])
def test_notifications(text, company, status, at):
    result = parse_message(text, NOW)
    assert result["company"] == company
    assert result["status"] == status
    assert (result["event"]["at"] if result["event"] else None) == at


@pytest.mark.parametrize("text", ["如未通过面试，将发送短信。", "如果通过，将发送录用通知。", "尚未收到录用通知。", "尚未安排面试时间。", "可能邀请您参加面试。", "未完成测评不会影响申请。", "预计面试时间下周。"])
def test_uncertain_or_conditional_status_not_fact(text):
    assert parse_message(text, NOW)["status"] is None


def test_conflicting_status_and_companies():
    result = parse_message("【示例甲】面试邀请。\n【示例乙】正式录用。", NOW)
    assert result["company"] is None
    assert result["status"] is None
    assert result["event"] is None


@pytest.mark.parametrize("sender", ["北森", "Moka", "飞书招聘", "测评系统"])
def test_ats_not_employer(sender):
    assert parse_message(f"【{sender}】测评邀请", NOW)["company"] is None


def test_signature_and_year_warning():
    result = parse_message("面试邀请：9月21日15:00\n示例科技招聘团队", NOW)
    assert result["company"] == "示例科技"
    assert result["event"]["at"] == "2026-09-21T15:00:00+08:00"
    assert any("年份" in s for s in result["warnings"])


@pytest.mark.parametrize("date", ["9月23日", "下周二", "2026年2月30日12:00", "2026年9月25日25:00", "2026年9月25日下午15:00"])
def test_incomplete_invalid_date_no_invention(date):
    assert parse_message(f"【示例科技】测评邀请，截止{date}。", NOW)["event"]["at"] is None


def test_deadline_preferred_to_start():
    result = parse_message("【示例科技】测评邀请。开始时间2026年9月21日10:00，截止时间2026年9月23日18:00。", NOW)
    assert result["event"]["at"] == "2026-09-23T18:00:00+08:00"


def test_two_unknown_times_ambiguous():
    result = parse_message("【示例科技】测评邀请。2026年9月21日10:00或2026年9月23日18:00。", NOW)
    assert result["event"]["at"] is None


def test_no_year_rollover():
    result = parse_message("【示例科技】测评邀请，截止1月1日12:00。", NOW)
    assert result["event"]["at"].startswith("2026-01-01")
    assert any("已过去" in s for s in result["warnings"])


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/x", "http://localhost/a", "http://127.0.0.1/a", "http://127.1/a", "http://2130706433/a", "http://0x7f000001/a", "https://192.168.1.3/a", "http://[::1]/", "https://user:pass@example.com/a", "https://example.local/a", "https://example.com:8088/a", "https://example.com\\@127.0.0.1/a"])
def test_url_rejection(url):
    assert not safe_public_url(url)


def test_safe_links_and_multi_link_warning():
    result = parse_message("【示例科技】测评邀请 https://assessment.example.com/a https://meeting.example.com/b http://127.0.0.1/x", NOW)
    assert len(result["links"]) == 2
    assert result["event"]["url"] is None
    assert any("多个链接" in s for s in result["warnings"])


@pytest.mark.parametrize("text", [None, {}, "", "   ", "a" * 20001])
def test_input_bounds(text):
    with pytest.raises(ValueError):
        parse_message(text, NOW)


def test_naive_reference_rejected():
    with pytest.raises(ValueError):
        parse_message("测评邀请", datetime(2026, 9, 20))


@pytest.mark.parametrize("schedule", ["开始时间2026年9月21日10:00", "截止时间2026年9月21日10:xx", "截止2026年9月21日10:00 EST"])
def test_no_start_time_or_malformed_clock_as_deadline(schedule):
    assert parse_message(f"【示例科技】测评邀请。{schedule}", NOW)["event"]["at"] is None


def test_reference_timezone_converted_before_tomorrow():
    result = parse_message("【示例科技】面试时间明天10:00", datetime(2026, 9, 20, 23, tzinfo=timezone.utc))
    assert result["event"]["at"] == "2026-09-22T10:00:00+08:00"


def test_code_and_calendar_url_dates_not_schedule():
    result = parse_message("【示例科技】测评邀请，验证码123456。https://assessment.example.com/2026-09-22/10:30", NOW)
    assert result["event"]["at"] is None


def test_repeat_same_time_not_ambiguous():
    result = parse_message("【示例科技】面试时间2026年9月21日10:00。提醒：2026年9月21日10:00", NOW)
    assert result["event"]["at"] == "2026-09-21T10:00:00+08:00"


def test_relative_date_warns_old_messages():
    result = parse_message("面试时间明天10:00", NOW)
    assert any("旧通知" in warning for warning in result["warnings"])


def test_ambiguous_morning_twelve_not_midnight():
    assert parse_message("面试时间2026年9月21日上午12点", NOW)["event"]["at"] is None


def test_interview_time_range_uses_start():
    assert parse_message("面试时间9月22日14:00-15:00", NOW)["event"]["at"] == "2026-09-22T14:00:00+08:00"


def test_oa_open_without_deadline():
    assert parse_message("测评邀请，开放9/21 9:00，未注明截止", NOW)["event"]["at"] is None


@pytest.mark.parametrize("schedule", [
    "2026年9月22日09:00-18:00",
    "2026年9月22日09:00至18:00",
    "2026年9月22日09:00～下午6点",
    "2026年9月22日09:00-2026年9月23日18:00",
    "9月22日9点到9月23日18点",
    "9月22日至9月23日18点",
    "2026/9/22-2026/9/23 18:00",
])
def test_oa_ranges_never_infer_deadline_from_first_clock(schedule):
    result = parse_message(f"【示例科技】测评通知：测评时间{schedule}，请在此期间完成。", NOW)
    assert result["event"]["at"] is None
    assert result["evidence"]["time"] is None
    assert any("范围" in warning for warning in result["warnings"])
