"""从 Git 管理的 data/bot_texts.json 读取中英文文案，修改后重启生效。"""

from __future__ import annotations

import json
import string
from contextvars import ContextVar
from pathlib import Path

from .command_meta import command_example_name
from .privacy import redact_hidden_identifiers

TEXTS_PATH = Path(__file__).resolve().parents[3] / "data" / "bot_texts.json"
MESSAGES: dict[str, dict[str, str]] = json.loads(TEXTS_PATH.read_text(encoding="utf-8"))

_COMMAND_PARAM_IDS = {
    "sniper_add": "sniper.add",
    "sniper_delete": "sniper.delete",
    "blacklist_list": "blacklist.list",
    "blacklist_add": "blacklist.add",
    "blacklist_delete": "blacklist.delete",
    "tracking_open": "tracking.open",
    "tracking_open_riven": "tracking.open.riven",
    "tracker_add": "tracker.manage",
    "tracker_list": "tracker.manage.list",
    "tracker_delete": "tracker.manage.delete",
    "channel_dedupe": "channel.dedupe",
    "bargain_add": "bargain.add",
    "bargain_delete": "bargain.delete",
    "bargain_riven_add": "bargain.riven",
    "bargain_riven_list": "bargain.riven.list",
    "bargain_riven_delete": "bargain.riven.delete",
}

_locale: ContextVar[str] = ContextVar("riven_sniper_locale", default="zh")


def current_locale() -> str:
    return _locale.get()


def set_current_locale(locale: str) -> None:
    _locale.set("en" if locale == "en" else "zh")


def render(key: str, *, locale: str | None = None, **params) -> str:
    """渲染目标语言文案；示例采用实际注册的最短英文命令。"""
    selected = "en" if (locale or current_locale()) == "en" else "zh"
    text = MESSAGES[key][selected]
    fields = {field for _, field, _, _ in string.Formatter().parse(text)
              if field is not None}
    command_params = {
        name: command_example_name(command_id)
        for name, command_id in _COMMAND_PARAM_IDS.items()
        if name in fields
    }
    if any(value is None for value in command_params.values()):
        return render("通用.缺少英文简写", locale=selected)
    return redact_hidden_identifiers(text.format(**{**params, **command_params}))
