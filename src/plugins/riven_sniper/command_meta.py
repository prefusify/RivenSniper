"""命令元数据（无 NoneBot 依赖，供命令层、存储层与 WebUI 共用）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandNode:
    """一个由固定内部映射名识别、触发名可配置的独立命令。"""

    id: str
    default_name: str
    description: str
    default_aliases: tuple[str, ...] = ()


COMMAND_NODES = (
    CommandNode("sniper.add", "狙击添加", "添加狙击配置", ("s",)),
    CommandNode("sniper.list", "狙击列表", "查看当前目标配置", ("sl",)),
    CommandNode("sniper.delete", "狙击删除", "删除配置", ("sd",)),
    CommandNode("blacklist.add", "黑名单添加", "加入 WM 或频道卖家黑名单", ("b",)),
    CommandNode("blacklist.delete", "黑名单删除", "移出 WM 或频道卖家黑名单", ("bd",)),
    CommandNode("blacklist.list", "黑名单", "查看 WM 或频道卖家黑名单", ("bl",)),
    CommandNode("bargain.add", "捡漏添加", "监控道具低价挂单", ("d",)),
    CommandNode("bargain.list", "捡漏列表", "查看当前目标捡漏监控", ("dl",)),
    CommandNode("bargain.delete", "捡漏删除", "移出捡漏监控", ("dd",)),
    CommandNode("bargain.riven", "捡漏紫卡", "添加紫卡捡漏监控", ("rd",)),
    CommandNode("bargain.riven.list", "捡漏紫卡列表", "查看紫卡捡漏清单", ("rdl",)),
    CommandNode("bargain.riven.delete", "捡漏紫卡删除", "删除紫卡捡漏项", ("rdd",)),
    CommandNode("tracking.open", "开盒", "按玩家查询频道紫卡历史", ("w",)),
    CommandNode(
        "tracking.open.riven", "开盒紫卡", "按紫卡编号查询",
        default_aliases=("rh", "riven"),
    ),
    CommandNode(
        "tracker.manage", "上线提醒", "添加玩家频道上线提醒",
        default_aliases=("t", "Trackers"),
    ),
    CommandNode("tracker.manage.list", "上线提醒列表", "查看上线提醒", ("tl",)),
    CommandNode("tracker.manage.delete", "上线提醒删除", "删除上线提醒", ("td",)),
    CommandNode("channel.dedupe", "去重", "查看或设置当前目标的频道去重时间", ("cd",)),
    CommandNode("attribute.list", "词条列表", "词条最简写", ("st",)),
)

COMMAND_BY_ID = {node.id: node for node in COMMAND_NODES}
TOP_LEVEL_COMMANDS = COMMAND_NODES

# 20260808 -> 20260809 的旧命令模型迁移只认识当时存在的顶级标准名。
_LEGACY_TOP_LEVEL_IDS = frozenset({
    "sniper.add", "sniper.list", "sniper.delete",
    "blacklist.add", "blacklist.delete", "blacklist.list",
    "bargain.add", "bargain.list", "bargain.delete", "bargain.riven",
    "tracking.open", "tracker.manage", "channel.dedupe", "attribute.list",
})
LEGACY_COMMAND_ID_BY_NAME = {
    node.default_name: node.id
    for node in COMMAND_NODES
    if node.id in _LEGACY_TOP_LEVEL_IDS
}

# 这些入口曾经只在父命令参数中解析；升级为独立命令后用于拒绝旧入口。
LEGACY_SUBCOMMANDS_BY_PARENT: dict[str, tuple[str, ...]] = {
    "bargain.riven": ("bargain.riven.list", "bargain.riven.delete"),
    "tracking.open": ("tracking.open.riven",),
    "tracker.manage": ("tracker.manage.list", "tracker.manage.delete"),
}
LEGACY_SUBCOMMAND_TOKENS: dict[str, tuple[str, ...]] = {
    "bargain.riven.list": ("列表", "list"),
    "bargain.riven.delete": ("删除", "delete", "remove"),
    "tracking.open.riven": ("紫卡", "riven"),
    "tracker.manage.list": ("列表", "list"),
    "tracker.manage.delete": ("删除", "delete", "remove"),
}

# 这些历史触发词不得重新注册；固定内部映射名也永远不能成为触发名或别名。
RETIRED_BOT_COMMANDS = frozenset({
    "狙击开关", "命令大全", "狙击帮助", "捡漏开关", "捡漏帮助",
    "语言", "狙击复制", "黑名单 添加", "黑名单 删除",
})
INTERNAL_MAPPING_NAMES = frozenset(COMMAND_BY_ID)

# 运行中实际注册的触发名和别名。控制台据此判断保存内容是否等待重启生效。
ACTIVE_COMMAND_NAMES: dict[str, str] = {
    node.id: node.default_name for node in COMMAND_NODES
}
ACTIVE_ALIASES: dict[str, list[str]] = {
    node.id: list(node.default_aliases) for node in COMMAND_NODES
}


def active_command_name(command_id: str) -> str:
    """返回本进程实际注册的触发名；未注册时使用默认值。"""
    return ACTIVE_COMMAND_NAMES.get(
        command_id, COMMAND_BY_ID[command_id].default_name,
    )


def command_example_name(command_id: str) -> str | None:
    """示例只使用实际注册的最短英文入口；没有时不编造可执行命令。"""
    candidates = [active_command_name(command_id),
                  *ACTIVE_ALIASES.get(command_id, ())]
    english = [name for name in candidates
               if name.isascii() and name.isalpha()]
    return min(english, key=lambda name: (len(name), name.casefold())) \
        if english else None


def command_token_conflict(
    command_id: str,
    token: str,
    names_by_command: dict[str, str],
    aliases_by_command: dict[str, list[str]],
    *,
    skip_name: bool = False,
    skip_alias: str | None = None,
) -> bool:
    """判断触发名或别名是否与任一命令入口或保留名冲突。"""
    folded = token.casefold()
    if folded in {name.casefold() for name in RETIRED_BOT_COMMANDS}:
        return True
    if folded in {name.casefold() for name in INTERNAL_MAPPING_NAMES}:
        return True
    for other_id, name in names_by_command.items():
        if skip_name and other_id == command_id:
            continue
        if name.casefold() == folded:
            return True
    skipped = skip_alias.casefold() if skip_alias is not None else None
    for other_id, aliases in aliases_by_command.items():
        for alias in aliases:
            alias_folded = alias.casefold()
            if other_id == command_id and alias_folded == skipped:
                continue
            if alias_folded == folded:
                return True
    return False


def seed_base_aliases(store) -> None:
    """一次性种入默认别名，之后全部作为普通别名由控制台维护。"""
    if store.kv_get("command_aliases_v2_seeded"):
        return
    legacy_seeded = bool(store.kv_get("base_aliases_seeded"))
    for node in COMMAND_NODES:
        if legacy_seeded and node.id in _LEGACY_TOP_LEVEL_IDS:
            continue
        for alias in node.default_aliases:
            store.add_command_alias(alias, node.id)
    store.kv_set("command_aliases_v2_seeded", "1")
