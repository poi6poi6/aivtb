"""皮套：目前只有 MockAvatar（把表情打到终端）。

接 VTube Studio：`pip install pyvts`，按 pyvts 文档实现同样的
async show(emotion) 接口（触发表情热键），然后在 main.py 里替换 MockAvatar。
"""
from __future__ import annotations


class MockAvatar:
    async def show(self, emotion: str) -> None:
        print(f"🎭 表情 -> {emotion}")
