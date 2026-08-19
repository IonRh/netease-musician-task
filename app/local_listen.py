"""本地互助歌曲 ID 的解析与规范化。"""

from __future__ import annotations

import re


def parse_item_ids(raw: str | None) -> list[str]:
    """支持歌曲 ID、逗号/空格/换行分隔，以及 album:专辑ID。"""
    values = re.split(r"[,，\s]+", str(raw or "").strip())
    result: list[str] = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        if value.startswith("album:"):
            item_id = value[6:].strip()
            if not item_id.isdigit():
                raise ValueError(f"无效专辑 ID：{value}")
            value = f"album:{item_id}"
        elif not value.isdigit():
            raise ValueError(f"无效歌曲 ID：{value}")
        if value not in result:
            result.append(value)
    return result
