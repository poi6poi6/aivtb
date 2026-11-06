# RVC 翻唱

实际流程：`唱歌 歌名` → 精确匹配本地原曲 → BS-Roformer 分离 → RVC + RMVPE 转换人声 → 混入伴奏 → WAV 成品 → 现有播放队列。

配置 `config/rvc.json`：

- `model`：自备 RVC 推理模型 `.pth` 路径，绝对路径或相对项目路径。
- `index`：可选 `.index` 路径。留空时关闭检索；有索引时使用 `index_rate`。
- `pitch`：半音移调，默认0，听过样例再调整。

将原曲放到 `data/music/sources/歌名.wav`（也支持mp3、flac、m4a、ogg；最长10分钟）。弹幕使用 `唱歌 歌名`。只匹配完整文件名，不会模糊选择错误版本。网易云自动取音源尚未接入。

主服务启动后自动串行处理翻唱任务，不会同时启动多个GPU转换。朋友测试不启动制歌工作进程。面板显示缺模型、等原曲、制作中、失败和完成；可以取消或重试，成品可再次播放。成品位于 `data/songs/RVC-任务ID.wav`，日志和中间音轨位于 `data/music/work/任务ID/`。

基础模型及环境验证：

```powershell
.\RVC\.venv\Scripts\python.exe tools/check_rvc.py
```

独立转换（模型配置完成后）：

```powershell
.\RVC\.venv\Scripts\python.exe tools/rvc_cover.py --source "./data/music/sources/歌名.wav" --work "./data/music/manual" --output "./data/music/manual/cover.wav"
```

上游：https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI ，当前固定提交 `81eed5e8f68b6bed1789f682fe78cdd324495afc`。独立 Python 3.12 / Torch 2.7.1 CUDA 12.8 环境，不修改 IndexTTS2 依赖。
