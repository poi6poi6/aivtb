from __future__ import annotations

import unittest

from brain.speech_text import SentenceChunker


class SpokenLeadinChunkerTests(unittest.TestCase):
    def test_default_keeps_existing_immediate_sentence_boundaries(self):
        chunker = SentenceChunker()
        self.assertEqual(chunker.feed('我想想。更接近夜里开着灯的窗户。'),
                         ['我想想。', '更接近夜里开着灯的窗户。'])

    def test_opt_in_holds_opening_leadin_until_following_sentence(self):
        for leadin in ('我想想。', '让我想想。', '怎么说呢？', '该怎么说呢？', '嗯。', '唔。'):
            with self.subTest(leadin=leadin):
                chunker = SentenceChunker(join_spoken_leadins=True)
                self.assertEqual(chunker.feed(leadin), [])
                self.assertEqual(chunker.feed('它更像是'), [])
                self.assertEqual(chunker.feed('夜里开着灯的窗户。'),
                                 [leadin + '它更像是夜里开着灯的窗户。'])
                self.assertEqual(chunker.flush(), [])

    def test_think_for_a_moment_leadins_stay_with_their_following_body(self):
        for leadin in ('我想一下。', '让我想一下。'):
            with self.subTest(leadin=leadin):
                chunker = SentenceChunker(join_spoken_leadins=True)
                self.assertEqual(chunker.feed(leadin), [])
                self.assertEqual(chunker.feed('更接近夜里开着灯的窗户。'),
                                 [leadin + '更接近夜里开着灯的窗户。'])
                self.assertEqual(chunker.flush(), [])

    def test_token_boundaries_do_not_separate_a_leadin_or_punctuation_cluster(self):
        originals = [
            '怎么说呢？！它像是刚下过雨的小路。',
            '嗯……我比较喜欢这段。',
            '嗯嗯，我比较喜欢这段。',
            '我想想。\n更接近夜里开着灯的窗户。',
            '我想想...更接近夜里开着灯的窗户。',
        ]
        for original in originals:
            for size in (1, 2, len(original)):
                with self.subTest(original=original, token_size=size):
                    chunker = SentenceChunker(join_spoken_leadins=True)
                    chunks = []
                    for start in range(0, len(original), size):
                        chunks.extend(chunker.feed(original[start:start + size]))
                    chunks.extend(chunker.flush())
                    self.assertEqual(chunks, [original])

    def test_known_performance_metadata_remains_in_combined_chunk(self):
        prefix = '[平静][动作:弯眼]怎么说呢？！'
        chunker = SentenceChunker(join_spoken_leadins=True)
        self.assertEqual(chunker.feed(prefix), [])
        self.assertEqual(chunker.feed('它更像是夜里开着灯的窗户。'),
                         [prefix + '它更像是夜里开着灯的窗户。'])

    def test_real_questions_and_substantive_openers_are_not_joined(self):
        for sentence in ('你怎么想？', '怎么说才合适？', '我想想明天要做的事。',
                         '我想想你刚才提出的问题。', '你让我想想。'):
            with self.subTest(sentence=sentence):
                chunker = SentenceChunker(join_spoken_leadins=True)
                self.assertEqual(chunker.feed(sentence), [sentence])
                self.assertEqual(chunker.feed('接着我们再聊别的。'), ['接着我们再聊别的。'])

    def test_quoted_or_unknown_bracketed_text_keeps_existing_behavior(self):
        for original in ('“我想想。”这是她说的话。', '[来源]我想想。接着再说。',
                         '「怎么说呢？」是这个标题。'):
            with self.subTest(original=original):
                plain = SentenceChunker()
                joined = SentenceChunker(join_spoken_leadins=True)
                self.assertEqual(joined.feed(original), plain.feed(original))
                self.assertEqual(joined.flush(), plain.flush())

    def test_only_the_opening_leadin_gets_special_treatment(self):
        chunker = SentenceChunker(join_spoken_leadins=True)
        self.assertEqual(chunker.feed('先听完这一句。'), ['先听完这一句。'])
        self.assertEqual(chunker.feed('我想想。'), ['我想想。'])
        self.assertEqual(chunker.feed('更接近后面的旋律。'), ['更接近后面的旋律。'])

    def test_flush_keeps_a_standalone_leadin_even_with_long_metadata(self):
        for original in ('嗯。', '怎么说呢？！', '[平静][动作:弯眼]我想想。'):
            with self.subTest(original=original):
                chunker = SentenceChunker(max_chars=12, join_spoken_leadins=True)
                self.assertEqual(chunker.feed(original), [])
                self.assertEqual(chunker.flush(), [original])
                self.assertEqual(chunker.flush(), [])

    def test_first_clause_threshold_counts_the_body_without_the_leadin(self):
        chunker = SentenceChunker(first_clause_chars=8, join_spoken_leadins=True)
        text = '我想想。这段还不错，不过我更喜欢后面的转折。'
        self.assertEqual(chunker.feed(text), [text])
        self.assertFalse(chunker.first_clause_split)

    def test_first_clause_can_still_release_a_long_body_after_the_leadin(self):
        chunker = SentenceChunker(first_clause_chars=8, join_spoken_leadins=True)
        first = '怎么说呢？！我比较喜欢前面这一段，'
        tail = '后面的转折也很有意思。'
        self.assertEqual(chunker.feed(first + tail), [first, tail])
        self.assertTrue(chunker.first_clause_split)

    def test_early_comma_never_emits_a_pure_leadin(self):
        chunker = SentenceChunker(first_clause_chars=1, join_spoken_leadins=True)
        self.assertEqual(chunker.feed('嗯，'), [])
        self.assertFalse(chunker.first_clause_split)
        self.assertEqual(chunker.feed('我比较喜欢这段。'), ['嗯，我比较喜欢这段。'])

    def test_long_punctuation_free_body_still_has_bounded_streaming_chunks(self):
        leadin = '[平静][动作:弯眼]我想想。'
        body = '这段旋律确实很适合现在的心情也适合慢慢听完以后再说说感受'
        chunker = SentenceChunker(max_chars=12, min_soft_chars=6, join_spoken_leadins=True)
        chunks = chunker.feed(leadin + body)
        chunks.extend(chunker.flush())
        self.assertEqual(''.join(chunks), leadin + body)
        self.assertTrue(chunks[0].startswith(leadin))
        self.assertGreater(len(chunks[0]), len(leadin))
        self.assertLessEqual(len(chunks[0]) - len(leadin), 12)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks[1:]))


if __name__ == '__main__':
    unittest.main()
