import nonebot
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="riven_sniper",
    description="Warframe 紫卡与道具监控、游戏频道采集及多目标推送",
    usage="仅响应已启用目标的所有者命令",
    config=Config,
)

# 仅在 NoneBot 环境下注册命令和轮询（单元测试直接 import 子模块时跳过）
try:
    _driver = nonebot.get_driver()
except ValueError:
    _driver = None

if _driver is not None:
    from nonebot import logger

    from . import bargain, shared
    from .shared import get_config, get_store
    from .stats import install_log_sink
    from .runtime_info import runtime_identity, write_bot_state

    # 命令注册前载入捡漏参数（触发名和别名在 commands 导入时读取）
    bargain.load_params(get_store())

    # 默认别名首启注入可编辑的 command_aliases 表（必须早于 commands 导入）
    from .command_meta import seed_base_aliases
    seed_base_aliases(get_store())

    from . import commands, discord_bot  # noqa: F401  注册命令
    from .bargainpoller import BargainPoller
    from .irc_feed import IrcChatFeed
    from .poller import SniperPoller
    from .webui import mount as _mount_webui

    install_log_sink()
    _mount_webui()

    _poller: SniperPoller | None = None
    _bargain_poller: BargainPoller | None = None
    _irc_feed: IrcChatFeed | None = None
    _bot_identity = runtime_identity()

    @_driver.on_startup
    async def _start_poller():
        global _poller, _bargain_poller, _irc_feed
        write_bot_state("running", _bot_identity)
        cfg = get_config()
        store = get_store()
        active_platforms = (
            ("discord",) if cfg.discord_dm_enabled else ()
        )
        scopes = store.active_delivery_scope_ids(
            platforms=active_platforms)
        store.bootstrap_target_preferences(scopes)
        # 先建统一发送队列，再启动捡漏与 IRC 文件读取。
        _poller = SniperPoller(get_store(), get_config())
        shared.set_poller(_poller)
        _poller.start()
        _bargain_poller = BargainPoller(
            get_store(), get_config(), _poller)
        shared.set_bargain(_bargain_poller)
        _bargain_poller.start()
        # 游戏 IRC 旁路（独立采集器分槽 JSONL；兼容旧的单文件研究输入）
        if getattr(cfg, "irc_feed_enabled", False):
            _irc_feed = IrcChatFeed(cfg, _poller)
            _irc_feed.start()

    @_driver.on_shutdown
    async def _stop_poller():
        global _poller, _bargain_poller, _irc_feed
        try:
            try:
                if _irc_feed is not None:
                    await _irc_feed.stop()
            finally:
                try:
                    if _bargain_poller is not None:
                        await _bargain_poller.stop()
                finally:
                    if _poller is not None:
                        await _poller.stop()
        finally:
            _irc_feed = None
            _bargain_poller = None
            _poller = None
            shared.set_bargain(None)
            shared.set_poller(None)
            try:
                shared.close_store()
            finally:
                write_bot_state("stopped", _bot_identity)

    # QQ 掉线/重连可观测性：WARNING 级会出现在 WebUI 总览的「最近告警」里
    @_driver.on_bot_connect
    async def _on_connect(bot):
        adapter = bot.adapter.get_name()
        logger.info("{} 已连接: {}", adapter, bot.self_id)
        if adapter == "Discord" and _poller is not None:
            _poller.discord_bot_connected(bot)

    @_driver.on_bot_disconnect
    async def _on_disconnect(bot):
        adapter = bot.adapter.get_name()
        if adapter == "Discord" and _poller is not None:
            _poller.discord_bot_disconnected(bot)
        if adapter == "OneBot V11":
            logger.warning("OneBot 连接断开: {}；QQ 协议端需按自身配置恢复连接；"
                           "掉线期间的时效消息会直接丢弃", bot.self_id)
        elif adapter == "Discord":
            logger.warning(
                "Discord 连接断开: {}；未调用 API 的时效消息将在 60 秒窗口内暂存",
                bot.self_id,
            )
        else:
            logger.warning("{} 连接断开: {}", adapter, bot.self_id)

    # Discord 适配器在发送 Resume 后即可触发 on_bot_connect；只有真正收到
    # Ready/Resumed 才能确认 Gateway 会话可用并释放断线暂存消息。
    from nonebot.adapters.discord.event import ReadyEvent, ResumedEvent

    _discord_gateway_ready = nonebot.on_metaevent(priority=4, block=False)

    @_discord_gateway_ready.handle()
    async def _on_discord_gateway_ready(bot, event):
        if (isinstance(event, (ReadyEvent, ResumedEvent))
                and _poller is not None):
            _poller.discord_session_ready(bot)

    # 账号被踢下线（KickedOffLine）时反向 WS 仍连着，on_bot_disconnect 不触发，
    # 发送会持续超时并被逐条丢弃。用 OneBot 心跳的 status.online 感知这种
    # “WS 在、账号离线”的僵死态：仅在状态翻转时记日志（心跳每 30s 一次），
    # ERROR 级会在 WebUI 总览「最近告警」高亮，提示需重新登录小号。
    from nonebot.adapters.onebot.v11 import HeartbeatMetaEvent

    _heartbeat = nonebot.on_metaevent(priority=5, block=False)

    @_heartbeat.handle()
    async def _on_heartbeat(event: HeartbeatMetaEvent):
        online = bool(getattr(event.status, "online", False))
        prev, _ = shared.bot_online_state()
        shared.set_bot_online(online)
        if prev == online:
            return
        if online:
            if prev is False:
                logger.info("QQ 账号已恢复在线，新到消息可继续发送")
        else:
            logger.error("QQ 账号离线（心跳 online=false，可能被踢下线/登录失效）："
                         "OneBot 反向 WS 仍连接但无法发送，请重新登录小号；"
                         "掉线期间的时效消息会直接丢弃且不会补发")
