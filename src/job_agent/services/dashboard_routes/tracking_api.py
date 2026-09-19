"""Local-only, explicitly confirmed notification tracking routes."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from http import HTTPStatus

from job_agent.services.dashboard_routes import route
from job_agent.services.job_repository import JobDatabaseError
from job_agent.services.tracking_parser import parse_message


def _payload(handler):
    value = json.loads(handler._read_body(90000))
    if not isinstance(value, dict):
        raise ValueError('请求必须为对象。')
    return value


def _authorized(handler):
    if not handler._authorized_action():
        handler._reject_unauthorized_action()
        return False
    return True


@route('POST', r'/api/tracking/parse-message')
def parse(handler):
    if not _authorized(handler):
        return
    try:
        result = parse_message(_payload(handler).get('text'))
    except (ValueError, TypeError):
        handler._json({'error': '请输入不超过 20000 字符的通知正文。'}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)


@route('POST', r'/api/tracking/apply-update')
def apply(handler):
    if not _authorized(handler):
        return
    try:
        result = handler.repository.apply_tracking_update(_payload(handler))
    except (ValueError, TypeError, JobDatabaseError) as exc:
        handler._json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json(result)


@route('GET', r'/api/tracking/board')
def board(handler):
    if _authorized(handler):
        handler._json(handler.repository.tracking_board())


@route('POST', r'/api/tracking/reminders/(\d+)/complete')
def complete(handler, reminder_id):
    if not _authorized(handler):
        return
    try:
        if _payload(handler).get('confirmed') is not True:
            raise ValueError('请先确认完成日程。')
        handler.repository.complete_tracking_reminder(int(reminder_id))
    except (ValueError, TypeError) as exc:
        handler._json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    handler._json({'ok': True})


def _escape(value):
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _fold(line):
    # RFC 5545 limits physical lines to 75 UTF-8 octets, not characters.
    result, current = [], ''
    for char in line:
        if len((current + char).encode('utf-8')) > 75:
            result.append(current)
            current = ' '
        current += char
    result.append(current)
    return '\r\n'.join(result)


def build_calendar(reminders):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Personal Job Agent//Local Tracking//ZH', 'CALSCALE:GREGORIAN']
    for item in reminders:
        if item.get('completed'):
            continue
        at = datetime.fromisoformat(item['at']).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
        stamp = datetime.fromisoformat(item['created_at']).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
        label = '测评截止' if item['kind'] == 'oa' else '面试开始'
        # Do not invent a duration or an earlier deadline.
        lines.extend(['BEGIN:VEVENT', f"UID:tracking-{item['id']}-{stamp}@personal-job-agent.local", f'DTSTAMP:{stamp}', f'DTSTART:{at}', f"SUMMARY:{_escape(item['company'] + ' · ' + item['title'] + ' · ' + label)}"])
        if item.get('url'):
            lines.append(f"DESCRIPTION:{_escape('本人核对后打开：' + item['url'])}")
            lines.append(f"URL:{item['url']}")
        lines.append('END:VEVENT')
    lines.append('END:VCALENDAR')
    return ('\r\n'.join(_fold(line) for line in lines) + '\r\n').encode('utf-8')


@route('GET', r'/api/tracking/calendar\.ics')
def calendar(handler):
    if not _authorized(handler):
        return
    body = build_calendar(handler.repository.tracking_board()['reminders'])
    handler._headers(HTTPStatus.OK, 'text/calendar; charset=utf-8', extra_headers={'Content-Disposition': 'attachment; filename="job-agent-calendar.ics"', 'Content-Length': str(len(body))})
    handler.wfile.write(body)
