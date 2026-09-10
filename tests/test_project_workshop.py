from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from job_agent.models.job_record import JobDetail
from job_agent.services.project_workshop import (
    ProjectWorkshopError,
    project_preview_path,
    project_workshop_snapshot,
    recommend_project,
    run_project,
    verify_project,
)


def make_job(*, title: str = "数据运营实习生", jd: str = "负责业务数据分析与漏斗复盘") -> JobDetail:
    return JobDetail(
        job_id=7,
        company="示例科技",
        title=title,
        location="上海",
        jd_text=jd,
        status="discovered",
        match_score=80,
        sources=[],
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        first_seen_at="2026-09-01T00:00:00+00:00",
        last_seen_at="2026-09-01T00:00:00+00:00",
    )


class ProjectWorkshopTests(unittest.TestCase):
    def test_template_selection_prefers_hr_and_product_specific_projects(self) -> None:
        hr = make_job(title="HR 数字化实习生", jd="维护员工主数据")
        product = make_job(title="AI 产品运营实习生", jd="拆解用户需求")
        self.assertEqual(recommend_project(hr)["template_id"], "hr-data-quality")
        self.assertEqual(recommend_project(product)["template_id"], "jd-project-engine")

    def test_project_must_be_modified_rerun_and_explained_before_resume_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            job = make_job()
            idea = project_workshop_snapshot(output_dir, job, gaps=["漏斗指标证据不足"])
            self.assertEqual(idea["status"], "idea")

            ran = run_project(output_dir, job, gaps=["漏斗指标证据不足"])
            self.assertEqual(ran["status"], "ran")
            self.assertTrue(ran["tests_passed"])
            self.assertFalse(ran["resume_eligible"])
            self.assertTrue(project_preview_path(output_dir, job.job_id).is_file())
            root = output_dir / "project-workshop" / f"job-{job.job_id}"
            manifest = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["safety"]["data"], "synthetic_only")
            self.assertFalse(manifest["safety"]["arbitrary_code_execution"])
            self.assertEqual(manifest["safety"]["external_commands"], [])

            with self.assertRaisesRegex(ProjectWorkshopError, "还没有修改"):
                verify_project(
                    output_dir,
                    job,
                    explanation="这是一段还不应该被接受的说明。" * 10,
                    demo_confirmed=True,
                    understanding_confirmed=True,
                )

            config = dict(ran["config"])
            config["question"] = "高匹配岗位是否真的比低匹配岗位更容易获得面试？"
            config["threshold"] = 80
            rerun = run_project(output_dir, job, config_update=config)
            self.assertEqual(rerun["status"], "user_modified")
            self.assertTrue(rerun["user_modified"])
            self.assertTrue(rerun["requirements"]["ran"])

            explanation = (
                "我想验证高匹配岗位是否更容易进入面试，所以把判断阈值从七十五分改成八十分，"
                "重新运行后检查了有效反馈和面试数量。结果只来自合成数据，不能代表真实招聘规律；"
                "下一步我会换成自己的匿名投递记录，再比较不同岗位方向和简历版本。"
            )
            ready = verify_project(
                output_dir,
                job,
                explanation=explanation,
                demo_confirmed=True,
                understanding_confirmed=True,
            )
            self.assertEqual(ready["status"], "resume_ready")
            self.assertTrue(ready["resume_eligible"])
            self.assertTrue(ready["requirements"]["explained"])

    def test_invalid_config_is_rejected_without_arbitrary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            job = make_job()
            with self.assertRaisesRegex(ProjectWorkshopError, "未知字段"):
                run_project(
                    output_dir,
                    job,
                    config_update={
                        "project_name": "测试",
                        "question": "测试什么",
                        "focus": "测试能力",
                        "threshold": 75,
                        "command": "do-something",
                    },
                )


if __name__ == "__main__":
    unittest.main()
