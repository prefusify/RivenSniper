"""管理员 WebUI REST API。"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .. import bargain, configops, envutil, marketdata, rivendata, texts
from ..blacklist_input import (
    BLACKLIST_NICK_MAX_LENGTH,
    BlacklistInputError,
    parse_blacklist_names,
)
from ..chat_tracking import TrackingStore, tracking_database_path
from ..command_meta import (
    ACTIVE_ALIASES,
    ACTIVE_COMMAND_NAMES,
    COMMAND_BY_ID,
    COMMAND_NODES,
    TOP_LEVEL_COMMANDS,
    command_token_conflict,
)
from ..formatter import describe_config, format_config_command
from ..delivery import DeliverySource
from ..platform_identity import normalize_player_nick
from ..privacy import (
    contains_hidden_identifier,
    redact_hidden_identifiers_deep,
)
from ..shared import (bot_online_state, get_bargain, get_config, get_poller,
                      get_store)
from ..stats import LOGS, STATS
from ..store import TARGET_NOTE_MAX_LENGTH
from ..version import VERSION
from ..wfm_fast import query_keys
router = APIRouter()
_STATIC = Path(__file__).resolve().parent / "static"
_INDEX_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")
_ROOT = envutil.ENV_PATH.parent

_poll_lock = asyncio.Lock()
_refresh_lock = asyncio.Lock()
_DATA_REFRESH_TIMEOUT_SECONDS = 120


def _adapter_bot(bots: dict, name: str):
    for bot in bots.values():
        if bot.adapter.get_name() == name:
            return bot
    return None


def _connected_bots() -> dict:
    try:
        import nonebot
        return nonebot.get_bots()
    except ValueError:
        return {}


async def _platform_username(target: dict, bots: dict) -> str | None:
    """实时读取平台提供的目标所有者名称，不在业务库内复制保存。"""
    try:
        if target["platform"] == "qq":
            bot = _adapter_bot(bots, "OneBot V11")
            if bot is None:
                return None
            info = await asyncio.wait_for(
                bot.get_group_member_info(
                    group_id=int(target["external_id"]),
                    user_id=int(target["owner_qq"]),
                    no_cache=True,
                ),
                timeout=3,
            )
            name = str(
                info.get("card") or info.get("nickname") or "").strip()
            return name or None
        if target["platform"] == "discord":
            bot = _adapter_bot(bots, "Discord")
            if bot is None:
                return None
            user = await asyncio.wait_for(
                bot.get_user(user_id=int(target["external_id"])),
                timeout=3,
            )
            return str(
                getattr(user, "global_name", None)
                or getattr(user, "username", "")
            ).strip() or None
    except Exception:
        return None
    return None


async def _add_platform_usernames(targets: list[dict]) -> list[dict]:
    bots = _connected_bots()
    names = await asyncio.gather(*(
        _platform_username(target, bots) for target in targets
    ))
    return [dict(target, username=name)
            for target, name in zip(targets, names, strict=True)]


def _delete_tracking_target_data(path: Path, scope_id: int) -> int:
    if not path.is_file():
        return 0
    with TrackingStore(path) as tracking:
        return tracking.delete_target_data(scope_id)


def _send_queue_depth(poller) -> int | None:
    if poller is None:
        return None
    pipeline_depth = getattr(poller, "queue_depth", None)
    return pipeline_depth if pipeline_depth is not None else poller.queue.qsize()


def _request_graceful_shutdown() -> None:
    """让 Uvicorn 的 SIGINT 处理器进入 lifespan 正常关闭流程。"""
    signal.raise_signal(signal.SIGINT)


async def _terminate_subprocess(proc) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


_PLATFORM_TARGET_LABELS = {
    "discord": "Discord 用户",
}


def _scope_label(store, scope_id: int) -> str:
    """返回可直接用于 WebUI 与审计记录的跨平台目标名称。"""
    if scope_id > 0:
        return f"QQ群 {scope_id}"
    target = store.get_target(scope_id)
    if target is None:
        return f"未知目标 {scope_id}"
    prefix = _PLATFORM_TARGET_LABELS.get(
        target["platform"], f"{target['platform'].upper()} 用户")
    return f"{prefix} {target['external_id']}"


def _require_target_scope(scope_id: int) -> dict:
    """校验通用目标作用域；所有配置只能挂在已登记目标下。"""
    if scope_id == 0:
        raise HTTPException(400, "目标作用域不能为 0")
    target = get_store().get_target(scope_id)
    if target is None:
        raise HTTPException(404, "目标不存在")
    return target


def _serialize_scope_row(row: dict, _target: dict | None = None) -> dict:
    return dict(row)


def _platform_delivery_enabled(config, platform: str) -> bool:
    return bool(getattr(
        config, f"{platform}_dm_enabled",
        getattr(config, f"{platform}_enabled", False)))


def _target_blacklists(store, scope_id: int) -> dict[str, list[str]]:
    return {
        scope: [
            seller for seller in store.list_blacklist(scope_id, scope=scope)
            if not contains_hidden_identifier(seller)
        ]
        for scope in ("wm", "channel")
    }


def _public_log_entries(entries: list[dict]) -> list[dict]:
    return redact_hidden_identifiers_deep(entries)


def _require_delivery_scope(scope_id: int) -> None:
    target = _require_target_scope(scope_id)
    if not target["active"]:
        raise HTTPException(409, "目标未启用")
    if target["platform"] != "qq" and not _platform_delivery_enabled(
            get_config(), target["platform"]):
        raise HTTPException(409, f"{target['platform'].upper()} 投递未启用")


def _target_catalog(store, config, configs: list[dict] | None = None) -> list[dict]:
    """构建 QQ 群与 Discord 私聊共用的 WebUI 目标目录。"""
    configs = store.list_configs() if configs is None else configs
    config_counts = Counter(row["group_id"] for row in configs)
    bargain_item_counts = Counter(
        row["group_id"] for row in store.list_bargain_items())
    bargain_riven_counts = Counter(
        row["group_id"] for row in store.list_bargain_riven_items())

    targets = []
    for target in store.list_targets():
        scope_id = target["scope_id"]
        preference = store.get_target_preferences(scope_id)
        blacklists = _target_blacklists(store, scope_id)
        infrastructure_enabled = (
            True if target["platform"] == "qq"
            else _platform_delivery_enabled(config, target["platform"])
        )
        targets.append({
            "scope_id": scope_id,
            "platform": target["platform"],
            "external_id": target["external_id"],
            "label": _scope_label(store, scope_id),
            "active": target["active"],
            "enabled": target["enabled"],
            "owner_qq": (str(target["owner_qq"])
                         if target["owner_qq"] is not None else None),
            "note": target["note"],
            "username": None,
            "enabled_until": target["enabled_until"],
            "delivery_enabled": target["active"] and infrastructure_enabled,
            "locale": preference["locale"],
            "channel_enabled": preference["channel_enabled"],
            "wm_fast_enabled": preference["wm_fast_enabled"],
            "configs": config_counts[scope_id],
            "blacklist": sum(map(len, blacklists.values())),
            "blacklists": {scope: len(names)
                           for scope, names in blacklists.items()},
            "bargain_items": bargain_item_counts[scope_id],
            "bargain_rivens": bargain_riven_counts[scope_id],
        })
    return targets


class TargetPreferenceBody(BaseModel):
    locale: Literal["zh", "en"] | None = None
    channel_enabled: bool | None = None
    wm_fast_enabled: bool | None = None


@router.patch("/api/targets/{scope_id}/preferences")
async def target_preferences_update(scope_id: int, body: TargetPreferenceBody):
    _require_target_scope(scope_id)
    if all(value is None for value in (
            body.locale, body.channel_enabled, body.wm_fast_enabled)):
        raise HTTPException(400, "没有需要修改的目标设置")
    store = get_store()
    if body.locale is not None:
        store.set_target_locale(scope_id, body.locale)
    if body.channel_enabled is not None:
        store.set_target_channel_enabled(scope_id, body.channel_enabled)
    if body.wm_fast_enabled is not None:
        store.set_target_wm_fast_enabled(scope_id, body.wm_fast_enabled)
        poller = get_poller()
        if poller is not None:
            poller.fast.wakeup()
    preference = store.get_target_preferences(scope_id)
    store.add_audit(
        "webui", "webui", "target_preferences", _scope_label(store, scope_id),
        f"locale={preference['locale']}; channel_enabled={preference['channel_enabled']}; "
        f"wm_fast_enabled={preference['wm_fast_enabled']}",
    )
    return {"preferences": preference}


@router.get("/api/wm-fast")
async def wm_fast_status(scope_id: int | None = None):
    if scope_id is not None:
        _require_target_scope(scope_id)
    poller = get_poller()
    fast = getattr(poller, "fast", None)
    result = fast.snapshot(scope_id) if fast is not None else {
        "state": "stopped", "query_count": 0, "pool": None, "queries": [],
        "interval": getattr(get_config(), "wm_fast_interval", 2),
        "actual_interval_p95": None}
    states = {(row["weapon"], tuple(row["positives"])): row["state"]
              for row in result["queries"]}
    baselines = {(row["weapon"], tuple(row["positives"])): set(row["baseline_rules"])
                 for row in result["queries"]}
    result["rules"] = []
    if scope_id is not None:
        target = get_store().get_target(scope_id)
        if not target["active"] or (target["platform"] == "discord" and not getattr(
                get_config(), "discord_dm_enabled", False)):
            result["state"] = "target_inactive"
        for config in get_store().list_configs(scope_id):
            keys = query_keys(config)
            result["rules"].append({
                "number": config["display_number"], "eligible": bool(keys),
                "query_count": len(keys),
                "error_count": sum(states.get((key.weapon, key.positives)) == "error"
                                   for key in keys),
                "baseline_count": sum(states.get((key.weapon, key.positives)) in (None, "baseline")
                                      or config["display_number"] in baselines.get(
                                          (key.weapon, key.positives), ())
                                      for key in keys),
                "truncated_count": sum(states.get((key.weapon, key.positives)) == "truncated"
                                       for key in keys)})
        if result["state"] == "running" and not any(rule["eligible"] for rule in result["rules"]):
            result["state"] = "idle"
    return result


# ---- 页面 ----

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def index_page():
    return HTMLResponse(_INDEX_HTML)


# ---- 状态 ----

@router.get("/api/status")
async def status():
    store, cfg = get_store(), get_config()
    poller = get_poller()

    bots = _connected_bots()

    onebot = _adapter_bot(bots, "OneBot V11")
    discord = _adapter_bot(bots, "Discord")
    snowluma = None
    if onebot is not None:
        try:
            snowluma = await asyncio.wait_for(onebot.get_status(), timeout=3)
        except Exception:
            snowluma = {"online": None, "good": False}

    # 账号在线态优先取心跳（被动接收，账号被踢下线也能立刻反映）；
    # 心跳会持续刷新时间戳，僵死态下 online 为 False。None = 尚无心跳
    online, online_ts = bot_online_state()
    account_online = online if online_ts else None

    configs = store.list_configs()
    targets = _target_catalog(store, cfg, configs)
    return {
        "version": VERSION,
        "dry_run": cfg.sniper_dry_run,
        "bot_connected": onebot is not None,
        "discord_connected": discord is not None,
        "discord_ready": bool(getattr(poller, "discord_ready", False)),
        "account_online": account_online,
        "bot_ids": list(bots.keys()),
        "snowluma": snowluma,
        "poller": STATS.snapshot(),
        "sender": getattr(poller, "sender_health", None),
        "delivery": getattr(poller, "delivery_status", None),
        "queue_depth": _send_queue_depth(poller),
        "poll_interval": cfg.sniper_poll_interval,
        "groups": [
            {
                "group_id": target["scope_id"],
                "enabled": target["enabled"],
                "configs": sum(
                    1 for c in configs
                    if c["group_id"] == target["scope_id"]),
                "blacklist": sum(map(
                    len, _target_blacklists(
                        store, target["scope_id"]).values())),
            } for target in store.list_targets("qq")
        ],
        "targets": targets,
        "counts": {
            "configs": len(configs),
        },
        "recent_warnings": _public_log_entries(
            LOGS.recent(20, "WARNING")),
    }


@router.get("/api/targets")
async def targets():
    """WebUI 通用配置目标：单所有者 QQ 群和 Discord 私聊用户。"""
    catalog = _target_catalog(get_store(), get_config())
    return {"targets": await _add_platform_usernames(catalog)}


class ManagedTargetCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["qq", "discord"]
    external_id: str
    owner_qq: str | None = None
    note: str | None = Field(default=None, max_length=TARGET_NOTE_MAX_LENGTH)
    enabled: bool = True
    duration_days: int | None = Field(default=None, gt=0)


class ManagedTargetUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_qq: str | None = None
    note: str | None = Field(default=None, max_length=TARGET_NOTE_MAX_LENGTH)
    enabled: bool | None = None
    duration_days: int | None = Field(default=None, gt=0)


class ManagedTargetRenewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_days: int = Field(gt=0)


@router.get("/api/managed-targets")
async def managed_targets():
    catalog = _target_catalog(get_store(), get_config())
    return {"targets": await _add_platform_usernames(catalog)}


@router.post("/api/managed-targets")
async def managed_target_create(body: ManagedTargetCreateBody):
    external_id = body.external_id.strip()
    if not external_id.isdigit() or int(external_id) <= 0:
        raise HTTPException(400, "目标 ID 必须是正整数")
    if not body.enabled and body.duration_days is not None:
        raise HTTPException(400, "停用目标不能设置有效期")
    store = get_store()
    if body.platform == "qq":
        owner = (body.owner_qq or "").strip()
        if not owner.isdigit() or int(owner) <= 0:
            raise HTTPException(400, "QQ群目标必须指定正整数 owner_qq")
        target = store.upsert_qq_target(
            int(external_id), int(owner), enabled=body.enabled,
            duration_days=body.duration_days, note=body.note)
    else:
        if body.owner_qq not in (None, ""):
            raise HTTPException(400, "Discord 私聊目标不使用 owner_qq")
        target = store.upsert_discord_target(
            external_id, enabled=body.enabled,
            duration_days=body.duration_days, note=body.note)
    store.add_audit(
        "webui", "webui", "target_create", f"{body.platform}:{external_id}",
        f"enabled={body.enabled}; enabled_until={target['enabled_until']}",
    )
    return {"target": target}


@router.patch("/api/managed-targets/{scope_id}")
async def managed_target_update(scope_id: int, body: ManagedTargetUpdateBody):
    store = get_store()
    target = _require_target_scope(scope_id)
    fields = body.model_fields_set
    actionable = (
        (body.owner_qq is not None and "owner_qq" in fields)
        or "note" in fields
        or (body.enabled is not None and "enabled" in fields)
    )
    if not actionable:
        raise HTTPException(400, "没有需要修改的项")
    if ("duration_days" in fields and body.duration_days is not None
            and body.enabled is not True):
        raise HTTPException(400, "有效期只能在启用目标时设置")
    if target["platform"] == "qq":
        if body.owner_qq is not None and "owner_qq" in fields:
            owner = body.owner_qq.strip()
            if not owner.isdigit() or int(owner) <= 0:
                raise HTTPException(400, "owner_qq 必须是正整数")
            target = store.set_target_owner(scope_id, int(owner))
    else:
        if "owner_qq" in fields and body.owner_qq not in (None, ""):
            raise HTTPException(400, "Discord 私聊目标不使用 owner_qq")
    if "note" in fields:
        target = store.set_target_note(scope_id, body.note or "")
    if body.enabled is not None and "enabled" in fields:
        target = store.set_target_enabled(
            scope_id, body.enabled, duration_days=body.duration_days)
    store.add_audit(
        "webui", "webui", "target_update", _scope_label(store, scope_id),
        f"owner_qq={target['owner_qq']}; enabled={target['enabled']}; "
        f"enabled_until={target['enabled_until']}",
    )
    return {"target": target}


@router.post("/api/managed-targets/{scope_id}/renew")
async def managed_target_renew(scope_id: int, body: ManagedTargetRenewBody):
    store = get_store()
    target = _require_target_scope(scope_id)
    if target["enabled_until"] is None:
        raise HTTPException(400, "目标没有可续期的限时有效期")
    target = store.renew_target(scope_id, body.duration_days)
    store.add_audit(
        "webui", "webui", "target_renew", _scope_label(store, scope_id),
        f"duration_days={body.duration_days}; "
        f"enabled_until={target['enabled_until']}",
    )
    return {"target": target}


@router.delete("/api/managed-targets/{scope_id}")
async def managed_target_delete(scope_id: int):
    store = get_store()
    _require_target_scope(scope_id)
    label = _scope_label(store, scope_id)
    tracking_path = tracking_database_path(get_config())
    try:
        tracking_rows = await asyncio.to_thread(
            _delete_tracking_target_data, tracking_path, scope_id)
    except Exception as exc:
        raise HTTPException(
            500, "追踪关联数据清理失败，目标未删除") from exc

    result = store.delete_target(scope_id)
    if result is None:
        raise HTTPException(404, "目标不存在")
    removed = dict(result["removed"])
    removed["presence_alerts"] = tracking_rows
    store.add_audit(
        "webui", "webui", "target_delete", label,
        json.dumps(removed, ensure_ascii=False, sort_keys=True),
    )
    return {"deleted": True, "scope_id": scope_id, "removed": removed}


# ---- 日志 ----

@router.get("/api/logs")
async def logs(n: int = 200, level: str = "INFO"):
    return {"logs": _public_log_entries(
        LOGS.recent(min(n, 500), level))}


@router.get("/api/logs/stream")
async def logs_stream():
    async def gen():
        q = LOGS.subscribe()
        try:
            for e in LOGS.recent(50):
                public = redact_hidden_identifiers_deep(e)
                yield f"data: {json.dumps(public, ensure_ascii=False)}\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=25)
                    public = redact_hidden_identifiers_deep(e)
                    yield f"data: {json.dumps(public, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            LOGS.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- 狙击配置 ----

@router.get("/api/targets/{gid}/configs")
@router.get("/api/groups/{gid}/configs")
async def group_configs(gid: int):
    platform_target = _require_target_scope(gid)
    store = get_store()
    configs = store.list_configs(gid)
    blacklists = _target_blacklists(store, gid)
    return {"configs": [
        {**_serialize_scope_row(c, platform_target),
         "description": describe_config(c),
         "command": format_config_command(c)} for c in configs
    ], "blacklist": blacklists["wm"], "blacklists": blacklists}


class ConfigCopyBody(BaseModel):
    source_scope_id: int


@router.post("/api/targets/{gid}/configs/copy")
async def copy_target_configs(gid: int, body: ConfigCopyBody):
    _require_target_scope(gid)
    _require_target_scope(body.source_scope_id)
    if gid == body.source_scope_id:
        raise HTTPException(400, "源目标和目标不能相同")
    store = get_store()
    copied, skipped, capped, total = configops.copy_configs(
        store, get_config(), body.source_scope_id, gid)
    store.add_audit(
        "webui", "webui", "config_copy", _scope_label(store, gid),
        f"from={_scope_label(store, body.source_scope_id)}; "
        f"copied={copied}; skipped={skipped}; capped={capped}",
    )
    return {"copied": copied, "skipped": skipped,
            "capped": capped, "total": total}


@router.delete("/api/targets/{gid}/configs/{cid}")
@router.delete("/api/groups/{gid}/configs/{cid}")
async def delete_config(gid: int, cid: int):
    _require_target_scope(gid)
    store = get_store()
    cfg = store.get_config(cid, gid)
    if not cfg:
        raise HTTPException(404, "配置不存在")
    store.delete_config(cid, gid)
    store.add_audit("webui", "webui", "config_delete",
                    f"{_scope_label(store, gid)}#{cfg['display_number']}",
                    describe_config(cfg))
    return {"ok": True}


# ---- 审计 ----

@router.get("/api/audit")
async def audit(n: int = 100):
    return {"audit": redact_hidden_identifiers_deep(
        get_store().list_audit(min(n, 500)))}


# ---- 命令管理 ----


@router.get("/api/commands")
async def commands_list():
    store = get_store()
    disabled = set(store.list_disabled_commands())
    usage = store.command_usage_7d()
    saved_names = store.list_command_names()
    saved_aliases = store.list_command_aliases()
    out = []
    for node in COMMAND_NODES:
        trigger_name = saved_names[node.id]
        aliases = saved_aliases.get(node.id, [])
        name_pending = trigger_name != ACTIVE_COMMAND_NAMES.get(
            node.id, node.default_name)
        aliases_pending = aliases != ACTIVE_ALIASES.get(node.id, [])
        out.append({
            "id": node.id,
            "mapping_name": node.id,
            "trigger_name": trigger_name,
            "description": node.description,
            "enabled_global": node.id not in disabled,
            "usage_7d": usage.get(node.id, 0),
            "aliases": aliases,
            "name_pending_restart": name_pending,
            "aliases_pending_restart": aliases_pending,
            "pending_restart": name_pending or aliases_pending,
        })
    return {"commands": out}


# ---- 命令触发名与别名（重启生效）----

class CommandNameBody(BaseModel):
    name: str


@router.patch("/api/commands/{command_id}/name")
async def command_name_update(command_id: str, body: CommandNameBody):
    if command_id not in COMMAND_BY_ID:
        raise HTTPException(404, "命令不存在")
    store = get_store()
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "触发名不能为空")
    if any(character.isspace() for character in name):
        raise HTTPException(400, "触发名不能包含空白字符")
    if command_token_conflict(
        command_id,
        name,
        store.list_command_names(),
        store.list_command_aliases(),
        skip_name=True,
    ):
        raise HTTPException(400, f"「{name}」与其他命令入口或保留名冲突")
    if not store.set_command_name(command_id, name):
        raise HTTPException(400, "触发名保存失败")
    store.add_audit(
        "webui", "webui", "command_name_update", command_id, f"-> {name}",
    )
    return {"ok": True, "restart_required": True}

class CommandAliasBody(BaseModel):
    alias: str


@router.post("/api/commands/{command_id}/aliases")
async def command_alias_add(command_id: str, body: CommandAliasBody):
    if command_id not in COMMAND_BY_ID:
        raise HTTPException(404, "命令不存在")
    store = get_store()
    alias = body.alias.strip()
    if not alias:
        raise HTTPException(400, "别名不能为空")
    if any(ch.isspace() for ch in alias):
        raise HTTPException(400, "别名不能包含空白字符")
    if command_token_conflict(
        command_id,
        alias,
        store.list_command_names(),
        store.list_command_aliases(),
    ):
        raise HTTPException(400, f"「{alias}」与其他命令入口或保留名冲突")
    if not store.add_command_alias(alias, command_id):
        raise HTTPException(400, "别名保存失败")
    store.add_audit(
        "webui", "webui", "command_alias_add", alias,
        f"-> {command_id}",
    )
    return {"ok": True, "restart_required": True}


class CommandAliasRenameBody(BaseModel):
    new_alias: str


@router.patch(
    "/api/commands/{command_id}/aliases/{alias}",
)
async def command_alias_rename(
    command_id: str, alias: str, body: CommandAliasRenameBody,
):
    if command_id not in COMMAND_BY_ID:
        raise HTTPException(404, "命令不存在")
    store = get_store()
    saved = store.list_command_aliases()
    if alias not in saved.get(command_id, []):
        raise HTTPException(404, "别名不存在")
    new = body.new_alias.strip()
    if not new:
        raise HTTPException(400, "别名不能为空")
    if any(ch.isspace() for ch in new):
        raise HTTPException(400, "别名不能包含空白字符")
    if new == alias:
        return {"ok": True, "restart_required": True}
    if command_token_conflict(
        command_id,
        new,
        store.list_command_names(),
        saved,
        skip_alias=alias,
    ):
        raise HTTPException(400, f"「{new}」与其他命令入口或保留名冲突")
    if not store.rename_command_alias(command_id, alias, new):
        raise HTTPException(400, "别名保存失败")
    store.add_audit(
        "webui", "webui", "command_alias_rename", alias, f"-> {new}",
    )
    return {"ok": True, "restart_required": True}


@router.delete(
    "/api/commands/{command_id}/aliases/{alias}",
)
async def command_alias_del(command_id: str, alias: str):
    store = get_store()
    if not store.remove_command_alias(command_id, alias):
        raise HTTPException(404, "别名不存在")
    store.add_audit(
        "webui", "webui", "command_alias_del", alias, f"from {command_id}",
    )
    return {"ok": True, "restart_required": True}


class CommandToggleBody(BaseModel):
    enabled: bool


@router.patch("/api/commands/{command_id}")
async def command_toggle(command_id: str, body: CommandToggleBody):
    if command_id not in {node.id for node in TOP_LEVEL_COMMANDS}:
        raise HTTPException(404, "命令不存在")
    store = get_store()
    if body.enabled:
        store.enable_command(command_id)
    else:
        store.disable_command(command_id)
    store.add_audit("webui", "webui", "command_toggle", command_id,
                    "enable" if body.enabled else "disable")
    return {"ok": True}


# ---- 词库管理 ----

@router.get("/api/aliases")
async def aliases_view():
    al = rivendata.aliases()
    attrs = rivendata.attribute_catalog()
    weapons = rivendata.weapons()
    display = al.get("attribute_display", {})
    rev: dict[str, list[str]] = {}
    for alias, slug in al["attributes"].items():
        rev.setdefault(slug, []).append(alias)
    weapon_rev: dict[str, list[str]] = {}
    for alias, slug in al.get("weapons", {}).items():
        weapon_rev.setdefault(slug, []).append(alias)

    def ordered_attribute_aliases(slug: str) -> list[str]:
        shown = set(display.get(slug, []))
        return sorted(rev.get(slug, []), key=lambda x: (x not in shown, x))

    return {
        "attributes": [
            {
                "slug": slug,
                "name_zh": meta.get("name_zh"),
                "name_en": meta.get("name_en"),
                "builtin_aliases": rivendata.builtin_attribute_aliases(slug),
                "aliases": ordered_attribute_aliases(slug),
                "display": display.get(slug, []),
            } for slug, meta in attrs.items()
        ],
        "weapon_aliases": al.get("weapons", {}),
        "weapons_index": [
            {
                "slug": s,
                "zh": w.get("name_zh") or w.get("name_en") or s,
                "en": w.get("name_en") or s,
                "group": w.get("group") or "other",
                "aliases": sorted(weapon_rev.get(s, [])),
            }
            for s, w in weapons.items()
        ],
        "wildcards": al.get("wildcards", {}),
        "wildcard_categories": rivendata.WILDCARD_CATEGORY_ZH,
    }


class AttrAliasBody(BaseModel):
    alias: str
    slug: str
    show_in_list: bool = False


@router.post("/api/aliases/attribute")
async def attr_alias_add(body: AttrAliasBody):
    try:
        rivendata.add_attribute_alias(body.alias, body.slug, body.show_in_list)
    except ValueError as e:
        raise HTTPException(400, str(e))
    get_store().add_audit("webui", "webui", "alias_add", body.alias,
                          f"-> {body.slug}" + ("（展示）" if body.show_in_list else ""))
    return {"ok": True}


class AttrAliasUpdateBody(BaseModel):
    new_alias: str | None = None
    show_in_list: bool | None = None


@router.patch("/api/aliases/attribute/{alias}")
async def attr_alias_update(alias: str, body: AttrAliasUpdateBody):
    try:
        rivendata.update_attribute_alias(alias, body.new_alias, body.show_in_list)
    except KeyError:
        raise HTTPException(404, "别名不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    detail = []
    if body.new_alias and body.new_alias != alias:
        detail.append(f"改名 -> {body.new_alias}")
    if body.show_in_list is not None:
        detail.append("展示" if body.show_in_list else "隐藏")
    get_store().add_audit("webui", "webui", "alias_update", alias, "; ".join(detail))
    return {"ok": True}


@router.delete("/api/aliases/attribute/{alias}")
async def attr_alias_del(alias: str):
    if not rivendata.remove_attribute_alias(alias):
        raise HTTPException(404, "别名不存在")
    get_store().add_audit("webui", "webui", "alias_del", alias)
    return {"ok": True}


class WeaponAliasBody(BaseModel):
    alias: str
    slug: str


@router.post("/api/aliases/weapon")
async def weapon_alias_add(body: WeaponAliasBody):
    try:
        rivendata.add_weapon_alias(body.alias, body.slug)
    except ValueError as e:
        raise HTTPException(400, str(e))
    get_store().add_audit("webui", "webui", "weapon_alias_add", body.alias,
                          f"-> {body.slug}")
    return {"ok": True}


class WeaponAliasUpdateBody(BaseModel):
    new_alias: str | None = None
    slug: str | None = None


@router.patch("/api/aliases/weapon/{alias}")
async def weapon_alias_update(alias: str, body: WeaponAliasUpdateBody):
    try:
        rivendata.update_weapon_alias(alias, body.new_alias, body.slug)
    except KeyError:
        raise HTTPException(404, "武器别名不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    get_store().add_audit("webui", "webui", "weapon_alias_update", alias,
                          f"-> {body.new_alias or alias} / {body.slug or '目标不变'}")
    return {"ok": True}


@router.delete("/api/aliases/weapon/{alias}")
async def weapon_alias_del(alias: str):
    if not rivendata.remove_weapon_alias(alias):
        raise HTTPException(404, "别名不存在")
    get_store().add_audit("webui", "webui", "weapon_alias_del", alias)
    return {"ok": True}


class WildcardAliasBody(BaseModel):
    alias: str
    category: str


class WildcardAliasUpdateBody(BaseModel):
    new_alias: str | None = None
    category: str | None = None


@router.post("/api/aliases/category/wildcard")
async def wildcard_alias_add(body: WildcardAliasBody):
    try:
        rivendata.add_wildcard_alias(body.alias, body.category)
    except ValueError as e:
        raise HTTPException(400, str(e))
    get_store().add_audit("webui", "webui", "wildcard_alias_add", body.alias,
                          f"-> {body.category}")
    return {"ok": True}


@router.patch("/api/aliases/category/wildcard/{alias}")
async def wildcard_alias_update(
    alias: str, body: WildcardAliasUpdateBody,
):
    try:
        rivendata.update_wildcard_alias(
            alias, body.new_alias, body.category)
    except KeyError:
        raise HTTPException(404, "别名不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    get_store().add_audit("webui", "webui", "wildcard_alias_update", alias,
                          f"-> {body.new_alias or alias} / {body.category or '类别不变'}")
    return {"ok": True}


@router.delete("/api/aliases/category/wildcard/{alias}")
async def wildcard_alias_del(alias: str):
    if not rivendata.remove_wildcard_alias(alias):
        raise HTTPException(404, "别名不存在")
    get_store().add_audit("webui", "webui", "wildcard_alias_del", alias)
    return {"ok": True}


# ---- 卖家黑名单 ----

class BlacklistBody(BaseModel):
    seller: str
    scope: Literal["wm", "channel", "all"] = "wm"


@router.post("/api/targets/{gid}/blacklist")
@router.post("/api/groups/{gid}/blacklist")
async def blacklist_add(gid: int, body: BlacklistBody):
    _require_target_scope(gid)
    try:
        sellers = parse_blacklist_names(body.seller)
    except BlacklistInputError as error:
        if error.reason == "too_long":
            raise HTTPException(
                400,
                f"第 {error.line_number} 行卖家名超过 "
                f"{BLACKLIST_NICK_MAX_LENGTH} 个字符",
            ) from error
        raise HTTPException(400, "卖家名格式无效") from error
    store = get_store()
    scopes = ("wm", "channel") if body.scope == "all" else (body.scope,)
    added = 0
    for scope in scopes:
        for seller in sellers:
            if not store.add_blacklist(gid, seller, scope=scope):
                continue
            added += 1
            store.add_audit(
                "webui", "webui", f"{scope}_blacklist_add", seller,
                _scope_label(store, gid),
            )
    existing = len(sellers) * len(scopes) - added
    if len(sellers) == 1 and len(scopes) == 1 and not added:
        raise HTTPException(
            400, f"{sellers[0]} 已在 {body.scope} 黑名单里")
    return {"ok": True, "added": added, "existing": existing}


@router.delete("/api/targets/{gid}/blacklist/{seller}")
@router.delete("/api/groups/{gid}/blacklist/{seller}")
async def blacklist_del(
        gid: int, seller: str,
        scope: Literal["wm", "channel"] = "wm"):
    _require_target_scope(gid)
    seller = normalize_player_nick(seller)
    if contains_hidden_identifier(seller):
        raise HTTPException(400, "卖家名格式无效")
    store = get_store()
    if not store.remove_blacklist(gid, seller, scope=scope):
        raise HTTPException(404, f"{scope} 黑名单里没有该卖家")
    store.add_audit("webui", "webui", f"{scope}_blacklist_del", seller,
                    _scope_label(store, gid))
    return {"ok": True}


# ---- 捡漏管理 ----

@router.get("/api/bargain/params")
async def bargain_params_view():
    return {"params": bargain.params(), "defaults": bargain.DEFAULT_PARAMS}


class BargainParamsBody(BaseModel):
    params: dict


@router.put("/api/bargain/params")
async def bargain_params_save(body: BargainParamsBody):
    err = bargain.save_params(get_store(), body.params)
    if err:
        raise HTTPException(400, err)
    get_store().add_audit("webui", "webui", "bargain_params", "",
                          json.dumps(bargain.params(), ensure_ascii=False)[:200])
    return {"ok": True, "params": bargain.params()}


def _bargain_item_match(slug: str) -> dict:
    zh, en = marketdata.item_names(slug)
    max_rank = marketdata.item_max_rank(slug)
    return {
        "slug": slug,
        "zh": zh,
        "en": en,
        "max_rank": max_rank,
        "has_level": max_rank > 0,
    }


@router.get("/api/bargain/resolve")
async def bargain_resolve(q: str = "", kind: Literal["item", "riven"] = "item"):
    if not q.strip():
        matches = []
    elif kind == "item":
        matches = [_bargain_item_match(s) for s in marketdata.resolve_item(q)]
    else:
        slug = rivendata.resolve_weapon(q)
        matches = ([{
            "slug": slug,
            "zh": rivendata.weapon_name(slug, "zh"),
            "en": rivendata.weapon_name(slug, "en"),
        }] if slug else [])
    return {
        "matches": matches,
        "available": marketdata.available() if kind == "item" else bool(rivendata.weapons()),
    }


@router.get("/api/targets/{gid}/bargain")
@router.get("/api/groups/{gid}/bargain")
async def group_bargain(gid: int):
    platform_target = _require_target_scope(gid)
    store = get_store()
    items = []
    for row in store.list_bargain_items(gid):
        zh, en = marketdata.item_names(row["slug"])
        items.append({**_serialize_scope_row(row, platform_target),
                      "zh": zh, "en": en,
                      "display": marketdata.item_display_name(row["slug"]),
                      "max_rank": marketdata.item_max_rank(row["slug"])})
    riven_weapons = []
    for r in store.list_bargain_riven_items(gid):
        riven_weapons.append({
            **_serialize_scope_row(r, platform_target),
            "display": rivendata.weapon_display_name(r["weapon_slug"]),
        })
    return {
        "items": items,
        "riven_weapons": riven_weapons,
    }


class BargainItemBody(BaseModel):
    query: str
    threshold: int | None = Field(default=None, ge=5, le=90)
    level: Literal["0", "max"] | None = None


@router.post("/api/targets/{gid}/bargain-items")
@router.post("/api/groups/{gid}/bargain-items")
async def bargain_item_add(gid: int, body: BargainItemBody):
    _require_target_scope(gid)
    store = get_store()
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "道具名不能为空")
    matches = marketdata.resolve_item(query)
    if len(matches) != 1:
        raise HTTPException(400, "道具名称无法唯一匹配")
    slug = matches[0]
    level, level_error = bargain.validate_level_choice(slug, body.level)
    if level_error:
        raise HTTPException(400, level_error)
    text = slug
    if level is not None:
        text += f" {level}"
    if body.threshold is not None:
        text += f" {body.threshold}"
    ok, msg, iid = bargain.add_item_checked(store, get_config(), gid, text)
    if not ok:
        raise HTTPException(400, msg)
    row = store.get_bargain_item(iid, gid)
    store.add_audit("webui", "webui", "bargain_add",
                    f"{_scope_label(store, gid)}#{iid}",
                    bargain.describe_item(row))
    return {"ok": True, "message": msg, "id": iid}


class BargainItemUpdateBody(BaseModel):
    threshold: int | None = Field(default=None, ge=5, le=90)
    clear_threshold: bool = False    # true = 恢复全局默认
    level: Literal["0", "max"] | None = None


@router.patch("/api/targets/{gid}/bargain-items/{iid}")
@router.patch("/api/groups/{gid}/bargain-items/{iid}")
async def bargain_item_update(gid: int, iid: int, body: BargainItemUpdateBody):
    _require_target_scope(gid)
    store = get_store()
    row = store.get_bargain_item(iid, gid)
    if not row:
        raise HTTPException(404, "监控条目不存在")
    normalized_level = row.get("level")
    if "level" in body.model_fields_set:
        normalized_level, level_error = bargain.validate_level_choice(
            row["slug"], body.level)
        if level_error:
            raise HTTPException(400, level_error)
    detail = []
    if body.clear_threshold:
        store.set_bargain_item_threshold(iid, gid, None)
        detail.append("阈值恢复默认")
    elif body.threshold is not None:
        store.set_bargain_item_threshold(iid, gid, body.threshold / 100.0)
        detail.append(f"阈值{body.threshold}%")
    if "level" in body.model_fields_set:
        store.set_bargain_item_level(iid, gid, normalized_level)
        detail.append("等级" + ("0级" if normalized_level == "0" else
                              "满级" if normalized_level == "max" else "不适用"))
    if not detail:
        raise HTTPException(400, "没有需要修改的项")
    store.add_audit("webui", "webui", "bargain_item_update",
                    f"{_scope_label(store, gid)}#{iid}",
                    "; ".join(detail))
    return {"ok": True}


class BargainRivenBody(BaseModel):
    query: str
    threshold: int | None = Field(default=None, ge=5, le=90)


@router.post("/api/targets/{gid}/bargain-rivens")
@router.post("/api/groups/{gid}/bargain-rivens")
async def bargain_riven_add(gid: int, body: BargainRivenBody):
    _require_target_scope(gid)
    text = body.query.strip()
    if not text:
        raise HTTPException(400, "武器名不能为空")
    if body.threshold is not None:
        text += f" {body.threshold}"
    store = get_store()
    ok, msg, iid = bargain.add_riven_item_checked(
        store, get_config(), gid, text)
    if not ok:
        raise HTTPException(400, msg)
    row = store.get_bargain_riven_item(iid, gid)
    store.add_audit("webui", "webui", "bargain_riven_add",
                    f"{_scope_label(store, gid)}#{iid}",
                    bargain.describe_riven_item(row))
    runtime = get_bargain()
    if runtime is not None:
        runtime.request_riven_sample(row["weapon_slug"])
    return {"ok": True, "message": msg, "id": iid}


class BargainRivenUpdateBody(BaseModel):
    threshold: int | None = Field(default=None, ge=5, le=90)
    clear_threshold: bool = False


@router.patch("/api/targets/{gid}/bargain-rivens/{iid}")
@router.patch("/api/groups/{gid}/bargain-rivens/{iid}")
async def bargain_riven_update(gid: int, iid: int,
                               body: BargainRivenUpdateBody):
    _require_target_scope(gid)
    store = get_store()
    row = store.get_bargain_riven_item(iid, gid)
    if not row:
        raise HTTPException(404, "紫卡监控条目不存在")
    detail = []
    if body.clear_threshold:
        store.set_bargain_riven_item_threshold(iid, gid, None)
        detail.append("阈值恢复默认")
    elif body.threshold is not None:
        store.set_bargain_riven_item_threshold(
            iid, gid, body.threshold / 100.0)
        detail.append(f"阈值{body.threshold}%")
    if not detail:
        raise HTTPException(400, "没有需要修改的项")
    store.add_audit("webui", "webui", "bargain_riven_update",
                    f"{_scope_label(store, gid)}#{iid}",
                    "; ".join(detail))
    return {"ok": True}


@router.delete("/api/targets/{gid}/bargain-rivens/{iid}")
@router.delete("/api/groups/{gid}/bargain-rivens/{iid}")
async def bargain_riven_del(gid: int, iid: int):
    _require_target_scope(gid)
    store = get_store()
    row = store.get_bargain_riven_item(iid, gid)
    if not row:
        raise HTTPException(404, "紫卡监控条目不存在")
    store.delete_bargain_riven_item(iid, gid)
    store.add_audit("webui", "webui", "bargain_riven_del",
                    f"{_scope_label(store, gid)}#{iid}",
                    bargain.describe_riven_item(row))
    return {"ok": True}


@router.delete("/api/targets/{gid}/bargain-items/{iid}")
@router.delete("/api/groups/{gid}/bargain-items/{iid}")
async def bargain_item_del(gid: int, iid: int):
    _require_target_scope(gid)
    store = get_store()
    row = store.get_bargain_item(iid, gid)
    if not row:
        raise HTTPException(404, "监控条目不存在")
    store.delete_bargain_item(iid, gid)
    store.add_audit("webui", "webui", "bargain_del",
                    f"{_scope_label(store, gid)}#{iid}",
                    bargain.describe_item(row))
    return {"ok": True}


# ---- 配置预览与新建 ----

class TextBody(BaseModel):
    text: str


@router.post("/api/parse-preview")
async def parse_preview(body: TextBody):
    ok, msg = configops.preview_config(body.text)
    return {"ok": ok, "message": msg}


@router.post("/api/targets/{gid}/configs")
@router.post("/api/groups/{gid}/configs")
async def config_create(gid: int, body: TextBody):
    _require_target_scope(gid)
    ok, msg, cid = configops.add_config_checked(
        get_store(), get_config(), gid, body.text)
    if not ok:
        raise HTTPException(400, msg)
    saved = get_store().get_config(cid, gid)
    store = get_store()
    store.add_audit(
        "webui", "webui", "config_add",
        f"{_scope_label(store, gid)}#{saved['display_number']}",
        format_config_command(saved))
    return {"ok": True, "message": msg, "id": cid,
            "display_number": saved["display_number"],
            "command": format_config_command(saved)}


# ---- 系统操作 ----

@router.post("/api/system/poll-now")
async def poll_now():
    poller = get_poller()
    if poller is None:
        raise HTTPException(503, "轮询器未启动")
    if _poll_lock.locked():
        raise HTTPException(409, "已有轮询在执行中")
    async with _poll_lock:
        try:
            await poller._poll_once()
        except Exception as e:
            raise HTTPException(502, f"轮询失败: {e}")
    get_store().add_audit("webui", "webui", "poll_now")
    return {"ok": True, "poller": STATS.snapshot()}


class TestPushBody(BaseModel):
    scope_id: int = Field(
        validation_alias=AliasChoices("scope_id", "group_id"))


@router.post("/api/system/test-push")
async def test_push(body: TestPushBody):
    _require_delivery_scope(body.scope_id)
    poller = get_poller()
    if poller is None:
        raise HTTPException(503, "轮询器未启动")
    locale = get_store().get_target_preferences(body.scope_id)["locale"]
    poller.enqueue_delivery(poller.new_delivery(
        DeliverySource.SYSTEM,
        body.scope_id,
        texts.render("系统.测试推送", locale=locale, version=VERSION),
    ))
    store = get_store()
    store.add_audit("webui", "webui", "test_push",
                    _scope_label(store, body.scope_id))
    return {"ok": True, "queue_depth": _send_queue_depth(poller)}


@router.get("/api/system/backup")
async def backup_download():
    import tempfile

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    fd, tmp = tempfile.mkstemp(suffix=".db")
    import os as _os
    _os.close(fd)
    store = get_store()
    backup_task = asyncio.create_task(asyncio.to_thread(store.backup_to, tmp))
    try:
        await asyncio.shield(backup_task)
        store.add_audit("webui", "webui", "backup_download")
    except asyncio.CancelledError:
        await asyncio.gather(backup_task, return_exceptions=True)
        Path(tmp).unlink(missing_ok=True)
        raise
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    fname = f"rivensniper-backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(tmp, filename=fname, media_type="application/octet-stream",
                        background=BackgroundTask(
                            Path(tmp).unlink, missing_ok=True))


@router.post("/api/system/refresh-data")
async def refresh_data():
    if _refresh_lock.locked():
        raise HTTPException(409, "数据更新已在进行中")
    async with _refresh_lock:
        import os as _os
        import sys as _sys
        env = dict(_os.environ, PYTHONIOENCODING="utf-8")
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, str(_ROOT / "scripts" / "fetch_data.py"),
            cwd=str(_ROOT), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_DATA_REFRESH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await _terminate_subprocess(proc)
            raise HTTPException(504, "数据更新超时（120s）")
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_subprocess(proc))
            raise
    text = out.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise HTTPException(502, f"数据更新失败:\n{text[-600:]}")
    rivendata.invalidate_caches()
    marketdata.invalidate()
    get_store().add_audit("webui", "webui", "refresh_data", "",
                          text.strip().splitlines()[0] if text.strip() else "")
    return {"ok": True, "output": text[-800:]}


@router.post("/api/system/restart")
async def restart():
    get_store().add_audit("webui", "webui", "restart")
    asyncio.get_running_loop().call_later(0.5, _request_graceful_shutdown)
    return {"ok": True, "message": "进程将在 0.5 秒后开始退出；"
            "由 systemd/nssm 守护会自动拉起，手动运行需自行再启动"}


@router.get("/api/settings")
async def settings_view():
    cfg = get_config()
    return {
        "hot": {
            "wm_fast_interval": getattr(cfg, "wm_fast_interval", 2.0),
            "sniper_poll_interval": cfg.sniper_poll_interval,
            "sniper_send_interval": cfg.sniper_send_interval,
            "sniper_send_concurrency": getattr(
                cfg, "sniper_send_concurrency", 0),
            "trade_message_ttl_seconds": getattr(
                cfg, "trade_message_ttl_seconds", 60.0),
            "sniper_max_configs_per_group": cfg.sniper_max_configs_per_group,
            "bargain_max_distinct_slugs": cfg.bargain_max_distinct_slugs,
        },
        "readonly": {
            "sniper_dry_run": cfg.sniper_dry_run,
            "irc_riven_base_dedupe_hours": 1,
        },
    }


class SettingsBody(BaseModel):
    wm_fast_interval: float | None = Field(default=None, ge=1, le=60)
    sniper_poll_interval: float | None = None
    sniper_send_interval: float | None = None
    sniper_send_concurrency: int | None = None
    trade_message_ttl_seconds: float | None = None
    sniper_max_configs_per_group: int | None = None
    bargain_max_distinct_slugs: int | None = None


@router.patch("/api/settings")
async def settings_update(body: SettingsBody):
    cfg = get_config()
    changes: dict[str, str] = {}
    updates: dict[str, object] = {}
    if body.wm_fast_interval is not None:
        updates["wm_fast_interval"] = body.wm_fast_interval
        changes["WM_FAST_INTERVAL"] = str(body.wm_fast_interval)
    if body.sniper_poll_interval is not None:
        if not (1 <= body.sniper_poll_interval <= 3600):
            raise HTTPException(400, "轮询间隔需在 1~3600 秒之间")
        updates["sniper_poll_interval"] = body.sniper_poll_interval
        changes["SNIPER_POLL_INTERVAL"] = str(body.sniper_poll_interval)
    if body.sniper_send_interval is not None:
        if not (0 <= body.sniper_send_interval <= 60):
            raise HTTPException(400, "发送间隔需在 0~60 秒之间")
        updates["sniper_send_interval"] = body.sniper_send_interval
        changes["SNIPER_SEND_INTERVAL"] = str(body.sniper_send_interval)
    if body.sniper_send_concurrency is not None:
        if body.sniper_send_concurrency < 0:
            raise HTTPException(
                400,
                "主通道发送并发上限不能小于 0（0 表示不额外限制）",
            )
        updates["sniper_send_concurrency"] = body.sniper_send_concurrency
        changes["SNIPER_SEND_CONCURRENCY"] = str(
            body.sniper_send_concurrency)
    if body.trade_message_ttl_seconds is not None:
        if not (0 < body.trade_message_ttl_seconds <= 3600):
            raise HTTPException(400, "消息 TTL 需大于 0 且不超过 3600 秒")
        updates["trade_message_ttl_seconds"] = body.trade_message_ttl_seconds
        changes["TRADE_MESSAGE_TTL_SECONDS"] = str(
            body.trade_message_ttl_seconds)
    if body.sniper_max_configs_per_group is not None:
        if body.sniper_max_configs_per_group < 0:
            raise HTTPException(400, "配置上限不能小于 0（0 表示不限）")
        updates["sniper_max_configs_per_group"] = (
            body.sniper_max_configs_per_group
        )
        changes["SNIPER_MAX_CONFIGS_PER_GROUP"] = str(body.sniper_max_configs_per_group)
    if body.bargain_max_distinct_slugs is not None:
        if body.bargain_max_distinct_slugs < 0:
            raise HTTPException(
                400, "全局不同捡漏监控目标上限不能小于 0（0 表示不限）")
        updates["bargain_max_distinct_slugs"] = (
            body.bargain_max_distinct_slugs
        )
        changes["BARGAIN_MAX_DISTINCT_SLUGS"] = str(
            body.bargain_max_distinct_slugs)
    if not changes:
        raise HTTPException(400, "没有需要修改的项")

    # 所有字段完成校验且 .env 成功写入后，再统一更新运行态，避免接口
    # 返回失败时部分配置已经热生效。
    envutil.update_env_file(changes)
    for field, value in updates.items():
        setattr(cfg, field, value)
    if "wm_fast_interval" in updates:
        poller = get_poller()
        if poller is not None:
            poller.fast.wakeup()
    if updates.keys() & {
            "sniper_poll_interval", "sniper_send_interval",
            "sniper_send_concurrency"}:
        poller = get_poller()
        if poller is not None:
            await poller.notify_runtime_settings_changed()
    get_store().add_audit("webui", "webui", "settings_update", "",
                          "; ".join(f"{k}={v}" for k, v in changes.items()))
    return {"ok": True, "applied": changes}
