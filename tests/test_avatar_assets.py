from __future__ import annotations

import json
from html.parser import HTMLParser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "skin" / "avatar"


class AvatarAssetTests(unittest.TestCase):
    @unittest.skipUnless((MODEL_DIR / "avatar.model3.json").is_file(), "Local Live2D model required")
    def test_every_expression_asset_is_registered(self) -> None:
        model = json.loads((MODEL_DIR / "avatar.model3.json").read_text(encoding="utf-8"))
        references = model["FileReferences"]
        registered = {item["File"] for item in references["Expressions"]}
        available = {path.name for path in MODEL_DIR.glob("*.exp3.json")}
        self.assertEqual(available, registered)

    @unittest.skipUnless((MODEL_DIR / "avatar.model3.json").is_file(), "Local Live2D model required")
    def test_motion_asset_is_registered(self) -> None:
        model = json.loads((MODEL_DIR / "avatar.model3.json").read_text(encoding="utf-8"))
        motions = model["FileReferences"]["Motions"]
        registered = {item["File"] for group in motions.values() for item in group}
        available = {path.name for path in MODEL_DIR.glob("*.motion3.json")}
        self.assertEqual(available, registered)

    def test_frontend_contains_action_triggers(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        for action in ("wave", "phone", "controller", "phoneTap", "left", "right", "lean"):
            self.assertIn(action, html)
        for parameter in ("Anjian9", "Anjian12", "Anjian13", "Anjian14", "Anjian15"):
            self.assertIn(parameter, html)

    def test_avatar_actions_start_with_audio_playback(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        reply_branch = html.split("d.type === 'reply'", 1)[1].split(
            "d.type === 'audio'", 1
        )[0]
        self.assertNotIn("triggerAvatar(", reply_branch)
        self.assertIn("audioPlayer.addEventListener('playing', beginAudioAvatar)", html)
        self.assertIn("triggerAvatar(item.emotion", html)

    def test_pose_dependent_appearance_is_applied_before_pose_update(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        appearance_hook = html.split("function applyAvatarParts", 1)[1].split(
            "function includesAny", 1
        )[0]
        for parameter in ("Anjian9", "Anjian12", "Anjian13", "Anjian14", "Anjian15"):
            self.assertIn(parameter, appearance_hook)
        self.assertIn("pose.updateParameters =", html)
        self.assertIn("applyAvatarParts(core);", html)

    def test_glasses_modes_match_the_model_parameters(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        glasses = html.split("function setGlasses", 1)[1].split(
            "function resetExpression", 1
        )[0]
        self.assertIn("mode === 'none' ? [1, 0]", glasses)
        self.assertIn("mode === 'second' ? [1, 1]", glasses)
        self.assertIn("setGlasses('none')", html)

    def test_avatar_view_can_switch_between_half_and_full_body(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-avatar-view="half"', html)
        self.assertIn('data-avatar-view="full"', html)
        self.assertIn("localStorage.getItem('avatar-view-mode')", html)
        self.assertIn("avatarViewMode === 'half' ? fit * 2.15 : fit * 0.95", html)
        self.assertIn("if (avatarLayout) avatarLayout();", html)

    def test_facial_inputs_run_before_physics_and_final_hook_corrects_gaze(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")

        def require(fragment: str, source: str) -> None:
            self.assertTrue(fragment in source, f"Missing avatar contract: {fragment}")

        require("const eyeMotion = new CompanionGaze()", html)
        require("const speechPerformance = new CompanionSpeechPerformance()", html)
        require('/static/speech-performance.js?v=', html)
        require("gazeBlink:liveGazeBlink", html)
        require("gazeBlink:preview.state.gazeBlink", html)
        require("gazeX:gaze.headX, gazeY:gaze.headY", html)
        require("gazeX:liveGaze.headX, gazeY:liveGaze.headY", html)
        self.assertFalse("scheduleNaturalFocus" in html, "Obsolete gaze scheduler must stay removed")
        natural_hook = html.split("model.internalModel.updateNaturalMovements =", 1)[1].split(
            "const pose = model.internalModel.pose", 1
        )[0]
        # Live controllers keep advancing while independent preview controllers
        # may supply the rendered pose; both paths run before physics.
        # Keep the actual ordering contract without printing the whole page on failure.
        stages = (
            "const preview = sampleAvatarPreview(now)",
            "const liveFace = sampleAvatarFace(now)",
            "const liveGesture = conversationGestures.frame(now",
            "const gesture = preview ? preview.gestures.frame(now",
            "const baseFace = preview ? sampleAvatarFace(now, preview) : liveFace",
            "const liveSpeechPerformance = speechPerformance.frame(now",
            "const speech = preview ? preview.speechPerformance.frame(now",
            "const secondary = 1 - gesture.weight",
            "const face = {...baseFace",
            "applyAvatarFace(core, face)",
            "const liveGaze = eyeMotion.frame(now",
            "const gaze = preview ? preview.eyes.frame(now",
            "const liveMovement = bodyMotion.frame(now",
            "const baseMovement = preview ? preview.motion.frame(now",
            "const movement = {...baseMovement}",
            "coordinatedGaze = {...gaze",
        )
        for stage in stages:
            require(stage, natural_hook)
        positions = [natural_hook.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions), "Facial and gaze inputs must keep their pre-physics order")
        gesture_face = natural_hook.split("const face = {...baseFace", 1)[1].split("};", 1)[0]
        # Gesture styling may adjust lids and expression, but the real speech
        # jaw is inherited unchanged and a closed blink cannot be reopened.
        self.assertNotIn("mouthOpen:", gesture_face, "Gestures must preserve the speech-driven jaw")
        require("baseFace.mouthForm + gesture.smile", gesture_face)
        require("baseFace.browL + gesture.brow", gesture_face)
        require("baseFace.browR + gesture.brow", gesture_face)
        require("speech.smile*secondary", gesture_face)
        require("speech.browL*secondary", gesture_face)
        require("speech.browR*secondary", gesture_face)
        require("baseFace.eyeOpenL + (gesture.eyeOpen + speech.eyeOpen*secondary)*baseFace.blink", gesture_face)
        require("baseFace.eyeOpenR + (gesture.eyeOpen + speech.eyeOpen*secondary)*baseFace.blink", gesture_face)
        facial_inputs = html.split("function applyAvatarFace(core, face)", 1)[1].split(
            "function applyPupilGaze", 1
        )[0]
        for parameter in ("ParamMouthOpenY", "ParamEyeLOpen", "ParamEyeROpen",
                          "ParamMouthForm", "ParamBrowLForm", "MouthPuckerWiden"):
            require(parameter, facial_inputs)
        final_hook = html.split("model.internalModel.on('beforeModelUpdate'", 2)[-1]
        for parameter in (
            "ParamMouthOpenY", "ParamEyeLOpen", "ParamEyeROpen",
        ):
            self.assertFalse(f"setParameterValueById('{parameter}'" in final_hook,
                             f"Final gaze correction must not overwrite {parameter}")
        require('applyPupilGaze(core, gaze, listening.attention)', final_hook)
        pupil_hook = html.split('function applyPupilGaze(', 1)[1].split('function applyAvatarParts', 1)[0]
        require("setParameterValueById('ParamEyeBallX'", pupil_hook)
        require("setParameterValueById('ParamEyeBallY'", pupil_hook)

    def test_companion_chat_is_primary_and_legacy_tools_are_collapsed(self) -> None:
        html = (ROOT / "brain" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>", html)
        self.assertIn("source: voiceDraft ? 'voice' : 'companion'", html)

        class ConsoleStructure(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.nodes = {}

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                node_id = attributes.get('id')
                if node_id:
                    self.nodes[node_id] = (tag, attributes, tuple(node[1] for node in self.stack))
                if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                               'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                    self.stack.append((tag, node_id))

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1][0] == tag:
                    self.stack.pop()

        structure = ConsoleStructure()
        structure.feed(html)
        nodes = structure.nodes
        # Chat stays directly available; testing and advanced controls are closed
        # disclosures, regardless of future edits to their summary wording.
        for node_id in ('text', 'send', 'companion-user'):
            self.assertIn('inject', nodes[node_id][2], node_id)
            self.assertNotIn('feature-controls', nodes[node_id][2], node_id)
            self.assertNotIn('local-test-panel', nodes[node_id][2], node_id)
        for node_id in ('feature-controls', 'local-test-panel'):
            tag, attributes, parents = nodes[node_id]
            self.assertEqual(tag, 'details', node_id)
            self.assertNotIn('open', attributes, node_id)
            self.assertNotIn('inject', parents, node_id)
        self.assertIn('feature-controls', nodes['memory-panel'][2])
        for node_id in ('advanced-inject', 'mock-danmaku', 'mock-game'):
            self.assertIn('local-test-panel', nodes[node_id][2], node_id)
        for node_id in ('polls-link', 'shows-link', 'questions-link', 'runtime-link'):
            self.assertIn('console-nav', nodes[node_id][2], node_id)
            self.assertNotIn('inject', nodes[node_id][2], node_id)


if __name__ == "__main__":
    unittest.main()
