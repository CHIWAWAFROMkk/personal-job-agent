# Database Schema Constants
SCHEMA_VERSION = "5"
COMPATIBLE_SCHEMA_VERSIONS = {"1", "2", "3", "4", SCHEMA_VERSION}

# Dictionary & Parsing Constants
COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "集团公司",
    "集团",
    "公司",
)

CITY_NAMES = (
    "上海",
    "北京",
    "深圳",
    "广州",
    "杭州",
    "成都",
    "南京",
    "苏州",
    "武汉",
    "西安",
    "重庆",
    "天津",
)

TRACKING_QUERY_KEYS = {
    "from",
    "fromsource",
    "page_source",
    "pagesource",
    "pcm",
    "ref",
    "source",
    "spm",
}

# Status & Tracking Constants
CANDIDATE_VERIFICATION_STATUSES = {
    "pending",
    "live",
    "expired",
    "blocked",
    "irrelevant",
    "needs_manual_review",
}

APPLICATION_STATUSES = {
    "saved",
    "ready_to_apply",
    "applied",
    "hr_read",
    "resume_requested",
    "screening",
    "assessment",
    "written_test",
    "interview_1",
    "interview_2",
    "final_interview",
    "offer",
    "rejected",
    "withdrawn",
    "no_response",
}

SUBMITTED_APPLICATION_STATUSES = APPLICATION_STATUSES - {
    "saved",
    "ready_to_apply",
}

APPLICATION_STAGE_RANK = {
    "saved": 0,
    "ready_to_apply": 1,
    "applied": 2,
    "hr_read": 3,
    "resume_requested": 4,
    "screening": 5,
    "assessment": 6,
    "written_test": 6,
    "interview_1": 7,
    "interview_2": 8,
    "final_interview": 9,
    "offer": 10,
}

TERMINAL_APPLICATION_STATUSES = {"rejected", "withdrawn", "no_response"}

POSITIVE_FEEDBACK_STATUSES = {
    "resume_requested",
    "screening",
    "assessment",
    "written_test",
    "interview_1",
    "interview_2",
    "final_interview",
    "offer",
}

MEANINGFUL_FEEDBACK_STATUSES = POSITIVE_FEEDBACK_STATUSES | {"rejected"}

# Configuration & Limits Constants
DASHBOARD_VERSION = "phase13-dual-track-workshop-v1"
MAX_JSON_BODY = 16 * 1024
MAX_UPLOAD_BODY = 12 * 1024 * 1024
