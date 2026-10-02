# RivenSniper

[简体中文](README.md) | English

**A Warframe Riven and item monitoring tool with in-game chat collection and QQ / Discord notifications.**

Version **9.0.2** · **Windows 11 desktop** · **Python 3.12** · [MIT License](LICENSE)

RivenSniper filters trade listings by weapon, stats, grades, and price, then sends matching results to QQ groups or Discord direct messages. It retrieves market data through the warframe.market API and can also read in-game chat feeds produced by a separate collector. The management console runs on the same computer as the Bot.

The project helps you find trading opportunities and receive alerts. You contact sellers and complete trades yourself. Price checks use the statistical references described below; they do not guarantee a sale price.

## Contents

- [Purpose and features](#purpose-and-features)
- [Prerequisites](#prerequisites)
- [Download and startup](#download-and-startup)
- [Management console](#management-console)
- [Discord direct messages](#discord-direct-messages)
- [In-game chat collection](#in-game-chat-collection)
- [WM fast fetching](#wm-fast-fetching)
- [Common commands](#common-commands)
- [Configuration and local data](#configuration-and-local-data)
- [Runtime behavior and troubleshooting](#runtime-behavior-and-troubleshooting)
- [Source code and tests](#source-code-and-tests)
- [License and data sources](#license-and-data-sources)

## Purpose and features

| Feature | Description |
| --- | --- |
| Riven filters | Filter by a specific weapon or weapon type, two or three positive stats, negative-stat requirements, reroll conditions, and minimum grades. OR alternatives are supported within each stat position. |
| WM listing alerts | Poll warframe.market Riven auctions. The initial scan establishes a baseline of existing listings; subsequent scans match newly appearing listings. |
| WM fast fetching | Add a parallel search path for rules specifying a particular weapon and three exact positive stats. Ordinary polling continues alongside it, with shared deduplication by target and listing. |
| Item bargain alerts | Monitor newly created market orders in real time and compare prices against the mean of the latest daily bucket of closed-order statistics. Alert when the configured discount is reached. |
| Riven bargain alerts | Build local hourly price samples for each weapon and use a rolling mean to identify low-priced buyout listings. |
| In-game chat collection | Collect channel feeds using a separate four-slot or seventeen-slot collector. Save only chat messages containing complete OMG Riven links, then decode weapons, stats, and values offline. |
| Channel history queries | Look up Rivens observed under a player nickname, observed Riven ownership timelines, and changes in a player's visibility in subscribed channels. |
| Multiple notification targets | Send to QQ groups and Discord DMs, with separate rules, language, blocklists, and enabled status for each target. |
| Management console | Manage notification targets, Riven rules, bargain watches, channel switches, command aliases, runtime settings, dictionaries, and logs. |
| Cards and grades | Generate Riven displays and grades. Discord uses Rich Embeds / ANSI text; QQ uses card images or text depending on the message source. |

See [Bargain calculations](docs/bargain.md) and [OMG link decoding](docs/riven-link.md) for technical details. These supporting documents are in Chinese.

## Prerequisites

| Use case | What you need |
| --- | --- |
| Basic operation | A Windows 11 desktop computer, network access to dependency download sources and warframe.market, and the fully extracted project directory. |
| Startup environment | uv and Python 3.12. The scripts check for uv, offer to install it through winget if needed, and prepare Python and the locked dependencies. |
| QQ group notifications | A logged-in SnowLuma instance with a working OneBot v11 reverse WebSocket client and HTTP Server. The Bot account must be a member of the receiving QQ group. |
| QQ target configuration | The receiving group ID and the QQ ID of that target's sole owner. Each target accepts commands only from its assigned owner. |
| Discord DMs | Your own Discord application and Bot Token, the recipient's user ID, and Discord settings that allow the Bot to send DMs to that recipient. |
| In-game chat collection | A local standalone or Steam installation of Warframe; distinct accounts and nicknames for all four or seventeen slots; a `psk_current.bin` valid for the current game client; and accounts with device verification completed. |
| WM fast fetching | A working HTTP CONNECT proxy. The included VPS setup also requires an Ubuntu amd64 VPS with an assigned IPv6 prefix, the Windows OpenSSH client, and working SSH authentication. |

You must supply the QQ protocol client, Discord Token, game accounts, authentication material, and proxies yourself. None are included in the source package. Ordinary WFM monitoring does not require the game chat collector or a fast-fetching proxy.

The management interface is intended for a desktop browser with a mouse and keyboard. Windows is the supported environment for the complete workflow.

## Download and startup

### 1. Download the complete project

Download `RivenSniper-9.0.2.zip` from this repository's **Releases** page and extract the entire archive. You can also use **Code → Download ZIP**. Preserve the directory structure; do not copy only the `.cmd` files to your desktop.

The directory should contain at least `bot.py`, `pyproject.toml`, `uv.lock`, `.env.example`, `src/`, `scripts/`, `data/`, and both launch scripts.

The Chinese script names below are their actual filenames. Use them exactly as shown when launching the project or running commands.

### 2. Use the QQ one-click launcher

1. Install and log in to SnowLuma, then add that QQ account to the group that will receive notifications.
2. Double-click **`启动BOT.cmd`**.
3. On the first run, follow the prompts to prepare uv, Python 3.12, and the project dependencies. An existing environment will be reused.
4. Enter the receiving QQ group ID and the QQ ID of that target's sole owner.
5. The launcher creates the configuration and displays the OneBot connection details. Enable both channels below in SnowLuma, using the same launcher-generated authentication Token for both.

| SnowLuma setting | Default value |
| --- | --- |
| OneBot v11 reverse WebSocket | `ws://127.0.0.1:8180/onebot/v11/ws` |
| HTTP Server / API | `http://127.0.0.1:3000/` |
| Token for both channels | Use the value generated by the launcher. |

6. After configuring SnowLuma, press Enter in the launcher to start the Bot.
7. Open **`http://127.0.0.1:8180/admin`** in a desktop browser on the same computer. Check the connection status, make sure the target is enabled, and add your filter rules.
8. Keep both the Bot window and SnowLuma running. To stop the Bot, press `Ctrl+C` in its window.

For later runs, double-click the same script. The setup wizard does not overwrite existing configuration or rules. If you change `PORT` in `.env`, update both the browser URL and the SnowLuma reverse WebSocket URL to use the new port.

The reverse WebSocket receives incoming events. Outgoing OneBot actions use the HTTP API, so both channels must be configured.

### 3. Start manually

If [uv](https://docs.astral.sh/uv/getting-started/installation/) is already installed, open PowerShell in the project directory:

```powershell
uv python install 3.12
uv sync --locked
Copy-Item .env.example .env
```

Run `Copy-Item` only during initial setup, when the directory does not already contain `.env`. Edit `.env` and replace `ONEBOT_ACCESS_TOKEN` with your own random string. For QQ, configure SnowLuma as well. Then run:

```powershell
uv run --locked python bot.py
```

For Discord-only use, start manually and keep the basic OneBot settings from the example without creating a QQ target. The QQ one-click launcher requires you to create a QQ group target.

## Management console

Default address: **`http://127.0.0.1:8180/admin`**.

Suggested setup order:

1. In notification targets (`推送目标`), check or create a target. Enter the QQ group and owner, or a Discord user ID, then enable the target.
2. Set the target's reply language, expiration, in-game channel message switch, and WM fast-fetching switch.
3. Select the target on the Riven rules page and add weapon, stat, minimum-grade, and reroll conditions.
4. For low-price alerts, add item or Riven watches on the bargain page.
5. Use the system status and log pages to inspect connections, collection, queries, and notifications. Send a test notification to confirm delivery.

Channel messages and WM fast fetching are disabled by default for new targets. A target can remain enabled indefinitely or have an expiration date. Once it expires, commands and notifications stop being processed for that target.

The console is a local management interface and does not have a separate web login. Keep `HOST=127.0.0.1` in `.env` and access it from the computer running the Bot.

## Discord direct messages

1. Create an application and Bot in the [Discord Developer Portal](https://discord.com/developers/applications), then obtain your own Bot Token.
2. Set `DISCORD_DM_ENABLED=true` in `.env`.
3. Replace `DISCORD_BOTS=[]` with the complete configuration shown in `.env.example`, replacing the token placeholder with your own Token. Keep the example intents: enable `direct_messages` and disable server events and application commands.
4. Install the Bot so it can DM the recipient. Check shared-server membership and the recipient's DM privacy settings. Copy the recipient's user ID; if needed, enable Developer Mode under Discord **User Settings → Advanced**.
5. Restart the Bot, then create and enable the matching Discord DM target in the local management console.
6. Send commands to the Bot in a DM from that user, or configure rules and send a test message from the console.

The Discord integration handles only DMs and does not register server commands. QQ and Discord can run together, with independent target rules.

## In-game chat collection

Chat collection runs in a separate process. The Bot reads the local data written by the collector. When using both, configure the collector before starting the Bot.

### Four-slot and seventeen-slot modes

| Mode | Accounts / nicknames | Channel assignment and member state |
| --- | --- | --- |
| Four slots, default | 4 distinct accounts / nicknames | Sharded across regions. Each connection performs one rate-limited round of member queries. |
| Seventeen slots | 17 distinct accounts / nicknames | One slot per region, subscribing to that region's G/Q/R/T channels. No startup member queries; member state uses live join and leave events. |

Nicknames must match the in-game names exactly. Channel assignments are defined in `configs/chat_collector_shards.json` and `configs/chat_collector_shards_17.json`.

### Configuration and startup

1. Double-click **`启动聊天采集.cmd`** for the default four-slot mode. To start directly in seventeen-slot mode on the first run, execute this in PowerShell:

   ```powershell
   .\启动聊天采集.cmd -CollectorMode 17
   ```

2. Enter the game nicknames for the selected mode when prompted.
3. Supply a `psk_current.bin` valid for the current client and place it in the directory opened by the launcher. This authentication material is not distributed with the repository; collection cannot connect without it.
4. After validation, keep the separate chat collector supervisor (`聊天采集监督器`) window running.
5. Exit any running Warframe game and launcher. Select menu option **1** to automatically log in and obtain authentication tickets for slots that are not already running.
6. On the first run, enter each slot's account and password when prompted. Password input is hidden. Credentials are stored locally, encrypted using the current Windows user's DPAPI. The script launches the game, logs in, retrieves connection authentication, closes that game instance after success, and proceeds to the next slot.
7. Confirm that the required slots are running, then enter **0** to close the wizard. The supervisor must remain running.
8. Start or restart the Bot. Enable channel messages for the receiving target in the management console and add Riven rules that match the messages you want.

Automatic login supports both the standalone and Steam clients, preferring the standalone client when both are installed. Keep the game and launcher visible; do not minimize, cover, or take focus away from them. The game's client area must be at least 480×270, and the launcher's at least 700×400. Temporary changes to `EE.cfg` are restored when the automatic workflow finishes or recovers from failure.

### Collector menu

| Option | Action |
| --- | --- |
| 1 | Automatically log in and obtain tickets for slots that are not running. |
| 2 | Refresh status. |
| 3 | Open nickname configuration. |
| 4 | Stop all collection normally. |
| 5 | Stop collection, then switch between four-slot and seventeen-slot modes. |
| 6 | Request one member snapshot for a specified running slot. |
| 7 | Obtain tickets manually, one slot at a time. |
| 8 | Configure or update automatic-login credentials. |
| 0 | Close only the wizard, leaving the supervisor running. |

Each slot saves messages containing complete OMG links. Ordinary chat and ordinary item links are filtered out. Join and leave events record visibility in subscribed channels, not a player's online status across the entire game.

After a disconnection, the worker makes bounded reconnection attempts while authentication remains valid. If the process exits or authentication expires, obtain a new ticket. Missed messages are not replayed. Use menu option **4** to stop collection.

## WM fast fetching

Fast fetching applies to rules with **a specific weapon and three explicitly specified positive stats**. Rules using a weapon type, two positive stats, or an unspecified positive stat continue to use ordinary polling. Explicit OR alternatives in the three stat positions expand into shared queries. Negative stats, rerolls, grades, and blocklists are still checked locally.

1. Prepare an HTTP CONNECT proxy and create `.runtime/wm_fast_proxy.json` on the local computer.
2. Enable WM fast fetching for the desired targets in notification targets (`推送目标`) in the management console.
3. Adjust the query interval in system settings. The default is 2 seconds; the allowed range is 1–60 seconds.

Example proxy configuration:

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

All values above are placeholders. For a directly accessible HTTP proxy, omit the `ssh` section and enter the actual proxy URL. For SSH forwarding, the local computer needs `ssh`, a working private key, and a `known_hosts` entry for the server. Restart the Bot after changing this configuration.

The included `scripts/provision_wm_proxy.py` can set up the egress service on an Ubuntu amd64 VPS with an assigned IPv6 `/64`. On that VPS, run it as root, replacing the prefix placeholder with your assigned prefix:

```bash
python3 provision_wm_proxy.py --prefix "YOUR_ASSIGNED_IPV6_PREFIX/64" --interface eth0 --count 512
```

The script installs a fixed version of 3proxy, configures addresses and a systemd service, and generates `/etc/rivensniper-wm/client.json`. Save the proxy configuration in a private configuration file on your own computer, then add the SSH section. The service listens on the VPS loopback address. The VPS provides network egress only; searches and notifications run in the Windows Bot.

`WM_FAST_PROXY_CONFIG` defaults to `.runtime/wm_fast_proxy.json`. Missing proxy configuration does not affect ordinary polling. A query that returns 500 results falls back to ordinary polling. Requests through each egress are spaced at least 6.5 seconds apart; HTTP 429 responses trigger cooldown and backoff. The configured query interval is not a guarantee of listing-discovery latency. Fast fetching does not accelerate bargain monitoring or replay historical notifications.

## Common commands

The assigned QQ target owner sends commands in that target's group; a Discord target user sends them to the Bot in a DM. Commands from non-owners or for disabled targets are ignored. These are the default English command aliases for a fresh installation. You can change aliases in the console; restart the Bot for changes to take effect.

| Example | Purpose |
| --- | --- |
| `s Torid cc@A ms@B+ -z@A` | Add a Riven rule with minimum grades for positive and negative stats. |
| `sl` / `sd 3` | List rules / delete rule 3. |
| `st` | Show full stat names and available abbreviations. |
| `bl WM` / `bl Channel` | Show the seller blocklist for the specified source. |
| `b WM ExampleSeller` | Add the example seller to the WM blocklist. Multiple entries can be supplied on separate lines. |
| `bd Channel ExampleSeller` | Remove the example seller from the channel blocklist. |
| `d Arcane Grace 0 25` | Watch the rank-0 item and alert when its price is at least 25% below the reference price. |
| `dl` / `dd 3` | List / delete item bargain watches. |
| `rd Torid 30` | Add a Riven bargain watch and alert at least 30% below that weapon's reference price. |
| `rdl` / `rdd 3` | List / delete Riven bargain watches. |
| `w ExamplePlayer page 2` / `w ExamplePlayer all` | Look up Rivens observed under the example nickname in collected channels; paginate or export the results. |
| `rh 3 page 2` / `rh 3 all` | Look up observed ownership records for Riven 3. |
| `t ExamplePlayer` | Subscribe to alerts for the nickname's joins, leaves, and region changes in subscribed channels. |
| `tl` / `td 3` | List / delete channel tracking alerts. |
| `cd` / `cd 24` | View / set the current target's channel deduplication window, from 1 to 72 hours. |

Rules require two or three positive stats. Omitting the negative stat requires a Riven with no negative stat; `-any` accepts any negative stat. Use `/` to join OR alternatives within the same stat position. `@S`, `@A+`, `@A`, `@A-`, `@B+`, `@B`, `@B-`, `@C+`, `@C`, `@C-`, and `@F` specify minimum grades. Ungradable results marked `X` / `?` do not satisfy a minimum-grade requirement.

Channel queries require channel messages to be enabled for the target. Ownership records describe observations from collected chat; they are not completed-trade records or a player's current inventory. Blocklists filter Riven rule matching from the corresponding source only, not item or Riven bargain alerts.

## Configuration and local data

`.env.example` contains a complete configuration example. Common settings:

| Setting | Default / purpose |
| --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8180`: listen address for the Bot and local management interface. |
| `ONEBOT_ACCESS_TOKEN` | A random Token matching both SnowLuma channels. |
| `ONEBOT_API_ROOTS` | Mapping of SnowLuma HTTP API endpoints. |
| `SNIPER_POLL_INTERVAL` | `15.0`: ordinary Riven polling interval, in seconds. |
| `WM_FAST_INTERVAL` | `2.0`: desired fast-query interval, in seconds. |
| `WM_FAST_PROXY_CONFIG` | `.runtime/wm_fast_proxy.json`: private proxy configuration path. |
| `TRADE_MESSAGE_TTL_SECONDS` | `60`: lifetime of time-sensitive messages, in seconds. |
| `SEND_QUEUE_MAXSIZE` | `1000`: in-memory message queue capacity. |
| `DISCORD_DM_ENABLED` / `DISCORD_BOTS` | Discord DM switch and Bot configuration. |
| `IRC_FEED_ENABLED` / `IRC_FEED_DIR` | Channel feed reading switch and directory, written by the collector launcher. |
| `IRC_FEED_RETENTION_DAYS` | `7`: retention in days for channel JSONL data consumed by all cursors. |
| `IRC_PRESENCE_RETENTION_DAYS` | `7`: retention in days for raw join/leave events and completed visibility intervals. |

Manage rules, targets, and bargain parameters primarily through the console. Restart the Bot after editing `.env`.

| Path | Contents |
| --- | --- |
| `.env` | Local runtime configuration and Tokens. |
| `sniper.db` | Targets, rules, blocklists, price samples, and deduplication state. |
| `.runtime/chat_collector/` | Nickname configuration, encrypted credentials, authentication material, collector state, channel data, and tracking databases. |
| `.runtime/logs/bot.log` | Bot log. |
| `.runtime/chat_collector/logs/` | Collector logs. |
| `data/` | Version-controlled game weapons, stats, market catalogs, indexes, and Bot message text. |

Player, Riven, and observed ownership records in the tracking database are retained long term. The retention period for raw events does not remove these records. Runtime directories, account information, logs, and databases are stored only on the user's computer and are not part of the source release.

Chinese and English Bot messages are stored in `data/bot_texts.json`. When editing them, preserve key names and placeholders such as `{name}`. Restart the Bot for changes to take effect.

## Runtime behavior and troubleshooting

- **No alerts for old listings after startup:** Ordinary Riven polling and fast fetching first establish a baseline, then handle new listings. Item bargain monitoring handles only newly created orders received in real time.
- **Missed messages are not resent:** The queue has capacity and lifetime limits. Messages can be dropped while offline, after expiration, or when the queue overflows. There is no historical replay after disconnection.
- **The same Riven is not repeatedly pushed:** Channel messages have a one-hour global base deduplication window; each target can use a 1–72-hour window. Repeated observations refresh the observation timestamp.
- **No QQ notifications:** Check SnowLuma login, group membership, HTTP API and reverse WebSocket URLs / Tokens, target enabled status, and filter rules.
- **No Discord notifications:** Check the Token, DM switch, target user ID, Bot connection, and recipient's DM privacy settings.
- **Collection works but no channel alerts arrive:** Restart the Bot to load the collector configuration, then check the target's channel switch and Riven rules.
- **uv / winget not found:** Install or update Windows App Installer, or install uv using its official instructions, then reopen the launch script.
- **Port already in use:** Check whether an older Bot process is still running. After changing `PORT`, update the browser and protocol-client URLs as well.
- **Game login or ticket retrieval fails:** Check account device verification, authentication material for the current client, and the current Windows user and game permissions. Update credentials with menu option **8**, or obtain tickets manually with option **7**.
- **Database version is incompatible:** Preserve the original database and its backups. The application accepts only explicitly supported versions and backs up before migration. Do not erase the database as a substitute for migration.

Logs and launcher windows may contain account information or Tokens. Before sharing troubleshooting material, redact accounts, group IDs, nicknames, Tokens, proxy addresses, and personal paths.

## Source code and tests

The project uses NoneBot2, FastAPI, httpx, websockets, and Pillow. The WebUI is static HTML and does not require a separate frontend build.

```powershell
uv sync --locked
uv run --locked pytest -q
```

`scripts/fetch_data.py` refreshes `data/` from the listed sources. It accesses the network and modifies data files. `scripts/dry_run.py` uses a separate temporary database to check WFM retrieval, matching, and rendering without calling the QQ / Discord send APIs.

## License and data sources

RivenSniper's own source code is licensed under the [MIT License](LICENSE). Third-party dependencies retain their respective licenses. Game names, icons, and game data belong to their respective rights holders; see [Third-party notices](THIRD_PARTY_NOTICES.md).

- [warframe.market](https://warframe.market/): Market catalogs, weapon / stat information, listings, and statistics APIs.
- [WFCD/warframe-items](https://github.com/WFCD/warframe-items): Weapon metadata.
- [calamity-inc/warframe-riven-info](https://github.com/calamity-inc/warframe-riven-info): Source of Riven stat value data.

This is an independent community tool. It is not affiliated with or officially endorsed by Digital Extremes, warframe.market, QQ, or Discord.
