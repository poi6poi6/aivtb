"""配置加载：config/settings.yaml + config/persona.yaml + .env。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载：已存在的环境变量优先。"""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _abs(p: str) -> str:
    path = Path(p)
    return str(path if path.is_absolute() else ROOT / path)


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = "deepseek-flash"
    max_tokens: int = 320
    temperature: float | None = None   # 设为 null 则不传该参数
    reasoning_effort: str | None = None   # 思考模式强度；null = 不传
    thinking: bool | None = None   # DeepSeek: false=非思考，true=思考；null=不传
    first_clause_chars: int = 24   # 首段在自然逗号处提前送合成；0 关闭

    @property
    def api_key(self) -> str | None:
        key = os.environ.get(self.api_key_env, "").strip()
        return key or None


@dataclass
class PersonaLLMConfig:
    # Persona speech and autonomous activity share this; auxiliary calls keep llm.
    backend: str = "inherit"  # inherit | antigravity_cli | gemini_api
    api_key_env: str = "GEMINI_API_KEY"
    max_tokens: int = 4096  # Gemini includes thinking in its output budget.
    # Application budgets, not a claim about any provider's context window.
    # Input is conservatively estimated with UTF-8 bytes (no remote tokenizer).
    context_window_tokens: int = 131072
    max_input_tokens: int = 98304
    context_reserve_tokens: int = 1024  # Channel JSON/retry instructions added after assembly.
    command: str = "data/argon-runtime/bin/agy.exe"
    model: str = "gemini-4-argon-medium"
    effort: str = "medium"
    timeout_sec: float = 45.0
    qq_timeout_sec: float = 90.0  # Full QQ reply (including one format retry), separate from live speech.
    runtime_dir: str = "data/argon-runtime/turns"


@dataclass
class TTSConfig:
    backend: str = "mock"               # mock | gpt_sovits | indextts2 | fish_speech
    gpt_sovits_url: str = "http://127.0.0.1:9880"
    indextts2_url: str = "http://127.0.0.1:9890"
    indextts2_emo_alpha: float = 0.60
    indextts2_num_beams: int = 1       # 1 = 直播低延迟；3 = 默认高质量但明显更慢
    trim_edge_silence: bool = True    # 只缩短 PCM16 首尾过长静音，保留句内停顿
    indextts2_reference_style: bool = True
    indextts2_natural_delivery: bool = True  # 同轮参考连贯，明确情绪轻量混合
    indextts2_delivery_preset: str = "reference"  # reference = A; expressive = B，可单独关闭吐槽增强
    indextts2_expressive_teasing: bool = True  # false = 吐槽用 A，闲聊和解释继续使用已选预设
    indextts2_ref_audio_path: str = ""
    indextts2_short_ref_audio_path: str = ""
    indextts2_long_min_chars: int = 28
    indextts2_long_num_beams: int = 3
    fish_speech_url: str = "http://127.0.0.1:8081"
    fish_speech_reference_id: str = ""
    fish_speech_ref_audio_path: str = ""
    fish_speech_prompt_text: str = ""   # Fish 参考音频逐字转写，与其他后端独立
    fish_speech_api_key_env: str = "FISH_SPEECH_API_KEY"
    fish_speech_timeout_sec: float = 240.0
    fish_speech_emotion_tags: bool = True
    fish_speech_natural_delivery: bool = True
    fish_speech_breathing: bool = True
    out_dir: str = "data/audio"
    audio_retention_enabled: bool = True
    audio_retention_days: float = 7.0
    audio_retention_max_mib: int = 1024
    audio_retention_interval_sec: float = 300.0
    ref_audio_path: str = ""            # GPT-SoVITS 角色参考音频
    prompt_text: str = ""               # 参考音频对应的文本
    prompt_lang: str = "zh"


@dataclass
class PlaybackConfig:
    max_queue: int = 3
    max_age_sec: float = 75.0
    start_timeout_sec: float = 20.0
    max_play_sec: float = 180.0
    interrupt_priority: int = 80


@dataclass
class BroadcastConfig:
    wait_notice_sec: float = 8.0
    reply_timeout_sec: float = 45.0
    synthesis_timeout_sec: float = 60.0
    failure_limit: int = 3
    cooldown_sec: float = 30.0


@dataclass
class SourceConfig:
    """Optional automatic event sources; direct companion chat is always available."""

    bilibili_enabled: bool = False
    mock_danmaku_enabled: bool = False
    mock_game_enabled: bool = False


@dataclass
class GameAgentConfig:
    enabled: bool = False
    observe_only: bool = True       # 必须改为 false，网页开关才可能发送输入
    window_title: str = ""          # 只捕获标题包含此文本的可见窗口；空值拒绝捕获
    profile_path: str = "config/games/generic_wasd.yaml"
    capture_fps: float = 2.0
    decision_interval_sec: float = 1.0
    event_interval_sec: float = 12.0
    max_actions_per_minute: int = 30
    allowed_keys: list[str] = field(default_factory=lambda: ["w", "a", "s", "d", "space", "e", "enter", "esc"])
    allow_mouse: bool = False
    allow_gamepad: bool = False
    emergency_stop_key: str = "f12"
    require_foreground: bool = True
    ocr_enabled: bool = True
    ocr_language: str = "chi_sim+eng"
    tesseract_cmd: str = ""
    jpeg_quality: int = 72


@dataclass
class ArbiterConfig:
    danmaku_cooldown_sec: float = 8.0
    idle_comment_sec: float = 45.0
    max_pending: int = 200
    normal_max_age_sec: float = 30.0
    aggregate_limit: int = 5


@dataclass
class DirectorConfig:
    # 模块单独构造时保持原有行为；项目默认开关由 settings.yaml 设为 false。
    enabled: bool = True
    opening_delay_sec: float = 2.0
    opening_turns: int = 3
    proactive_interval_sec: float = 35.0
    interaction_every_turns: int = 6
    interaction_turns: int = 2
    interaction_min_viewers: int = 2
    interaction_response_timeout_sec: float = 28.0
    interaction_audio_start_fallback_sec: float = 12.0
    engagement_window_sec: float = 90.0
    high_chat_rate_per_min: float = 6.0
    highlight_turns: int = 2
    topic_max_turns: int = 6
    active_viewer_window_sec: float = 900.0
    closing_after_minutes: float = 0.0  # 0 = 不自动收播，可在测试台手动切换
    topic_seeds: list[str] = field(default_factory=lambda: [
        "今天遇到的一件小幸运",
        "最近反复在听的一首歌",
        "忙完以后最喜欢的放松方式",
        "最近想学会的一件小事",
        "如果今天可以多出一小时会做什么",
    ])


@dataclass
class MemoryConfig:
    path: str = "data/memory.jsonl"
    viewer_db: str = "data/viewers.json"


@dataclass
class SelectiveMemoryConfig:
    enabled: bool = True
    state_path: str = "data/autobiographical_memory.json"
    core_memories_path: str = "config/core_memories.yaml"
    max_entries: int = 1200
    recall_limit: int = 4
    minimum_salience: float = 0.45
    minimum_confidence: float = 0.55
    minimum_recall_score: float = 0.42
    episode_retention_days: int = 45
    temporal_graph: bool = True
    associative_hops: int = 2
    associative_decay: float = 0.72
    minimum_association_score: float = 0.30
    associative_recall_limit: int = 1
    timeline_context_window: int = 3
    llm_extraction: bool = True
    extraction_max_tokens: int = 260
    semantic_recall: bool = True   # 本地同义概念重排，不发送记忆到额外服务
    context_timeout_sec: float = 0.75  # 独立记忆源和审计准备各自的等待上限


@dataclass
class LearningConfig:
    enabled: bool = True
    state_path: str = "data/learning.json"
    max_episodes: int = 500
    feedback_window_sec: float = 90.0
    consolidation_interval: int = 20


@dataclass
class PersonalityEvolutionConfig:
    enabled: bool = True
    state_path: str = "data/personality_evolution.json"
    profile: str = "generic-v1"
    consolidation_interval: int = 20
    max_evidence: int = 1000
    max_snapshots: int = 200
    max_relationships: int = 300
    global_learning_rate: float = 0.012
    relationship_learning_rate: float = 0.04
    max_global_delta_per_consolidation: float = 0.025
    daily_global_delta_limit: float = 0.05
    return_to_baseline: float = 0.015
    base_traits: dict[str, float] = field(default_factory=lambda: {
        "energy": 0.36,
        "warmth": 0.62,
        "playfulness": 0.32,
        "initiative": 0.40,
        "directness": 0.78,
        "emotional_openness": 0.32,
        "boundary_strength": 0.92,
        "resilience": 0.90,
    })


@dataclass
class CognitionConfig:
    enabled: bool = False
    database_path: str = "data/cognition.db"


@dataclass
class SearchConfig:
    enabled: bool = True
    max_results: int = 4
    timeout_sec: float = 5.0
    cache_ttl_sec: float = 1800.0


@dataclass
class WebUIConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    public_test: bool = False
    public_token_env: str = "VTUBER_SHARE_TOKEN"


@dataclass
class AgentTasksConfig:
    enabled: bool = False
    omp_command: str = 'data/agent-runtime/omp.exe'
    codex_command: str = 'codex'
    max_queue: int = 8
    timeout_sec: int = 900
    max_dispatch_retries: int = 1
    max_tool_calls: int = 80
    max_model_calls: int = 4
    foreground_wait_sec: int = 180  # Legacy config compatibility; resource waits now remain queued.
    notification_retries: int = 1


@dataclass
class Settings:
    room_id: int = 0
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona_llm: PersonaLLMConfig = field(default_factory=PersonaLLMConfig)
    agent_tasks: AgentTasksConfig = field(default_factory=AgentTasksConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    broadcast: BroadcastConfig = field(default_factory=BroadcastConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    game_agent: GameAgentConfig = field(default_factory=GameAgentConfig)
    arbiter: ArbiterConfig = field(default_factory=ArbiterConfig)
    director: DirectorConfig = field(default_factory=DirectorConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    selective_memory: SelectiveMemoryConfig = field(default_factory=SelectiveMemoryConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    personality_evolution: PersonalityEvolutionConfig = field(default_factory=PersonalityEvolutionConfig)
    cognition: CognitionConfig = field(default_factory=CognitionConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)


@dataclass
class PersonaConfig:
    name: str = "助手"
    species: str = "AI 虚拟主播"
    tone: str = ""
    speaking_style: str = ""
    revision: str = "generic-v1"
    age: str = ""
    gender: str = ""
    current_era: str = ""
    appearance: str = ""
    interests: list[str] = field(default_factory=list)
    background: str = ""
    reply_examples: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    emotions: list[str] = field(
        default_factory=lambda: ["开心", "吐槽", "惊讶", "得意", "委屈", "平静"]
    )


def load_settings(path: Path | None = None) -> Settings:
    raw = _yaml(path or Path(os.environ.get('VTUBER_SETTINGS_PATH', CONFIG_DIR / 'settings.yaml')))
    settings = Settings(room_id=int(raw.get("room_id", 0)))
    for key, cls in [
        ("llm", LLMConfig),
        ("persona_llm", PersonaLLMConfig),
        ("agent_tasks", AgentTasksConfig),
        ("tts", TTSConfig),
        ("playback", PlaybackConfig),
        ("broadcast", BroadcastConfig),
        ("sources", SourceConfig),
        ("game_agent", GameAgentConfig),
        ("arbiter", ArbiterConfig),
        ("director", DirectorConfig),
        ("memory", MemoryConfig),
        ("selective_memory", SelectiveMemoryConfig),
        ("learning", LearningConfig),
        ("personality_evolution", PersonalityEvolutionConfig),
        ("cognition", CognitionConfig),
        ("search", SearchConfig),
        ("webui", WebUIConfig),
    ]:
        section = raw.get(key) or {}
        valid = {k: v for k, v in section.items() if k in cls.__dataclass_fields__}
        setattr(settings, key, cls(**valid))
    settings.memory.path = _abs(settings.memory.path)
    settings.persona_llm.runtime_dir = _abs(settings.persona_llm.runtime_dir)
    if '/' in settings.persona_llm.command or '\\' in settings.persona_llm.command:
        settings.persona_llm.command = _abs(settings.persona_llm.command)
    settings.memory.viewer_db = _abs(settings.memory.viewer_db)
    settings.selective_memory.state_path = _abs(settings.selective_memory.state_path)
    settings.selective_memory.core_memories_path = _abs(settings.selective_memory.core_memories_path)
    settings.learning.state_path = _abs(settings.learning.state_path)
    settings.personality_evolution.state_path = _abs(settings.personality_evolution.state_path)
    settings.cognition.database_path = _abs(settings.cognition.database_path)
    settings.tts.out_dir = _abs(settings.tts.out_dir)
    if settings.tts.fish_speech_ref_audio_path:
        settings.tts.fish_speech_ref_audio_path = _abs(settings.tts.fish_speech_ref_audio_path)
    settings.game_agent.profile_path = _abs(settings.game_agent.profile_path)
    return settings


def load_persona(path: Path | None = None) -> PersonaConfig:
    raw = _yaml(path or CONFIG_DIR / "persona.yaml")
    valid = {k: v for k, v in raw.items() if k in PersonaConfig.__dataclass_fields__}
    return PersonaConfig(**valid)
