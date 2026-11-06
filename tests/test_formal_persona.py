import json
import tempfile
import unittest
from pathlib import Path
from brain.config import load_persona, load_settings, PersonalityEvolutionConfig, LLMConfig
from brain.personality_evolution import PersonalityEvolution
from brain.persona import Persona, FALLBACK, AI_QUESTION_FALLBACK
from brain.events import Event
from brain.memory import Memory


class FormalPersonaTests(unittest.TestCase):
    def test_formal_lore_reaches_prompt_and_old_events_cannot_force_cute_style(self):
        cfg = load_persona()
        with tempfile.TemporaryDirectory() as folder:
            memory = Memory(str(Path(folder) / 'memory.jsonl'), str(Path(folder) / 'viewers.json'))
            persona = Persona(cfg, LLMConfig(), memory)
            system = persona.system_prompt()
            for fact in [cfg.name, cfg.revision, cfg.species, cfg.tone]:
                self.assertIn(fact, system)
            for kind in ['gift', 'death', 'find']:
                prompt = persona.user_prompt(Event('game', kind, '测试回执'))
                self.assertNotIn('软萌', prompt)
                self.assertNotIn('可爱的懊恼', prompt)
            self.assertNotIn('禁止超自然', system)
            self.assertEqual(cfg.appearance, '')
            self.assertEqual(cfg.background, '')

    def test_profile_migration_preserves_relationships_but_retires_old_style_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'personality.json'
            old = PersonalityEvolution(PersonalityEvolutionConfig(state_path=str(path), profile='legacy-test-profile'), '助手')
            old.state['traits']['energy'] = 0.94
            old.state['relationships']['friend'] = {'trust': 0.8, 'familiarity': 0.4}
            old.state['evidence'] = [{'id': 1, 'applied_snapshot_id': None, 'deltas': {'energy': 0.2}}]
            old.close()
            new = PersonalityEvolution(PersonalityEvolutionConfig(state_path=str(path)), '助手')
            self.assertEqual(new.state['traits']['energy'], 0.36)
            self.assertEqual(new.state['relationships']['friend']['trust'], 0.8)
            self.assertEqual(new.state['evidence'][0]['applied_snapshot_id'], -1)
            self.assertEqual(new.state['mood']['label'], '沉静')
            new.close()
            again = PersonalityEvolution(PersonalityEvolutionConfig(state_path=str(path)), '助手')
            self.assertEqual(len(again.state['profile_migrations']), 1)

    def test_fallback_and_voice_settings_do_not_reintroduce_test_persona(self):
        lines = [text for pool in FALLBACK.values() for text in pool] + AI_QUESTION_FALLBACK
        for text in lines:
            for word in ['嘿嘿', '呜，', '精神满满']:
                self.assertNotIn(word, text)
        cfg = load_settings()
        self.assertEqual(cfg.personality_evolution.profile, 'generic-v1')
        self.assertEqual(cfg.tts.backend, 'mock')
        self.assertFalse(cfg.cognition.enabled)
        self.assertEqual(cfg.tts.indextts2_long_num_beams, 3)
        self.assertEqual(cfg.tts.indextts2_num_beams, 1)
        self.assertEqual(cfg.tts.indextts2_emo_alpha, 0.60)
