# Fish Speech 语音选项

项目新增 `fish_speech` 后端，可连接官方 Fish Speech 的 Drama 3 或 S2-Pro API 服务。原来的 IndexTTS2、GPT-SoVITS 和 mock 继续可选；安装这次更新不会切换当前语音。

## 在启动中心选择

1. 双击根目录 `启动.cmd`，选 **7 语音模型设置**，再选 **1 Fish Speech**。
2. 填写服务根地址，默认 `http://127.0.0.1:8081`。不要填 `/v1/tts`，也不要占用主控制台的 8080 端口。
3. 选择音色：已保存在 Fish 服务端的参考 ID，或本机参考音频与其逐字转写。两者都未配置时使用模型随机音色，不适合保持角色声线。
4. 保存后停止并重新启动主服务。已有主服务运行时，重复选择“正式运行”会复用旧进程，不能加载新设置。

也可直接运行 `启动.cmd -Action voice-settings`。设置工具只修改所选的语音字段，保留其他配置和注释，并在 `config/` 下保存一份 `settings.yaml.tts-时间-编号.bak`。它不会下载模型、调用合成或切断正在进行的对话。

## 启动独立的 Fish 服务

先按 [Fish Speech 官方安装文档](https://speech.fish.audio/install/)准备独立环境及对应权重。不要把 Fish 的 PyTorch/CUDA 依赖安装进本项目的 `.venv`、IndexTTS2 或 GPT-SoVITS 环境。也可以连接已部署在其他机器上的官方 API 服务。

准备与所选权重相匹配的硬件和独立 Python 环境。

在 **Fish Speech 仓库目录**，使用它自己的 Python 环境启动 Drama 3：

```powershell
python tools/api_server.py --listen 127.0.0.1:8081 --llama-checkpoint-path checkpoints/drama-3 --device cuda:0
```

如使用 S2-Pro，使用对应的模型和 codec 权重：

```powershell
python tools/api_server.py --listen 127.0.0.1:8081 --llama-checkpoint-path checkpoints/s2-pro --decoder-checkpoint-path checkpoints/s2-pro/codec.pth --decoder-config-name modded_dac_vq
```

模型由服务启动时加载的权重决定，本项目不发送 `model` 参数。Drama 3 不支持 `--compile`；硬件要求及 codec 权重准备以[官方推理文档](https://speech.fish.audio/inference/)为准。

启动器的“高级操作 → 7 检查 Fish Speech 服务”或下列命令只请求 `/v1/health`，不生成音频：

```powershell
.\启动.cmd -Action fish-check
```

“正式运行”在配置为 Fish Speech 时也执行这个检查，再启动主控。服务不可用会明确提示；启动器不会改用 IndexTTS2，也不会启动或停止这个外部服务。健康检查成功只说明接口可用，实际声线、语种、延迟仍需在控制台发一句话试听。

## 配置项

启动中心可设置后端、服务地址和参考音色；其他参数可在 `config/settings.yaml` 的现有 `tts:` 段调整，不要另加第二个 `tts:`：

```yaml
tts:
  backend: fish_speech
  fish_speech_url: http://127.0.0.1:8081
  fish_speech_reference_id: ""
  fish_speech_ref_audio_path: "data/references/reference.wav"
  fish_speech_prompt_text: "这段参考音频的逐字转写。"
  fish_speech_api_key_env: FISH_SPEECH_API_KEY
  fish_speech_timeout_sec: 240
  fish_speech_emotion_tags: true
  fish_speech_natural_delivery: true
  fish_speech_breathing: true
```

- 参考 ID 优先：设置 `fish_speech_reference_id` 后，不发送本机参考文件。ID 必须已存在于 Fish 服务端，不能把音频路径当作 ID。
- 本机参考文件和转写必须成对填写。相对路径以本项目根目录为基准，文件内容会随合成请求发给配置的 Fish 服务。不会直接沿用 IndexTTS2 的音频或 GPT-SoVITS 的转写。
- 若官方服务启用了鉴权，在项目 `.env` 或启动主控的环境中设置 `FISH_SPEECH_API_KEY`，值应与服务端一致；无需 OpenAI API Key。设置菜单不会显示或修改密钥。
- 情绪标签默认开启，将角色情绪映射为 Fish 的语气提示；设为 `false` 可关闭，不影响 Live2D 情绪动作。
- `fish_speech_natural_delivery: true` 增加日常会话节奏、好奇问句、感叹重点和转折处的起伏。续句保持当前情绪的能量，开心不再越说越轻、吐槽不再统一变柔；只有明确的安慰或缓和内容才收束。普通逗号依靠模型自然停顿，不再逢长句固定加短停顿。不会随机切换情绪或添加口头禅。关闭此项恢复原基础情绪标签，关闭 `fish_speech_emotion_tags` 则所有自动语气提示一起关闭。
- 文件参考启用官方音频哈希缓存，减少每段重复编码同一参考音频；仍随请求发送当前音频和转写，换参考文件后按新哈希缓存。使用 `reference_id` 时不启用这项文件缓存，以便读取服务端参考的更新。缓存只能减少额外开销，不能保证实时合成。
- 每句通过 `/v1/tts` 合成完整 WAV，然后交给已有播放、口型和音频清理流程。使用 JSON 请求，参考音频以 base64 传输；未启用服务端音频流式播放。

## 等待过久或打断

`fish_speech_timeout_sec` 控制 HTTP 等待时间。主控的 `broadcast.synthesis_timeout_sec` 仍是独立的合成时间上限，两者以先到的为准；HTTP 设为 240 秒不会自动把全局 60 秒改成 240 秒。先在独立服务上预热并确认能及时生成，再按实际设备调整全局上限。

用户打断时会取消本项目的 HTTP 等待，旧结果不会进入播放队列。官方接口没有本项目 IndexTTS2 的 `/cancel` 协议，因此不能保证服务端 GPU 推理同时停止。

自动化测试覆盖请求结构、音频响应检查、取消与异常处理、设置保存和启动器路由。
