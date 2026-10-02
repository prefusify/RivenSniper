from pydantic import BaseModel, Field


class Config(BaseModel):
    sniper_poll_interval: float = Field(default=15.0, ge=1, le=3600)
    sniper_dry_run: bool = False
    # 仅具体武器 + 三个明确正词条；每个目标另有默认关闭的开关。
    wm_fast_interval: float = Field(default=2.0, ge=1, le=60)
    # 代理及可选 SSH 转发配置，凭据保存在运行目录，不能进入源码。
    wm_fast_proxy_config: str = ".runtime/wm_fast_proxy.json"
    # 0 = 不限制每群狙击配置数量。
    sniper_max_configs_per_group: int = Field(default=0, ge=0)
    # 0 = 发送成功后不额外等待；同一目标内始终串行。
    sniper_send_interval: float = Field(default=0.0, ge=0, le=60)
    # 主通道不同目标的全局并发上限；0 = 不额外限制。
    sniper_send_concurrency: int = Field(default=0, ge=0)
    # 所有交易来源共用的内存发送队列；满时淘汰队首最旧消息。
    send_queue_maxsize: int = Field(default=1000, gt=0, le=100_000)
    # 消息从首次入队起的最大有效时间。过期消息不会发送或重试。
    trade_message_ttl_seconds: float = Field(default=60.0, gt=0, le=3600)
    # 只有实际调用发送 API 后发生瞬时网络错误才重试。
    send_max_retries: int = Field(default=1, ge=0, le=10)
    send_retry_delay_seconds: float = Field(default=2.0, ge=0, le=60)
    # 所有 Bot 目标共用的全局不同监控 slug 上限；0 = 不限制。
    bargain_max_distinct_slugs: int = Field(default=0, ge=0)
    # ---- Discord 官方 Bot（仅私聊命令与推送）----
    # Bot token 由 NoneBot Discord 适配器的 DISCORD_BOTS 配置提供。
    discord_dm_enabled: bool = False

    # ---- 游戏 IRC 旁路喂送 ----
    irc_feed_enabled: bool = False
    # 兼容单文件输入；irc_feed_dir 非空时优先读取当前采集拓扑的分槽文件。
    irc_feed_path: str = ""
    # 独立采集器 feed 目录，按控制文件读取当天 A-D 或 A-Q 分槽 JSONL。
    irc_feed_dir: str = ""
    # 跨重启持久化的完整行游标；留空时放在 feed 目录旁。
    irc_feed_checkpoint_path: str = ""
    # 轮询 jsonl 间隔（秒）
    irc_feed_interval: float = Field(default=0.2, gt=0)
    # 文件超过该秒数未写入时标记频道采集停更
    irc_feed_stale_seconds: float = Field(default=180.0, gt=0)
    # 留空时使用 feed 目录同级的 track.db。
    irc_track_db_path: str = ""
    # 已被全部游标消费的每日 JSONL 保留天数；0 = 不自动清理。
    irc_feed_retention_days: int = Field(default=7, ge=0, le=3650)
    # presence 原始事件与已结束会话保留天数；玩家、紫卡和归属永久保留。
    irc_presence_retention_days: int = Field(default=7, ge=1, le=3650)
