# 统一启动器

正式启动时不等待语音加载即可启动文字核心；不再为了等待 IndexTTS2 或 Fish Speech 阻塞主控页面。语音未就绪的聊天回合使用完整文字，后续回合在服务恢复后使用语音。`/runtime` 分别显示核心、语音、QQ 与 B站浏览器状态；文字核心就绪表示工作循环与关键持久化正常，不代表外部模型的连接、权限或额度已经通过验证。健康探针不会生成测试音频，也不证明本次合成或发送已成功。

语音进程早退、依赖缺失或临时盘符冲突会让语音显示不可用，文字服务仍可启动。启动器只清理自己创建且进程身份仍匹配的语音进程，保留复用的外部服务与不属于本次启动的盘符映射。

Windows 下双击项目根目录的 `启动.cmd`，从中文菜单选择需要的服务。日常只需记住这一个文件；启动、停止和清理脚本集中在 `tools/launcher/`。

正式主服务和离线测试在当前启动窗口运行，服务运行时请保留该窗口。正式主服务关闭时沿用原有语音进程和临时盘符清理规则；离线测试按 `Ctrl+C` 结束。菜单不会额外连带启动 Minecraft、后台任务或公网服务，正式主服务仍遵循 `config/settings.yaml` 中已有的配置。

## 常用命令

在项目目录的 PowerShell 中执行：

| 用途 | 命令 |
| --- | --- |
| 打开菜单 | `.\启动.cmd` |
| 正式主服务 | `.\启动.cmd -Action run` |
| 本地离线测试 | `.\启动.cmd -Action local-test` |
| 检查正式及离线启动条件 | `.\启动.cmd -Action check` |
| 查看服务状态 | `.\启动.cmd -Action status` |
| 打开日志目录 | `.\启动.cmd -Action logs` |
| 启动 / 停止后台任务服务 | `.\启动.cmd -Action tasks-start` / `.\启动.cmd -Action tasks-stop` |
| 启动 / 停止 Minecraft 服务端 | `.\启动.cmd -Action minecraft-start` / `.\启动.cmd -Action minecraft-stop` |
| 单独启动 IndexTTS2 | `.\启动.cmd -Action tts` |
| 选择语音模型及 Fish 音色 | `.\启动.cmd -Action voice-settings` |
| 检查外部 Fish Speech 服务 | `.\启动.cmd -Action fish-check` |
| 停止朋友测试 | `.\启动.cmd -Action friend-stop` |

`run` 和 `local-test` 支持 `-NoBrowser`（不打开浏览器）与 `-ValidateOnly`（只检查）。离线测试还支持自定义端口：

```powershell
.\启动.cmd -Action run -NoBrowser
.\启动.cmd -Action local-test -Port 18081
.\启动.cmd -Action local-test -ValidateOnly
```

离线测试使用脚本回复和提示音，不调用模型、不写正式记忆。详情见 [本地测试](local-test.md)。后台任务停止会取消任务，Minecraft 停止会正常保存世界；退出菜单或关闭浏览器不等于停止这些独立服务。

## Fish Speech 选项

主菜单 **7 语音模型设置** 可选择 Fish Speech、IndexTTS2、GPT-SoVITS 或 mock。选择 Fish 后可设置外部官方 API 服务根地址和参考音色；保存会保留其他配置和注释，备份原文件，停止并重新启动主服务后生效。当前默认语音不会因更新代码而改变。

Fish 服务需在独立环境预先启动。高级菜单 **7 检查 Fish Speech 服务** 仅请求健康接口，不生成声音；正式运行会检查并复用这个外部服务，退出主控时不会关闭它。`run -ValidateOnly` 仅检查本地启动组件，不请求健康接口。安装及完整参数见 [Fish Speech 接入](fish-speech.md)。

## 旧脚本路径

原根目录脚本已迁到 `tools/launcher/`。旧快捷方式、外部脚本或文档中的根目录路径需要更新；建议改为上面的统一命令。需要继续直接调用旧脚本时，在路径前加 `tools\launcher\`：

| 原根目录文件 | 当前位置 / 推荐命令 |
| --- | --- |
| `start-all.cmd`、`start-all.ps1` | `tools/launcher/` 同名文件；推荐 `-Action run` |
| `start-local-test.cmd`、`start-local-test.ps1` | `tools/launcher/` 同名文件；推荐 `-Action local-test` |
| `start-agent-tasks.ps1`、`stop-agent-tasks.ps1` | `tools/launcher/` 同名文件；推荐 `tasks-start`、`tasks-stop` |
| `start-minecraft.ps1`、`stop-minecraft.ps1` | `tools/launcher/` 同名文件；推荐 `minecraft-start`、`minecraft-stop` |
| `start-indextts2.ps1` | `tools/launcher/start-indextts2.ps1`；推荐 `-Action tts` |
| `stop-friend-test.ps1` | `tools/launcher/stop-friend-test.ps1`；推荐 `-Action friend-stop` |
| `launcher-cleanup.ps1` | `tools/launcher/launcher-cleanup.ps1`；由启动器调用 |

运行数据和日志仍放在原来的 `data/` 路径，正式配置仍在 `config/`。本次整理不代表真实模型、语音或游戏服务已完成实机验收。
