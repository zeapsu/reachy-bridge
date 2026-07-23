"""One-shot multimodal probe used only by the committed compatibility gate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

IMAGE_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEm"
    "KzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCABIAIADASIAAhEBAxEB/8QAGgAB"
    "AAMBAQEAAAAAAAAAAAAAAAQFBgMBAv/EADEQAAEEAgEDAgMGBwEAAAAAAAEAAgMEBREGEhMhMUEUFWEi"
    "MjNRcYEHIyVSgpGSwf/EABgBAQEBAQEAAAAAAAAAAAAAAAACAwEG/8QAIxEAAgEEAgICAwAAAAAAAAAA"
    "AAECAxESQRMxIVGh0WGx8P/aAAwDAQACEQMRAD8AzyIi9eeZCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiLR"
    "2+O4fHvjiu5+SKZ8TJC0US4AOG/UOUSnGPhlxg5dGcRX7uL/wBVxtaK+yapkj/ItMjPsdHbSfUH22vZM"
    "Nx2GV8T+TSBzHFpHy93qP8AJTzQ1+mVxS/mjPop1LG/Mc3Hjqk3cbLN0MlLNbbv72vbx50vrNYr5Rf+H"
    "bOLET42ywzBvSJGOGwdf7H7K845Y7IxdstFeivIuP14KkNjMZRmP+Ib1xRCF0sjm+ziBrQPsvo8Xf8AN"
    "cfWZcjlqZF2oLcbSQRvR+ydEEe4Uc0PZXFL0UKLrPD2LUkHV1dt5Z1a1vR0r6zxPtW8pSgvd63j2tkE"
    "Xa6e8zQLiPJ8jfoqlUjHtnFCUujOIrLE4j5jFaszz/DVKkfXLN0dXk/daBsbJP1VaqUk20tHGmlcIiLp"
    "IREQBbjON43JlakeWdkY5XVYQ6SFzO20dI0dEE/qsOpmTyc2VssnnbG1zImxAMBA00aHqT5WNSm5STvb"
    "s1hNRi0a1zpK/NMJim1hXqUnj4bT+vuNcdl/Vob2qq5lOOtvWA/jcj3CVwLvmDhs7PnXSocPJrsLMcDF"
    "BK/Gv6oJJGku1/aSD5au7uVB73Pfx/Cuc47JNZ3k/wDSwVKSfla07bZs6kWu/i5K4pVY2tlMqZoaYbGa"
    "9aSd5DWvfv3AJ2G/T3XTM49s3D6szLtW5NjH9qV9Z5cBE87bvYHofH7qku5qa7QZRFevXrtndP0QNLQX"
    "O/Un0HgfRc8flZ8bDbhiZHJHchMUjJASNexGiPI9lfFNyz3fr8dfZCqRSx1YtOcAu5G6Zv4E0ET4D7Fn"
    "QB4/cFe8ZpXYsvhLcwcKs1kth2/1IPnQ/wDVEqclt16cdOetTvwRfhNtw9fb+gOwdfReSclyEuWrZJ/a"
    "LqhHYhDOmJgHsGjXhMKmHHbVvgZQzzuTreT483ITtfxt7niVwLvj3DZ3666V98nyE+L/AIg2rtZ3TLDI"
    "xw/I/YbsH6EeFm5ZnTWHzOADnvLiB6bJ2pGVyU2YyU1+w2NssxBcIwQ0aAHjZP5KlRSkvVmu2/Rx1bx"
    "fu/2XvMbMVd0OLowCvVeBbewHfVI8b8/QDwFllMyeTmytlk87Y2uZE2IBgIGmjQ9SfKhrSlDCCT7IqSy"
    "k2giItDMIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiID//2Q=="
)

FAIL_CLOSED = "reachy_probe_snapshot denied: one-shot token already consumed"


def _audit(outcome: str) -> None:
    path_value = os.environ.get("REACHY_PROBE_AUDIT_FILE")
    if path_value:
        with Path(path_value).open("a", encoding="utf-8") as handle:
            handle.write(f"{outcome}\n")


def reachy_probe_snapshot(*_: Any, **__: Any) -> dict[str, Any] | str:
    """Consume the gate-owned nonce and return one in-turn image envelope."""
    nonce_value = os.environ.get("REACHY_PROBE_NONCE_FILE")
    nonce = Path(nonce_value) if nonce_value else None
    if nonce is None or not nonce.is_file():
        _audit("denied")
        return FAIL_CLOSED
    try:
        nonce.unlink()
    except OSError:
        _audit("denied")
        return FAIL_CLOSED
    _audit("image")
    return {
        "_multimodal": True,
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{IMAGE_BASE64}",
                },
            }
        ],
        "text_summary": "Reachy compatibility probe snapshot captured.",
    }


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="reachy_probe_snapshot",
        toolset="reachy_probe",
        schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=reachy_probe_snapshot,
        description="Capture the one authorized compatibility-probe snapshot.",
        emoji="📷",
    )
