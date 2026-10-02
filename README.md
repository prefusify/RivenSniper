# RivenSniper

简体中文 | [English](README.en.md)

**Warframe 紫卡与道具监控、游戏频道采集及 QQ / Discord 推送工具。**

版本 **9.0.2** · **Windows 11 桌面端** · **Python 3.12** · [MIT License](LICENSE)

RivenSniper 根据你设置的武器、词条、评级和价格条件筛选交易信息，将命中结果发送到 QQ 群或 Discord 私聊。它通过 warframe.market 接口获取市场信息，也支持读取独立采集器提供的游戏频道消息。管理控制台与 Bot 在同一台电脑上运行。

项目用于查找和提醒交易机会；买卖沟通和交易由使用者自行完成。价格判断使用下文说明的统计基准，不代表保证成交价。

## 目录

- [用途与功能](#用途与功能)
- [前置要求](#前置要求)
- [下载与启动](#下载与启动)
- [管理控制台](#管理控制台)
- [配置 Discord 私聊](#配置-discord-私聊)
- [启用游戏频道采集](#启用游戏频道采集)
- [启用 WM 快速获取](#启用-wm-快速获取)
- [常用命令](#常用命令)
- [配置与本地数据](#配置与本地数据)
- [运行行为与排障](#运行行为与排障)
- [源码与测试](#源码与测试)
- [许可证与数据来源](#许可证与数据来源)

## 用途与功能

| 功能 | 说明 |
| --- | --- |
| 紫卡条件筛选 | 按具体武器或武器类型、2/3 个正词条、有无负词条、洗练条件和最低评级筛选；支持同一词条位置的 OR 条件 |
| WM 挂单提醒 | 轮询 warframe.market 紫卡拍卖；首次建立存量基线，之后匹配新出现的挂单 |
| WM 快速获取 | 为“具体武器 + 三个完全指定正词条”增加并行搜索通道；普通轮询同时运行，按目标与挂单统一去重 |
| 普通道具捡漏 | 监听市场实时新建订单，与最新成交统计日桶均价比较，按设置的折扣提醒 |
| 紫卡捡漏 | 按武器建立本地小时价格样本，以滚动均价判断低价直售挂单 |
| 游戏频道采集 | 通过四槽或十七槽独立采集器接收频道信息，只保存包含完整 OMG 紫卡链接的聊天消息；离线解析武器、词条和数值 |
| 频道记录查询 | 查询昵称在已订阅频道出现过的紫卡、紫卡持有者观测时间线，以及频道可见状态变化 |
| 多目标推送 | 支持 QQ 群与 Discord 私聊，每个目标分别配置规则、语言、黑名单和启用状态 |
| 管理控制台 | 管理推送目标、狙击规则、捡漏项、频道开关、命令别名、运行参数、词库及日志 |
| 卡片与评级 | 生成紫卡展示内容和评级；Discord 使用 Rich Embed / ANSI 文本，QQ 根据消息来源采用卡图或文本 |

捡漏计算细节见 [捡漏算法](docs/bargain.md)，OMG 链接解析见 [链接解码说明](docs/riven-link.md)。

## 前置要求

| 场景 | 需要准备 |
| --- | --- |
| 基础运行 | Windows 11 桌面电脑、可访问依赖下载源及 warframe.market 的网络、完整解压后的项目目录 |
| 启动环境 | uv 和 Python 3.12；脚本会检查 uv、在需要时询问通过 winget 安装，并准备 Python 与锁定依赖 |
| QQ 群推送 | 已登录的 SnowLuma、可用的 OneBot v11 反向 WebSocket 客户端和 HTTP Server；Bot 已加入目标 QQ 群 |
| QQ 目标配置 | 接收群号、该群唯一所有者的 QQ 号；每个目标仅接受对应所有者发送的命令 |
| Discord 私聊 | 自行创建的 Discord 应用与 Bot Token、接收者用户 ID，以及允许该 Bot 向接收者发送私聊的 Discord 设置 |
| 游戏频道采集 | 本机 Warframe 官网独立版或 Steam 版；四槽或十七槽所需的不同账号/昵称；当前客户端适用的 `psk_current.bin`；账号已完成设备验证 |
| WM 快速获取 | 可用的 HTTP CONNECT 代理；使用随附 VPS 方案时，还需要已分配 IPv6 前缀的 Ubuntu amd64 VPS、Windows OpenSSH 客户端和可用的 SSH 认证 |

QQ 协议端、Discord Token、游戏账号、认证密钥和代理均由使用者自行准备，不包含在源码包中。只使用普通 WFM 监控时，无需启动游戏频道采集器，也无需配置快速获取代理。

管理界面按桌面浏览器、鼠标和键盘使用；完整运行环境以 Windows 为准。

## 下载与启动

### 1. 下载完整项目

在本仓库的 **Releases** 页面下载 `RivenSniper-9.0.2.zip` 并完整解压，也可使用 **Code → Download ZIP**。请保留目录结构，不要只复制 `.cmd` 文件到桌面。

目录内应至少包含 `bot.py`、`pyproject.toml`、`uv.lock`、`.env.example`、`src/`、`scripts/`、`data/` 和两个启动脚本。

### 2. 使用 QQ 一键启动

1. 先安装并登录 SnowLuma，让该 QQ 账号加入接收推送的群。
2. 双击 **`启动BOT.cmd`**。
3. 首次运行按提示准备 uv、Python 3.12 和项目依赖。已有环境会复用。
4. 输入接收推送的 QQ 群号和这个目标唯一所有者的 QQ 号。
5. 启动器生成配置并显示 OneBot 连接信息。在 SnowLuma 中启用下列两条通道，鉴权 Token 都使用启动器显示的同一个值。

| SnowLuma 配置项 | 默认值 |
| --- | --- |
| OneBot v11 反向 WebSocket | `ws://127.0.0.1:8180/onebot/v11/ws` |
| HTTP Server / API | `http://127.0.0.1:3000/` |
| 两条通道的 Token | 使用启动器生成的值 |

6. 完成协议端配置后，在启动器按回车启动 Bot。
7. 用本机桌面浏览器打开 **`http://127.0.0.1:8180/admin`**，确认连接状态，检查目标已启用并添加筛选规则。
8. 保持 Bot 窗口和 SnowLuma 运行；停止 Bot 时在其窗口按 `Ctrl+C`。

之后再次双击同一个脚本即可。已有配置和规则不会被启动向导覆盖。若更改 `.env` 中的 `PORT`，浏览器地址与 SnowLuma 反向 WebSocket 地址也要使用新端口。

反向 WebSocket 接收入站事件；Bot 发出的 OneBot 操作通过 HTTP API 完成，因此两条通道都必须配置。

### 3. 手动启动

已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 时，在项目目录打开 PowerShell：

```powershell
uv python install 3.12
uv sync --locked
Copy-Item .env.example .env
```

`Copy-Item` 只在首次配置、目录中还没有 `.env` 时执行。编辑 `.env`，将 `ONEBOT_ACCESS_TOKEN` 改为自己的随机字符串；QQ 场景同时配置 SnowLuma。然后运行：

```powershell
uv run --locked python bot.py
```

仅使用 Discord 时可采用手动方式启动，保留示例中的基础 OneBot 配置项，不必创建 QQ 目标；QQ 一键启动器会要求创建一个 QQ 群目标。

## 管理控制台

默认地址：**`http://127.0.0.1:8180/admin`**。

控制台使用顺序：

1. 在“推送目标”中检查或创建目标，填写 QQ 群及所有者，或 Discord 用户 ID，并启用目标。
2. 设置目标的回复语言、有效期、游戏频道消息开关和 WM 快速获取开关。
3. 在狙击规则页面选择目标，添加武器、词条、最低评级和洗练条件。
4. 需要低价提醒时，在捡漏页面添加普通道具或紫卡监控项。
5. 在系统状态和日志页面查看连接、采集、查询和推送状态；用测试推送确认接收链路。

新目标的频道消息与 WM 快速获取默认关闭。目标可永久启用或设置有效期，到期后停止处理命令与推送。

控制台是本机管理入口，没有独立的网页登录认证。请保持 `.env` 的 `HOST=127.0.0.1`，在运行 Bot 的电脑上访问。

## 配置 Discord 私聊

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用及 Bot，取得自己的 Bot Token。
2. 在 `.env` 中设置 `DISCORD_DM_ENABLED=true`。
3. 将 `DISCORD_BOTS=[]` 替换为 `.env.example` 中给出的完整配置，把令牌占位符换成自己的 Token。保留示例中的 intents：启用 `direct_messages`，关闭服务器事件和应用命令。
4. 按 Discord 的 Bot 安装方式让 Bot 与接收者具备私聊条件；检查共同服务器及接收者的私信设置。复制接收者的用户 ID，需要时在 Discord“用户设置 → 高级”开启开发者模式。
5. 重启 Bot，在本机管理控制台创建并启用对应的 Discord 私聊目标。
6. 从该用户私聊 Bot 使用命令，或在控制台配置规则并发送测试消息。

Discord 入口只处理私聊，不注册服务器命令。QQ 与 Discord 可以同时使用，各自使用独立目标规则。

## 启用游戏频道采集

频道采集是独立进程。Bot 读取采集器写入的本地数据；同时部署时先配置采集器，再启动 Bot。

### 四槽与十七槽

| 模式 | 账号/昵称数量 | 频道分配与成员状态 |
| --- | --- | --- |
| 四槽，默认 | 4 个互不相同的账号/昵称 | 跨地区分片；每次连接后执行一轮限速成员查询 |
| 十七槽 | 17 个互不相同的账号/昵称 | 每个地区一个槽，订阅该地区 G/Q/R/T；不在启动时查询成员，使用实时进出事件 |

昵称必须与游戏内一致。频道分配见 `configs/chat_collector_shards.json` 和 `configs/chat_collector_shards_17.json`。

### 配置和启动

1. 双击 **`启动聊天采集.cmd`**，使用默认四槽；首次直接使用十七槽时，在 PowerShell 执行：

   ```powershell
   .\启动聊天采集.cmd -CollectorMode 17
   ```

2. 按提示填写当前模式的游戏昵称。
3. 准备当前客户端适用的 `psk_current.bin`，放入启动器打开的目录。该认证材料不随仓库分发，缺少它时无法完成采集连接。
4. 验证通过后，保持独立的“聊天采集监督器”窗口运行。
5. 退出正在运行的 Warframe 和启动器，选择菜单 **1**，为尚未运行的槽自动登录并取票。
6. 首次按提示输入各槽账号和密码。密码不回显；凭据使用当前 Windows 用户的 DPAPI 加密保存在本机。脚本依次启动游戏、登录、获取连接认证，成功后关闭该次游戏并继续下一槽。
7. 确认所需槽位显示运行中，输入 **0** 关闭向导。监督器仍须保持运行。
8. 启动或重启 Bot，在管理控制台为接收目标开启频道消息，并设置能命中的狙击规则。

自动登录支持官网独立版与 Steam 版，同时存在时优先官网独立版。运行时保持游戏和启动器可见，避免最小化、遮挡或抢占焦点。游戏客户区至少为 480×270，启动器客户区至少为 700×400。自动流程临时改动的 `EE.cfg` 会在完成或失败恢复时还原。

### 采集器菜单

| 菜单 | 操作 |
| --- | --- |
| 1 | 自动登录并为未运行的槽取票 |
| 2 | 刷新状态 |
| 3 | 打开昵称配置 |
| 4 | 正常停止全部采集 |
| 5 | 停止后切换四槽 / 十七槽模式 |
| 6 | 为指定运行中槽执行一次成员快照 |
| 7 | 手动逐槽取票 |
| 8 | 配置或更新自动登录凭据 |
| 0 | 仅关闭向导，保留监督器运行 |

每个槽保存含完整 OMG 链接的消息；普通聊天和普通物品链接会被过滤。频道进出事件用于记录在订阅频道中的可见状态，不代表全游戏在线状态。

断线后 worker 在认证有效期内有界重连；进程退出或认证材料失效后需重新取票。断线消息不会回放。停止采集请使用菜单 4。

## 启用 WM 快速获取

快速获取适用于 **具体武器 + 三个明确正词条**。含武器类型、两正词条或任意正词条的规则仍使用普通轮询。三个位置的显式 OR 条件会展开为共享查询，负词条、洗练、评级和黑名单继续在本地判断。

1. 准备 HTTP CONNECT 代理，在本机创建 `.runtime/wm_fast_proxy.json`。
2. 在管理控制台“推送目标”中为需要的目标开启 WM 快速获取。
3. 在系统设置调整查询间隔；默认 2 秒，可设置 1～60 秒。

代理配置示例：

```json
{
  "proxies": [
    "http://proxy-user:REPLACE_WITH_PASSWORD@127.0.0.1:23990"
  ],
  "ssh": {
    "host": "vps.example.com",
    "port": 22,
    "user": "root",
    "identity_file": "C:/path/to/private-key",
    "local_port": 23990,
    "remote_port": 23990
  }
}
```

这些值都是占位示例。直接使用独立 HTTP 代理时可省略 `ssh` 区段，填写实际代理 URL。使用 SSH 转发时，本机须具备 `ssh`、可用私钥及服务器的 `known_hosts` 记录；配置变化后重启 Bot。

随附的 `scripts/provision_wm_proxy.py` 可为有已分配 IPv6 `/64` 的 Ubuntu amd64 VPS 配置出口服务。在该 VPS 上以 root 运行：

```bash
python3 provision_wm_proxy.py --prefix <已分配的IPv6前缀/64> --interface eth0 --count 512
```

脚本安装固定版本的 3proxy、配置地址及 systemd 服务，并生成 `/etc/rivensniper-wm/client.json`。将其中代理配置保存在自己电脑的私有配置文件，再补充 SSH 区段。服务监听 VPS 回环地址；VPS 只提供网络出口，搜索和推送在 Windows Bot 执行。

配置项 `WM_FAST_PROXY_CONFIG` 默认指向 `.runtime/wm_fast_proxy.json`。代理缺失不影响普通轮询。查询结果达到 500 条时，该查询退回普通轮询。每出口至少间隔 6.5 秒，遇到 429 会遵循冷却与退避；设置的查询间隔不等于挂单发现延迟。快速获取不加速捡漏，也不提供历史补推。

## 常用命令

命令由 QQ 目标所有者在对应群发送，或由 Discord 目标用户私聊 Bot。非所有者和停用目标的命令会被忽略。下面是新安装的默认英文入口；别名可在控制台修改，修改后重启生效。

| 示例 | 用途 |
| --- | --- |
| `s Torid cc@A ms@B+ -z@A` | 添加紫卡规则：正词条及负词条使用评级下限 |
| `sl` / `sd 3` | 列出规则 / 删除编号 3 的规则 |
| `st` | 查看词条全名和可用简写 |
| `bl WM` / `bl Channel` | 查看对应来源的卖家黑名单 |
| `b WM ExampleSeller` | 将示例卖家加入 WM 黑名单；支持换行批量输入 |
| `bd Channel ExampleSeller` | 从频道黑名单移除示例卖家 |
| `d Arcane Grace 0 25` | 监控 0 级道具，价格低于参考价至少 25% 时提醒 |
| `dl` / `dd 3` | 查看 / 删除道具捡漏项 |
| `rd Torid 30` | 添加紫卡捡漏，低于该武器参考价至少 30% 时提醒 |
| `rdl` / `rdd 3` | 查看 / 删除紫卡捡漏项 |
| `w ExamplePlayer page 2` / `w ExamplePlayer all` | 查询示例昵称在已采集频道中出现的紫卡；分页或导出 |
| `rh 3 page 2` / `rh 3 all` | 查询编号 3 紫卡的持有者观测记录 |
| `t ExamplePlayer` | 登记该昵称在订阅频道中的进入、离开及地区变化提醒 |
| `tl` / `td 3` | 查看 / 删除频道提醒 |
| `cd` / `cd 24` | 查看 / 设置当前目标的频道去重时长，范围 1～72 小时 |

规则中正词条须填写 2 或 3 个；不写负词条表示要求无负词条，`-any` 表示任意负词条。`/` 连接同一位置的 OR 备选。`@S`、`@A+`、`@A`、`@A-`、`@B+`、`@B`、`@B-`、`@C+`、`@C`、`@C-`、`@F` 表示最低评级，无法评分的 `X` / `?` 不满足评级下限。

频道相关查询需要目标已启用频道消息。持有者记录表示采集到的频道观测，不等同于交易成交记录或当前库存。黑名单只过滤对应来源的紫卡狙击，不过滤普通道具或紫卡捡漏。

## 配置与本地数据

`.env.example` 给出完整配置示例。常用项如下：

| 配置 | 默认值 / 用途 |
| --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8180`，Bot 与本机管理界面的监听地址 |
| `ONEBOT_ACCESS_TOKEN` | 与 SnowLuma 两条通道一致的随机 Token |
| `ONEBOT_API_ROOTS` | SnowLuma HTTP API 地址映射 |
| `SNIPER_POLL_INTERVAL` | `15.0`，普通紫卡轮询秒数 |
| `WM_FAST_INTERVAL` | `2.0`，快速查询目标间隔秒数 |
| `WM_FAST_PROXY_CONFIG` | `.runtime/wm_fast_proxy.json`，私有代理配置位置 |
| `TRADE_MESSAGE_TTL_SECONDS` | `60`，时效消息有效期秒数 |
| `SEND_QUEUE_MAXSIZE` | `1000`，内存消息队列容量 |
| `DISCORD_DM_ENABLED` / `DISCORD_BOTS` | Discord 私聊开关及 Bot 配置 |
| `IRC_FEED_ENABLED` / `IRC_FEED_DIR` | 频道数据读取开关和目录，由采集启动器写入 |
| `IRC_FEED_RETENTION_DAYS` | `7`，已被全部游标消费的频道 JSONL 保留天数 |
| `IRC_PRESENCE_RETENTION_DAYS` | `7`，原始进出事件与已结束可见时段保留天数 |

规则、目标和捡漏参数主要在控制台维护；修改 `.env` 后重启 Bot。

| 路径 | 内容 |
| --- | --- |
| `.env` | 本机运行配置及 Token |
| `sniper.db` | 目标、规则、黑名单、价格样本和去重状态 |
| `.runtime/chat_collector/` | 昵称配置、加密凭据、认证材料、采集状态、频道数据和追踪库 |
| `.runtime/logs/bot.log` | Bot 日志 |
| `.runtime/chat_collector/logs/` | 采集器日志 |
| `data/` | 受版本管理的游戏武器、词条、市场目录、索引及消息文案 |

追踪库中的玩家、紫卡和持有者观测记录长期保留；原始事件的保留期不代表这些记录也会清除。运行目录、账号资料、日志和数据库只存放在使用者本机，不属于源码发布内容。

中英文 Bot 文案保存在 `data/bot_texts.json`。修改时保留键名和 `{name}` 等占位符；重启后生效。

## 运行行为与排障

- **刚启动没有旧订单提醒**：普通紫卡轮询和快速获取先建立存量基线，只处理之后的新挂单；道具捡漏只处理实时收到的新建订单。
- **已错过的消息没有补发**：队列有容量和有效期限制，离线、超时或队列溢出可导致丢弃；没有断线历史回放。
- **同一张卡不重复推送**：频道有一小时的全局基础去重，目标可设置 1～72 小时；持续出现会刷新观测时间。
- **QQ 收不到消息**：检查 SnowLuma 登录状态、群成员关系、HTTP API 和反向 WebSocket 的地址/Token，以及目标启用状态和筛选规则。
- **Discord 收不到消息**：检查 Token、私聊开关、目标用户 ID、Bot 连接状态及接收者私信设置。
- **采集正常但目标无频道提醒**：重启 Bot 加载采集配置，检查目标频道开关及狙击规则。
- **找不到 uv / winget**：在 Windows 中安装或更新“应用安装程序”，或按 uv 官方文档安装后重新打开启动脚本。
- **端口被占用**：先检查旧 Bot 是否仍运行；改动 `PORT` 后同步修改浏览器和协议端地址。
- **游戏登录或取票失败**：检查账号设备验证、客户端对应认证材料、当前 Windows 用户与游戏权限；用菜单 8 更新凭据，或菜单 7 手动取票。
- **启动提示数据库版本不兼容**：保留原数据库与备份；程序只接受明确支持的版本并在迁移前备份，不能用清空数据库代替迁移。

日志和启动窗口可能含账号或 Token。分享排障资料前，应遮盖账号、群号、昵称、令牌、代理地址及个人路径。

## 源码与测试

项目基于 NoneBot2、FastAPI、httpx、websockets 和 Pillow。WebUI 是静态 HTML，没有独立前端构建步骤。

```powershell
uv sync --locked
uv run --locked pytest -q
```

`scripts/fetch_data.py` 从所列数据源刷新 `data/`，会联网并改动数据文件。`scripts/dry_run.py` 使用独立临时数据库验证 WFM 读取、匹配和渲染，不调用 QQ / Discord 发送 API。

## 许可证与数据来源

RivenSniper 自有源代码采用 [MIT License](LICENSE)。第三方依赖按各自许可证提供；游戏名称、图标及游戏数据的权利归相应权利人，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

- [warframe.market](https://warframe.market/)：市场目录、武器/词条信息、挂单和统计接口。
- [WFCD/warframe-items](https://github.com/WFCD/warframe-items)：武器元数据。
- [calamity-inc/warframe-riven-info](https://github.com/calamity-inc/warframe-riven-info)：紫卡词条数值资料来源。

本项目为独立社区工具，与 Digital Extremes、warframe.market、QQ 和 Discord 无隶属或官方背书关系。
