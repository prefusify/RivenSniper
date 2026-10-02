"""Git 中的 JSON 文案：双语渲染、占位符和英文命令示例。"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.plugins.riven_sniper import texts  # noqa: E402

SAMPLE_PARAMS = {
    "id": 3, "name": "SomePlayer", "action": "delete", "version": "1.0.0",
    "description": "Rule 3\ns Torid cc ms", "limit": 30,
    "error": "Unknown weapon: xx", "existing": "Rule 1", "line": 2,
    "added": 3, "query": "SomePlayer", "candidates": "A\nB", "number": 3,
    "page": 2, "pages": 3, "total": 6000, "channel": "Trade", "hours": 24,
    "config": "Rule 3\ns Torid cc ms -any",
    "price_line": "Starting 100p / Buyout 150p", "seller": "SomeSeller",
    "status": "in game", "weapon_en": "Torid", "riven": "visi-ata",
    "price": "150", "value": "120", "name_en": "Arcane Grace",
    "bucket": " R5", "baseline": "390", "discount": 62, "quantity": 1,
    "seller_status": "offline", "whisper_item": "Arcane Grace (rank 5)",
    "weapon": "Torid", "re_rolls": 3,
}


def test_all_json_templates_render_in_both_languages():
    for key, translations in texts.MESSAGES.items():
        assert "." in key
        assert set(translations) == {"zh", "en"}
        for locale, template in translations.items():
            assert isinstance(template, str) and template.strip()
            rendered = texts.render(key, locale=locale, **SAMPLE_PARAMS)
            assert rendered
            if locale == "en":
                assert not re.search(
                    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]",
                    rendered), (key, rendered)


def test_current_copy_is_preserved():
    assert texts.render("系统.测试推送", version="9.0.0") == (
        "✅ RivenSniper v9.0.0 这是一条测试推送。")
    assert texts.render("系统.测试推送", locale="en", version="9.0.0") == (
        "✅ RivenSniper v9.0.0 This is a test notification.")
    assert texts.render("狙击推送.标题", config="Rule 3") == "WARFRAME.MARKET · RIVEN\n"
    assert "/w SomeSeller hi wtb your" in texts.render(
        "狙击推送.私聊", **SAMPLE_PARAMS)
    assert "/w SomeSeller Hi! I want to buy your" in texts.render(
        "狙击推送.私聊", locale="en", **SAMPLE_PARAMS)


def test_english_reply_preserves_original_player_name():
    assert texts.render("黑名单添加.成功", locale="en", name="中文玩家") == (
        "中文玩家 was added to the seller blacklist.")


def test_examples_use_active_shortcuts_even_when_sample_params_are_supplied(monkeypatch):
    from src.plugins.riven_sniper.command_meta import ACTIVE_ALIASES, ACTIVE_COMMAND_NAMES

    monkeypatch.setitem(ACTIVE_COMMAND_NAMES, "sniper.delete", "删除配置")
    monkeypatch.setitem(ACTIVE_ALIASES, "sniper.delete", ["remove", "rm"])
    for locale in ("zh", "en"):
        rendered = texts.render(
            "通用.编号用法", locale=locale, sniper_delete="狙击删除")
        assert rendered.splitlines()[-1] == "rm 3"
    monkeypatch.setitem(ACTIVE_ALIASES, "sniper.delete", ["删除"])
    rendered = texts.render("通用.编号用法", locale="en")
    assert "No English shortcut" in rendered
    assert "sd 3" not in rendered
