"""Fish-only delivery cues; never rewrite the words shown in the conversation."""
from __future__ import annotations

import re
from dataclasses import dataclass


FISH_EMOTION_TAGS = {
    '开心': '[happy]', '委屈': '[sad]', '惊讶': '[surprised]',
    '得意': '[confident]', '吐槽': '[sarcastic]',
}

_DELIVERY = {
    '平静': '[casual, conversational, natural cadence]',
    '开心': '[happy, animated, smiling tone]',
    '委屈': '[slightly sad, soft conversational tone]',
    '惊讶': '[surprised, lively conversational tone]',
    '得意': '[quietly confident, playful tone]',
    '吐槽': '[dry, gently teasing tone]',
}
_QUESTION_TONE = {
    '平静': '[curious, conversational questioning tone]',
    '开心': '[happy, animated questioning tone]',
    '委屈': '[slightly sad, soft questioning tone]',
    '惊讶': '[surprised, questioning tone]',
    '得意': '[confident, playfully questioning tone]',
    '吐槽': '[dry, teasing questioning tone]',
}
_FLOWING = {
    '平静': '[engaged, flowing conversational tone]',
    '开心': '[happy, smiling, flowing delivery]',
    '委屈': '[slightly sad, intimate conversational tone]',
    '惊讶': '[surprised, animated conversational tone]',
    '得意': '[confident, playful conversational tone]',
    '吐槽': '[dry, teasing, conversational delivery]',
}
_EMPHATIC = {
    '平静': '[animated, conversational emphasis]',
    '开心': '[happy, excited emphasis]',
    '委屈': '[sad, earnest emphasis]',
    '惊讶': '[surprised, emphatic reaction]',
    '得意': '[confident, playful emphasis]',
    '吐槽': '[dry, teasing emphasis]',
}
_SETTLED = {
    '平静': '[reassuring, unhurried tone]',
    '开心': '[warm, softly smiling tone]',
    '委屈': '[soft, slightly sad tone]',
    '惊讶': '[softly surprised, conversational tone]',
    '得意': '[confident, relaxed tone]',
    '吐槽': '[dry, relaxed conversational tone]',
}
# Keep quoted text, links and explicit cues intact. A prefix still controls
# delivery without placing an instruction inside something the user quoted.
_LITERAL = re.compile(r'''https?://|www\.|```|[\[\]【】“”‘’"'「」『』（）()]''', re.I)
_QUESTION = re.compile(r'[^。！？!?]+[？?]')
_REASSURANCE = re.compile(r'^(?:先)?(?:别(?:着)?急|不用(?:着)?急|不着急|没关系|慢慢(?:说|讲|来)|放松(?:一下)?|休息(?:一下)?|歇一会|我在听|我陪你)')
_CARE_QUESTION = re.compile(r'还好吗|累了|难受|不舒服|怎么了')
_CONTRAST = re.compile(r'^(?:不过|但是|可惜|倒是|其实|结果|没想到|只是)')
_FOCUS = re.compile(r'^(?:而是|关键(?:是|在于|要)|重点(?:是|在于)|最重要|真正(?:重要|想|要|在意|喜欢|需要|该)|尤其|特别是|我(?:更|最|真正)?(?:在意|想说|担心)的是)')
_HESITATION = re.compile(
    r'\s*(?:(?:嗯|唔)(?:…+|\.{3,})[，,。！？!?；;]*'
    r'|(?:该怎么说呢|怎么说呢|让我想一下|让我想想|我想一下|我想想)'
    r'(?:[，,。！？!?；;…]+|\.{3,}|$))\s*'
)


def _hesitation_end(text: str) -> int:
    # Only already-written, unmistakable lead-ins. "嗯，知道了" is an
    # acknowledgement, not a request to sound unsure. Never invent filler words.
    end = 0
    for _ in range(2):
        match = _HESITATION.match(text, end)
        if match is None:
            break
        end = match.end()
    return end


def _phrase_tone(text: str, emotion: str, *, continuation: bool = False,
                 questioning: bool = False) -> str:
    # Interpret only the next phrase: later reassurance must not soften an
    # opening reaction, and a later exclamation must not colour the whole reply.
    phrase = re.split(r'[，,。；;！？!?]', text.strip(), maxsplit=1)[0]
    if _REASSURANCE.search(phrase):
        return _SETTLED[emotion]
    if questioning:
        if emotion == '平静' and _CARE_QUESTION.search(phrase):
            return '[gentle questioning tone]'
        return _QUESTION_TONE[emotion]
    first_sentence = re.split(r'[。？?；;]', text, maxsplit=1)[0]
    if re.search(r'[！!]', first_sentence):
        return _EMPHATIC[emotion]
    return _FLOWING[emotion] if continuation else _DELIVERY[emotion]


@dataclass(frozen=True)
class DeliveryPlan:
    text: str
    # Position in the unmodified text; zero means before this continuation.
    breath_at: int | None
    remaining_units: int


def _units(text: str) -> int:
    return sum(char.isalnum() for char in text)


def _boundaries(text: str):
    for boundary in re.finditer('[，,。；;！？!?]', text):
        index = boundary.end()
        if (index < len(text) and boundary.start()
                and text[boundary.start() - 1].isdigit() and text[index].isdigit()):
            continue
        yield index, _units(text[:index]), _units(text[index:])


def _rhythm_focus(text: str, boundaries: list[tuple[int, int, int]]) -> tuple[int, int] | None:
    """Choose one explicit focal clause, not an arbitrary word to exaggerate."""
    ends = [index for index, _, _ in boundaries]
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    clauses = list(zip([0] + ends[:-1], ends))
    # An explicit point wins over an earlier conversational contrast.
    for pattern in (_FOCUS, _CONTRAST):
        for start, end in clauses:
            clause = text[start:end].lstrip()
            if _units(clause) < 6 or not pattern.search(clause):
                continue
            if pattern is _CONTRAST and _units(text[:start]) < 6:
                continue
            return start, end
    return None


def _with_rhythm(cue: str, rhythm: str) -> str:
    # One combined instruction, so emotion and pace never compete as stacked tags.
    return cue[:-1] + ', ' + rhythm + ']'


def _rhythm_delivery(text: str, emotion: str, cue: str,
                     boundaries: list[tuple[int, int, int]], insertions: dict[int, str]):
    focus = _rhythm_focus(text, boundaries)
    if focus is None:
        return cue, insertions
    start, end = focus
    focus_text = text[start:end]
    focus_cue = (_phrase_tone(focus_text, emotion, questioning=True)
                 if focus_text.rstrip().endswith(('？', '?')) else _EMPHATIC[emotion])
    focus_cue = _with_rhythm(focus_cue, 'slower, deliberate emphasis')
    # Replace the ordinary question/transition plan instead of layering extra cues.
    # The three possible beats are lead-in, focal clause, and a return to normal.
    changes: dict[int, str] = {}
    if start:
        gentle = emotion == '委屈' or _REASSURANCE.search(text.lstrip()) or 'gentle' in cue
        cue = _with_rhythm(cue, 'connected delivery, lighter stress' if gentle
                           else 'slightly quicker, lighter stress')
        changes[start] = focus_cue + ' '
    else:
        cue = focus_cue
    if _units(text[end:]):
        tail = text[end:]
        tail_question = re.split('[。！？!?；;]', tail, maxsplit=1)
        first_end = len(tail_question[0])
        questioning = first_end < len(tail) and tail[first_end] in '？?'
        reset = _phrase_tone(tail, emotion, continuation=True, questioning=questioning)
        # Reassurance keeps its own slower tone; other tails release the emphasis.
        rhythm = ('easy pace, lighter stress' if _REASSURANCE.search(tail.lstrip())
                  else 'normal conversational pace, lighter stress')
        changes[end] = _with_rhythm(reset, rhythm) + ' '
    return cue, changes


def _hesitation_delivery(text: str, emotion: str, end: int, boundaries,
                         insertions: dict[int, str]):
    cue = _with_rhythm(_FLOWING[emotion], 'thoughtful, brief hesitation')
    if not _units(text[end:]):
        return cue, {}
    focus = _rhythm_focus(text, boundaries)
    if focus is not None and focus[0] > end and _units(text[end:focus[0]]) <= 24:
        # A short searching lead-in can resolve directly into the existing focal
        # phrase. Preserve its pace reset without adding a fourth tone cue.
        return cue, insertions
    changes = {index: value for index, value in insertions.items() if index >= end}
    if end not in changes:
        body_question = _QUESTION.search(text, end)
        reset = _phrase_tone(text[end:], emotion, continuation=True,
                             questioning=body_question is not None and body_question.start() == end)
        changes[end] = reset + ' '
    if len(changes) > 2:
        # Punctuation carries any intermediate question; reserve explicit cues
        # for finishing the hesitation and the final tone transition.
        positions = sorted(changes)
        changes = {index: changes[index] for index in (positions[0], positions[-1])}
    return cue, changes


def plan_fish_speech(text: str, emotion: str, *, natural: bool = True,
                     breathing: bool = False, spoken_since_breath: int = 0,
                     continuation: bool = False) -> DeliveryPlan:
    prior = max(0, spoken_since_breath)
    total = prior + _units(text)
    if not text:
        return DeliveryPlan(text, None, prior)
    if not natural:
        cue = FISH_EMOTION_TAGS.get(emotion)
        return DeliveryPlan(cue + ' ' + text if cue else text, None, total)
    if emotion not in _DELIVERY:
        return DeliveryPlan(text, None, total)
    cue = _phrase_tone(text, emotion, continuation=continuation)
    if _LITERAL.search(text):
        return DeliveryPlan(cue + ' ' + text, None, total)

    # Coordinates refer to the original text, so no punctuation or spoken word
    # is removed. A continuation keeps its energy; softening is content-driven.
    insertions: dict[int, str] = {}
    boundaries = list(_boundaries(text))
    hesitation_end = _hesitation_end(text)
    question = _QUESTION.search(text, hesitation_end)
    if question:
        if question.start() == 0:
            cue = _phrase_tone(question.group(), emotion, questioning=True)
        else:
            insertions[question.start()] = _phrase_tone(question.group(), emotion, questioning=True) + ' '
        if _units(text[question.end():]):
            insertions[question.end()] = _phrase_tone(text[question.end():], emotion, continuation=True) + ' '
    else:
        for index, before, after in boundaries:
            tail = text[index:].lstrip()
            if before >= 6 and after >= 6 and (_REASSURANCE.search(tail) or _CONTRAST.search(tail)):
                next_cue = (_phrase_tone(tail, emotion, continuation=True)
                            if _REASSURANCE.search(tail) else _EMPHATIC[emotion])
                if next_cue != cue:
                    insertions[index] = next_cue + ' '
                break

    cue, insertions = _rhythm_delivery(text, emotion, cue, boundaries, insertions)
    if hesitation_end:
        cue, insertions = _hesitation_delivery(text, emotion, hesitation_end, boundaries, insertions)

    # Use one light intake at a phrase boundary, never a repeated panting loop.
    # The caller supplies only successful, consecutive segments from this turn.
    breath_at = None
    if breathing:
        if prior >= 28 and _units(text) >= 8 and not hesitation_end:
            breath_at = 0
        else:
            for index, before, after in boundaries:
                if index > hesitation_end and before >= 8 and after >= 6 and prior + before >= 24:
                    breath_at = index
                    break
    if breath_at is not None:
        insertions[breath_at] = '[soft inhale] ' + insertions.get(breath_at, '')
    elif len(text) >= 22:
        # Punctuation already provides ordinary pauses. Only underline a real
        # contrast, rather than stopping after the first comma in every reply.
        for index, before, after in boundaries:
            if index > hesitation_end and before >= 6 and after >= 6 and _CONTRAST.search(text[index:].lstrip()):
                insertions[index] = '[short pause] ' + insertions.get(index, '')
                break

    delivered = text
    for index in sorted(insertions, reverse=True):
        delivered = delivered[:index] + insertions[index] + delivered[index:]
    remaining = _units(text[breath_at:]) if breath_at is not None else total
    return DeliveryPlan(cue + ' ' + delivered, breath_at, remaining)


def fish_speech_text(text: str, emotion: str, *, natural: bool = True,
                     breathing: bool = False, spoken_since_breath: int = 0,
                     continuation: bool = False) -> str:
    return plan_fish_speech(text, emotion, natural=natural, breathing=breathing,
                            spoken_since_breath=spoken_since_breath,
                            continuation=continuation).text
