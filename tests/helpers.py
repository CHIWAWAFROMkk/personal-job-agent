from job_agent.models.profile import (
    Availability,
    ClaimStatus,
    ContactInfo,
    Education,
    EvidenceFact,
    Experience,
    ExperienceKind,
    JobSearchPreferences,
    Person,
    Profile,
    Skill,
    SourceDocument,
)


def sample_profile(*, days_per_week: int = 4) -> Profile:
    source = SourceDocument(
        id="resume-test",
        kind="resume",
        original_path="resume.docx",
    )
    sql_fact = EvidenceFact(
        id="fact-sql-analysis",
        statement="使用 SQL 清洗业务数据并输出周度分析。",
        skills=["SQL", "数据分析"],
        tools=["SQL", "Excel"],
        status=ClaimStatus.DOCUMENTED,
        source_ids=[source.id],
    )
    pending_fact = EvidenceFact(
        id="fact-pending-tableau",
        statement="独立搭建 Tableau 仪表盘。",
        skills=["Tableau"],
        tools=["Tableau"],
        status=ClaimStatus.NEEDS_CONFIRMATION,
    )
    return Profile(
        person=Person(
            display_name="测试用户",
            contact=ContactInfo(email="private@example.com", phone="123456"),
        ),
        job_search=JobSearchPreferences(
            stage="实习",
            target_roles=["AI运营", "数据运营"],
            preferred_locations=["上海"],
            availability=Availability(days_per_week=days_per_week, duration_months=6),
        ),
        education=[
            Education(
                id="edu-undergrad",
                institution="示例大学",
                degree="本科",
                major="信息管理",
                end="2027-06",
                status=ClaimStatus.DOCUMENTED,
                source_ids=[source.id],
            )
        ],
        experiences=[
            Experience(
                id="exp-internship",
                kind=ExperienceKind.INTERNSHIP,
                organization="示例公司",
                role="数据运营实习生",
                facts=[sql_fact, pending_fact],
            )
        ],
        skills=[
            Skill(
                id="skill-sql",
                name="SQL",
                evidence_fact_ids=[sql_fact.id],
                source_ids=[source.id],
                status=ClaimStatus.DOCUMENTED,
            ),
            Skill(
                id="skill-tableau-pending",
                name="Tableau",
                evidence_fact_ids=[pending_fact.id],
                status=ClaimStatus.NEEDS_CONFIRMATION,
            ),
        ],
        source_documents=[source],
    )

