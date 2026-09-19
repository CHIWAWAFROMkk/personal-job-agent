import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from job_agent.services.browser_use_agent import get_safe_controller_class


class BrowserActionBoundaries(unittest.TestCase):
    def test_denied_paths_never_delegate(self):
        from browser_use import Controller, ActionResult
        cases = [
            {'send_keys': {'keys':'ENTER'}}, {'evaluate': {'code':'form.submit()'}},
            {'new_action': {}}, {'click': {'coordinate_x':10,'coordinate_y':20}},
            {'click': {'index':1}}, {'input': {'index':1,'text':'hello\n'}},
        ]
        for payload in cases:
            with self.subTest(action=next(iter(payload))):
                controller=get_safe_controller_class()()
                session=SimpleNamespace(get_element_by_index=AsyncMock(side_effect=RuntimeError('synthetic')))
                action=SimpleNamespace(model_dump=lambda **kw:payload)
                with patch.object(Controller,'act',new=AsyncMock(return_value=ActionResult())) as base:
                    result=asyncio.run(controller.act(action,session))
                base.assert_not_awaited()
                self.assertIn('Hard Stop',result.error)

    def test_unknown_and_submit_buttons_require_manual_click(self):
        from browser_use import Controller
        for text in ['投个简历','下一步','Finish']:
            controller=get_safe_controller_class()()
            node=SimpleNamespace(attributes={},parent=None,get_all_children_text=lambda:text)
            session=SimpleNamespace(get_element_by_index=AsyncMock(return_value=node))
            action=SimpleNamespace(model_dump=lambda **kw:{'click':{'index':1}})
            with patch.object(Controller,'act',new=AsyncMock()) as base:
                result=asyncio.run(controller.act(action,session))
            base.assert_not_awaited()
            self.assertIn('Hard Stop',result.error)
