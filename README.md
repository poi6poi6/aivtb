# ai-vtuber

在 Windows 上运行的虚拟主播控制台。可以先在浏览器里离线演练弹幕和播放流程，再接入文字模型、语音服务、自备 Live2D 皮套和直播软件。项目也提供观众记忆、后台任务、点歌、投票、游戏和 OBS 展示页。

## 先试控制台

在项目根目录打开 PowerShell，使用 Python 3.10 或更高版本创建环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m brain.local_test
```

浏览器会打开本地控制台。输入一段文字或模拟一条弹幕，页面会用预设回复和测试提示音演示流程。此模式可以离线使用，也可以从 `启动.cmd` 菜单进入。按 Ctrl+C 关闭。[本地测试说明](docs/local-test.md)

## 使用文字对话

1. 在根目录执行 `Copy-Item .env.example .env`，把自己的模型密钥写入 `.env`。默认接口读取 `DEEPSEEK_API_KEY`。
2. 在 `config/settings.yaml` 的 `llm` 段核对服务地址、模型 ID 和密钥环境变量。默认 `persona_llm.backend: inherit` 使用同一接口。
3. 在项目根目录执行以下命令，再用浏览器打开 [本机控制台](http://127.0.0.1:8080/)。

```powershell
.\.venv\Scripts\python.exe -m brain.main
```

公共配置使用 `tts.backend: mock`。直接对话需要文字模型接口，独立语音引擎和人物素材可以之后再装。启动器也提供正式运行、环境检查和日志入口。[启动器命令](docs/launcher.md)

## 配置自己的角色

- 在 `config/persona.yaml` 定义名称、说话风格和交互规则，保存后重启主服务。公开示例使用通用名称「助手」。[角色配置](docs/formal-persona.md)
- 需要人物画面时，把自己有权使用的 Cubism 模型放入本地 `skin/avatar/`，入口文件名为 `avatar.model3.json`。按照该文件中的相对路径放置模型、贴图、动作和表情等配套文件，再刷新浏览器。自定义模型支持的动作取决于它提供的参数和资源。
- 随仓库提供的 PixiJS、pixi-live2d-display 和 Cubism Core 是通用显示运行库。语音模型、参考录音、角色素材和本地状态均由使用者自行准备。

## 可选组件

| 功能 | 准备工作 |
| --- | --- |
| 语音 | 在独立环境安装相应上游推理服务和权重，可选 IndexTTS2、GPT-SoVITS 或 Fish Speech。切换 `tts.backend` 后配置服务地址、自备参考音频及引擎要求的逐字转写。IndexTTS2 的本地启动器约定项目下的 `IndexTTS2/.venv` 和 `IndexTTS2/checkpoints`。[Fish Speech 接入](docs/fish-speech.md) |
| 持久认知内核 | 需要单独取得包含 Python 模块 `cognitive_os` 的项目。若放在同级 `构想/`，从本项目根目录执行 `.\.venv\Scripts\python.exe -m pip install "../构想"`，再按需要开启 `cognition.enabled`。 |
| B 站弹幕 | 安装 `requirements-bilibili.txt`，填写自己的 `room_id` 并启用 `sources.bilibili_enabled`。[直播接入](docs/bilibili-live.md) |
| OBS 展示 | 将 `http://127.0.0.1:8080/?display=1` 添加为浏览器源。`启动OBS.cmd` 仅适用于已放好 `data/obs-studio` 的便携版安装。 |
| 后台任务 | 独立任务服务使用本机运行时和账户登录。[后台任务](docs/agent-tasks.md) |
| 点播、翻唱与原创 | 本地音乐放入 `data/songs/`。翻唱需单独安装 RVC 并配置自备音色模型；原创支持手动导入作品。[点播](docs/bilibili-media.md) · [翻唱](docs/rvc.md) · [原创](docs/suno-manual.md) |
| 游戏 | 视觉/输入接入使用 `requirements-live.txt`，OCR 另需 Tesseract 和对应语言数据。Minecraft 有独立 Node 项目及服务端。[Minecraft 设置](docs/minecraft.md) |

## 项目结构

- `brain/` 包含 Python 主服务与浏览器前端。
- `body/`、`minecraft/` 提供游戏接入。
- `config/` 存放通用示例配置，`tools/` 包含启动器、扩展和诊断工具。
- `tests/` 包含 Python 与 JavaScript 测试。部分集成用例需要相应的本地外部服务。
- `vendor/blivedm/` 包含 B 站弹幕客户端及其来源、许可证和项目兼容补丁。
- `data/` 由服务在本地创建，用于保存日志、对话数据和生成音频。

[服务架构](docs/architecture-20261017.mmd) · [直播准备流程](docs/broadcast-workflow-20260927.md) · [QQ 接入](docs/qq-private-chat.md) · [栏目管理](docs/show-sessions.md)

