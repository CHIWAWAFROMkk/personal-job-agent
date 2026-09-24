import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from job_agent.models.profile import Profile
from job_agent.services.ai_provider import get_openai_client, AIProviderError
from job_agent.services.browser_use_agent import build_browser_use_task_prompt
from job_agent.services.interview_copilot import generate_interview_prep
from job_agent.services.outreach_copilot import generate_greetings
from job_agent.services.runtime_config import RuntimeConfig, AIConnectorConfig
from tests.helpers import sample_profile


class PrivateProfileCopilotTests(unittest.TestCase):
    def test_runtime_openai_default_and_explicit_model(self):
        self.assertEqual(AIConnectorConfig(provider='openai').model, 'gpt-4o')
        self.assertEqual(AIConnectorConfig(provider='openai', model='user-selected-model').model, 'user-selected-model')

    def setUp(self):
        self.job = SimpleNamespace(job_id=1, company='示例企业', title='示例岗位', jd_text='合成岗位职责', sources=[])

    def test_empty_profile_is_neutral(self):
        prompt = build_browser_use_task_prompt(self.job, Profile(), None)
        self.assertIn('未填写', prompt)
        self.assertIn('尚未录入已确认经历', prompt)
        self.assertNotIn('None', prompt)
        self.assertIn('不得请求或自动填写验证码', prompt)
        greetings = generate_greetings(self.job, Profile(), RuntimeConfig())
        text = ' '.join(x.content for x in greetings.greetings)
        for unsupported in ['实习经验', '立即到岗', '5天', '3个月', '开发实践']:
            self.assertNotIn(unsupported, text)
        result = generate_interview_prep(self.job, Profile(), RuntimeConfig())
        self.assertIn('尚未录入已确认经历', str(result.to_dict()))

    def test_local_outreach_uses_only_confirmed_facts(self):
        profile = sample_profile()
        text = str(generate_greetings(self.job, profile, RuntimeConfig()))
        self.assertIn(profile.experiences[0].facts[0].statement, text)
        self.assertNotIn(profile.experiences[0].facts[1].statement, text)

    def test_legacy_imported_name_is_not_an_experience(self):
        profile = sample_profile()
        profile.person.display_name = '验收用户'
        original = profile.experiences[0].facts[0]
        identity = original.model_copy(update={'id': 'synthetic-name', 'statement': '验收用户（合成资料）'})
        profile.experiences[0].facts.insert(0, identity)
        text = str(generate_greetings(self.job, profile, RuntimeConfig()))
        self.assertNotIn('我的相关经历：验收用户', text)
        self.assertIn(original.statement, text)

    @patch('openai.OpenAI')
    def test_client_defaults_explicit_models_and_limits(self, factory):
        for provider, default in [('openai', 'gpt-4o'), ('deepseek', 'deepseek-chat')]:
            config = SimpleNamespace(ai=SimpleNamespace(provider=provider, api_key='synthetic-key', model='', base_url=None))
            _, model = get_openai_client(config)
            self.assertEqual(model, default)
            self.assertEqual(factory.call_args.kwargs['timeout'], 25)
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
            config.ai.model = 'user-selected-model'
            self.assertEqual(get_openai_client(config)[1], 'user-selected-model')

    def test_client_rejects_unsupported_and_missing_configuration(self):
        for provider, key, model, url in [('local', '', '', None), ('openai', '', 'm', None), ('openai_compatible', 'synthetic', '', 'https://example.com')]:
            config = SimpleNamespace(ai=SimpleNamespace(provider=provider, api_key=key, model=model, base_url=url))
            with self.assertRaises(AIProviderError):
                get_openai_client(config)

    @patch('job_agent.services.interview_copilot.get_openai_client')
    def test_cloud_prompt_has_only_current_verified_evidence(self, helper):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError('offline')
        helper.return_value = (client, 'synthetic-model')
        profile = sample_profile()
        config = SimpleNamespace(ai=SimpleNamespace(provider='openai'))
        generate_interview_prep(self.job, profile, config)
        messages = str(client.chat.completions.create.call_args.kwargs['messages'])
        self.assertIn(profile.education[0].institution, messages)
        self.assertIn(profile.experiences[0].facts[0].statement, messages)
        self.assertNotIn(profile.experiences[0].facts[1].statement, messages)
        self.assertNotIn(profile.person.contact.email, messages)
