# 角色配置

在本地编辑 `config/persona.yaml`，填写自己的角色名称、说话方式、情绪标签和交互规则。需要叙事背景时再填写 `background`，公开示例使用通用名称「助手」。修改后重启主服务，开启新对话检查称呼、回复和动作是否符合预期。

皮套单独存放在 `skin/avatar/`，入口是 `avatar.model3.json`。为角色配置语音时，在 `config/settings.yaml` 选择引擎、服务地址以及本机的参考音频或服务端的音色编号。

`config/core_memories.yaml` 管理希望跨场次保留的核心记忆；观众互动和运行期间产生的记录保存在本机 `data/`。
