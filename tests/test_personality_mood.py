"""Persistent mood is a time projection, independent of UI polling frequency."""
import copy
import tempfile
import unittest
from pathlib import Path

from brain.config import PersonalityEvolutionConfig
from brain.events import Event
from brain.personality_evolution import MOOD_HALF_LIFE_SEC, PersonalityEvolution


class PersonalityMoodTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'personality.json'
        self.now = 1800000000.0
        self.cfg = PersonalityEvolutionConfig(state_path=str(self.path))
        self.engine = PersonalityEvolution(self.cfg, '测试角色', clock=lambda: self.now)

    def excite(self):
        self.engine._set_mood('明快', .9, .9,
                              {'type': 'fixture', 'identity': 'PRIVATE_OWNER', 'user': 'PRIVATE_NAME'}, strength=1)

    def test_half_life_returns_halfway_to_baseline_without_writing(self):
        self.excite()
        original = copy.deepcopy(self.engine.state)
        changed = self.now
        self.now += MOOD_HALF_LIFE_SEC
        mood = self.engine.mood_snapshot()
        self.assertAlmostEqual(mood['valence'], (.9 + .55) / 2, places=4)
        self.assertAlmostEqual(mood['arousal'], (.9 + self.engine.base_traits['energy']) / 2, places=4)
        self.assertEqual(mood['last_changed_at'], changed)
        self.assertEqual(mood['half_life_sec'], MOOD_HALF_LIFE_SEC)
        self.assertEqual(self.engine.state, original)
        self.assertFalse(self.path.exists())

    def test_many_reads_and_neutral_events_do_not_compound_decay(self):
        self.excite()
        anchor = copy.deepcopy(self.engine.state['mood'])
        start = self.now
        self.now += MOOD_HALF_LIFE_SEC
        once = self.engine.mood_snapshot()
        for offset in range(1, 121):
            self.now = start + offset * MOOD_HALF_LIFE_SEC / 120
            self.engine.mood_snapshot()
            self.engine.context_for()
            self.engine.summary()
            self.engine.observe_event(Event('companion', 'chat', '看一看', ts=self.now))
        self.assertEqual(self.engine.mood_snapshot(), once)
        self.assertEqual(self.engine.state['mood'], anchor)

    def test_restart_uses_original_anchor_not_last_console_read(self):
        self.excite()
        self.engine.close()
        saved = self.path.read_bytes()
        self.now += MOOD_HALF_LIFE_SEC / 2
        self.engine.mood_snapshot()
        self.assertEqual(self.path.read_bytes(), saved)
        self.now += MOOD_HALF_LIFE_SEC / 2
        expected = self.engine.mood_snapshot()
        loaded = PersonalityEvolution(self.cfg, '测试角色', clock=lambda: self.now)
        self.assertEqual(loaded.mood_snapshot(), expected)
        self.assertEqual(loaded.state['mood'], self.engine.state['mood'])

    def test_new_event_blends_from_current_projection_and_resets_anchor(self):
        self.excite()
        self.now += MOOD_HALF_LIFE_SEC
        current = self.engine.mood_snapshot()
        self.engine._set_mood('关切', .48, .48, {'type': 'fixture'}, strength=.5)
        updated = self.engine.mood_snapshot()
        self.assertAlmostEqual(updated['valence'], (current['valence'] + .48) / 2, places=4)
        self.assertAlmostEqual(updated['arousal'], (current['arousal'] + .48) / 2, places=4)
        self.assertEqual(updated['last_changed_at'], self.now)
        self.assertEqual(updated['label'], '关切')

    def test_idle_context_and_summary_both_relax_without_new_messages(self):
        self.excite()
        self.assertEqual(self.engine.summary()['mood'], '明快')
        self.assertIn('短期心境：明快', self.engine.context_for())
        self.now += MOOD_HALF_LIFE_SEC * 5
        self.assertEqual(self.engine.summary()['mood'], '沉静')
        self.assertIn('短期心境：沉静', self.engine.context_for())
        self.assertEqual(self.engine.state['mood']['label'], '明快')

    def test_snapshot_does_not_expose_trigger_or_identity_and_clock_reversal_is_bounded(self):
        self.excite()
        mood = self.engine.mood_snapshot()
        self.assertNotIn('PRIVATE_', str(mood))
        self.assertNotIn('source', mood)
        self.now -= 3600
        reversed_clock = self.engine.mood_snapshot()
        self.assertEqual(reversed_clock['valence'], mood['valence'])
        self.assertEqual(reversed_clock['arousal'], mood['arousal'])
        self.engine.state['mood'].update(valence=float('nan'), arousal=float('inf'), updated_at='bad')
        recovered = self.engine.mood_snapshot()
        self.assertEqual(recovered['valence'], .55)
        self.assertEqual(recovered['arousal'], self.engine.base_traits['energy'])
        self.assertIsNone(recovered['last_changed_at'])


if __name__ == '__main__':
    unittest.main()
