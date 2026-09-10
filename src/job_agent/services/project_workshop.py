from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_agent.models.job_record import JobDetail


class ProjectWorkshopError(RuntimeError):
    pass


_TEMPLATES: dict[str, dict[str, object]] = {
    "application-funnel": {
        "title": "求职投递漏斗分析台",
        "duration": "1–2 天",
        "capabilities": ["数据清洗", "漏斗分析", "运营诊断", "结果表达"],
        "why": "把零散投递记录变成可解释的运营漏斗，适合数据运营、业务运营和销售运营。",
    },
    "jd-project-engine": {
        "title": "JD 驱动的项目推荐与验证器",
        "duration": "2–3 天",
        "capabilities": ["需求拆解", "规则设计", "产品流程", "AI 协作边界"],
        "why": "把 JD 的能力缺口转成可执行项目，并用验证门槛防止“生成即算完成”。",
    },
    "hr-data-quality": {
        "title": "HR 主数据异常处理模拟器",
        "duration": "1–3 天",
        "capabilities": ["主数据治理", "异常识别", "流程设计", "跨部门协作"],
        "why": "用合成的人事主数据复现缺失、冲突和重复问题，适合 HR 数字化与数据运营。",
    },
}

_CONFIG_KEYS = {"project_name", "question", "focus", "threshold"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectWorkshopError(f"{label}无法读取：{exc}") from exc
    if not isinstance(value, dict):
        raise ProjectWorkshopError(f"{label}格式不正确。")
    return value


def _project_root(output_dir: Path, job_id: int) -> Path:
    if job_id < 1:
        raise ProjectWorkshopError("岗位编号无效。")
    return output_dir.resolve() / "project-workshop" / f"job-{job_id}"


def _select_template(job: JobDetail) -> str:
    text = f"{job.title}\n{job.jd_text}".casefold()
    if any(marker in text for marker in ("hr", "人力", "人事", "主数据", "员工数据")):
        return "hr-data-quality"
    if any(marker in text for marker in ("ai", "人工智能", "产品运营", "产品经理", "需求")):
        return "jd-project-engine"
    return "application-funnel"


def recommend_project(job: JobDetail, gaps: list[str] | None = None) -> dict[str, object]:
    template_id = _select_template(job)
    template = _TEMPLATES[template_id]
    return {
        "template_id": template_id,
        "title": template["title"],
        "duration": template["duration"],
        "capabilities": template["capabilities"],
        "why": template["why"],
        "target_job": f"{job.company} · {job.title}",
        "gaps": list(gaps or [])[:4],
        "truth_boundary": (
            "项目由 AI 辅助搭建；只有本人修改关键配置、跑通、演示并能解释后，"
            "才可以标记为简历可用。"
        ),
    }


def _default_config(job: JobDetail, template_id: str) -> dict[str, object]:
    title = str(_TEMPLATES[template_id]["title"])
    questions = {
        "application-funnel": "哪些岗位方向、平台和简历版本更容易获得有效回复？",
        "jd-project-engine": "如何把一份 JD 的能力缺口转成能验证、能演示的项目？",
        "hr-data-quality": "如何发现 HR 主数据异常，并把问题交给正确负责人闭环？",
    }
    focuses = {
        "application-funnel": "有效回复率、面试转化与下一批投递动作",
        "jd-project-engine": "缺口优先级、项目选择和简历准入门槛",
        "hr-data-quality": "缺失值、重复记录、字段冲突和处理优先级",
    }
    return {
        "project_name": f"{job.company}｜{title}"[:80],
        "question": questions[template_id],
        "focus": focuses[template_id],
        "threshold": 75,
    }


def _validate_config(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProjectWorkshopError("项目设置必须是一个对象。")
    extra = set(value) - _CONFIG_KEYS
    missing = _CONFIG_KEYS - set(value)
    if extra or missing:
        raise ProjectWorkshopError("项目设置字段不完整或包含未知字段。")
    cleaned: dict[str, object] = {}
    for key in ("project_name", "question", "focus"):
        text = str(value[key]).strip()
        if not text:
            raise ProjectWorkshopError("项目名称、问题和重点都不能为空。")
        maximum = 80 if key == "project_name" else 240
        if len(text) > maximum:
            raise ProjectWorkshopError(f"{key} 最多 {maximum} 个字符。")
        cleaned[key] = text
    threshold = value["threshold"]
    if isinstance(threshold, bool):
        raise ProjectWorkshopError("项目阈值必须是 1–100 的整数。")
    try:
        parsed_threshold = int(threshold)
    except (TypeError, ValueError) as exc:
        raise ProjectWorkshopError("项目阈值必须是 1–100 的整数。") from exc
    if parsed_threshold < 1 or parsed_threshold > 100:
        raise ProjectWorkshopError("项目阈值必须在 1–100 之间。")
    cleaned["threshold"] = parsed_threshold
    return cleaned


def _funnel_rows() -> list[dict[str, object]]:
    stages = (
        "applied", "hr_read", "applied", "resume_requested", "rejected", "applied",
        "interview", "hr_read", "applied", "screening", "rejected", "applied",
        "interview", "hr_read", "applied", "resume_requested", "applied", "rejected",
        "screening", "applied", "hr_read", "interview", "applied", "applied",
    )
    platforms = ("BOSS直聘", "企业官网", "牛客", "实习僧")
    roles = ("数据运营", "产品运营", "HR数字化")
    return [
        {
            "application_id": index,
            "role_direction": roles[(index - 1) % len(roles)],
            "platform": platforms[(index - 1) % len(platforms)],
            "resume_version": f"{roles[(index - 1) % len(roles)]}版",
            "match_score": 62 + (index * 7) % 34,
            "status": stage,
        }
        for index, stage in enumerate(stages, start=1)
    ]


def _jd_rows() -> list[dict[str, object]]:
    return [
        {"requirement": "SQL 数据提取", "current_level": "部分掌握", "project_feature": "漏斗查询", "coverage_score": 78},
        {"requirement": "业务指标设计", "current_level": "部分掌握", "project_feature": "指标字典", "coverage_score": 82},
        {"requirement": "产品流程", "current_level": "需要补足", "project_feature": "状态机与准入门槛", "coverage_score": 74},
        {"requirement": "跨部门表达", "current_level": "已有证据", "project_feature": "异常负责人视图", "coverage_score": 88},
        {"requirement": "可验证结果", "current_level": "需要补足", "project_feature": "自动测试与演示页", "coverage_score": 92},
    ]


def _hr_rows() -> list[dict[str, object]]:
    return [
        {"employee_id": "E1001", "department": "Sales", "field": "manager_id", "issue": "负责人不存在", "severity": 92, "owner": "HRBP"},
        {"employee_id": "E1002", "department": "Finance", "field": "email", "issue": "邮箱重复", "severity": 88, "owner": "HRIS"},
        {"employee_id": "E1003", "department": "Operations", "field": "location", "issue": "地点为空", "severity": 68, "owner": "HR Ops"},
        {"employee_id": "E1004", "department": "Sales", "field": "department", "issue": "部门编码冲突", "severity": 84, "owner": "HRIS"},
        {"employee_id": "E1005", "department": "Product", "field": "start_date", "issue": "日期格式错误", "severity": 61, "owner": "HR Ops"},
        {"employee_id": "E1006", "department": "Operations", "field": "employee_id", "issue": "疑似重复员工", "severity": 96, "owner": "HRIS"},
    ]


def _build_result(template_id: str, config: dict[str, object]) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
    threshold = int(config["threshold"])
    if template_id == "application-funnel":
        rows = _funnel_rows()
        applied = len(rows)
        meaningful = sum(row["status"] in {"resume_requested", "screening", "interview", "rejected"} for row in rows)
        interviews = sum(row["status"] == "interview" for row in rows)
        strong_match = sum(int(row["match_score"]) >= threshold for row in rows)
        metrics = [
            ("样本投递", str(applied)),
            ("有效反馈", str(meaningful)),
            ("有效回复率", f"{meaningful / applied:.1%}"),
            (f"匹配分 ≥ {threshold}", str(strong_match)),
            ("面试", str(interviews)),
        ]
        return rows, metrics
    if template_id == "jd-project-engine":
        rows = _jd_rows()
        ready = sum(int(row["coverage_score"]) >= threshold for row in rows)
        average = sum(int(row["coverage_score"]) for row in rows) / len(rows)
        return rows, [("能力缺口", str(len(rows))), ("达到阈值", str(ready)), ("平均覆盖", f"{average:.0f}"), ("验证阈值", str(threshold))]
    rows = _hr_rows()
    urgent = sum(int(row["severity"]) >= threshold for row in rows)
    owners = len({str(row["owner"]) for row in rows if int(row["severity"]) >= threshold})
    return rows, [("异常记录", str(len(rows))), ("优先处理", str(urgent)), ("涉及负责人", str(owners)), ("优先阈值", str(threshold))]


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def _report_html(
    *,
    config: dict[str, object],
    template_title: str,
    metrics: list[tuple[str, str]],
    rows: list[dict[str, object]],
) -> str:
    headings = list(rows[0])
    metric_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in metrics
    )
    head_html = "".join(f"<th>{html.escape(item.replace('_', ' '))}</th>" for item in headings)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row[item]))}</td>" for item in headings) + "</tr>"
        for row in rows
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(config['project_name']))}</title>
<style>
:root{{--ink:#17202a;--muted:#64707d;--line:#dfe4e8;--accent:#9f263d;--surface:#f4f6f7}}
*{{box-sizing:border-box}}body{{margin:0;background:#eef2f4;color:var(--ink);font:15px/1.75 system-ui,"Microsoft YaHei",sans-serif}}
main{{width:min(1120px,calc(100% - 32px));margin:32px auto;background:white;border:1px solid #fff;border-radius:22px;padding:clamp(24px,5vw,60px);box-shadow:0 20px 60px #1e293b18}}
.eyebrow{{color:var(--accent);font-size:12px;letter-spacing:.12em;text-transform:uppercase}}h1{{font-size:clamp(28px,5vw,48px);line-height:1.22;letter-spacing:-.04em;margin:.25em 0}}.lead{{max-width:70ch;color:var(--muted)}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin:36px 0;border-block:1px solid var(--line)}}.metrics div{{padding:22px 14px;border-right:1px solid var(--line)}}.metrics span{{display:block;color:var(--muted);font-size:12px}}.metrics strong{{display:block;font-size:30px;margin-top:5px}}
.table{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:12px;border-bottom:1px solid var(--line);white-space:nowrap}}th{{color:var(--muted);font-weight:600;background:var(--surface)}}footer{{margin-top:30px;color:var(--muted);font-size:12px}}
</style></head><body><main>
<div class="eyebrow">AI-assisted portfolio prototype</div>
<h1>{html.escape(str(config['project_name']))}</h1>
<p class="lead"><strong>{html.escape(template_title)}</strong><br>{html.escape(str(config['question']))}<br>分析重点：{html.escape(str(config['focus']))}</p>
<section class="metrics">{metric_html}</section>
<div class="table"><table><thead><tr>{head_html}</tr></thead><tbody>{row_html}</tbody></table></div>
<footer>全部数据均为合成数据。该页面由固定的本地安全引擎生成，没有执行外部命令或任意 AI 代码。</footer>
</main></body></html>"""


def _readme(job: JobDetail, recommendation: dict[str, object]) -> str:
    return f"""# {recommendation['title']}

目标岗位：{job.company} · {job.title}

这是一个由 Job Agent 用固定模板搭起的 AI-assisted 项目。它只使用合成数据，不会读取你的简历、API Key、浏览器 Cookie 或其他私人文件。

## 你要做什么

1. 先打开 `学习路线.md`，用最简单的话理解输入、处理和输出。
2. 在软件里修改“项目要回答的问题”或“分析重点”。
3. 点击“保存修改并重新运行”，再打开演示页检查结果。
4. 用自己的话讲清楚为什么这样改、结果说明什么。
5. 通过软件里的验证门槛后，项目才会显示“可写入简历”。

## 真实性边界

- 可以说：AI 辅助搭建，本人负责需求、关键配置修改、验证、迭代与解释。
- 不可以说：项目完全由本人从零独立开发，或使用了真实公司数据。
- 当前项目不会自动写入经历库或简历。
"""


def _learning_guide(recommendation: dict[str, object]) -> str:
    capabilities = "、".join(str(item) for item in recommendation["capabilities"])
    return f"""# 学习路线｜像给三岁小朋友讲

## 这个项目像什么

想象桌上有一堆积木。原始数据就是散着的积木；规则负责把同类积木放到一起；演示页负责告诉别人，你发现了什么。

## 三个盒子

1. **输入**：`data/synthetic-data.csv`，全部是假数据，可以放心练习。
2. **处理**：软件读取 `project_config.json` 中的问题、重点和阈值，再做固定计算。
3. **输出**：`output/report.html` 是可以展示的结果，`output/summary.json` 是机器可读结果。

## 你真正要学会的能力

{capabilities}

## 面试时怎么讲

先说问题，再说你改了什么规则，然后展示结果，最后说下一步会怎么验证。不会的地方直接说仍在学习，不要把 AI 生成说成独立完成。
"""


def project_workshop_snapshot(
    output_dir: Path,
    job: JobDetail,
    *,
    gaps: list[str] | None = None,
) -> dict[str, object]:
    recommendation = recommend_project(job, gaps)
    root = _project_root(output_dir, job.job_id)
    manifest_path = root / "project.json"
    if not manifest_path.is_file():
        return {
            "status": "idea",
            "status_label": "建议项目",
            "recommendation": recommendation,
            "config": None,
            "preview_url": None,
            "resume_eligible": False,
            "user_modified": False,
            "tests_passed": False,
            "requirements": {
                "ran": False,
                "modified": False,
                "demo_confirmed": False,
                "explained": False,
            },
        }
    manifest = _read_json(manifest_path, label="项目记录")
    config_path = root / "project_config.json"
    config = _validate_config(_read_json(config_path, label="项目设置"))
    current_hash = _hash_bytes(_json_bytes(config))
    user_modified = current_hash != str(manifest.get("initial_config_sha256") or "")
    ran_current = current_hash == str(manifest.get("last_run_config_sha256") or "")
    tests_passed = bool(manifest.get("tests_passed")) and ran_current
    status = str(manifest.get("status") or "ran")
    if not ran_current:
        status = "user_modified"
    labels = {
        "ran": "已跑通",
        "user_modified": "已修改，待重跑",
        "resume_ready": "已验证，可写入简历",
    }
    verification = manifest.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    return {
        "status": status,
        "status_label": labels.get(status, status),
        "recommendation": recommendation,
        "config": config,
        "preview_url": f"/project-workshop/{job.job_id}/preview" if (root / "output" / "report.html").is_file() else None,
        "resume_eligible": bool(manifest.get("resume_eligible")) and ran_current,
        "user_modified": user_modified,
        "tests_passed": tests_passed,
        "requirements": {
            "ran": ran_current and tests_passed,
            "modified": user_modified,
            "demo_confirmed": bool(verification.get("demo_confirmed")),
            "explained": bool(verification.get("explanation")),
        },
    }


def run_project(
    output_dir: Path,
    job: JobDetail,
    *,
    config_update: object | None = None,
    gaps: list[str] | None = None,
) -> dict[str, object]:
    recommendation = recommend_project(job, gaps)
    template_id = str(recommendation["template_id"])
    root = _project_root(output_dir, job.job_id)
    manifest_path = root / "project.json"
    config_path = root / "project_config.json"
    manifest = _read_json(manifest_path, label="项目记录") if manifest_path.is_file() else {}

    if config_path.is_file():
        config = _validate_config(_read_json(config_path, label="项目设置"))
    else:
        config = _default_config(job, template_id)
    if config_update is not None:
        config = _validate_config(config_update)
    config_bytes = _json_bytes(config)
    initial_hash = str(manifest.get("initial_config_sha256") or _hash_bytes(config_bytes))

    rows, metrics = _build_result(template_id, config)
    report = _report_html(
        config=config,
        template_title=str(recommendation["title"]),
        metrics=metrics,
        rows=rows,
    )
    summary = {
        "template_id": template_id,
        "generated_at": _now(),
        "row_count": len(rows),
        "metrics": [{"label": label, "value": value} for label, value in metrics],
        "data_policy": "synthetic_only",
    }

    _write_atomic(config_path, config_bytes)
    _write_atomic(root / "README.md", _readme(job, recommendation).encode("utf-8"))
    _write_atomic(root / "学习路线.md", _learning_guide(recommendation).encode("utf-8"))
    _write_atomic(root / "data" / "synthetic-data.csv", _csv_bytes(rows))
    _write_atomic(root / "output" / "summary.json", _json_bytes(summary))
    _write_atomic(root / "output" / "report.html", report.encode("utf-8"))

    current_hash = _hash_bytes(config_bytes)
    user_modified = current_hash != initial_hash
    tests_passed = (
        len(rows) > 0
        and summary["row_count"] == len(rows)
        and (root / "output" / "report.html").is_file()
    )
    prior_ready = bool(manifest.get("resume_eligible")) and current_hash == str(manifest.get("last_run_config_sha256") or "")
    manifest = {
        "schema_version": "project-workshop-v1",
        "job_id": job.job_id,
        "target_job": f"{job.company} · {job.title}",
        "template_id": template_id,
        "status": "resume_ready" if prior_ready else ("user_modified" if user_modified else "ran"),
        "created_at": manifest.get("created_at") or _now(),
        "updated_at": _now(),
        "initial_config_sha256": initial_hash,
        "last_run_config_sha256": current_hash,
        "tests_passed": tests_passed,
        "test_summary": f"{len(rows)} 条合成记录通过固定计算和输出完整性检查。",
        "resume_eligible": prior_ready,
        "verification": manifest.get("verification") if prior_ready else None,
        "safety": {
            "data": "synthetic_only",
            "arbitrary_code_execution": False,
            "external_commands": [],
            "secret_access": False,
        },
    }
    _write_atomic(manifest_path, _json_bytes(manifest))
    return project_workshop_snapshot(output_dir, job, gaps=gaps)


def verify_project(
    output_dir: Path,
    job: JobDetail,
    *,
    explanation: str,
    demo_confirmed: bool,
    understanding_confirmed: bool,
    gaps: list[str] | None = None,
) -> dict[str, object]:
    root = _project_root(output_dir, job.job_id)
    manifest_path = root / "project.json"
    config_path = root / "project_config.json"
    if not manifest_path.is_file() or not config_path.is_file():
        raise ProjectWorkshopError("请先生成并运行项目。")
    manifest = _read_json(manifest_path, label="项目记录")
    config = _validate_config(_read_json(config_path, label="项目设置"))
    current_hash = _hash_bytes(_json_bytes(config))
    if current_hash == str(manifest.get("initial_config_sha256") or ""):
        raise ProjectWorkshopError("你还没有修改关键项目设置；先改问题、重点或阈值，再重新运行。")
    if current_hash != str(manifest.get("last_run_config_sha256") or "") or not manifest.get("tests_passed"):
        raise ProjectWorkshopError("当前修改尚未重新跑通，请先点击“保存修改并重新运行”。")
    if not demo_confirmed:
        raise ProjectWorkshopError("请先打开演示页并确认结果可展示。")
    if not understanding_confirmed:
        raise ProjectWorkshopError("请确认你能解释输入、规则和输出。")
    cleaned_explanation = explanation.strip()
    if len(cleaned_explanation) < 80:
        raise ProjectWorkshopError("请至少用 80 个字讲清问题、你的修改、结果和下一步。")
    if len(cleaned_explanation) > 2000:
        raise ProjectWorkshopError("项目解释最多 2000 个字符。")

    manifest["status"] = "resume_ready"
    manifest["resume_eligible"] = True
    manifest["updated_at"] = _now()
    manifest["verification"] = {
        "verified_at": _now(),
        "demo_confirmed": True,
        "understanding_confirmed": True,
        "explanation": cleaned_explanation,
        "truth_label": "AI-assisted; user-owned requirements, modification, validation and explanation",
    }
    _write_atomic(manifest_path, _json_bytes(manifest))
    return project_workshop_snapshot(output_dir, job, gaps=gaps)


def project_preview_path(output_dir: Path, job_id: int) -> Path:
    path = _project_root(output_dir, job_id) / "output" / "report.html"
    if not path.is_file():
        raise ProjectWorkshopError("项目演示页尚未生成。")
    return path
