"""Discord 官方 Bot：仅私聊、授权与共享命令核心。"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 先在 NoneBot 初始化前载入包，避免插件 __init__ 在测试收集阶段挂载真实运行时；
# 命令别名使用独立内存库，不读取或修改工作区 sniper.db。
from src.plugins.riven_sniper import shared  # noqa: E402
from src.plugins.riven_sniper.command_meta import seed_base_aliases  # noqa: E402
from src.plugins.riven_sniper.store import Store  # noqa: E402

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

_bootstrap_store = Store(":memory:")
seed_base_aliases(_bootstrap_store)
shared._store = _bootstrap_store

from nonebot.adapters.discord.event import (  # noqa: E402
    DirectMessageCreateEvent,
    GuildMessageCreateEvent,
)

from src.plugins.riven_sniper import chat_tracking, commands, discord_bot  # noqa: E402
from src.plugins.riven_sniper.chat_tracking import TrackingStore  # noqa: E402
from src.plugins.riven_sniper.commands import (  # noqa: E402
    ScopedCommandEvent,
    TrackingCommandResult,
    execute_scoped_command,
    parse_scoped_command,
)
from src.plugins.riven_sniper.criteria import ANY_ATTRIBUTE  # noqa: E402
from tests.test_tracking_view import _seed_distinct_rivens  # noqa: E402


def _config():
    return SimpleNamespace(
        discord_dm_enabled=True,
        sniper_max_configs_per_group=0,
        bargain_max_distinct_slugs=0,
    )


def _observe_player(
    store: TrackingStore, account_id: str, nick: str, *,
    platform: str, observed_at: int,
) -> None:
    store.ingest({
        "t": observed_at,
        "nick": nick,
        "platform": platform,
        "sender_id": account_id,
        "chan": "#T_ZH",
        "text": "",
    }, dedupe_seconds=0, historical=True)


def test_private_command_parser_uses_existing_names_and_optional_slash():
    assert parse_scoped_command("狙击添加 托里德 暴击 多重") == (
        "sniper.add", "托里德 暴击 多重")
    assert parse_scoped_command("/捡漏列表") == ("bargain.list", "")
    assert parse_scoped_command("riven #3") == (
        "tracking.open.riven", "#3")
    assert parse_scoped_command("这不是命令") is None


def test_every_existing_command_name_is_available_in_private_messages():
    from src.plugins.riven_sniper.command_meta import TOP_LEVEL_COMMANDS

    for node in TOP_LEVEL_COMMANDS:
        assert parse_scoped_command(node.default_name) == (node.id, "")
        assert parse_scoped_command(node.id) is None


def test_private_parser_uses_edited_name_and_alias(monkeypatch):
    monkeypatch.setitem(commands._DB_COMMAND_NAMES, "sniper.add", "监听添加")
    monkeypatch.setitem(commands._DB_ALIASES, "sniper.add", {"加监听"})

    assert parse_scoped_command("监听添加 托里德 暴击 多重") == (
        "sniper.add", "托里德 暴击 多重")
    assert parse_scoped_command("加监听 托里德 暴击 多重") == (
        "sniper.add", "托里德 暴击 多重")
    assert parse_scoped_command("狙击添加 托里德 暴击 多重") is None
    assert parse_scoped_command("sniper.add 托里德") is None

    # 即使自定义入口是内部映射名的前缀，内部映射名本身仍不能触发。
    monkeypatch.setitem(commands._DB_COMMAND_NAMES, "sniper.add", "sniper")
    assert parse_scoped_command("sniper 托里德") == (
        "sniper.add", "托里德")
    assert parse_scoped_command("sniper.add 托里德") is None


def test_attribute_list_includes_any_without_internal_identifier():
    text = commands._attr_list_text()
    any_line = next(line for line in text.splitlines() if line.startswith("任意 - "))

    assert any_line == "任意 - ANY - 任意"
    assert ANY_ATTRIBUTE not in text


def test_independent_commands_parse_without_restoring_subcommand_entries():
    from src.plugins.riven_sniper.command_meta import COMMAND_BY_ID

    for command_id in (
        "blacklist.list.add",
        "blacklist.list.delete",
        "bargain.riven.help",
        "bargain.riven.enable",
        "bargain.riven.disable",
        "bargain.riven.toggle",
    ):
        assert command_id not in COMMAND_BY_ID
    assert parse_scoped_command("捡漏紫卡列表") == (
        "bargain.riven.list", "")
    assert parse_scoped_command("捡漏紫卡删除3") == (
        "bargain.riven.delete", "3")
    assert parse_scoped_command("开盒紫卡 #3") == (
        "tracking.open.riven", "#3")
    assert parse_scoped_command("上线提醒列表") == (
        "tracker.manage.list", "")
    assert parse_scoped_command("上线提醒删除3") == (
        "tracker.manage.delete", "3")
    assert parse_scoped_command("捡漏紫卡 列表") == (
        "bargain.riven", "列表")
    assert parse_scoped_command("开盒 紫卡 #3") == (
        "tracking.open", "紫卡 #3")
    assert parse_scoped_command("上线提醒 列表") == (
        "tracker.manage", "列表")
    assert parse_scoped_command("黑名单 添加 WM Seller") == (
        "blacklist.list", "添加 WM Seller",
    )


async def test_riven_bargain_actions_use_independent_commands(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", _config)
    target = store.upsert_discord_target("1013")
    event = ScopedCommandEvent(
        target["scope_id"], 1013, "discord", "1013",
    )

    usage = await execute_scoped_command("bargain.riven", "", event)
    removed_help = await execute_scoped_command(
        "bargain.riven", "帮助", event,
    )
    added = await execute_scoped_command(
        "bargain.riven", "Torid", event,
    )
    retired_listing = await execute_scoped_command(
        "bargain.riven", "列表", event,
    )
    retired_delete = await execute_scoped_command(
        "bargain.riven", "删除 1", event,
    )
    listing = await execute_scoped_command(
        "bargain.riven.list", "", event,
    )

    assert "rd Torid 30" in usage
    assert "未找到武器“帮助”" in removed_help
    assert "紫卡监控已添加" in added
    assert "rd Torid 30" in retired_listing
    assert "rd Torid 30" in retired_delete
    assert "托里德" in listing
    assert len(store.list_bargain_riven_items(target["scope_id"])) == 1
    assert "已移除" in await execute_scoped_command(
        "bargain.riven.delete", "1", event)
    assert store.list_bargain_riven_items(target["scope_id"]) == []
    store.close()


async def test_independent_command_aliases_are_top_level(
        monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    monkeypatch.setitem(
        commands._DB_ALIASES, "tracker.manage.list", {"show"},
    )
    target = store.upsert_discord_target("1012")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1012, "discord", "1012",
    )

    assert parse_scoped_command("show") == ("tracker.manage.list", "")
    assert parse_scoped_command("上线提醒 show") == (
        "tracker.manage", "show")
    reply = await execute_scoped_command("tracker.manage.list", "", event)

    assert reply == "暂无频道提醒"
    store.close()


async def test_retired_tracker_subcommands_have_no_effect(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", _config)
    target = store.upsert_discord_target("1016")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1016, "discord", "1016",
    )
    await execute_scoped_command("tracker.manage", "UnseenPlayer", event)
    tracker_id = store.list_player_trackers(target["scope_id"])[0]["id"]

    retired_list = await execute_scoped_command(
        "tracker.manage", "列表", event)
    retired_delete = await execute_scoped_command(
        "tracker.manage", f"删除 {tracker_id}", event)

    assert "t SomePlayer" in retired_list
    assert "t SomePlayer" in retired_delete
    assert len(store.list_player_trackers(target["scope_id"])) == 1
    assert "已删除" in await execute_scoped_command(
        "tracker.manage.delete", str(tracker_id), event)
    assert store.list_player_trackers(target["scope_id"]) == []
    store.close()


def test_discord_text_split_respects_limit():
    chunks = discord_bot.split_discord_text("第一行\n" + "x" * 4100, 1900)
    assert "".join(chunks).replace("\n", "") == "第一行" + "x" * 4100
    assert all(0 < len(chunk) <= 1900 for chunk in chunks)


def test_discord_response_uses_private_scope_wording():
    adapted = discord_bot.adapt_discord_text(
        "RivenSniper-QQ\n本群配置（目标所有者）")
    assert "RivenSniper-QQ" not in adapted
    assert "当前私聊配置" in adapted
    assert "当前用户" in adapted


async def test_tracking_command_pages_and_discord_full_export(monkeypatch, tmp_path):
    store = Store(":memory:")
    config = _config()
    config.irc_track_db_path = str(tmp_path / "tracking.db")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1011")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1011, "discord", "1011")
    with TrackingStore(config.irc_track_db_path) as tracking:
        _seed_distinct_rivens(tracking, 200)

    first = await execute_scoped_command(
        "tracking.open", "Seller", event)
    last = await execute_scoped_command(
        "tracking.open", "Seller 页 8", event)
    exported = await execute_scoped_command(
        "tracking.open", "Seller 全部", event, rich=True)
    riven = await execute_scoped_command(
        "tracking.open.riven", "#1", event)
    retired_riven = await execute_scoped_command(
        "tracking.open", "紫卡 #1", event)
    monkeypatch.setattr(chat_tracking, "TRACKING_EXPORT_LIMIT", 100)
    rejected = await execute_scoped_command(
        "tracking.open", "Seller 全部", event, rich=True)

    assert sum(line.startswith("#") for line in first.splitlines()) == 25
    assert sum(line.startswith("#") for line in last.splitlines()) == 25
    assert "次观测" not in first + last
    assert isinstance(exported, TrackingCommandResult)
    assert "#1" in riven and "观测到的持有者变化" in riven
    assert "w SomePlayer" in retired_riven
    assert exported.export_text is not None
    assert sum(line.startswith("#")
               for line in exported.export_text.splitlines()) == 200
    assert len(exported.export_pages) == 8
    assert isinstance(rejected, TrackingCommandResult)
    assert rejected.export_text is None
    assert "超过单次导出上限 100 条" in rejected.text
    attachment = discord_bot.build_tracking_attachment(exported)
    assert (attachment[1].data["attachment"].filename
            == "rivensniper-player-rivens.txt")
    assert attachment[1].data["file"].content.startswith(b"\xef\xbb\xbf")
    nodes = commands.build_tracking_forward_nodes("123", exported.export_pages)
    assert len(nodes) == 8
    assert all(node["type"] == "node" for node in nodes)
    assert all(node["data"]["uin"] == "123" for node in nodes)

    class FakeBot:
        self_id = "123"

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **params):
            self.calls.append((action, params))

    fake = FakeBot()
    await commands._send_tracking_export_qq(
        fake, SimpleNamespace(group_id=1059455391), exported)
    assert [call[0] for call in fake.calls] == ["send_group_forward_msg"]
    assert len(fake.calls[0][1]["messages"]) == 8
    store.close()


def test_rule_accepts_direct_messages_only(monkeypatch):
    monkeypatch.setattr(discord_bot, "get_config", _config)
    direct = DirectMessageCreateEvent.model_construct()
    guild = GuildMessageCreateEvent.model_construct()
    assert discord_bot._is_direct_message(direct) is True
    assert discord_bot._is_direct_message(guild) is False


async def test_qq_owner_rule_requires_active_target_and_exact_owner(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    store.upsert_qq_target(123456, 42, enabled=True)

    assert await commands._owned_qq_target(
        SimpleNamespace(group_id=123456, user_id=42)) is True
    assert await commands._owned_qq_target(
        SimpleNamespace(group_id=123456, user_id=43)) is False
    assert await commands._owned_qq_target(
        SimpleNamespace(group_id=654321, user_id=42)) is False
    assert await commands._owned_qq_target(SimpleNamespace(
        group_id=123456,
        user_id=42,
        get_plaintext=lambda: "sniper.add 托里德",
    )) is False

    store.set_target_enabled(123456, False)
    assert await commands._owned_qq_target(
        SimpleNamespace(group_id=123456, user_id=42)) is False
    store.close()


async def test_channel_switch_silences_only_channel_commands(monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1008")
    event = ScopedCommandEvent(
        target["scope_id"], 1008, "discord", "1008")

    assert await execute_scoped_command("tracking.open", "Player", event) is None
    assert await execute_scoped_command(
        "tracking.open.riven", "1", event) is None
    assert await execute_scoped_command("tracker.manage", "Player", event) is None
    assert await execute_scoped_command(
        "tracker.manage.list", "", event) is None
    assert await execute_scoped_command(
        "tracker.manage.delete", "1", event) is None
    assert await execute_scoped_command(
        "blacklist.add", "频道 ChannelSeller", event) is None
    assert await execute_scoped_command(
        "blacklist.add", "频道\nFirstSeller\nSecondSeller", event) is None
    assert await execute_scoped_command("blacklist.list", "频道", event) is None
    assert store.list_blacklist(
        target["scope_id"], scope="channel") == []
    assert store.command_usage_7d() == {}

    store.add_blacklist(target["scope_id"], "SharedSeller", scope="wm")
    store.add_blacklist(target["scope_id"], "SharedSeller", scope="channel")
    store.add_blacklist(target["scope_id"], "ChannelOnly", scope="channel")
    assert await execute_scoped_command(
        "blacklist.delete", "频道 SharedSeller", event) is None
    assert store.is_blacklisted(
        target["scope_id"], "SharedSeller", scope="channel")

    default_list = await execute_scoped_command(
        "blacklist.list", "", event)
    assert "卖家黑名单 · WM" in default_list
    assert "SharedSeller" in default_list
    assert "频道" not in default_list
    assert "ChannelOnly" not in default_list

    default_delete = await execute_scoped_command(
        "blacklist.delete", "SharedSeller", event)
    assert default_delete.startswith("[WM]")
    assert "[频道]" not in default_delete
    assert not store.is_blacklisted(
        target["scope_id"], "SharedSeller", scope="wm")
    assert store.is_blacklisted(
        target["scope_id"], "SharedSeller", scope="channel")

    wm_reply = await execute_scoped_command(
        "blacklist.add", "WMSeller", event)
    assert wm_reply.startswith("[WM]")
    assert "WMSeller" in await execute_scoped_command(
        "blacklist.list", "WM", event)
    wm_delete = await execute_scoped_command(
        "blacklist.delete", "WM WMSeller", event)
    assert wm_delete.startswith("[WM]")
    assert not store.is_blacklisted(target["scope_id"], "WMSeller")
    assert store.list_blacklist(target["scope_id"]) == []
    assert store.list_blacklist(
        target["scope_id"], scope="channel") == [
            "SharedSeller", "ChannelOnly"]
    assert "暂无狙击配置" in await execute_scoped_command(
        "sniper.list", "", event)

    wrong_user = ScopedCommandEvent(
        target["scope_id"], 9999, "discord", "9999")
    assert await execute_scoped_command(
        "sniper.list", "", wrong_user) is None
    store.set_target_enabled(target["scope_id"], False)
    assert await execute_scoped_command(
        "sniper.list", "", event) is None
    store.close()


async def test_dedupe_command_is_target_local_and_available_when_channel_off(
        monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", _config)
    alice = store.upsert_discord_target("1014")
    bob = store.upsert_discord_target("1015")
    alice_event = ScopedCommandEvent(
        alice["scope_id"], 1014, "discord", "1014")
    bob_event = ScopedCommandEvent(
        bob["scope_id"], 1015, "discord", "1015")

    current = await execute_scoped_command("channel.dedupe", "", alice_event)
    assert "频道紫卡去重间隔：1 小时" in current
    assert "距上次被观察到至少 1 小时" in current
    updated = await execute_scoped_command("channel.dedupe", "72", alice_event)
    assert "频道紫卡去重间隔已设为 72 小时" in updated
    assert "距上次被观察到至少 72 小时" in updated
    assert store.get_target_preferences(
        alice["scope_id"])["channel_dedupe_hours"] == 72
    assert store.get_target_preferences(
        bob["scope_id"])["channel_dedupe_hours"] == 1
    for invalid in ("0", "73", "1.5", "1小时"):
        assert "只接受 1～72 的整数" in await execute_scoped_command(
            "channel.dedupe", invalid, alice_event)
    assert store.get_target_preferences(
        alice["scope_id"])["channel_dedupe_hours"] == 72
    store.close()


async def test_users_have_isolated_command_configuration(monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)

    alice = store.upsert_discord_target("1001")
    bob = store.upsert_discord_target("1002")
    alice_event = ScopedCommandEvent(
        alice["scope_id"], 1001, "discord", "1001")
    bob_event = ScopedCommandEvent(
        bob["scope_id"], 1002, "discord", "1002")

    reply = await execute_scoped_command(
        "sniper.add", "托里德 暴击 多重", alice_event)
    assert "s Torid cc ms" in reply
    assert len(store.list_configs(alice["scope_id"])) == 1
    assert store.list_configs(bob["scope_id"]) == []
    assert "暂无狙击配置" in await execute_scoped_command(
        "sniper.list", "", bob_event)
    store.close()


async def test_target_blacklist_commands_keep_wm_and_channel_independent(
        monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1009")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1009, "discord", "1009")

    wm_add = await execute_scoped_command(
        "blacklist.add", "WM SameSeller", event)
    explicit_wm_add = await execute_scoped_command(
        "blacklist.add", "WM OnlyWmSeller", event)
    channel_add = await execute_scoped_command(
        "blacklist.add", "频道 SameSeller", event)
    default_add = await execute_scoped_command(
        "blacklist.add", "LegacySeller", event)
    spaced_add = await execute_scoped_command(
        "blacklist.add", "频道 Example\u00a0o", event)

    assert wm_add.startswith("[WM]")
    assert explicit_wm_add.startswith("[WM]")
    assert "\n" not in explicit_wm_add
    assert channel_add.startswith("[频道]")
    assert default_add.startswith("[WM]")
    assert "\n[频道]" in default_add
    assert spaced_add.startswith("[频道]")
    assert store.list_blacklist(target["scope_id"], scope="wm") == [
        "SameSeller", "OnlyWmSeller", "LegacySeller"]
    assert store.list_blacklist(target["scope_id"], scope="channel") == [
        "SameSeller", "LegacySeller", "Example o"]
    assert not store.is_blacklisted(
        target["scope_id"], "OnlyWmSeller", scope="channel")
    assert "LegacySeller" in await execute_scoped_command(
        "blacklist.list", "WM", event)
    assert "LegacySeller" in await execute_scoped_command(
        "blacklist.list", "频道", event)
    default_list = await execute_scoped_command(
        "blacklist.list", "", event)
    assert "卖家黑名单 · WM" in default_list
    assert "OnlyWmSeller" in default_list
    assert "卖家黑名单 · 频道" in default_list
    assert "Example o" in default_list

    nested_add = await execute_scoped_command(
        "blacklist.list", "添加 WM NestedSeller", event)
    nested_delete = await execute_scoped_command(
        "blacklist.list", "删除 WM LegacySeller", event)
    assert "bl WM" in nested_add
    assert "bl WM" in nested_delete
    assert not store.is_blacklisted(
        target["scope_id"], "NestedSeller", scope="wm")
    assert store.is_blacklisted(
        target["scope_id"], "LegacySeller", scope="wm")

    removed = await execute_scoped_command(
        "blacklist.delete", "频道 SameSeller", event)
    assert removed.startswith("[频道]")
    assert store.is_blacklisted(
        target["scope_id"], "SameSeller", scope="wm")
    assert not store.is_blacklisted(
        target["scope_id"], "SameSeller", scope="channel")

    await execute_scoped_command(
        "blacklist.delete", "频道 Example o", event)
    assert not store.is_blacklisted(
        target["scope_id"], "Example\u00a0o", scope="channel")

    removed_both = await execute_scoped_command(
        "blacklist.delete", "LegacySeller", event)
    assert removed_both.startswith("[WM]")
    assert "\n[频道]" in removed_both
    assert not store.is_blacklisted(
        target["scope_id"], "LegacySeller", scope="wm")
    assert not store.is_blacklisted(
        target["scope_id"], "LegacySeller", scope="channel")
    store.close()


async def test_blacklist_add_command_accepts_multiline_and_limits_each_name(
        monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1010")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1010, "discord", "1010")

    reply = await execute_scoped_command(
        "blacklist.add", "WM\nAlpha\nExample\u00a0o\nAlpha", event)
    assert reply == "[WM] 批量添加完成：新增 2 个，已存在 1 个"
    assert store.list_blacklist(target["scope_id"], scope="wm") == [
        "Alpha", "Example o"]

    reply = await execute_scoped_command(
        "blacklist.add", f"频道\nValidSeller\n{'x' * 33}", event)
    assert "第 2 行卖家名超过 32 个字符" in reply
    assert not store.is_blacklisted(
        target["scope_id"], "ValidSeller", scope="channel")

    reply = await execute_scoped_command(
        "blacklist.add", "y" * 32, event)
    assert reply.startswith("[WM]") and "\n[频道]" in reply
    assert store.is_blacklisted(target["scope_id"], "y" * 32, scope="wm")
    assert store.is_blacklisted(
        target["scope_id"], "y" * 32, scope="channel")
    store.close()


async def test_player_commands_accept_names_without_exposing_internal_ids(
        monkeypatch, tmp_path):
    store = Store(":memory:")
    config = _config()
    config.irc_track_db_path = str(tmp_path / "tracking.db")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1010")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(
        target["scope_id"], 1010, "discord", "1010")

    pending_reply = await execute_scoped_command(
        "tracker.manage", "UnseenPlayer", event)
    tracker_list = await execute_scoped_command(
        "tracker.manage.list", "", event)

    assert "UnseenPlayer" in pending_reply
    assert "首次" in pending_reply
    assert "UnseenPlayer" in tracker_list
    assert "pending-nick:" not in tracker_list
    assert store.list_player_trackers(target["scope_id"])[0]["resolved"] is False

    account_id = "0123456789abcdef01234567"
    other_id = "89abcdef0123456701234567"
    with TrackingStore(config.irc_track_db_path) as tracking:
        _observe_player(tracking,
            account_id, "KnownPlayer", platform="windows", observed_at=100)
        _observe_player(tracking,
            account_id, "SharedName", platform="windows", observed_at=101)
        _observe_player(tracking,
            other_id, "SharedName", platform="windows", observed_at=102)

    player_reply = await execute_scoped_command(
        "tracking.open", "KnownPlayer", event)
    ambiguous_reply = await execute_scoped_command(
        "tracking.open", "SharedName", event)
    blocked_account = await execute_scoped_command(
        "tracker.manage", account_id, event)
    blocked_fingerprint = await execute_scoped_command(
        "tracker.manage", "a" * 64, event)
    blocked_blacklist = await execute_scoped_command(
        "blacklist.add", f"频道 {account_id}", event)
    embedded_account = await execute_scoped_command(
        "tracker.manage", f"Alias {account_id}", event)
    embedded_fingerprint = await execute_scoped_command(
        "tracker.manage", f"Alias {'a' * 64}", event)
    embedded_blacklist = await execute_scoped_command(
        "blacklist.add", f"频道 Alias {account_id}", event)

    assert "KnownPlayer" in player_reply
    assert account_id not in player_reply
    assert account_id not in ambiguous_reply
    assert other_id not in ambiguous_reply
    assert account_id not in blocked_account
    assert "a" * 64 not in blocked_fingerprint
    assert account_id not in blocked_blacklist
    assert account_id not in embedded_account
    assert "a" * 64 not in embedded_fingerprint
    assert account_id not in embedded_blacklist
    assert store.list_blacklist(target["scope_id"], scope="channel") == []
    assert len(store.list_player_trackers(target["scope_id"])) == 1

    store._conn.execute(
        """INSERT INTO channel_blacklist
           (group_id,seller,created_at) VALUES (?,?,?)""",
        (target["scope_id"], f"Legacy {account_id}", 1),
    )
    store._conn.execute(
        """INSERT INTO player_trackers
           (scope_id,account_id,target_nick,enabled,created_at)
           VALUES (?,?,?,1,?)""",
        (target["scope_id"], "pending-nick:legacy",
         f"Legacy {account_id}", 1),
    )
    store._conn.commit()
    blacklist_list = await execute_scoped_command(
        "blacklist.list", "频道", event)
    tracker_list = await execute_scoped_command(
        "tracker.manage.list", "", event)
    assert account_id not in blacklist_list
    assert account_id not in tracker_list
    store.close()


def test_console_only_commands_are_not_parsed_in_discord_dm():
    assert parse_scoped_command("语言 English") is None
    assert parse_scoped_command("狙击复制 1001") is None


async def test_english_target_uses_english_parser_states_and_actions(monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", lambda: config)
    target = store.upsert_discord_target("1005")
    store.set_target_locale(target["scope_id"], "en")
    event = ScopedCommandEvent(target["scope_id"], 1005, "discord", "1005")

    parse_error = await execute_scoped_command(
        "sniper.add", "not-a-weapon Critical Chance Multishot",
        event)
    bargain_list = await execute_scoped_command(
        "bargain.list", "", event)
    store.add_config(
        target["scope_id"], weapon="torid", wildcard=None,
        positives=[["critical_chance"], ["multishot"]],
    )
    deleted = await execute_scoped_command("sniper.delete", "1", event)

    assert "Unknown weapon or type" in parse_error
    assert "不认识" not in parse_error
    assert "No item bargain watches yet." in bargain_list
    assert "deleted" in deleted
    store.close()


async def test_all_english_command_help_and_stat_lists(monkeypatch):
    from src.plugins.riven_sniper import command_meta, rivendata

    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", _config)
    target = store.upsert_discord_target("1020")
    store.set_target_locale(target["scope_id"], "en")
    store.set_target_channel_enabled(target["scope_id"], True)
    event = ScopedCommandEvent(target["scope_id"], 1020, "discord", "1020")
    for node in command_meta.COMMAND_NODES:
        shortcut = command_meta.command_example_name(node.id)
        assert parse_scoped_command(shortcut) == (node.id, "")
        reply = await execute_scoped_command(node.id, "", event)
        content = reply.text if isinstance(reply, TrackingCommandResult) else reply
        assert content and not re.search(r"[\u3400-\u9fff]", content), (node.id, content)

    en = await execute_scoped_command("attribute.list", "", event)
    store.set_target_locale(target["scope_id"], "zh")
    zh = await execute_scoped_command("attribute.list", "", event)
    en_lines, zh_lines = en.splitlines()[1:], zh.splitlines()[1:]
    assert len(en_lines) == len(zh_lines) == len(rivendata.attribute_catalog())
    assert "Additional Combo Count Chance - ACCC" in en_lines
    assert "额外连击数获取 - ACCC - 额外连击" in zh_lines
    assert "Any - ANY" in en_lines and "任意 - ANY - 任意" in zh_lines
    english_shortcuts = {}
    for line in en_lines:
        name, english = line.split(" - ")
        assert english.isascii() and english == english.upper()
        assert not any(c.isspace() for c in english)
        slug = rivendata.resolve_attribute(english)
        assert slug and name == rivendata.attribute_catalog()[slug]["name_en"]
        english_shortcuts[slug] = english
    resolved = set()
    for line in zh_lines:
        name, english, chinese = line.split(" - ")
        assert english.isascii() and english == english.upper()
        slug = rivendata.resolve_attribute(english)
        assert slug and rivendata.resolve_attribute(chinese) == slug
        assert name == rivendata.attribute_catalog()[slug]["name_zh"]
        assert english == english_shortcuts[slug]
        resolved.add(slug)
    assert resolved == set(english_shortcuts) == set(rivendata.attribute_catalog())
    store.close()


async def test_english_replies_preserve_raw_nicknames_and_export_pages(monkeypatch):
    from src.plugins.riven_sniper import texts

    store = Store(":memory:")
    monkeypatch.setattr(commands, "get_store", lambda: store)
    monkeypatch.setattr(commands, "get_config", _config)
    target = store.upsert_discord_target("1021")
    store.set_target_locale(target["scope_id"], "en")
    event = ScopedCommandEvent(target["scope_id"], 1021, "discord", "1021")
    reply = await execute_scoped_command("blacklist.add", "WM 中文玩家", event)
    assert "中文玩家" in reply
    assert not re.search(r"[\u3400-\u9fff]", reply.replace("中文玩家", ""))

    calls = []

    async def call_api(action, **params):
        calls.append((action, params))

    token = texts._locale.set("en")
    try:
        await commands._send_tracking_export_qq(
            SimpleNamespace(self_id="123", call_api=call_api),
            SimpleNamespace(group_id=111),
            TrackingCommandResult("Ready", export_pages=("Original 中文玩家",)),
        )
    finally:
        texts._locale.reset(token)
    params = calls[0][1]
    assert params["messages"][0]["data"]["content"] == "Original 中文玩家"
    for key in ("source", "summary", "prompt"):
        assert not re.search(r"[\u3400-\u9fff]", params[key])
    store.close()


def test_removed_access_commands_are_not_parsed():
    assert parse_scoped_command("白名单添加 1003 永久") is None
    assert parse_scoped_command("白名单删除 1003") is None


class _FakeDiscordBot:
    def __init__(self):
        self.messages = []

    async def send_to(self, channel_id, message):
        self.messages.append((channel_id, str(message)))


async def test_unregistered_dm_is_silently_ignored(monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(discord_bot, "get_store", lambda: store)
    monkeypatch.setattr(discord_bot, "get_config", lambda: config)
    event = DirectMessageCreateEvent.model_construct(
        channel_id=88,
        author=SimpleNamespace(id=1004, bot=False),
        content="狙击列表",
    )
    bot = _FakeDiscordBot()

    await discord_bot._handle_discord_dm(bot, event)

    assert bot.messages == []
    assert store.get_target_by_external("discord", "1004") is None
    store.close()


async def test_dm_boundary_replies_follow_saved_target_language(monkeypatch):
    store = Store(":memory:")
    config = _config()
    monkeypatch.setattr(discord_bot, "get_store", lambda: store)
    monkeypatch.setattr(discord_bot, "get_config", lambda: config)
    target = store.upsert_discord_target("1006")
    store.set_target_locale(target["scope_id"], "en")
    event = SimpleNamespace(
        channel_id=89,
        author=SimpleNamespace(id=1006, bot=False),
        get_plaintext=lambda: "not-a-command",
    )
    bot = _FakeDiscordBot()

    await discord_bot._handle_discord_dm(bot, event)

    assert bot.messages == [(89, "Unknown command: not-a-command")]

    store.set_target_enabled(target["scope_id"], False)
    bot.messages.clear()
    await discord_bot._handle_discord_dm(bot, event)

    assert bot.messages == []
    store.close()
