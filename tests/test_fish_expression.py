from __future__ import annotations

import re
import unittest

from brain.fish_expression import fish_speech_text, plan_fish_speech


_CUE = re.compile(r'\[[^\]\r\n]+\] ?')


class FishExpressionTests(unittest.TestCase):
    def test_legacy_mode_retains_exact_emotion_contract(self):
        original = '今天开心吗？我已经准备好了，可以听你慢慢讲今天的故事。'
        for emotion, prefix in [('平静', ''), ('开心', '[happy] '),
                                ('委屈', '[sad] '), ('惊讶', '[surprised] '),
                                ('得意', '[confident] '), ('吐槽', '[sarcastic] '),
                                ('未知', '')]:
            with self.subTest(emotion=emotion):
                self.assertEqual(fish_speech_text(original, emotion, natural=False),
                                 prefix + original)

    def test_natural_delivery_adds_a_conversational_cue_for_known_emotions(self):
        original = '我在听，你慢慢说。'
        for emotion in ('平静', '开心', '委屈', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                delivered = fish_speech_text(original, emotion)
                self.assertNotEqual(delivered, fish_speech_text(original, emotion, natural=False))
                self.assertTrue(delivered.startswith('['))
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_empty_and_unknown_emotions_do_not_invent_delivery_or_speech(self):
        original = '今天开心吗？我已经准备好了，可以听你慢慢讲今天的故事。'
        for natural in (False, True):
            with self.subTest(natural=natural):
                self.assertEqual(fish_speech_text('', '开心', natural=natural), '')
                self.assertEqual(fish_speech_text(original, '未知', natural=natural), original)

    def test_words_numbers_punctuation_and_whitespace_are_preserved(self):
        original = '今天 19:30 到 20:15，  我们一起听第 3 首歌；然后聊一聊今天发生的事情。'
        for emotion in ('平静', '开心', '委屈', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                self.assertEqual(_CUE.sub('', fish_speech_text(original, emotion)), original)

    def test_neutral_question_gets_a_distinct_tone_and_returns_to_conversation(self):
        original = '今天过得怎么样？我在这里。'
        delivered = fish_speech_text(original, '平静')
        cues = _CUE.findall(delivered)
        self.assertEqual(_CUE.sub('', delivered), original)
        self.assertEqual(len(cues), 2)
        self.assertNotEqual(cues[0], cues[1])
        self.assertIn('？[', delivered)
        statement = fish_speech_text('今天过得不错。', '平静')
        self.assertNotEqual(cues[0], _CUE.findall(statement)[0])

    def test_question_at_end_does_not_append_an_unspoken_reset(self):
        original = '今天过得怎么样？'
        delivered = fish_speech_text(original, '平静')
        self.assertEqual(len(_CUE.findall(delivered)), 1)
        self.assertTrue(delivered.endswith(original))

    def test_multiple_questions_have_bounded_delivery_and_pause_cues(self):
        original = '我已经准备好了。今天过得怎么样？想听什么歌？我找找看，你慢慢告诉我就好。'
        delivered = fish_speech_text(original, '平静')
        cues = [cue.strip() for cue in _CUE.findall(delivered)]
        self.assertLessEqual(sum(cue == '[short pause]' for cue in cues), 1)
        self.assertLessEqual(sum(cue != '[short pause]' for cue in cues), 3)
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_ordinary_long_reply_uses_punctuation_without_an_extra_pause(self):
        original = '今天发生的事情确实有点多，我们可以坐下来慢慢聊，你不用急着一次说完。'
        delivered = fish_speech_text(original, '平静')
        self.assertNotIn('[short pause]', delivered)
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_long_reply_can_emphasize_one_real_contrast_with_a_short_pause(self):
        original = '今天发生的事情确实有点多，不过我们可以坐下来慢慢聊，但是也不用急着一次说完。'
        delivered = fish_speech_text(original, '平静')
        self.assertEqual(delivered.count('[short pause]'), 1)
        self.assertRegex(delivered, r'，\[short pause\] (?:\[[^\]]+\] )?不过')
        tones = [cue for cue in _CUE.findall(delivered) if cue.strip() != '[short pause]']
        self.assertNotEqual(tones[0], tones[-1])
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_short_and_unbalanced_clauses_do_not_get_mechanical_pauses(self):
        originals = [
            '我在听，你慢慢说。',
            '今天发生的事情确实有点多。',
            '好的，今天发生的事情可以慢慢说给我听不用急着一次全部说完',
            '今天发生的事情可以慢慢说给我听不用急着一次全部说完，好吗',
            '今天发生的事情可以慢慢说给我听不用急着一次全部说完',
        ]
        for original in originals:
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '平静')
                self.assertNotIn('[short pause]', delivered)
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_formatted_numbers_are_never_split_by_pause_instructions(self):
        original = '今年预计收到的总金额为 123,456.78 元并会按照计划全部存好'
        delivered = fish_speech_text(original, '平静')
        self.assertIn('123,456.78', delivered)
        self.assertNotIn('[short pause]', delivered)

    def test_literal_content_gets_prefix_only(self):
        originals = [
            '你看看 https://example.com/query?a=12,b=34，我稍后会解释完整内容。',
            '你看看 www.example.com，里面包含了我们今天需要讨论的完整内容。',
            '代码是 ```python\nprint("今天，你好吗？")\n```，我稍后解释内容。',
            '她说“今天发生了很多事情，你想慢慢听我说吗？”然后开始讲述。',
            '她说「今天发生了很多事情，你想慢慢听我说吗？」然后开始讲述。',
            '她说『今天发生了很多事情，你想慢慢听我说吗？』然后开始讲述。',
            '她说‘今天发生了很多事情，你想慢慢听我说吗？’然后开始讲述。',
            '她说 "今天发生了很多事情，你想慢慢听我说吗？" 然后开始讲述。',
            "她说 '今天发生了很多事情，你想慢慢听我说吗？' 然后开始讲述。",
            '这项配置叫 [未知参数, 123]，我们可以等准备好以后再开始研究。',
            '这项配置叫【未知参数】，我们可以等准备好以后再开始研究。',
            '备注（这里需要保留原文，不能插入内容），我稍后会仔细说明。',
        ]
        for original in originals:
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '平静')
                self.assertTrue(delivered.endswith(original))
                prefix = delivered[:-len(original)]
                self.assertRegex(prefix, r'^\[[^\]\r\n]+\] $')

    def test_breath_uses_a_balanced_existing_boundary_and_resets_units(self):
        original = '今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。'
        plan = plan_fish_speech(original, '开心', breathing=True)
        self.assertEqual(_CUE.sub('', plan.text), original)
        self.assertEqual(plan.text.count('[soft inhale]'), 1)
        self.assertNotIn('[short pause]', plan.text)
        self.assertIsInstance(plan.breath_at, int)
        self.assertIn(original[plan.breath_at - 1], '，,。！？!?；;')
        before = sum(char.isalnum() for char in original[:plan.breath_at])
        after = sum(char.isalnum() for char in original[plan.breath_at:])
        self.assertGreaterEqual(before, 24)
        self.assertGreaterEqual(after, 6)
        self.assertEqual(plan.remaining_units, after)

    def test_breath_can_use_previous_speech_but_still_needs_a_balanced_clause(self):
        original = '今天终于把这个难题解开了，我们可以慢慢聊一聊后面的事情。'
        plan = plan_fish_speech(original, '平静', breathing=True, spoken_since_breath=12)
        self.assertEqual(plan.breath_at, original.index('，') + 1)
        self.assertEqual(_CUE.sub('', plan.text), original)
        self.assertEqual(plan.text.count('[soft inhale]'), 1)

    def test_accumulated_short_segments_can_take_one_breath_at_start(self):
        original = '我想慢慢听你讲一讲今天的事情。'
        plan = plan_fish_speech(original, '平静', breathing=True,
                                spoken_since_breath=28, continuation=True)
        self.assertEqual(plan.breath_at, 0)
        self.assertIn('[soft inhale]', plan.text[:plan.text.index('我')])
        self.assertEqual(plan.remaining_units, sum(char.isalnum() for char in original))
        self.assertEqual(_CUE.sub('', plan.text), original)

    def test_short_clauses_and_number_separators_never_force_a_breath(self):
        originals = [
            '好的，我在听。',
            '今天发生的事情可以慢慢说给我听不用急着一次全部说完，好吗',
            '今天发生的事情可以慢慢说给我听不用急着一次全部说完',
            '今年预计收到的总金额为 123,456.78 元并会按照计划全部存好',
        ]
        for original in originals:
            with self.subTest(original=original):
                plan = plan_fish_speech(original, '平静', breathing=True)
                self.assertIsNone(plan.breath_at)
                self.assertNotIn('[soft inhale]', plan.text)
                self.assertEqual(_CUE.sub('', plan.text), original)
        self.assertNotIn('[soft inhale]', fish_speech_text(
            '好的，我在听。', '平静', breathing=True, spoken_since_breath=80))

    def test_breathing_is_opt_in_and_legacy_mode_never_adds_breaths(self):
        original = '今天终于把这个难题解开了，想到你也一定会跟着高兴起来，我就忍不住想多说几句。'
        for options in ({}, {'breathing': False}, {'natural': False, 'breathing': True}):
            with self.subTest(options=options):
                plan = plan_fish_speech(original, '开心', spoken_since_breath=80, **options)
                self.assertIsNone(plan.breath_at)
                self.assertNotIn('[soft inhale]', plan.text)
                self.assertEqual(plan.text, fish_speech_text(
                    original, '开心', spoken_since_breath=80, **options))

    def test_literal_content_never_gets_a_breath_even_with_accumulated_units(self):
        originals = [
            '你看看 https://example.com/query?a=12,b=34，我稍后会解释完整内容。',
            '她说“今天发生了很多事情，你想慢慢听我说吗？”然后开始讲述。',
            '代码是 ```python\nprint("今天，你好吗？")\n```，我稍后解释内容。',
        ]
        for original in originals:
            with self.subTest(original=original):
                plan = plan_fish_speech(original, '开心', breathing=True, spoken_since_breath=80)
                self.assertIsNone(plan.breath_at)
                self.assertNotIn('[soft inhale]', plan.text)
                self.assertNotIn('[short pause]', plan.text)
                self.assertTrue(plan.text.endswith(original))

    def test_empty_text_does_not_create_a_breath_or_spoken_units(self):
        plan = plan_fish_speech('', '开心', breathing=True, spoken_since_breath=19)
        self.assertEqual(plan.text, '')
        self.assertIsNone(plan.breath_at)
        self.assertEqual(plan.remaining_units, 19)

    def test_emotional_questions_keep_their_emotion_and_limit_cues(self):
        original = '你真的想听这个故事吗？我还记得那天发生了很多事情。后来呢？我慢慢讲给你听。'
        neutral = _CUE.findall(fish_speech_text(original, '平静'))
        for emotion in ('开心', '委屈', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                plan = plan_fish_speech(original, emotion, breathing=True)
                cues = [cue for cue in _CUE.findall(plan.text)
                        if cue.strip() not in ('[short pause]', '[soft inhale]')]
                self.assertEqual(_CUE.sub('', plan.text), original)
                self.assertLessEqual(len(cues), 3)
                self.assertGreaterEqual(len(cues), 2)
                self.assertNotEqual(cues[0], neutral[0])
                self.assertNotEqual(cues[-1], neutral[-1])
                self.assertLessEqual(plan.text.count('[short pause]') + plan.text.count('[soft inhale]'), 1)

    def test_continuation_keeps_emotion_energy_without_changing_words(self):
        original = '我可以慢慢讲给你听。'
        for emotion in ('平静', '开心', '委屈', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                start = fish_speech_text(original, emotion)
                continuation = fish_speech_text(original, emotion, continuation=True)
                self.assertNotEqual(start, continuation)
                self.assertEqual(_CUE.sub('', continuation), original)
                if emotion != '委屈':
                    self.assertNotRegex(_CUE.findall(continuation)[0], r'\b(?:soft|softly|gentle|relaxed)\b')

    def test_neutral_conversation_is_not_always_a_soothing_delivery(self):
        ordinary = fish_speech_text('今天我们先聊聊这个新发现。', '平静')
        reassuring = fish_speech_text('没关系，我们可以慢慢来。', '平静')
        ordinary_tone, reassuring_tone = _CUE.findall(ordinary)[0], _CUE.findall(reassuring)[0]
        self.assertNotEqual(ordinary_tone, reassuring_tone)
        self.assertNotRegex(ordinary_tone, r'\b(?:gentle|soft|softly|relaxed|unhurried)\b')
        self.assertRegex(reassuring_tone, r'\b(?:reassuring|gentle|soft|unhurried)\b')

    def test_same_emotion_responds_to_reaction_question_and_reassurance(self):
        originals = ['今天这个故事我已经准备好了。', '今天这个故事你想听吗？',
                     '今天这个故事真是太有意思了！', '没关系，我可以陪你慢慢讲。']
        tones = []
        for original in originals:
            delivered = fish_speech_text(original, '开心')
            tones.append(_CUE.findall(delivered)[0])
            self.assertEqual(_CUE.sub('', delivered), original)
        self.assertEqual(len(set(tones)), len(originals))
        self.assertRegex(tones[1], r'\bhappy\b')
        self.assertRegex(tones[2], r'\b(?:excited|emphatic|emphasis)\b')
        self.assertRegex(tones[3], r'\b(?:soft|softly|reassuring)\b')

    def test_question_followup_keeps_energy_until_actual_reassurance(self):
        original = '这个故事你也觉得很有意思吗？接下来还有更有趣的发现。'
        for emotion in ('开心', '惊讶', '得意', '吐槽'):
            with self.subTest(emotion=emotion):
                delivered = fish_speech_text(original, emotion)
                tones = _CUE.findall(delivered)
                self.assertEqual(len(tones), 2)
                self.assertNotRegex(tones[-1], r'\b(?:soft|softly|gentle|relaxed)\b')
                self.assertEqual(_CUE.sub('', delivered), original)
        caring = fish_speech_text('这个故事你也觉得很有意思吗？没关系，我可以慢慢讲给你听。', '开心')
        self.assertRegex(_CUE.findall(caring)[-1], r'\b(?:soft|softly|reassuring)\b')

    def test_later_reassurance_does_not_soften_the_opening_reaction(self):
        original = '今天终于赢下这场比赛了！没关系，我们可以慢慢聊一聊刚才的过程。'
        delivered = fish_speech_text(original, '开心', continuation=True)
        tones = _CUE.findall(delivered)
        self.assertEqual(len(tones), 2)
        self.assertRegex(tones[0], r'\b(?:excited|emphatic|emphasis)\b')
        self.assertRegex(tones[-1], r'\b(?:soft|softly|reassuring)\b')
        self.assertNotIn('[short pause]', delivered)
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_care_question_softens_neutral_voice_but_regular_question_stays_curious(self):
        caring = fish_speech_text('你今天是不是有一点累了？', '平静')
        ordinary = fish_speech_text('你今天准备先聊什么呢？', '平静')
        self.assertNotEqual(_CUE.findall(caring)[0], _CUE.findall(ordinary)[0])
        self.assertRegex(_CUE.findall(caring)[0], r'\b(?:gentle|soft|reassuring)\b')
        self.assertNotRegex(_CUE.findall(ordinary)[0], r'\b(?:gentle|soft|relaxed)\b')

    def test_semantic_delivery_is_deterministic_and_never_adds_filler_words(self):
        originals = ['今天 19:30 我们一起聊第 3 个故事。', '这个故事你想听吗？接着还有新的发现。',
                     '今天终于把这个难题解开了，不过后面的事情还需要一起努力。',
                     '今天这个故事真是太有意思了！没关系，我们可以慢慢讲。']
        for original in originals:
            with self.subTest(original=original):
                plans = [plan_fish_speech(original, '开心', breathing=True,
                                          spoken_since_breath=15, continuation=True) for _ in range(4)]
                self.assertTrue(all(plan == plans[0] for plan in plans))
                self.assertEqual(_CUE.sub('', plans[0].text), original)
                self.assertLessEqual(plans[0].text.count('[soft inhale]') +
                                     plans[0].text.count('[short pause]'), 1)

    def test_not_this_but_that_uses_light_lead_in_focus_and_released_tail(self):
        original = '不是想让你赶快说完，而是想听听你的真实想法。接下来我们可以慢慢聊。'
        delivered = fish_speech_text(original, '平静')
        tones = _CUE.findall(delivered)
        self.assertEqual(len(tones), 3)
        self.assertIn('quicker', tones[0])
        self.assertIn('lighter stress', tones[0])
        self.assertRegex(delivered, r'，\[[^\]]*slower[^\]]*deliberate emphasis[^\]]*\] 而是')
        self.assertRegex(delivered, r'。\[[^\]]*normal conversational pace[^\]]*\] 接下来')
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_explicit_focal_clause_at_start_does_not_delay_emphasis(self):
        for prefix in ('关键是', '最重要的是', '真正需要的是'):
            original = prefix + '让你听清楚每个想法。接下来我们再聊其他事情。'
            with self.subTest(prefix=prefix):
                delivered = fish_speech_text(original, '得意')
                tones = _CUE.findall(delivered)
                self.assertEqual(len(tones), 2)
                self.assertIn('slower', tones[0])
                self.assertIn('deliberate emphasis', tones[0])
                self.assertIn('normal conversational pace', tones[1])
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_final_focal_clause_does_not_emit_an_unspoken_tempo_reset(self):
        original = '今天我们可以先聊近况，关键是把真正想说的话讲清楚。'
        delivered = fish_speech_text(original, '平静')
        self.assertEqual(len(_CUE.findall(delivered)), 2)
        self.assertIn('slower', _CUE.findall(delivered)[-1])
        self.assertNotIn('normal conversational pace', delivered)
        self.assertTrue(delivered.endswith('讲清楚。'))
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_explicit_point_wins_over_an_earlier_contrast(self):
        original = '今天的安排看起来很多，不过我们可以一件一件完成，最重要的是先把你的感受说清楚。接着再讨论其他安排。'
        delivered = fish_speech_text(original, '开心')
        focus = re.search(r'\[[^\]]*slower[^\]]*\] ([^，。]+)', delivered)
        self.assertIsNotNone(focus)
        self.assertTrue(focus.group(1).startswith('最重要的是'))
        self.assertEqual(delivered.count('deliberate emphasis'), 1)
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_rhythm_never_invents_a_focus_in_plain_text_or_splits_inside_words(self):
        originals = [
            '今天先聊聊最近的事情。然后一起听一首歌。',
            '今天我真正想说的是我们可以先把事情慢慢讲清楚',
            '关键帧的位置在第 12 秒，然后画面会自然转场。',
            '真正的故事发生在星期三，我们当时正准备吃饭。',
            '今天的安排看起来很多，关键是，再聊。',
            '今天预计收到 123,456.78 元并按计划存好。',
        ]
        for original in originals:
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '平静')
                self.assertNotIn('deliberate emphasis', delivered)
                self.assertNotIn('slightly quicker', delivered)
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_rhythm_does_not_speed_up_a_sad_or_reassuring_lead_in(self):
        for text, emotion in [
                ('今天的事情让我有一点难过，关键是还有你愿意听我说。我们再一起想想。', '委屈'),
                ('没关系我们可以慢慢讲，关键是把你的真实想法说出来。我们再一起想想。', '平静')]:
            with self.subTest(emotion=emotion):
                delivered = fish_speech_text(text, emotion)
                first_tone = _CUE.findall(delivered)[0]
                self.assertIn('lighter stress', first_tone)
                self.assertNotIn('quicker', first_tone)
                self.assertNotIn('faster', first_tone)
                self.assertIn('deliberate emphasis', delivered)
                self.assertEqual(_CUE.sub('', delivered), text)

    def test_focal_rhythm_keeps_emotion_and_respects_cue_and_breath_limits(self):
        original = '今天的计划是不是有一点太满了？不过我们可以先把桌面收拾清楚，关键是先听听你真正想说的话。然后再决定下一步，好吗？'
        for emotion, marker in [('平静', 'conversational'), ('开心', 'happy'), ('委屈', 'sad'),
                                ('惊讶', 'surprised'), ('得意', 'confident'), ('吐槽', 'teasing')]:
            with self.subTest(emotion=emotion):
                plan = plan_fish_speech(original, emotion, breathing=True, spoken_since_breath=15)
                tones = [cue for cue in _CUE.findall(plan.text)
                         if cue.strip() not in ('[short pause]', '[soft inhale]')]
                focused = next(cue for cue in tones if 'deliberate emphasis' in cue)
                self.assertIn(marker, focused)
                self.assertLessEqual(len(tones), 3)
                self.assertLessEqual(plan.text.count('[short pause]') + plan.text.count('[soft inhale]'), 1)
                self.assertEqual(_CUE.sub('', plan.text), original)

    def test_rhythm_keeps_literal_content_and_legacy_delivery_untouched(self):
        literal_texts = [
            '她说“关键是先听清楚我的意思”，然后开始解释每一步。',
            '歌曲叫「最重要的是你」，我们可以一起听一听。',
            '关键是先看看 https://example.com/，然后再一起讨论。',
        ]
        for original in literal_texts:
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '开心', breathing=True, spoken_since_breath=80)
                self.assertTrue(delivered.endswith(original))
                self.assertEqual(len(_CUE.findall(delivered[:-len(original)])), 1)
                self.assertNotIn('deliberate emphasis', delivered)
        original = '不是想让你赶快说完，而是想听听你的真实想法。接下来我们可以慢慢聊。'
        self.assertEqual(fish_speech_text(original, '开心', natural=False), '[happy] ' + original)

    def test_existing_searching_lead_in_is_brief_then_body_is_clear(self):
        body = '这件事情需要从昨天发生的故事说起。'
        for lead in ('嗯……', '嗯...', '唔……', '怎么说呢，', '该怎么说呢，',
                     '我想想，', '让我想想，', '我想一下，', '让我想一下，'):
            original = lead + body
            with self.subTest(lead=lead):
                delivered = fish_speech_text(original, '平静')
                tones = _CUE.findall(delivered)
                self.assertEqual(len(tones), 2)
                self.assertIn('thoughtful', tones[0])
                self.assertIn('brief hesitation', tones[0])
                self.assertNotIn('hesitation', tones[1])
                self.assertNotIn('questioning', tones[0])
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_searching_question_is_not_a_question_to_the_listener(self):
        original = '怎么说呢？这件事情需要从昨天发生的故事说起。'
        delivered = fish_speech_text(original, '平静')
        tones = _CUE.findall(delivered)
        self.assertEqual(len(tones), 2)
        self.assertIn('thoughtful', tones[0])
        self.assertFalse(any('questioning' in cue for cue in tones))
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_real_question_after_searching_lead_in_keeps_its_question_tone(self):
        original = '怎么说呢？你今天想先听哪一首歌？'
        delivered = fish_speech_text(original, '开心')
        tones = _CUE.findall(delivered)
        self.assertEqual(len(tones), 2)
        self.assertIn('thoughtful', tones[0])
        self.assertNotIn('questioning', tones[0])
        self.assertIn('questioning', tones[1])
        self.assertIn('happy', tones[1])
        self.assertEqual(_CUE.sub('', delivered), original)

    def test_acknowledgement_and_similar_words_are_not_forced_to_hesitate(self):
        for original in ('嗯，知道了。', '我想想办法帮你把这件事情解决。',
                         '今天我想想，明天再来和你聊聊。', '怎么说呢这几个字写在笔记里面。'):
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '平静')
                self.assertNotIn('hesitation', delivered)
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_searching_lead_and_focus_share_the_existing_three_tone_budget(self):
        originals = [
            '怎么说呢，我不想让你急着给答案，而是先找到自己真正想说的话。后面我们可以慢慢聊。',
            '我想想，关键是把这件事情解释清楚。然后我们再决定下一步。',
        ]
        for original in originals:
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '得意')
                tones = [cue for cue in _CUE.findall(delivered)
                         if cue.strip() not in ('[short pause]', '[soft inhale]')]
                self.assertLessEqual(len(tones), 3)
                self.assertIn('thoughtful', tones[0])
                self.assertTrue(any('deliberate emphasis' in cue for cue in tones))
                self.assertIn('normal conversational pace', tones[-1])
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_searching_lead_does_not_stack_breath_or_pause_at_its_edges(self):
        lead = '让我想一下，怎么说呢，'
        original = lead + '不过事情可能没有听起来那么简单，我们可以慢慢理清楚。'
        for breathing in (False, True):
            with self.subTest(breathing=breathing):
                plan = plan_fish_speech(original, '平静', breathing=breathing, spoken_since_breath=80)
                if plan.breath_at is not None:
                    self.assertGreater(plan.breath_at, len(lead))
                before_body = plan.text[:plan.text.index('不过')]
                self.assertNotIn('[soft inhale]', before_body)
                self.assertNotIn('[short pause]', before_body)
                self.assertLessEqual(plan.text.count('[soft inhale]') + plan.text.count('[short pause]'), 1)
                self.assertEqual(_CUE.sub('', plan.text), original)

    def test_searching_lead_keeps_emotion_and_does_not_append_empty_reset(self):
        for emotion, marker in [('平静', 'conversational'), ('开心', 'happy'), ('委屈', 'sad'),
                                ('惊讶', 'surprised'), ('得意', 'confident'), ('吐槽', 'teasing')]:
            with self.subTest(emotion=emotion):
                original = '嗯……怎么说呢？'
                delivered = fish_speech_text(original, emotion)
                tones = _CUE.findall(delivered)
                self.assertEqual(len(tones), 1)
                self.assertIn('thoughtful', tones[0])
                self.assertIn(marker, tones[0])
                self.assertTrue(delivered.endswith(original))
                self.assertEqual(_CUE.sub('', delivered), original)

    def test_searching_lead_preserves_quotes_links_and_legacy_mode(self):
        for original in ('怎么说呢？她说“我想想，再来回答你”。',
                         '嗯……你看看 https://example.com/，然后我们再聊。'):
            with self.subTest(original=original):
                delivered = fish_speech_text(original, '开心', breathing=True, spoken_since_breath=80)
                self.assertTrue(delivered.endswith(original))
                self.assertEqual(len(_CUE.findall(delivered[:-len(original)])), 1)
        original = '嗯……怎么说呢？这件事情需要先听听你的想法。'
        self.assertEqual(fish_speech_text(original, '开心', natural=False), '[happy] ' + original)


class PreferredDeliveryRegressionTests(unittest.TestCase):
    def test_selected_feeling_preview_keeps_its_recorded_delivery(self):
        # The user selected feeling_after.wav; pin its recorded TTS text here
        # instead of depending on local, ignored preview artifacts at runtime.
        text = '怎么说呢，这段旋律有一点寂寞，但又不是难过，更像是终于能安静下来了。'
        expected = ('[engaged, flowing conversational tone, thoughtful, brief hesitation] 怎么说呢，'
                    '[engaged, flowing conversational tone] '
                    '这段旋律有一点寂寞，但又不是难过，更像是终于能安静下来了。')
        plan = plan_fish_speech(text, '平静', breathing=True)
        self.assertEqual(plan.text, expected)
        self.assertEqual(_CUE.sub('', plan.text), text)

    def test_plain_feelings_do_not_get_forced_assessment_emphasis(self):
        originals = [
            '感觉挺舒服的。',
            '我觉得这段挺温柔的，像被好好接住。',
            '淡绿的树影，我会觉得外面刚安静下来。那种绿很轻，像适合放下手里的事，看一会儿。',
            '安静，像刚洗过的颜色。要是我在窗边，大概会多看一会儿，再顺手给花浇点水。',
        ]
        for text in originals:
            with self.subTest(text=text):
                delivered = fish_speech_text(text, '平静')
                self.assertNotRegex(delivered, r'\[[^\]]*\bemphasis\b[^\]]*\]')
                self.assertEqual(_CUE.sub('', delivered), text)


if __name__ == '__main__':
    unittest.main()
