# ai-vtuber

[简体中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

A Windows control panel for running a virtual streamer. Start with an offline rehearsal in your browser to try live chat and playback. Then connect a language model, a speech service, your own Live2D avatar and your streaming software. The project also supports viewer memory, background tasks, song requests, polls, games and OBS overlays.

## Try the offline console

Open PowerShell in the project root and run the following commands with Python 3.10 or later:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m brain.local_test
```

The local console opens in your browser. Type a message or simulate a viewer's live chat message to see a scripted reply and hear a test tone. Once the dependencies are installed, this mode works offline. You can also start it from the `启动.cmd` menu. Press Ctrl+C in the terminal to stop it. The detailed guides linked below are in Chinese. [Offline test guide](docs/local-test.md)

## Chat with a language model

1. From the project root, run `Copy-Item .env.example .env` and add your model provider's API key to `.env`. The default provider reads `DEEPSEEK_API_KEY`.
2. In the `llm` section of `config/settings.yaml`, check the service URL, model ID and name of the environment variable holding the key. The default setting, `persona_llm.backend: inherit`, uses the same endpoint for character dialogue.
3. Run the command below from the project root, then open the [local console](http://127.0.0.1:8080/) in your browser.

```powershell
.\.venv\Scripts\python.exe -m brain.main
```

The example configuration uses `tts.backend: mock`. For text chat, all you need beyond the base installation is a working language model endpoint. You can add a speech engine and avatar assets later. The launcher also lets you start the main service, check the environment and open the logs. [Launcher commands](docs/launcher.md)

## Configure your own character

- Set the name, speaking style and interaction rules in `config/persona.yaml`. Restart the main service after saving. The example uses the generic name 助手 (“Assistant”). [Character configuration](docs/formal-persona.md)
- To display an avatar, place a Cubism model you have permission to use in your local `skin/avatar/` directory. Name its entry file `avatar.model3.json`. Place the model data, textures, motions and expressions at the relative paths specified in that file. If you created the `skin/` directory while the service was running, restart the service before refreshing the browser. The actions available depend on the parameters and assets your model provides.
- The repository includes PixiJS, pixi-live2d-display and Cubism Core for rendering. Supply your own voice models, reference recordings and character assets. Runtime data is stored locally.

## Optional components

| Feature | Setup |
| --- | --- |
| Speech synthesis | Choose IndexTTS2, GPT-SoVITS or Fish Speech and install its inference service and model weights in a separate environment. Set `tts.backend`, then configure the service URL, your reference audio and a verbatim transcript if the engine requires one. The local IndexTTS2 launcher expects `IndexTTS2/.venv` and `IndexTTS2/checkpoints` under the project root. [Fish Speech setup](docs/fish-speech.md) |
| Persistent cognition | Obtain the separate project that provides the `cognitive_os` Python module. If you place it in a sibling directory named `构想/`, run `.\.venv\Scripts\python.exe -m pip install "../构想"` from this project's root, then turn on `cognition.enabled` when you want to use it. |
| Bilibili live chat | Install the dependencies in `requirements-bilibili.txt`, set `room_id` to your live room ID and enable `sources.bilibili_enabled`. [Livestream setup](docs/bilibili-live.md) |
| OBS overlay | Add `http://127.0.0.1:8080/?display=1` as a browser source. To use `启动OBS.cmd`, place a portable OBS installation in `data/obs-studio`. |
| Background tasks | The separate task service uses a local runtime and requires you to sign in to your account. [Background tasks](docs/agent-tasks.md) |
| Song requests, covers and original songs | Place local music in `data/songs/`. To generate covers, install RVC separately and configure your own voice model. You can import finished original songs manually. [Song requests](docs/bilibili-media.md) · [Covers](docs/rvc.md) · [Original songs](docs/suno-manual.md) |
| Games | Install the dependencies in `requirements-live.txt` for game capture and input. OCR also requires Tesseract and the relevant language data. The Minecraft integration uses a separate Node.js project and game server. [Minecraft setup](docs/minecraft.md) |

## Project layout

- `brain/` contains the main Python service and the browser frontend.
- `body/` and `minecraft/` provide game integrations.
- `config/` holds example configuration files. `tools/` contains the launcher, extensions and diagnostic tools.
- `tests/` contains the Python and JavaScript tests. Some integration tests require additional services running locally.
- `vendor/blivedm/` contains the Bilibili live chat client, its upstream source information, its license and the project's compatibility patches.
- `data/` is created locally by the service for logs, chat data and generated audio.

[Service architecture](docs/architecture-20261017.mmd) · [Livestream preparation](docs/broadcast-workflow-20260927.md) · [QQ integration](docs/qq-private-chat.md) · [Managing show segments](docs/show-sessions.md)
