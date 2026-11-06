"""IndexTTS2 常驻本地 HTTP 服务；使用 IndexTTS2 的 uv 环境运行。"""
from __future__ import annotations

import argparse
import faulthandler
import json
import math
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brain.index_expression import EMOTION_VECTORS, index_emotion_options


class SynthesisCancelled(Exception):
    """Exit inference at a generation step, before the remaining audio stages."""


class InvalidSynthesisParameter(ValueError):
    """A caller supplied an unsupported synthesis parameter value."""


class CancellationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._early: dict[str, float] = {}

    def register(self, request_id: str) -> threading.Event:
        with self._lock:
            if request_id in self._active:
                raise ValueError("duplicate request_id")
            event = threading.Event()
            expiry = self._early.pop(request_id, 0)
            if expiry > time.monotonic():
                event.set()
            self._active[request_id] = event
            return event

    def cancel(self, request_id: str) -> None:
        with self._lock:
            if request_id in self._active:
                self._active[request_id].set()
            else:
                now = time.monotonic()
                self._early = {key: expiry for key, expiry in self._early.items() if expiry > now}
                if len(self._early) >= 512:
                    self._early.pop(next(iter(self._early)))
                self._early[request_id] = now + 300

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._active.pop(request_id, None)


class CancelAtGenerationStep:
    def __init__(self, event: threading.Event) -> None:
        self.event = event

    def __call__(self, input_ids=None, scores=None, **kwargs):
        if self.event.is_set():
            raise SynthesisCancelled()
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IndexTTS2 local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9890)
    parser.add_argument("--project-dir", default="IndexTTS2")
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--warmup", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def make_handler(tts, *, fp16: bool = True, warmed_up: bool = False):
    inference_lock = threading.Lock()
    registry = CancellationRegistry()

    class Handler(BaseHTTPRequestHandler):
        server_version = "IndexTTS2Local/1.0"

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._json(200, {"status": "ok", "backend": "indextts2", "fp16": fp16,
                                 "warmed_up": warmed_up, "cancellation": True,
                                 "natural_delivery": True, "temperature": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path not in {"/tts", "/cancel"}:
                self._json(404, {"error": "not found"})
                return
            output_path: str | None = None
            request_id = ""
            registered = False
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 1_000_000:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(size))
                request_id = str(payload.get("request_id", ""))
                if request_id and (len(request_id) > 64 or not request_id.isascii() or not request_id.isalnum()):
                    raise ValueError("invalid request_id")
                if self.path == "/cancel":
                    if not request_id:
                        raise ValueError("request_id required")
                    registry.cancel(request_id)
                    self._json(200, {"cancelled": True})
                    return
                cancel_event = registry.register(request_id) if request_id else threading.Event()
                registered = bool(request_id)
                cancel_check = CancelAtGenerationStep(cancel_event)
                text = str(payload.get("text", "")).strip()
                reference = Path(os.path.abspath(str(payload.get("ref_audio_path", ""))))
                emotion = str(payload.get("emotion", "平静"))
                alpha = float(payload.get("emo_alpha", 0.75))
                emotion_options = index_emotion_options(
                    emotion, alpha,
                    reference_style=payload.get('reference_style', True) is True,
                    natural_delivery=payload.get('natural_delivery', False) is True,
                )
                # 单束采样显著减少自回归阶段计算量，适合直播；仍允许调用方调高质量。
                num_beams = max(1, min(10, int(payload.get("num_beams", 1))))
                generation_options = {}
                if "temperature" in payload:
                    temperature = payload["temperature"]
                    if (
                        isinstance(temperature, bool)
                        or not isinstance(temperature, (int, float))
                        or not 0.1 <= temperature <= 1.5
                        or not math.isfinite(temperature)
                    ):
                        raise InvalidSynthesisParameter(
                            "temperature must be a finite number between 0.1 and 1.5"
                        )
                    generation_options["temperature"] = float(temperature)
                # 省略时沿用模型自身的 0.8 默认，兼容已有客户端。
                if not text or len(text) > 1200:
                    raise ValueError("text must contain 1-1200 characters")
                if not reference.is_file():
                    raise ValueError(f"reference audio not found: {reference}")
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
                    output_path = output.name
                with inference_lock:
                    cancel_check()
                    result = tts.infer(
                        spk_audio_prompt=str(reference), text=text, output_path=output_path,
                        **emotion_options, use_random=False, verbose=False,
                        num_beams=num_beams,
                        **generation_options,
                        stopping_criteria=[cancel_check],
                    )
                    cancel_check()
                if result is None or not Path(output_path).is_file():
                    raise RuntimeError("IndexTTS2 did not produce audio")
                audio = Path(output_path).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
            except SynthesisCancelled:
                try:
                    self._json(409, {"cancelled": True})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except InvalidSynthesisParameter as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                if registered:
                    registry.finish(request_id)
                if output_path:
                    Path(output_path).unlink(missing_ok=True)

        def log_message(self, fmt: str, *values: object) -> None:
            print(f"[IndexTTS2 API] {self.address_string()} {fmt % values}", flush=True)

    return Handler


def main() -> None:
    args = parse_args()
    diagnostic = open(Path(__file__).with_name("data") / "indextts2_stack.log", "w", encoding="utf-8")
    faulthandler.enable(file=diagnostic)
    faulthandler.dump_traceback_later(120, repeat=True, file=diagnostic)
    # 不使用 Path.resolve()：它会把 Windows subst 盘符还原为含中文的真实路径，
    # 而 SentencePiece/kaldifst 无法读取该路径。
    project = Path(os.path.abspath(args.project_dir))
    checkpoints = project / "checkpoints"
    if not (checkpoints / "config.yaml").is_file():
        raise SystemExit(f"缺少 IndexTTS2 模型：{checkpoints}")
    sys.path.insert(0, str(project))
    os.chdir(project)

    from indextts.infer_v2 import IndexTTS2

    print(f"[IndexTTS2] 正在加载模型，fp16={args.fp16} ...", flush=True)
    tts = IndexTTS2(
        cfg_path=str(checkpoints / "config.yaml"), model_dir=str(checkpoints),
        use_fp16=args.fp16, use_cuda_kernel=False, use_deepspeed=False,
    )
    warmed_up = False
    if args.warmup:
        try:
            import yaml

            settings_path = project.parent / "config" / "settings.yaml"
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            tts_settings = settings.get('tts') or {}
            reference_text = str(tts_settings.get('indextts2_ref_audio_path') or tts_settings.get('ref_audio_path', ''))
            reference = Path(reference_text)
            if not reference.is_absolute():
                reference = project.parent / reference
            if reference_text and reference.is_file():
                print("[IndexTTS2] Warming up reference and generation caches...", flush=True)
                tts.infer(
                    spk_audio_prompt=str(reference), text="你好，我在听。", output_path=None,
                    emo_vector=None if tts_settings.get('indextts2_reference_style', True) else EMOTION_VECTORS["平静"], emo_alpha=0.6,
                    use_random=False, verbose=False, num_beams=1,
                )
                warmed_up = True
        except Exception as exc:
            print(f"[IndexTTS2] Warmup skipped: {type(exc).__name__}: {exc}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(tts, fp16=args.fp16, warmed_up=warmed_up))
    faulthandler.cancel_dump_traceback_later()
    print(f"[IndexTTS2] API 已启动：http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[IndexTTS2] 已停止", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
