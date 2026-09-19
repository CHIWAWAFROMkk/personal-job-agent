import unittest
from job_agent.models.profile import Experience, EvidenceFact
from job_agent.services.portable_resume import _selected_experiences
from job_agent.services.local_matcher import match_job_locally, structure_job_locally
from tests.helpers import sample_profile


class SelectionCoverageTests(unittest.TestCase):
    def test_non_keyword_project_is_not_silently_dropped(self):
        profile=sample_profile()
        profile.experiences.append(Experience(id='exp-extra',kind='project',organization='示例组织',role='协作实践',facts=[EvidenceFact(id='fact-extra',statement='协调组员完成成果展示。',status='documented')]))
        result=match_job_locally(profile,structure_job_locally('SQL 数据分析'))
        selected=_selected_experiences(profile,result,'SQL 数据分析')
        self.assertIn('exp-extra',[e.id for e,_ in selected])

    def test_five_experiences_have_reserved_space_and_duplicates_removed(self):
        profile=sample_profile()
        profile.skills=[]
        profile.experiences=[]
        for i in range(5):
            profile.experiences.append(Experience(id=f'exp-{i}',kind='internship',organization='示例组织',role='实践',facts=[EvidenceFact(id=f'fact-{i}-{j}',statement=f'SQL 数据分析交付 {j % 2}。',status='documented') for j in range(4)]))
        result=match_job_locally(profile,structure_job_locally('SQL 数据分析'))
        selected=_selected_experiences(profile,result,'SQL 数据分析')
        self.assertEqual(len(selected),5)
        self.assertLessEqual(sum(len(f) for _,f in selected),10)
        for _, facts in selected:
            self.assertGreaterEqual(len(facts),1)
            self.assertEqual(len(facts),len({f.statement for f in facts}))
