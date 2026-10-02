"""中文命令层。

狙击添加 <武器|类型> <正词条位置[@最低评级]...> [-负词条位置[@最低评级]] [0洗]
词条位置以空格表示 AND、/ 表示位置内 OR；评级绑定具体 OR 备选，+ 仅为输入别名。
狙击列表 / 狙击删除 <展示编号>
黑名单 [WM|频道] / 黑名单添加 [WM|频道] <每行一个玩家名> / 黑名单删除 [WM|频道] <玩家名>
捡漏添加 <道具名> [折扣] [0|满] / 捡漏列表 / 捡漏删除 <id> / 捡漏紫卡
捡漏紫卡列表 / 捡漏紫卡删除 <id> / 开盒 / 开盒紫卡
上线提醒 / 上线提醒列表 / 上线提醒删除 <id> / 词条列表

QQ群只接受目标 owner_qq 的命令；Discord 私聊只接受目标对应用户的命令。
回复文案统一走 texts.py 注册表（WebUI 可自定义，保存即生效）；
命令内部映射名固定；触发名和自定义别名存 store（重启生效）。
"""

from __future__ import annotations

from dataclasses import dataclass

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import Rule

from . import bargain, configops, rivendata, texts
from .blacklist_input import (
    BLACKLIST_NICK_MAX_LENGTH,
    BlacklistInputError,
    parse_blacklist_names,
)
from .chat_tracking import (
    TrackingExportTooLarge,
    TrackingStore,
    open_tracking_reader,
    run_tracking_query,
)
from .command_meta import (
    ACTIVE_ALIASES,
    ACTIVE_COMMAND_NAMES,
    COMMAND_BY_ID,
    INTERNAL_MAPPING_NAMES,
    LEGACY_SUBCOMMANDS_BY_PARENT,
    LEGACY_SUBCOMMAND_TOKENS,
    TOP_LEVEL_COMMANDS,
    command_example_name,
)
from .formatter import describe_config, format_config_command
from .parsing import normalize
from .platform_identity import normalize_player_nick
from .privacy import contains_hidden_identifier, redact_hidden_identifiers
from .shared import get_bargain, get_config, get_store
from .store import (
    TARGET_CHANNEL_DEDUPE_MAX_HOURS,
    TARGET_CHANNEL_DEDUPE_MIN_HOURS,
)
from .tracking_view import (
    TRACKING_PAGE_SIZE,
    format_ownership,
    format_riven_summary,
    format_tracking_time,
    pagination_label,
    parse_tracking_request,
    platform_name as tracking_platform_name,
)


def _load_aliases() -> dict[str, set[str]]:
    """读取命令别名；进程启动后保持固定，修改需重启。"""
    saved = get_store().list_command_aliases()
    return {
        command_id: set(aliases)
        for command_id, aliases in saved.items()
        if command_id in COMMAND_BY_ID
    }


_DB_COMMAND_NAMES = get_store().list_command_names()
_DB_ALIASES = _load_aliases()
ACTIVE_COMMAND_NAMES.clear()
ACTIVE_COMMAND_NAMES.update(_DB_COMMAND_NAMES)
ACTIVE_ALIASES.clear()
ACTIVE_ALIASES.update({command_id: sorted(aliases, key=str.casefold)
                       for command_id, aliases in _DB_ALIASES.items()})


def _aliases(command_id: str) -> set[str]:
    return set(_DB_ALIASES.get(command_id, set()))


def _command_name(command_id: str) -> str:
    return _DB_COMMAND_NAMES[command_id]


def _match_legacy_subcommand(
    parent_id: str,
    text: str,
) -> tuple[str | None, str]:
    """识别已退役的父命令子入口，供父命令拒绝旧写法。"""
    value = text.strip()
    folded = value.casefold()
    candidates = [
        (token, command_id)
        for command_id in LEGACY_SUBCOMMANDS_BY_PARENT.get(parent_id, ())
        for token in {
            *LEGACY_SUBCOMMAND_TOKENS[command_id], *_aliases(command_id),
        }
        if token
    ]
    for token, command_id in sorted(
        candidates, key=lambda item: len(item[0]), reverse=True,
    ):
        token_folded = token.casefold()
        if not folded.startswith(token_folded):
            continue
        remainder = value[len(token):]
        if command_id.endswith(".list"):
            if not remainder:
                return command_id, ""
            continue
        if (not remainder or remainder[0].isspace()
                or command_id.endswith(".delete")):
            return command_id, remainder.strip()
    return None, value


@dataclass(frozen=True)
class ScopedCommandEvent:
    """平台无关的私有配置作用域，字段与现有命令核心所需事件字段一致。"""

    group_id: int
    user_id: int
    platform: str
    external_user_id: str


@dataclass(frozen=True, slots=True)
class TrackingCommandResult:
    text: str
    export_text: str | None = None
    export_pages: tuple[str, ...] = ()
    filename: str | None = None


def _is_internal_mapping_invocation(raw: str) -> bool:
    """内部映射名只能用于程序分发，不能从任一聊天入口触发。"""
    text = str(raw or "").strip()
    if text.startswith("/"):
        text = text[1:].lstrip()
    folded = text.casefold()
    for mapping_name in INTERNAL_MAPPING_NAMES:
        mapping_folded = mapping_name.casefold()
        if not folded.startswith(mapping_folded):
            continue
        remainder = text[len(mapping_name):]
        if not remainder or remainder[0].isspace():
            return True
    return False


async def _owned_qq_target(event: GroupMessageEvent) -> bool:
    """在事件循环中访问共享 Store，避免同步 Rule 被放入工作线程。"""
    get_plaintext = getattr(event, "get_plaintext", None)
    if callable(get_plaintext) and _is_internal_mapping_invocation(
        get_plaintext()
    ):
        return False
    target = get_store().get_target(event.group_id)
    return bool(
        target and target["platform"] == "qq" and target["active"]
        and target["owner_qq"] == event.user_id)


_QQ_OWNER_RULE = Rule(_owned_qq_target)
_SILENT = object()


def _channel_command_blocked(command_id: str, args: str, scope_id: int) -> bool:
    if get_store().get_target_preferences(scope_id)["channel_enabled"]:
        return False
    if command_id in {
        "tracking.open", "tracking.open.riven",
        "tracker.manage", "tracker.manage.list", "tracker.manage.delete",
    }:
        return True
    if command_id not in {"blacklist.list", "blacklist.add", "blacklist.delete"}:
        return False
    value = normalize(args)
    scope, _remainder, explicit = _parse_blacklist_scope(value)
    return explicit and scope == "channel"


async def _gate(command_id: str, event: GroupMessageEvent,
                args: str = "") -> str | object | None:
    """功能开关检查；频道命令关闭时返回静默标记。"""
    store = get_store()
    texts.set_current_locale(store.get_target_preferences(event.group_id)["locale"])
    if _channel_command_blocked(command_id, args, event.group_id):
        return _SILENT
    if store.is_command_disabled(command_id):
        return texts.render(
            "通用.命令已停用",
            name=command_example_name(command_id) or "?",
        )
    store.record_command_usage(command_id)
    return None


def _audit(event: GroupMessageEvent | ScopedCommandEvent, action: str,
           target: str = "", detail: str = ""):
    platform = getattr(event, "platform", "qq")
    external_id = getattr(event, "external_user_id", str(event.user_id))
    get_store().add_audit(
        f"{platform}:{external_id}", platform, action, target, detail)


def _matcher(command_id: str, *, priority: int = 5):
    return on_command(
        _command_name(command_id), aliases=_aliases(command_id), rule=_QQ_OWNER_RULE,
        priority=priority, block=True)


sniper_add = _matcher("sniper.add")
sniper_list = _matcher("sniper.list")
sniper_del = _matcher("sniper.delete")
bl_add = _matcher("blacklist.add")
bl_del = _matcher("blacklist.delete")
bl_root = _matcher("blacklist.list", priority=10)
attr_list = _matcher("attribute.list")
name_history = _matcher("tracking.open")
riven_history = _matcher("tracking.open.riven")
player_trackers = _matcher("tracker.manage")
player_trackers_list = _matcher("tracker.manage.list")
player_trackers_delete = _matcher("tracker.manage.delete")
channel_dedupe = _matcher("channel.dedupe")
bargain_add = _matcher("bargain.add")
bargain_list = _matcher("bargain.list")
bargain_del = _matcher("bargain.delete")
bargain_riven = _matcher("bargain.riven")
bargain_riven_list = _matcher("bargain.riven.list")
bargain_riven_delete = _matcher("bargain.riven.delete")


# ---- 核心逻辑（供多个入口复用）----

def _do_add(event: GroupMessageEvent, text: str) -> str:
    ok, msg, config_id = configops.add_config_checked(
        get_store(), get_config(), event.group_id, text)
    if ok:
        saved = get_store().get_config(config_id, event.group_id)
        _audit(event, "config_add",
               f"群{event.group_id}#{saved['display_number']}",
               format_config_command(saved))
    return msg


def _do_list(event: GroupMessageEvent) -> str:
    configs = get_store().list_configs(event.group_id)
    if not configs:
        return texts.render("狙击列表.空")
    entries = [describe_config(config) for config in configs]
    return texts.render("狙击列表.标题") + "\n" + \
        "\n".join(entries)


def _find_config(event: GroupMessageEvent, text: str) -> tuple[dict | None, str | None]:
    cid = text.strip().lstrip("#")
    if not cid.isdigit():
        return None, texts.render(
            "通用.编号用法",
            action="delete" if texts.current_locale() == "en" else "删除",
        )
    cfg = get_store().get_config_by_display_number(int(cid), event.group_id)
    if not cfg:
        return None, texts.render("通用.配置不存在")
    return cfg, None


def _do_del(event: GroupMessageEvent, text: str) -> str:
    cfg, err = _find_config(event, text)
    if err:
        return err
    get_store().delete_config(cfg["id"], event.group_id)
    _audit(event, "config_delete",
           f"群{event.group_id}#{cfg['display_number']}", describe_config(cfg))
    return texts.render("狙击删除.成功", id=cfg["display_number"])


_BLACKLIST_SCOPE_ALIASES = {
    "wm": "wm", "wfm": "wm", "频道": "channel", "channel": "channel",
}


def _parse_blacklist_scope(text: str) -> tuple[str, str, bool]:
    text = normalize(text).strip()
    parts = text.split(maxsplit=1)
    head = parts[0] if parts else ""
    scope = _BLACKLIST_SCOPE_ALIASES.get(head.casefold())
    if scope is None:
        return "wm", text, False
    return scope, parts[1].strip() if len(parts) > 1 else "", True


def _blacklist_scope_label(scope: str) -> str:
    if scope == "wm":
        return "WM"
    return "Channel" if texts.current_locale() == "en" else "频道"


def _blacklist_scoped_reply(scope: str, message: str) -> str:
    return f"[{_blacklist_scope_label(scope)}] {message}"


def _blacklist_target_scopes(
    scope: str, *, explicit: bool, channel_enabled: bool,
) -> tuple[str, ...]:
    if explicit or not channel_enabled:
        return (scope,)
    return ("wm", "channel")


def _do_bl_add(event: GroupMessageEvent | ScopedCommandEvent, value: str) -> str:
    scope, names_text, explicit = _parse_blacklist_scope(value)
    try:
        names = parse_blacklist_names(names_text)
    except BlacklistInputError as error:
        if error.reason == "too_long":
            return texts.render(
                "黑名单添加.名称过长",
                line=error.line_number,
                limit=BLACKLIST_NICK_MAX_LENGTH,
            )
        return texts.render("黑名单添加.用法")
    store = get_store()
    scopes = _blacklist_target_scopes(
        scope,
        explicit=explicit,
        channel_enabled=store.get_target_preferences(
            event.group_id)["channel_enabled"],
    )

    replies: list[str] = []
    for target_scope in scopes:
        added = 0
        for name in names:
            ok = store.add_blacklist(
                event.group_id, name, scope=target_scope)
            if ok:
                added += 1
                _audit(
                    event, f"{target_scope}_blacklist_add", name,
                    f"群{event.group_id}",
                )
        if len(names) == 1:
            message = texts.render(
                "黑名单添加.成功" if added else "黑名单添加.已存在",
                name=names[0],
            )
        else:
            message = texts.render(
                "黑名单添加.批量完成",
                added=added,
                existing=len(names) - added,
            )
        replies.append(_blacklist_scoped_reply(target_scope, message))
    return "\n".join(replies)


def _do_bl_del(
    event: GroupMessageEvent | ScopedCommandEvent, name: str,
) -> str:
    scope, name, explicit = _parse_blacklist_scope(name)
    name = normalize_player_nick(name)
    if not name or contains_hidden_identifier(name):
        return texts.render("黑名单删除.用法")
    store = get_store()
    scopes = _blacklist_target_scopes(
        scope,
        explicit=explicit,
        channel_enabled=store.get_target_preferences(
            event.group_id)["channel_enabled"],
    )
    replies: list[str] = []
    for target_scope in scopes:
        ok = store.remove_blacklist(
            event.group_id, name, scope=target_scope)
        if ok:
            _audit(
                event, f"{target_scope}_blacklist_del", name,
                f"群{event.group_id}",
            )
        message = texts.render(
            "黑名单删除.成功" if ok else "黑名单删除.不存在", name=name)
        replies.append(_blacklist_scoped_reply(target_scope, message))
    return "\n".join(replies)


def _do_bl_list(
    event: GroupMessageEvent | ScopedCommandEvent, scope_text: str = "",
) -> str:
    scope, remainder, explicit = _parse_blacklist_scope(scope_text)
    if remainder or (scope_text.strip() and not explicit):
        return texts.render("黑名单.子命令错误")
    store = get_store()
    scopes = _blacklist_target_scopes(
        scope,
        explicit=explicit,
        channel_enabled=store.get_target_preferences(
            event.group_id)["channel_enabled"],
    )
    sections: list[str] = []
    for target_scope in scopes:
        names = [
            name for name in store.list_blacklist(
                event.group_id, scope=target_scope)
            if not contains_hidden_identifier(name)
        ]
        label = _blacklist_scope_label(target_scope)
        if not names:
            sections.append(f"{texts.render('黑名单.空')} · {label}")
            continue
        sections.append(
            f"{texts.render('黑名单.标题')} · {label}\n"
            + "\n".join(f"- {name}" for name in names)
        )
    return redact_hidden_identifiers("\n\n".join(sections))


def _attr_list_text() -> str:
    """列出完整词条名与简写；英文简写统一使用大写。"""
    locale = texts.current_locale()
    lines = []
    for slug in rivendata.attribute_catalog():
        name = rivendata.attribute_name(slug, locale)
        english = rivendata.attribute_short_name(slug).upper()
        if locale == "en":
            lines.append(f"{name} - {english}")
        else:
            chinese = rivendata.attribute_short_name(slug, "zh")
            lines.append(f"{name} - {english} - {chinese}")
    return texts.render("词条列表.标题") + "\n" + "\n".join(sorted(lines))


def _do_channel_dedupe(
        event: GroupMessageEvent | ScopedCommandEvent, text: str) -> str:
    value = text.strip()
    store = get_store()
    if not value:
        hours = store.get_target_preferences(
            event.group_id)["channel_dedupe_hours"]
        return texts.render("去重.当前", hours=hours)
    if not value.isascii() or not value.isdigit():
        return texts.render("去重.用法")
    hours = int(value)
    if not (TARGET_CHANNEL_DEDUPE_MIN_HOURS
            <= hours <= TARGET_CHANNEL_DEDUPE_MAX_HOURS):
        return texts.render("去重.用法")
    store.set_target_channel_dedupe_hours(event.group_id, hours)
    _audit(
        event, "channel_dedupe_set", str(event.group_id), f"hours={hours}",
    )
    return texts.render("去重.成功", hours=hours)


def _platform_name(platform: str, locale: str) -> str:
    return tracking_platform_name(platform, locale)


def _tracking_match_label(player: dict, locale: str) -> str:
    identities = player.get("matched_identities") or ({
        "nick": player["nick"], "platform": player["platform"],
    },)
    return " / ".join(
        f"{identity['nick']} "
        f"[{_platform_name(identity['platform'], locale)}]"
        for identity in identities
    )


def _tracking_store() -> TrackingStore:
    return open_tracking_reader(get_config())


def _find_tracking_players(query: str) -> list[dict]:
    with _tracking_store() as tracking:
        return tracking.find_players(query)


def _resolve_tracking_player(store: TrackingStore, query: str) -> tuple[dict | None, str | None]:
    if contains_hidden_identifier(query):
        return None, texts.render("开盒.用法")
    matches = store.find_players(query)
    if not matches:
        return None, texts.render("开盒.无结果", query=query)
    if len(matches) > 1:
        locale = texts.current_locale()
        candidates = "\n".join(
            f"- {_tracking_match_label(row, locale)}"
            for row in matches)
        return None, texts.render(
            "开盒.名称歧义", query=query, candidates=candidates)
    return matches[0], None


def _page_metadata(total: int, page: int) -> dict[str, int | bool]:
    pages = (total + TRACKING_PAGE_SIZE - 1) // TRACKING_PAGE_SIZE
    offset = (page - 1) * TRACKING_PAGE_SIZE
    return {
        "page": page, "page_size": TRACKING_PAGE_SIZE, "total": total,
        "pages": pages, "offset": offset,
        "start": 0 if total == 0 else offset + 1,
        "end": min(total, offset + TRACKING_PAGE_SIZE),
        "out_of_range": bool(page > max(1, pages)), "all_results": False,
    }


def _player_history_text(
    report: dict, query: str, locale: str, *, include_profile: bool = True,
) -> str:
    pagination = report["pagination"]
    lines = [texts.render("开盒.玩家标题", name=report["nick"])]
    if include_profile:
        lines.append("In-game names by platform" if locale == "en" else "各平台游戏名")
        separator = ": " if locale == "en" else "："
        for identity in report["platforms"]:
            lines.append(
                f"- {_platform_name(identity['platform'], locale)}{separator}"
                f"{identity['current_nick']}"
            )
        lines.append("Name history" if locale == "en" else "曾用名")
        for identity in report["platforms"]:
            lines.append(f"- {_platform_name(identity['platform'], locale)}")
            for name in identity["names"]:
                lines.append(
                    f"  - {name['nick']} ({format_tracking_time(name['first_seen'])} ~ "
                    f"{format_tracking_time(name['last_seen'])})")
    label = "Riven history" if locale == "en" else "紫卡历史"
    lines.append(f"{label}: {pagination['total']}")
    if pagination["total"]:
        lines.append(pagination_label(pagination, locale))
        lines.append(texts.render("开盒.词条说明", locale=locale))
        lines.extend(format_riven_summary(card, locale) for card in report["rivens"])
        if not pagination.get("all_results"):
            command = command_example_name("tracking.open")
            if command is None:
                lines.append(texts.render("通用.缺少英文简写", locale=locale))
                return "\n".join(lines)
            hint = (f"Next:\n{command} {query} page {pagination['page'] + 1}\n"
                    f"Export all:\n{command} {query} all" if locale == "en" else
                    f"下一页：\n{command} {query} page {pagination['page'] + 1}\n"
                    f"导出全部：\n{command} {query} all")
            if int(pagination["page"]) < int(pagination["pages"]):
                lines.append(hint)
            else:
                lines.append((f"Export all:\n{command} {query} all" if locale == "en"
                              else f"导出全部：\n{command} {query} all"))
    return "\n".join(lines)


def _riven_history_text(report: dict, locale: str) -> str:
    pagination = report["pagination"]
    lines = [format_riven_summary(report, locale),
             texts.render("开盒.词条说明", locale=locale)]
    label = "Observed holder history" if locale == "en" else "观测到的持有者变化"
    lines.append(f"{label}: {pagination['total']}")
    if pagination["total"]:
        lines.append(pagination_label(pagination, locale))
        lines.extend(format_ownership(row, locale) for row in report["ownerships"])
    else:
        lines.append("No reliable holder record" if locale == "en" else "暂无可靠持有者记录")
    if not pagination.get("all_results"):
        number = int(report["riven_no"])
        command = command_example_name("tracking.open.riven")
        if command is None:
            lines.append(texts.render("通用.缺少英文简写", locale=locale))
            return "\n".join(lines)
        if int(pagination["page"]) < int(pagination["pages"]):
            lines.append((f"Next:\n{command} {number} page {pagination['page'] + 1}\n"
                          f"Export all:\n{command} {number} all" if locale == "en" else
                          f"下一页：\n{command} {number} page {pagination['page'] + 1}\n"
                          f"导出全部：\n{command} {number} all"))
        else:
            lines.append((f"Export all:\n{command} {number} all" if locale == "en"
                          else f"导出全部：\n{command} {number} all"))
    return "\n".join(lines)


def _tracking_export_pages(
    report: dict, kind: str, query: str, locale: str,
) -> tuple[str, ...]:
    key = "rivens" if kind == "player" else "ownerships"
    records = report[key]
    total = len(records)
    pages = max(1, (total + TRACKING_PAGE_SIZE - 1) // TRACKING_PAGE_SIZE)
    output = []
    for page in range(1, pages + 1):
        start = (page - 1) * TRACKING_PAGE_SIZE
        metadata = _page_metadata(total, page)
        metadata["all_results"] = True
        partial = {**report, key: records[start:start + TRACKING_PAGE_SIZE],
                   "pagination": metadata}
        text = (_player_history_text(
            partial, query, locale, include_profile=page == 1,
        ) if kind == "player" else _riven_history_text(partial, locale))
        output.append(text)
    return tuple(output)


def _build_tracking_result(query: str, kind: str) -> TrackingCommandResult:
    """分页浏览玩家或紫卡归属历史，或构建完整导出。"""
    if kind == "player" and _match_legacy_subcommand(
        "tracking.open", query,
    )[0] is not None:
        return TrackingCommandResult(texts.render("开盒.用法"))
    request = parse_tracking_request(query, kind=kind)
    if request is None:
        return TrackingCommandResult(texts.render("开盒.用法"))
    locale = texts.current_locale()
    with _tracking_store() as store:
        if request.kind == "riven":
            number = int(request.target)
            try:
                report = store.riven_report(
                    number, page=request.page, all_results=request.export_all,
                )
            except TrackingExportTooLarge as error:
                return TrackingCommandResult(texts.render(
                    "开盒.导出过大", total=error.total, limit=error.limit,
                ))
            if report is None:
                return TrackingCommandResult(
                    texts.render("开盒.紫卡不存在", number=number))
            if report["pagination"]["out_of_range"]:
                return TrackingCommandResult(texts.render(
                    "开盒.页码超限", page=request.page,
                    pages=max(1, report["pagination"]["pages"])))
            text = _riven_history_text(report, locale)
            if not request.export_all:
                return TrackingCommandResult(redact_hidden_identifiers(text))
            pages = _tracking_export_pages(report, "riven", str(number), locale)
            return TrackingCommandResult(
                text=("Riven history export is ready." if locale == "en"
                      else "紫卡持有者观测历史已生成。"),
                export_text=redact_hidden_identifiers(text),
                export_pages=tuple(redact_hidden_identifiers(page) for page in pages),
                filename=f"rivensniper-riven-{number}-history.txt",
            )

        player_query = str(request.target)
        if contains_hidden_identifier(player_query):
            return TrackingCommandResult(texts.render("开盒.用法"))
        player, error = _resolve_tracking_player(store, player_query)
        if error:
            return TrackingCommandResult(error)
        try:
            report = store.player_report(
                player["account_id"], page=request.page,
                all_results=request.export_all,
            )
        except TrackingExportTooLarge as error:
            return TrackingCommandResult(texts.render(
                "开盒.导出过大", total=error.total, limit=error.limit,
            ))
        if report is None:
            return TrackingCommandResult(
                texts.render("开盒.无结果", query=player_query))
        if report["pagination"]["out_of_range"]:
            return TrackingCommandResult(texts.render(
                "开盒.页码超限", page=request.page,
                pages=max(1, report["pagination"]["pages"])))
        text = _player_history_text(report, player_query, locale)
        if not request.export_all:
            return TrackingCommandResult(redact_hidden_identifiers(text))
        pages = _tracking_export_pages(report, "player", player_query, locale)
        return TrackingCommandResult(
            text=("Player Riven history export is ready." if locale == "en"
                  else "玩家紫卡历史已生成。"),
            export_text=redact_hidden_identifiers(text),
            export_pages=tuple(redact_hidden_identifiers(page) for page in pages),
            filename="rivensniper-player-rivens.txt",
        )


def build_tracking_forward_nodes(
    bot_id: str | int, pages: tuple[str, ...],
) -> list[dict[str, object]]:
    return [{
        "type": "node",
        "data": {
            "uin": str(bot_id),
            "name": "RivenSniper",
            "content": page,
        },
    } for page in pages]


async def _send_tracking_export_qq(
    bot: Bot, event: GroupMessageEvent, result: TrackingCommandResult,
) -> None:
    pages = result.export_pages or (result.export_text or result.text,)
    english = texts.current_locale() == "en"
    for start in range(0, len(pages), 99):
        batch = pages[start:start + 99]
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=build_tracking_forward_nodes(bot.self_id, batch),
            source="RivenSniper Riven history" if english else "RivenSniper 紫卡观测历史",
            summary=f"{len(pages)} pages" if english else f"共 {len(pages)} 页",
            prompt="[Riven history]" if english else "[紫卡观测历史]",
        )


async def _do_player_tracker_add(
    event: GroupMessageEvent | ScopedCommandEvent, raw: str,
) -> str:
    value = raw.strip()
    locale = texts.current_locale()
    store = get_store()
    if not value:
        return texts.render("上线提醒.用法")
    if _match_legacy_subcommand("tracker.manage", value)[0] is not None:
        return texts.render("上线提醒.用法")
    if contains_hidden_identifier(value):
        return texts.render("上线提醒.用法")
    matches = await run_tracking_query(
        lambda: _find_tracking_players(value))
    if len(matches) > 1:
        locale = texts.current_locale()
        candidates = "\n".join(
            f"- {_tracking_match_label(row, locale)}" for row in matches)
        return texts.render(
            "开盒.名称歧义", query=value, candidates=candidates)
    player = matches[0] if matches else None
    name = player["nick"] if player is not None else value
    tracker_id, created = store.add_player_tracker(
        event.group_id, name,
        account_id=player["account_id"] if player is not None else None)
    if created and player is None:
        return texts.render(
            "上线提醒.待识别已添加", id=tracker_id, name=name,
            locale=locale)
    return texts.render(
        "上线提醒.已添加" if created else "上线提醒.已存在",
        id=tracker_id, name=name, locale=locale)


async def _do_player_tracker_list(
    event: GroupMessageEvent | ScopedCommandEvent, raw: str = "",
) -> str:
    if raw.strip():
        return texts.render("上线提醒.用法")
    rows = get_store().list_player_trackers(event.group_id)
    if not rows:
        return texts.render("上线提醒.列表空")

    def lookup_names() -> dict[int, str]:
        names: dict[int, str] = {}
        with _tracking_store() as tracking:
            for row in rows:
                name = str(row.get("target_nick") or "").strip()
                if not name and row["resolved"]:
                    matches = tracking.find_players(row["account_id"])
                    name = matches[0]["nick"] if matches else ""
                names[row["id"]] = name
        return names

    names = await run_tracking_query(lookup_names)
    for row in rows:
        name = names[row["id"]]
        names[row["id"]] = (
            texts.render("上线提醒.玩家未知")
            if contains_hidden_identifier(name)
            else name or texts.render("上线提醒.玩家未知")
        )
    lines = [texts.render("上线提醒.列表标题")]
    for row in rows:
        pending = (" " + texts.render("上线提醒.等待识别")
                   if not row["resolved"] else "")
        lines.append(f"- {row['id']}  {names[row['id']]}{pending}")
    return redact_hidden_identifiers("\n".join(lines))


def _do_player_tracker_delete(
    event: GroupMessageEvent | ScopedCommandEvent, raw: str,
) -> str:
    value = raw.strip().lstrip("#")
    if not value.isdigit():
        return texts.render("上线提醒.用法")
    tracker_id = int(value)
    removed = get_store().delete_player_tracker(tracker_id, event.group_id)
    return texts.render(
        "上线提醒.删除成功" if removed else "上线提醒.删除失败",
        id=tracker_id,
    )

# ---- 命令入口 ----


@sniper_add.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    blocked = await _gate("sniper.add", event)
    if blocked:
        await sniper_add.finish(blocked)
    await sniper_add.finish(_do_add(event, args.extract_plain_text()))


@sniper_list.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    blocked = await _gate("sniper.list", event)
    if blocked:
        await sniper_list.finish(blocked)
    await sniper_list.finish(_do_list(event))


@sniper_del.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    blocked = await _gate("sniper.delete", event)
    if blocked:
        await sniper_del.finish(blocked)
    await sniper_del.finish(_do_del(event, args.extract_plain_text()))


@bl_add.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("blacklist.add", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await bl_add.finish(blocked)
    await bl_add.finish(_do_bl_add(event, value))


@bl_del.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("blacklist.delete", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await bl_del.finish(blocked)
    await bl_del.finish(_do_bl_del(event, value))


@bl_root.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    """按范围查看当前目标的卖家黑名单。"""
    text = normalize(args.extract_plain_text())
    blocked = await _gate("blacklist.list", event, text)
    if blocked is _SILENT:
        return
    if blocked:
        await bl_root.finish(blocked)
    await bl_root.finish(_do_bl_list(event, text))


@attr_list.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    blocked = await _gate("attribute.list", event)
    if blocked:
        await attr_list.finish(blocked)
    await attr_list.finish(_attr_list_text())


@channel_dedupe.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text().strip()
    blocked = await _gate("channel.dedupe", event, value)
    if blocked:
        await channel_dedupe.finish(blocked)
    await channel_dedupe.finish(_do_channel_dedupe(event, value))


@name_history.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text().strip()
    blocked = await _gate("tracking.open", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await name_history.finish(blocked)
    result = await run_tracking_query(
        lambda: _build_tracking_result(value, "player"))
    if result.export_text is not None:
        await _send_tracking_export_qq(bot, event, result)
        await name_history.finish()
    await name_history.finish(result.text)


@riven_history.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text().strip()
    blocked = await _gate("tracking.open.riven", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await riven_history.finish(blocked)
    result = await run_tracking_query(
        lambda: _build_tracking_result(value, "riven"))
    if result.export_text is not None:
        await _send_tracking_export_qq(bot, event, result)
        await riven_history.finish()
    await riven_history.finish(result.text)


@player_trackers.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("tracker.manage", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await player_trackers.finish(blocked)
    await player_trackers.finish(await _do_player_tracker_add(event, value))


@player_trackers_list.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("tracker.manage.list", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await player_trackers_list.finish(blocked)
    await player_trackers_list.finish(
        await _do_player_tracker_list(event, value))


@player_trackers_delete.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("tracker.manage.delete", event, value)
    if blocked is _SILENT:
        return
    if blocked:
        await player_trackers_delete.finish(blocked)
    await player_trackers_delete.finish(
        _do_player_tracker_delete(event, value))


# ---- 捡漏 ----

def _do_bargain_add(event: GroupMessageEvent, text: str) -> str:
    ok, msg, item_id = bargain.add_item_checked(
        get_store(), get_config(), event.group_id, text)
    if ok:
        row = get_store().get_bargain_item(item_id, event.group_id)
        _audit(event, "bargain_add", f"#{item_id}", bargain.describe_item(row))
    return msg


def _do_bargain_list(event: GroupMessageEvent) -> str:
    store = get_store()
    rows = store.list_bargain_items(event.group_id)
    if not rows:
        return texts.render("捡漏列表.空")
    return texts.render("捡漏列表.标题") + "\n" + \
        "\n".join(bargain.describe_item(r) for r in rows)


def _find_bargain_item(
        event: GroupMessageEvent, text: str) -> tuple[dict | None, str | None]:
    iid = text.strip().lstrip("#")
    if not iid.isdigit():
        return None, texts.render("捡漏删除.用法")
    row = get_store().get_bargain_item(int(iid), event.group_id)
    if not row:
        return None, texts.render("通用.监控不存在", id=int(iid))
    return row, None


def _do_bargain_del(event: GroupMessageEvent, text: str) -> str:
    row, err = _find_bargain_item(event, text)
    if err:
        return err
    get_store().delete_bargain_item(row["id"], event.group_id)
    _audit(event, "bargain_del", f"#{row['id']}", bargain.describe_item(row))
    return texts.render("捡漏删除.成功", id=row["id"])


def _do_bargain_riven_add(event: GroupMessageEvent, text: str) -> str:
    """添加紫卡捡漏监控；旧列表/删除子入口明确拒绝。"""
    store = get_store()
    text = text.strip()
    if not text:
        return texts.render("捡漏紫卡.用法")
    if _match_legacy_subcommand("bargain.riven", text)[0] is not None:
        return texts.render("捡漏紫卡.用法")
    ok, msg, iid = bargain.add_riven_item_checked(
        store, get_config(), event.group_id, text)
    if ok:
        row = store.get_bargain_riven_item(iid, event.group_id)
        _audit(event, "bargain_riven_add", f"#{iid}", bargain.describe_riven_item(row))
        runtime = get_bargain()
        if runtime is not None:
            runtime.request_riven_sample(row["weapon_slug"])
    return msg


def _do_bargain_riven_list(
    event: GroupMessageEvent | ScopedCommandEvent, text: str = "",
) -> str:
    if text.strip():
        return texts.render("捡漏紫卡.用法")
    store = get_store()
    header = texts.render("捡漏紫卡.列表标题")
    rows = store.list_bargain_riven_items(event.group_id)
    if not rows:
        return header + "\n" + texts.render("捡漏紫卡.列表空")
    return header + "\n" + "\n".join(bargain.describe_riven_item(r) for r in rows)


def _find_bargain_riven_item(
        event: GroupMessageEvent, text: str) -> tuple[dict | None, str | None]:
    iid = text.strip().lstrip("#")
    if not iid.isdigit():
        return None, texts.render("捡漏紫卡.删除用法")
    row = get_store().get_bargain_riven_item(int(iid), event.group_id)
    if not row:
        return None, texts.render("通用.监控不存在", id=int(iid))
    return row, None


def _do_bargain_riven_del(event: GroupMessageEvent, text: str) -> str:
    row, err = _find_bargain_riven_item(event, text)
    if err:
        return err
    get_store().delete_bargain_riven_item(row["id"], event.group_id)
    _audit(event, "bargain_riven_del", f"#{row['id']}", bargain.describe_riven_item(row))
    return texts.render("捡漏紫卡.删除成功", id=row["id"])

@bargain_add.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    blocked = await _gate("bargain.add", event)
    if blocked:
        await bargain_add.finish(blocked)
    await bargain_add.finish(_do_bargain_add(event, args.extract_plain_text().strip()))


@bargain_list.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    blocked = await _gate("bargain.list", event)
    if blocked:
        await bargain_list.finish(blocked)
    await bargain_list.finish(_do_bargain_list(event))


@bargain_del.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    blocked = await _gate("bargain.delete", event)
    if blocked:
        await bargain_del.finish(blocked)
    await bargain_del.finish(_do_bargain_del(event, args.extract_plain_text()))


@bargain_riven.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    blocked = await _gate("bargain.riven", event)
    if blocked:
        await bargain_riven.finish(blocked)
    await bargain_riven.finish(
        _do_bargain_riven_add(event, args.extract_plain_text()))


@bargain_riven_list.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("bargain.riven.list", event, value)
    if blocked:
        await bargain_riven_list.finish(blocked)
    await bargain_riven_list.finish(_do_bargain_riven_list(event, value))


@bargain_riven_delete.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    value = args.extract_plain_text()
    blocked = await _gate("bargain.riven.delete", event, value)
    if blocked:
        await bargain_riven_delete.finish(blocked)
    await bargain_riven_delete.finish(_do_bargain_riven_del(event, value))


# ---- Discord 私聊命令分发 ----

def scoped_command_triggers() -> list[tuple[str, str]]:
    """返回 (触发名, command_id)；最长优先避免短别名抢先匹配。"""
    triggers: list[tuple[str, str]] = []
    for node in TOP_LEVEL_COMMANDS:
        for trigger in {_command_name(node.id), *_aliases(node.id)}:
            trigger = trigger.strip()
            if trigger:
                triggers.append((trigger, node.id))
    return sorted(set(triggers), key=lambda item: len(item[0]), reverse=True)


def parse_scoped_command(raw: str) -> tuple[str, str] | None:
    """解析私聊中的文本命令；兼容裸命令和单个前导 ``/``。"""
    text = raw.strip()
    if text.startswith("/"):
        text = text[1:].lstrip()
    if _is_internal_mapping_invocation(text):
        return None
    folded = text.casefold()
    for trigger, command_id in scoped_command_triggers():
        if folded.startswith(trigger.casefold()):
            return command_id, text[len(trigger):].strip()
    return None


def _gate_scoped(command_id: str, event: ScopedCommandEvent,
                 args: str = "") -> str | object | None:
    """Discord 私聊版本的功能开关检查。"""
    store = get_store()
    texts.set_current_locale(store.get_target_preferences(event.group_id)["locale"])
    if _channel_command_blocked(command_id, args, event.group_id):
        return _SILENT
    if store.is_command_disabled(command_id):
        return texts.render(
            "通用.命令已停用",
            name=command_example_name(command_id) or "?",
        )
    store.record_command_usage(command_id)
    return None


def _owned_scoped_target(event: ScopedCommandEvent) -> bool:
    target = get_store().get_target(event.group_id)
    if not target or not target["active"] or target["platform"] != event.platform:
        return False
    if event.platform == "qq":
        return target["owner_qq"] == event.user_id
    if event.platform == "discord":
        return (target["external_id"] == event.external_user_id
                and str(event.user_id) == event.external_user_id)
    return False


async def execute_scoped_command(command_id: str, args: str,
                                 event: ScopedCommandEvent, *,
                                 rich: bool = False,
                                 ) -> str | TrackingCommandResult | None:
    """用现有命令核心执行 Discord 私聊命令。"""
    if not _owned_scoped_target(event):
        return None
    texts.set_current_locale(
        get_store().get_target_preferences(event.group_id)["locale"])
    if command_id == "blacklist.list":
        text = normalize(args)
        blocked = _gate_scoped("blacklist.list", event, text)
        if blocked is _SILENT:
            return None
        if blocked:
            return str(blocked)
        return _do_bl_list(event, text)

    blocked = _gate_scoped(command_id, event, args)
    if blocked is _SILENT:
        return None
    if blocked:
        return str(blocked)

    if command_id == "sniper.add":
        return _do_add(event, args)
    if command_id == "sniper.list":
        return _do_list(event)
    if command_id == "sniper.delete":
        return _do_del(event, args)
    if command_id == "blacklist.add":
        return _do_bl_add(event, args)
    if command_id == "blacklist.delete":
        return _do_bl_del(event, args)
    if command_id == "attribute.list":
        return _attr_list_text()
    if command_id == "channel.dedupe":
        return _do_channel_dedupe(event, args)
    if command_id == "tracking.open":
        result = await run_tracking_query(
            lambda: _build_tracking_result(args.strip(), "player"))
        return result if rich else result.text
    if command_id == "tracking.open.riven":
        result = await run_tracking_query(
            lambda: _build_tracking_result(args.strip(), "riven"))
        return result if rich else result.text
    if command_id == "tracker.manage":
        return await _do_player_tracker_add(event, args)
    if command_id == "tracker.manage.list":
        return await _do_player_tracker_list(event, args)
    if command_id == "tracker.manage.delete":
        return _do_player_tracker_delete(event, args)
    if command_id == "bargain.add":
        return _do_bargain_add(event, args.strip())
    if command_id == "bargain.list":
        return _do_bargain_list(event)
    if command_id == "bargain.delete":
        return _do_bargain_del(event, args)
    if command_id == "bargain.riven":
        return _do_bargain_riven_add(event, args)
    if command_id == "bargain.riven.list":
        return _do_bargain_riven_list(event, args)
    if command_id == "bargain.riven.delete":
        return _do_bargain_riven_del(event, args)
    return texts.render("通用.未知命令", name=command_id)
