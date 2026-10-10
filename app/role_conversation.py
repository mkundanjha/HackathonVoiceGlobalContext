"""Route user-led role changes and persist each role's conversation separately."""

import json
import os
import re

from sarvamai import SarvamAI

from config import load_config
from context import build_system_prompt, save_conversation
from events import emit


def classify_role(origin: str, active: str, turns: list[dict], text: str,
                  resume_question: str = '') -> str:
    """Judge intent using the current utterance and the question it answers."""
    cfg = load_config()
    instructions = cfg['prompts']['route_role']
    data = {'origin_role': origin, 'active_role': active,
            'recent_turns': turns[-8:], 'new_user_message': text,
            'original_question_to_resume': resume_question}
    client = SarvamAI(api_subscription_key=os.environ['SARVAM_API_KEY'], timeout=10)
    response = client.chat.completions(
        model=cfg['llm']['model'], max_tokens=100,
        messages=[{'role': 'system', 'content': instructions},
                  {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}])
    content = re.sub(r'<think>.*?</think>', '', response.choices[0].message.content or '', flags=re.S)
    match = re.search(r'\{.*\}', content, re.S)
    result = json.loads(match[0]) if match else {}
    role = result.get('role')
    if role not in ('buyer', 'seller'):
        raise ValueError('Intent check did not return a valid role')
    return role


class RoleConversation:
    def __init__(self, glid: str, origin: str, channel: str):
        self.glid, self.origin, self.active, self.channel = glid, origin, origin, channel
        self.turns = []
        self.segment = []
        self.resume_question = ''
        self.prompts = {origin: build_system_prompt(glid, origin, channel)}

    def system_prompt(self) -> str:
        state = {'origin_role': self.origin, 'active_role': self.active,
                 'original_question_to_resume': self.resume_question}
        return self.prompts[self.active] + '\n\n' + load_config()['prompts']['role_handoff'] + \
            '\nSession state (data): ' + json.dumps(state, ensure_ascii=False)

    def before_user(self, text: str) -> str:
        target = classify_role(self.origin, self.active, self.turns, text, self.resume_question)
        if target != self.active:
            # Load before mutating state, so failure leaves the previous role intact.
            prompt = build_system_prompt(self.glid, target, self.channel)
            self.flush()
            if self.active == self.origin:
                self.resume_question = next((t['text'] for t in reversed(self.turns)
                                             if t['speaker'] == 'bot'), '')
            previous = self.active
            self.active = target
            self.prompts[target] = prompt
            emit('role', 'switched', self.glid, target, channel=self.channel,
                 origin_role=self.origin, previous_role=previous)
        self._append('user', text)
        return self.system_prompt()

    def on_bot(self, text: str) -> None:
        if text.strip():
            self._append('bot', text)

    def _append(self, speaker: str, text: str) -> None:
        turn = {'speaker': speaker, 'text': text}
        self.turns.append({**turn, 'role': self.active})
        self.segment.append(turn)

    def flush(self) -> None:
        if any(t['speaker'] == 'user' for t in self.segment):
            save_conversation(self.glid, self.active, self.channel, list(self.segment))
        self.segment = []
