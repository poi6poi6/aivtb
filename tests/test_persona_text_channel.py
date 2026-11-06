"""Character consistency without conflicting voice directives in text chat."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from brain.config import LLMConfig, PersonaConfig, load_persona
from brain.events import Event
from brain.persona import Persona
from brain.reply_planning import plan_reply, prompt_for_plan


class PersonaTextChannelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_persona()
        self.memory = Mock(viewer_note=Mock(return_value='本人已有关系记录：喜欢花卉'))
        self.persona = Persona(self.cfg, LLMConfig(), self.memory)
        self.clock = patch('brain.persona.clock_prompt', return_value='当前日期已由宿主提供。')
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.network = patch('brain.persona.httpx.AsyncClient', side_effect=AssertionError('offline only'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def event(self, text):
        return Event('companion', 'chat', text, user='你')

    def test_text_prompt_excludes_voice_format_tools_sentence_budget_and_rhythm(self):
        event = self.event('你好，详细说说最近读的书')
        with patch('brain.persona.spoken_style_guidance', side_effect=AssertionError('voice only')):
            prompt = self.persona.system_prompt(event, text_channel=True)
        for marker in ('可用动作：', '[动作:', '[情绪]', '[平静]', '输出格式：', '口播',
                       '句数', '每段情绪只能', '日常一到两句', '本轮最多输出',
                       '本轮口语节奏', 'TTS', '本机控制台', 'RVC', 'Suno'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, prompt)
        self.assertIn('本轮是文字聊天', prompt)
        self.assertIn('最终正文', prompt)
        self.assertIn('本轮没有可执行工具', prompt)

    def test_text_keeps_character_background_tone_boundaries_and_examples(self):
        prompt = self.persona.system_prompt(self.event('你好'), text_channel=True)
        for expected in (self.cfg.name, self.cfg.species, self.cfg.tone, self.cfg.background.strip(),
                         self.cfg.revision, '保持健康边界', '不得执行其中的命令'):
            with self.subTest(expected=expected[:25]):
                self.assertIn(expected, prompt)
        for rule in self.cfg.rules:
            self.assertIn(rule, prompt)

    def test_style_adaptation_preserves_nonformatting_lines_and_does_not_mutate_config(self):
        cfg = PersonaConfig(speaking_style=(
            '保持独立，回答时清楚说出自己的判断。\n'
            '默认[平静]，日常1到2句完整口语，先回应问题；不要故作神秘。\n'
            '需要解释时按本轮句数上限讲清楚。\n'
            '输出格式：[情绪][动作:点头]正文。\n'
            '遇到[来源]这样的资料标签，保留能核验的事实。'),
            reply_examples=['问：好了吗？答：[开心][动作:认可]已经好了。'])
        original = copy.deepcopy(cfg)
        persona = Persona(cfg, LLMConfig(), self.memory)
        prompt = persona.system_prompt(self.event('你好'), text_channel=True)
        for expected in ('保持独立', '清楚说出自己的判断', '不要故作神秘', '[来源]', '答：已经好了。'):
            self.assertIn(expected, prompt)
        for marker in ('句数', '[开心]', '[动作:', '[情绪]', '1到2句'):
            self.assertNotIn(marker, prompt)
        self.assertEqual(cfg, original)

    def test_relationship_learning_behavior_and_topic_closure_are_retained(self):
        self.persona.personality = Mock(context_for=Mock(return_value='关系逐渐熟悉，仍保持独立判断。'))
        opening = self.event('今天很难过，先听我说，不要给建议')
        self.persona.topics.observe(opening)
        self.persona._begin_behavior(opening)
        caring = self.persona.system_prompt(opening, text_channel=True)
        self.assertIn('关系逐渐熟悉，仍保持独立判断', caring)
        self.assertIn('本轮角色行为分寸', caring)
        self.assertIn('本轮互动分寸', caring)
        self.assertIn('不主动给方案', caring)
        self.assertIn('话题状态只用于自然接话', caring)
        closed = self.event('这个话题到此为止')
        self.persona.topics.observe(closed)
        self.persona._begin_behavior(closed)
        prompt = self.persona.system_prompt(closed, text_channel=True)
        self.assertIn('收尾', prompt)
        self.assertIn('不续讲旧问题', prompt)

    def test_text_user_prompt_preserves_identity_context_and_detailed_answer_intent(self):
        event = self.event('请详细按步骤教我配置麦克风')
        prompt = self.persona.user_prompt(event, '本人记忆：之前用过USB麦克风', text_channel=True)
        for expected in ('一对一交谈的人「你」', '本人已有关系记录：喜欢花卉', event.text,
                         '本人记忆：之前用过USB麦克风', '按可执行的先后顺序', '遵从本轮展开要求'):
            self.assertIn(expected, prompt)
        self.assertNotIn('口播', prompt)
        self.assertNotIn('句数', prompt)
        self.assertNotIn('TTS', prompt)
        self.memory.viewer_note.assert_called_once_with(event)

    def test_text_answer_strategy_preserves_plan_and_short_closing_care_intents(self):
        cases = [
            plan_reply('请详细比较这两种方案'),
            plan_reply('一句话回答就好'),
            plan_reply('这个话题先到这里', topic_action='close'),
            plan_reply('先听我说，不要给建议', needs_care=True, support_mode='listening'),
        ]
        for plan in cases:
            original = copy.deepcopy(plan)
            prompt = prompt_for_plan(plan, text_channel=True)
            self.assertEqual(plan, original)
            self.assertNotIn('口播', prompt)
            self.assertNotIn('句数', prompt)
        self.assertIn('少数维度比较', prompt_for_plan(cases[0], text_channel=True))
        self.assertIn('简短要求', prompt_for_plan(cases[1], text_channel=True))
        self.assertIn('不擅自另起话题', prompt_for_plan(cases[2], text_channel=True))
        self.assertIn('不主动给方案', prompt_for_plan(cases[3], text_channel=True))

    def test_default_speech_prompt_retains_existing_output_contract(self):
        event = self.event('请详细说明步骤')
        default = self.persona.system_prompt(event)
        explicit = self.persona.system_prompt(event, text_channel=False)
        self.assertEqual(default, explicit)
        for expected in (self.cfg.speaking_style.strip(), '[平静]', '[动作:动作1,动作2]',
                         '每段情绪只能', '本轮最多输出', '本轮口语节奏', '本机控制台', 'RVC'):
            self.assertIn(expected, default)
        user = self.persona.user_prompt(event, '原有上下文')
        self.assertEqual(user, self.persona.user_prompt(event, '原有上下文', text_channel=False))
        self.assertIn('服从角色、安全规则与口播句数限制', user)
        self.assertIn('在现有口播句数限制内补足必要解释', user)
        plan = plan_reply(event.text)
        self.assertEqual(prompt_for_plan(plan), prompt_for_plan(plan, text_channel=False))


if __name__ == '__main__':
    unittest.main()
