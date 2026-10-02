"""对照仓库内静态表和 OMG 解码结果的研究脚本，不是生产采集入口。

它把历史上的两条研究路径合成一条链：

1. 通过 ``PRIVMSG`` 字符串 xref 动态定位入站处理点；
2. 只落盘含完整 ``[OMG-...RandomModRare:<base64>]`` 的 PRIVMSG；
3. 加入频道后同时记录本连接发出的紫卡链接，供截图 ground-truth 对齐；
4. 同一条处理路径多次触发时只写一条规范化 JSON 记录。

设置 ``RIVEN_CAPTURE_CHAT_PREFIXES`` 后，还会落盘指定频道前缀的普通消息。这用于
低流量频道的协议采样；默认留空，不改变紫卡采集行为。

默认只记录入站消息。设置 ``RIVEN_CAPTURE_JOIN_ENABLED=1`` 后，才会在已验证的
游戏版本上加入指定公开交易频道；不支持的版本会拒绝启动。不会另开一套
IRC 登录，也不会向 bot/游戏进程发送退出或重启信号。游戏重启后需要重新运行本脚本。

运行::

    uv run --with frida python scripts/frida_riven_capture.py

环境变量：

``RIVEN_CAPTURE_PATH``
    JSONL 输出；默认 ``.tmp/riven_chat_live.jsonl``。
``RIVEN_CAPTURE_LOG_PATH``
    运行日志；默认 ``.tmp/frida_riven_capture.log``。
``RIVEN_CAPTURE_JOIN_ENABLED``
    ``1`` 才启用多频道 JOIN；默认关闭。
``RIVEN_CAPTURE_CHANNELS``
    逗号分隔频道；默认全部已确认的语言/英文区域交易频道。当前共 17 个，
    会超过单个连接加上游戏自动加入频道后的上限；需要全量时应显式分批传入。
``RIVEN_CAPTURE_JOIN_DELAY_MIN_MS`` / ``RIVEN_CAPTURE_JOIN_DELAY_MAX_MS``
    相邻 JOIN 请求的随机间隔范围；默认 ``10000`` 到 ``15000`` 毫秒。
``RIVEN_CAPTURE_CHAT_PREFIXES``
    逗号分隔频道前缀；匹配的普通入站/出站 PRIVMSG 也会写入 JSONL。默认留空。
``RIVEN_CAPTURE_RIVEN_ENABLED``
    ``0`` 时不保存其它频道的紫卡消息，供普通频道独立采样；默认 ``1``。
``RIVEN_TABLE_PATH``
    可选 JSONL 输出。启用后从 OMG 解析器读取兼容武器表，并经已确认的类型对象字段
    与游戏字符串池还原 Lotus 路径；不扫描进程堆、不调用游戏函数。
``WATCH_SEC``
    大于 0 时到时退出；默认 0（持续运行）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import ctypes
from collections import OrderedDict
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGLISH_CHAT_REGION_SUFFIXES = (
    # 2026-07-16 英文客户端 EE.log 实测；亚洲和大洋洲共用 AS。
    "NA", "EU", "SA", "RU", "AS",
)
ENGLISH_TRADE_CHANNELS = tuple(
    f"#T_EN_{suffix}" for suffix in ENGLISH_CHAT_REGION_SUFFIXES
)
DEFAULT_CHANNELS = (
    "#T_ZH",
    *ENGLISH_TRADE_CHANNELS,
    "#T_FR", "#T_DE", "#T_ES", "#T_PT", "#T_RU", "#T_JA", "#T_KO",
    "#T_TC", "#T_IT", "#T_PL", "#T_UK",
)
MODULE_NAME = "warframe.x64.exe"
# 只能放经过静态反汇编确认的函数根入口。2026-07-15 build 中 ``IRC out:`` xref
# 位于 0x191cfcb；PE unwind chain 为 0x191cc00 -> 0x191cbda -> 0x191cba0，
# 最后一个才是可调用入口，签名 (this, WFString*, flag)。
KNOWN_SEND_RVAS = {
    "2b7a678-9c20078e6c8bb2e3": 0x14481E0,
    "2b7aa78-cb2964711d32070e": 0x191CBA0,
}
KNOWN_RIVEN_PARSER_RVAS = {
    "2b7a678-9c20078e6c8bb2e3": 0x144C450,
}
KNOWN_SYMBOL_TABLE_RVAS = {
    "2b7a678-9c20078e6c8bb2e3": 0x289F820,
}
RIVEN_LINK_RE = re.compile(
    r"\[OMG-(?P<category>\w+RandomModRare):"
    r"(?P<b64>[A-Za-z0-9+/]+={0,2})\]"
)
PRIVMSG_RE = re.compile(
    r"^:(?P<nick>[^!\s]+)(?:!(?P<identity>[^ ]*))? PRIVMSG "
    r"(?P<channel>#[^\s]+) :(?P<text>.*)$"
)
OUTGOING_RE = re.compile(r"^PRIVMSG (?P<channel>#[^\s]+) :(?P<text>.*)$")
IRC_JOIN_RE = re.compile(
    r"^:(?P<source>\S+)\s+JOIN\s+:?(?P<channel>#[^\s,]+)", re.IGNORECASE
)
IRC_NUMERIC_RE = re.compile(
    r"^(?::(?P<source>\S+)\s+)?(?P<code>\d{3})\s+"
    r"(?P<target>\S+)(?P<params>.*)$"
)
IRC_CHANNEL_RE = re.compile(r"(?:^|\s):?(?P<channel>#[^\s,]+)")
JOIN_CONFIRMATION_CODES = frozenset({353, 366})


def parse_riven_privmsg(raw: str) -> dict | None:
    """把含完整 OMG 紫卡链接的 IRC PRIVMSG 规范化；其它内容返回 None。"""
    record = parse_privmsg(raw)
    if not record or not RIVEN_LINK_RE.search(record["text"]):
        return None
    return record


def parse_privmsg(raw: str) -> dict | None:
    """规范化一条入站 IRC PRIVMSG。"""
    match = PRIVMSG_RE.match(raw.replace("\r", "").replace("\n", ""))
    if not match:
        return None
    identity = match.group("identity") or ""
    sender_match = re.match(r"([0-9a-fA-F]{24})_", identity)
    return {
        "dir": "in",
        "nick": match.group("nick"),
        "sender_id": sender_match.group(1).lower() if sender_match else "",
        "chan": match.group("channel"),
        "text": match.group("text"),
    }


def parse_outgoing_riven(raw: str) -> dict | None:
    """规范化本账号发出的 OMG 链接，供手工 ground-truth 采样。"""
    record = parse_outgoing_privmsg(raw)
    if not record or not RIVEN_LINK_RE.search(record["text"]):
        return None
    return record


def parse_outgoing_privmsg(raw: str) -> dict | None:
    """规范化一条本账号发出的 IRC PRIVMSG。"""
    match = OUTGOING_RE.match(raw.replace("\r", "").replace("\n", ""))
    if not match:
        return None
    return {
        "dir": "out",
        "nick": "self",
        "chan": match.group("channel"),
        "text": match.group("text"),
    }


def parse_irc_join_response(raw: str) -> dict | None:
    """解析服务器对 JOIN 的确认、拒绝或 JOIN 广播。"""
    normalized = raw.replace("\r", "").replace("\n", "")
    join_match = IRC_JOIN_RE.match(normalized)
    if join_match:
        source = join_match.group("source")
        return {
            "kind": "join",
            "channel": join_match.group("channel"),
            "code": None,
            "source": source.split("!", 1)[0],
            "target": None,
            "detail": "",
        }

    numeric_match = IRC_NUMERIC_RE.match(normalized)
    if not numeric_match:
        return None
    channel_match = IRC_CHANNEL_RE.search(numeric_match.group("params"))
    if not channel_match:
        return None
    code = int(numeric_match.group("code"))
    detail = numeric_match.group("params")[channel_match.end():].strip()
    if detail.startswith(":"):
        detail = detail[1:]
    if code in JOIN_CONFIRMATION_CODES:
        kind = "confirmed"
    elif 400 <= code < 600:
        kind = "rejected"
    else:
        kind = "numeric"
    return {
        "kind": kind,
        "channel": channel_match.group("channel"),
        "code": code,
        "source": numeric_match.group("source"),
        "target": numeric_match.group("target"),
        "detail": detail,
    }


class RecentMessages:
    """短窗去重；允许卖家稍后重新发送完全相同的广告。"""

    def __init__(self, ttl: float = 10.0, capacity: int = 10_000):
        self.ttl = ttl
        self.capacity = capacity
        self._seen: OrderedDict[tuple[str, str, str], float] = OrderedDict()

    def add(self, record: dict, now: float) -> bool:
        key = record["nick"], record["chan"], record["text"]
        previous = self._seen.get(key)
        if previous is not None and now - previous < self.ttl:
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        cutoff = now - self.ttl
        while self._seen:
            first_key = next(iter(self._seen))
            if len(self._seen) <= self.capacity and self._seen[first_key] >= cutoff:
                break
            self._seen.popitem(last=False)
        return True


def exe_build_id(path: str) -> str:
    digest = hashlib.sha1()
    size = 0
    with open(path, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return f"{size:x}-{digest.hexdigest()[:16]}"


def process_executable(pid: int) -> str | None:
    """读取进程镜像路径；轻量进程枚举不保证携带 parameters。"""
    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return buffer.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def main() -> None:
    try:
        import frida
    except ImportError:
        raise SystemExit(
            "缺少 frida：请用 `uv run --with frida python scripts/frida_riven_capture.py`"
        ) from None

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    output_path = Path(
        os.environ.get("RIVEN_CAPTURE_PATH", ROOT / ".tmp" / "riven_chat_live.jsonl")
    )
    log_path = Path(
        os.environ.get(
            "RIVEN_CAPTURE_LOG_PATH", ROOT / ".tmp" / "frida_riven_capture.log"
        )
    )
    join_enabled = os.environ.get("RIVEN_CAPTURE_JOIN_ENABLED") == "1"
    join_delay_min_ms = int(
        os.environ.get("RIVEN_CAPTURE_JOIN_DELAY_MIN_MS", "10000")
    )
    join_delay_max_ms = int(
        os.environ.get("RIVEN_CAPTURE_JOIN_DELAY_MAX_MS", "15000")
    )
    if join_delay_min_ms < 0 or join_delay_max_ms < join_delay_min_ms:
        raise SystemExit(
            "JOIN 随机间隔无效：需满足 0 <= MIN_MS <= MAX_MS"
        )
    table_path_value = os.environ.get("RIVEN_TABLE_PATH")
    table_path = Path(table_path_value) if table_path_value else None
    channels = tuple(
        channel.strip()
        for channel in os.environ.get(
            "RIVEN_CAPTURE_CHANNELS", ",".join(DEFAULT_CHANNELS)
        ).split(",")
        if channel.strip()
    )
    chat_prefixes = tuple(
        prefix.strip()
        for prefix in os.environ.get("RIVEN_CAPTURE_CHAT_PREFIXES", "").split(",")
        if prefix.strip()
    )
    capture_riven = os.environ.get("RIVEN_CAPTURE_RIVEN_ENABLED", "1") != "0"
    watch_sec = int(os.environ.get("WATCH_SEC", "0"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if table_path:
        table_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")
        print(line, flush=True)

    def append_record(record: dict) -> None:
        with output_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()

    def append_table(record: dict) -> None:
        if table_path is None:
            return
        with table_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()

    device = frida.get_local_device()
    processes = [
        process
        for process in device.enumerate_processes()
        if process.name.lower() == MODULE_NAME
    ]
    if not processes:
        raise SystemExit("未找到 Warframe.x64.exe；请先进入游戏")
    process = processes[0]
    executable = process_executable(process.pid)
    build = exe_build_id(executable) if executable and Path(executable).is_file() else "unknown"
    send_rva = KNOWN_SEND_RVAS.get(build, 0)
    parser_rva = KNOWN_RIVEN_PARSER_RVAS.get(build, 0) if table_path else 0
    symbol_table_rva = KNOWN_SYMBOL_TABLE_RVAS.get(build, 0) if table_path else 0
    if join_enabled and not send_rva:
        raise SystemExit(f"build {build} 没有已确认的 IRC send 入口，拒绝主动 JOIN")
    if table_path and (not parser_rva or not symbol_table_rva):
        raise SystemExit(
            f"build {build} 没有完整确认的 OMG parser/字符串表入口，拒绝采集类型表"
        )
    log(f"attach pid={process.pid} build={build} exe={executable}")
    session = None
    for attempt in range(1, 31):
        try:
            session = device.attach(process.pid)
            log(f"attached after attempt {attempt}")
            break
        except Exception as error:
            log(f"attach attempt {attempt}/30 failed: {error}")
            time.sleep(1)
    if session is None:
        raise SystemExit("Frida 连续 30 次 attach 失败；请确认没有旧采集器占用")

    javascript = r"""
'use strict';
var JOIN_ENABLED = %JOIN_ENABLED%;
var CHANNELS = %CHANNELS%;
var JOIN_DELAY_MIN_MS = %JOIN_DELAY_MIN_MS%;
var JOIN_DELAY_MAX_MS = %JOIN_DELAY_MAX_MS%;
var CHAT_PREFIXES = %CHAT_PREFIXES%;
var CAPTURE_RIVEN = %CAPTURE_RIVEN%;
var SEND_RVA = %SEND_RVA%;
var PARSER_RVA = %PARSER_RVA%;
var SYMBOL_TABLE_RVA = %SYMBOL_TABLE_RVA%;
var main = Process.enumerateModules().filter(function (module) {
  return module.name.toLowerCase() === 'warframe.x64.exe';
})[0];
if (!main) throw new Error('Warframe module not found');

function asciiSig(text) {
  var bytes = [];
  for (var i = 0; i < text.length; i++)
    bytes.push(('0' + text.charCodeAt(i).toString(16)).slice(-2));
  return bytes.join(' ');
}
function scanAll(pattern) {
  return Memory.scanSync(main.base, main.size, pattern).map(function (match) {
    return match.address;
  });
}
function leaXrefs(target) {
  var result = [];
  var prefixes = [
    '48 8d 05','48 8d 0d','48 8d 15','48 8d 1d',
    '48 8d 25','48 8d 2d','48 8d 35','48 8d 3d',
    '4c 8d 05','4c 8d 0d','4c 8d 15','4c 8d 1d',
    '4c 8d 25','4c 8d 2d','4c 8d 35','4c 8d 3d'
  ];
  prefixes.forEach(function (prefix) {
    scanAll(prefix).forEach(function (address) {
      try {
        var displacement = address.add(3).readS32();
        if (address.add(7).add(displacement).equals(target)) result.push(address);
      } catch (error) {}
    });
  });
  return result;
}
function resolveLiteralXrefs(text) {
  var result = [];
  scanAll(asciiSig(text) + ' 00').forEach(function (literal) {
    leaXrefs(literal).forEach(function (xref) { result.push(xref); });
  });
  return result;
}
function cString(pointer) {
  try {
    if (pointer.isNull()) return null;
    var value = pointer.readCString();
    return value && value.length < 3000 ? value : null;
  } catch (error) { return null; }
}
function readWFString(pointer) {
  try {
    if (pointer.isNull()) return null;
    var marker = pointer.add(0xf).readU8();
    if (marker === 0xff) {
      var heap = pointer.readPointer();
      var size = pointer.add(8).readU32() & 0x0fffffff;
      if (size > 0 && size < 3000) return heap.readUtf8String(size);
      return cString(heap);
    }
    if (marker === 0x0f) return '';
    var length = 15 - marker;
    if (length > 0 && length <= 15) return pointer.readUtf8String(length);
    return cString(pointer);
  } catch (error) { return cString(pointer); }
}
function makeWFString(value) {
  var object = Memory.alloc(0x20);
  for (var i = 0; i < 0x20; i++) object.add(i).writeU8(0);
  if (value.length <= 15) {
    object.writeUtf8String(value);
    object.add(0xf).writeU8(15 - value.length);
  } else {
    var heap = Memory.allocUtf8String(value);
    object.writePointer(heap);
    object.add(8).writeU64(value.length);
    object.add(0xf).writeU8(0xff);
  }
  return object;
}
function hasRivenLink(value) {
  return value && /\[OMG-\w+RandomModRare:[A-Za-z0-9+\/=]+\]/.test(value);
}
function isRivenPrivmsg(value) {
  return value && value.indexOf(' PRIVMSG #') >= 0 && hasRivenLink(value);
}
function channelMatchesPrefixes(channel) {
  return CHAT_PREFIXES.some(function (prefix) {
    return channel.indexOf(prefix) === 0;
  });
}
function isCapturedIncoming(value) {
  if (!value) return false;
  var match = / PRIVMSG (#[^\s]+) :/.exec(value);
  return match && channelMatchesPrefixes(match[1]);
}
function isCapturedOutgoing(value) {
  if (!value) return false;
  var match = /^PRIVMSG (#[^\s]+) :/.exec(value);
  return match && channelMatchesPrefixes(match[1]);
}
var privmsgXrefs = resolveLiteralXrefs('PRIVMSG');
var pmSite = privmsgXrefs.length ? privmsgXrefs[0] : null;
// IRC 队列节点的 WFString 位于 +0x18。hook 放在 LEA 后一条指令，保证 RSI
// 已指向尚未分派的完整原始行；该结构模式在当前 build 静态确认且要求唯一命中。
var rawDispatchMatches = scanAll(
  '49 8d 76 18 45 32 e4 80 f9 ff 75 05 48 8b 06 eb 03 48 8b c6 80 38 3a'
);
var rawSite = rawDispatchMatches.length === 1 ? rawDispatchMatches[0].add(4) : null;
var ircSend = SEND_RVA ? main.base.add(SEND_RVA) : null;
send({
  event: 'RESOLVED',
  privmsgSites: privmsgXrefs.length,
  rawSites: rawDispatchMatches.length,
  raw: rawSite ? rawSite.toString() : null,
  rawRva: rawSite ? rawSite.sub(main.base).toString() : null,
  pm: pmSite ? pmSite.toString() : null,
  pmRva: pmSite ? pmSite.sub(main.base).toString() : null,
  send: ircSend ? ircSend.toString() : null,
  sendRva: ircSend ? ircSend.sub(main.base).toString() : null
});
if (!pmSite) throw new Error('unable to resolve PRIVMSG hook');
if (JOIN_ENABLED && !rawSite)
  throw new Error('unable to resolve unique IRC raw hook required for JOIN replies');

var requestedChannels = new Set();
var rawListener = null;
function requestedResponseChannel(raw) {
  var match = /(?:^|\s):?(#[^\s,]+)/.exec(raw);
  if (!match) return null;
  var channel = match[1].toUpperCase();
  return requestedChannels.has(channel) ? channel : null;
}
if (JOIN_ENABLED) {
  rawListener = Interceptor.attach(rawSite, {
    onEnter: function () {
      var raw = readWFString(this.context.rsi) || cString(this.context.rsi);
      if (!raw) return;
      var isJoin = /\sJOIN\s+:?#[^\s,]+/i.test(raw);
      var numericMatch = /(?:^|\s)(\d{3})\s/.exec(raw);
      var isNumeric = numericMatch !== null;
      if (!isJoin && !isNumeric) return;
      var channel = requestedResponseChannel(raw);
      if (channel) {
        send({ event: 'IRC_JOIN_RESPONSE', channel: channel, raw: raw.slice(0, 3000) });
        if (numericMatch) {
          var code = parseInt(numericMatch[1], 10);
          if (code === 353 || code === 366 || (code >= 400 && code < 600))
            requestedChannels.delete(channel);
        }
      }
    }
  });
}

var pmListener = Interceptor.attach(pmSite, {
  onEnter: function () {
    // 该 xref 处完整 IRC 行在 RSI（历史 v2 长跑日志与当前 build 均验证）。
    // 不再扫描 14 个寄存器 + 48 个栈槽：全区流量下那会制造重复并拖垮游戏。
    var raw = readWFString(this.context.rsi) || cString(this.context.rsi);
    if (CAPTURE_RIVEN && isRivenPrivmsg(raw))
      send({ event: 'RIVEN', raw: raw.slice(0, 3000) });
    else if (isCapturedIncoming(raw))
      send({ event: 'CHAT', raw: raw.slice(0, 3000) });
  }
});

var seenRivenTables = new Set();
var tableListener = null;
if (PARSER_RVA) {
  var symbolTable = main.base.add(SYMBOL_TABLE_RVA).readPointer();
  function resolveSymbol(value) {
    var pool = value & 0xffff;
    var offset = value >>> 16;
    var poolBase = symbolTable.add(pool * 16).readPointer();
    return poolBase.add(offset).readCString();
  }
  function resolveTypePath(typePointer) {
    var descriptor = typePointer.add(0x10).readPointer();
    var directory = resolveSymbol(descriptor.readU32());
    var objectName = resolveSymbol(typePointer.add(0x2c).readU32());
    return directory + objectName;
  }
  tableListener = Interceptor.attach(main.base.add(PARSER_RVA), {
    onEnter: function (args) {
      try {
        var object = args[0];
        var table = object.add(0x1bb8).readPointer();
        var byteSize = object.add(0x1bc0).readU32();
        var count = Math.floor(byteSize / 32);
        if (count <= 0 || count > 1000 || byteSize !== count * 32) return;
        var paths = [];
        for (var index = 0; index < count; index++) {
          var typePointer = table.add(index * 32).readPointer();
          paths.push(resolveTypePath(typePointer));
        }
        var signature = count + ':' + paths.join(',');
        if (seenRivenTables.has(signature)) return;
        seenRivenTables.add(signature);
        send({
          event: 'RIVEN_TABLE',
          b64: readWFString(args[2]),
          count: count,
          paths: paths
        });
      } catch (error) {
        send({event: 'RIVEN_TABLE_ERROR', error: String(error)});
      }
    }
  });
}

var savedThis = null;
var sendListener = null;
var sendVerified = false;
var joinStarted = false;
var joinIndex = 0;
var joinInvoke = null;
function hookSend() {
  if (!ircSend) return;
  sendListener = Interceptor.attach(ircSend, {
    onEnter: function (args) {
      var outgoing = readWFString(args[1]) || cString(args[1]);
      if (!outgoing) return;
      savedThis = args[0];
      if (CAPTURE_RIVEN && outgoing.indexOf('PRIVMSG #') === 0 && hasRivenLink(outgoing))
        send({ event: 'RIVEN_OUT', raw: outgoing.slice(0, 3000) });
      else if (isCapturedOutgoing(outgoing))
        send({ event: 'CHAT_OUT', raw: outgoing.slice(0, 3000) });
      if (!sendVerified) {
        sendVerified = true;
        send({ event: 'SEND_VERIFIED', sample: outgoing.slice(0, 120) });
      }
      if (JOIN_ENABLED && !joinStarted) setTimeout(joinAll, 250);
    }
  });
}
function joinAll() {
  if (!JOIN_ENABLED || joinStarted || !savedThis || !ircSend) return;
  joinStarted = true;
  joinInvoke = new NativeFunction(ircSend, 'void', ['pointer', 'pointer', 'int']);
  joinNext();
}
function randomJoinDelayMs() {
  return JOIN_DELAY_MIN_MS + Math.floor(
    Math.random() * (JOIN_DELAY_MAX_MS - JOIN_DELAY_MIN_MS + 1)
  );
}
function finishJoinRequests() {
  send({ event: 'JOIN_SEND_DONE', total: CHANNELS.length });
  setTimeout(function () {
    requestedChannels.clear();
    send({ event: 'JOIN_STATUS_TIMEOUT' });
  }, 10000);
}
function joinNext() {
  if (joinIndex >= CHANNELS.length) {
    finishJoinRequests();
    return;
  }
  var channel = CHANNELS[joinIndex];
  var sequence = joinIndex + 1;
  joinIndex++;
  requestedChannels.add(channel.toUpperCase());
  try {
    joinInvoke(savedThis, makeWFString('JOIN ' + channel), 0);
    send({
      event: 'JOIN_SENT', channel: channel,
      sequence: sequence, total: CHANNELS.length
    });
  } catch (error) {
    requestedChannels.delete(channel.toUpperCase());
    send({
      event: 'JOIN_SEND_FAILED', channel: channel,
      sequence: sequence, total: CHANNELS.length, error: String(error)
    });
  }
  if (joinIndex >= CHANNELS.length) {
    finishJoinRequests();
    return;
  }
  var delayMs = randomJoinDelayMs();
  setTimeout(joinNext, delayMs);
}
if (JOIN_ENABLED) hookSend();
send({ event: 'READY' });
""".replace("%JOIN_ENABLED%", "true" if join_enabled else "false") \
    .replace("%CHANNELS%", json.dumps(channels)) \
    .replace("%JOIN_DELAY_MIN_MS%", str(join_delay_min_ms)) \
    .replace("%JOIN_DELAY_MAX_MS%", str(join_delay_max_ms)) \
    .replace("%CHAT_PREFIXES%", json.dumps(chat_prefixes)) \
    .replace("%CAPTURE_RIVEN%", "true" if capture_riven else "false") \
    .replace("%SEND_RVA%", str(send_rva)) \
    .replace("%PARSER_RVA%", str(parser_rva)) \
    .replace("%SYMBOL_TABLE_RVA%", str(symbol_table_rva))

    recent = RecentMessages()
    counters = {"received": 0, "written": 0, "duplicates": 0}
    join_status = {channel.upper(): "configured" for channel in channels}
    irc_identity = {"nick": None}
    observed_join_sources = {}

    def on_message(message, _data) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            if message.get("type") == "error":
                log(f"Frida JS error: {message.get('description')}")
            return
        event = payload.get("event")
        if event == "RESOLVED":
            log(
                "resolved "
                f"pm={payload.get('pm')} rva={payload.get('pmRva')} "
                f"raw={payload.get('raw')} rva={payload.get('rawRva')} "
                f"send={payload.get('send')} rva={payload.get('sendRva')} "
                f"sites=pm:{payload.get('privmsgSites')}/raw:{payload.get('rawSites')}"
            )
        elif event == "READY":
            mode = (
                f"JOIN enabled, delay={join_delay_min_ms}-{join_delay_max_ms}ms"
                if join_enabled else "passive verification"
            )
            log(f"hooks ready ({mode})")
        elif event == "SEND_VERIFIED":
            log(f"send hook verified: {payload.get('sample')!r}")
        elif event == "JOIN_SENT":
            channel = str(payload.get("channel") or "")
            key = channel.upper()
            if join_status.get(key) == "configured":
                join_status[key] = "sent"
            log(
                f"JOIN requested [{payload.get('sequence')}/{payload.get('total')}] "
                f"{channel}"
            )
        elif event == "JOIN_SEND_FAILED":
            channel = str(payload.get("channel") or "")
            join_status[channel.upper()] = "send_failed"
            log(f"JOIN send failed {channel}: {payload.get('error')}")
        elif event == "JOIN_SEND_DONE":
            log(
                f"JOIN requests complete total={payload.get('total')}; "
                "waiting 10s for server replies"
            )
        elif event == "IRC_JOIN_RESPONSE":
            response = parse_irc_join_response(payload.get("raw") or "")
            if not response:
                return
            channel = response["channel"]
            key = channel.upper()
            if key not in join_status:
                return
            target = response.get("target")
            if target and target != "*":
                irc_identity["nick"] = target
                for observed_key, source in tuple(observed_join_sources.items()):
                    if (
                        source.casefold() == target.casefold()
                        and join_status.get(observed_key) == "sent"
                    ):
                        join_status[observed_key] = "confirmed"
                        log(f"JOIN confirmed {observed_key} (server JOIN)")
                        observed_join_sources.pop(observed_key, None)
            if response["kind"] == "confirmed":
                if join_status[key] != "confirmed":
                    join_status[key] = "confirmed"
                    log(f"JOIN confirmed {channel} (IRC {response['code']})")
            elif response["kind"] == "rejected":
                join_status[key] = "rejected"
                detail = response["detail"] or "no detail"
                log(f"JOIN rejected {channel} (IRC {response['code']}): {detail}")
            elif response["kind"] == "join":
                own_nick = irc_identity["nick"]
                if own_nick and response["source"].casefold() == own_nick.casefold():
                    if join_status[key] != "confirmed":
                        join_status[key] = "confirmed"
                        log(f"JOIN confirmed {channel} (server JOIN)")
                else:
                    observed_join_sources[key] = response["source"]
                    log(f"JOIN observed {channel} source={response['source']}")
        elif event == "JOIN_STATUS_TIMEOUT":
            confirmed = [key for key, value in join_status.items() if value == "confirmed"]
            rejected = [key for key, value in join_status.items() if value == "rejected"]
            unconfirmed = [key for key, value in join_status.items() if value == "sent"]
            failed = [key for key, value in join_status.items() if value == "send_failed"]
            log(
                "JOIN server summary "
                f"confirmed={len(confirmed)} rejected={len(rejected)} "
                f"unconfirmed={len(unconfirmed)} send_failed={len(failed)}"
            )
            if rejected:
                log(f"JOIN rejected channels: {','.join(rejected)}")
            if unconfirmed:
                log(f"JOIN unconfirmed channels: {','.join(unconfirmed)}")
        elif event == "RIVEN_TABLE":
            record = {
                "ts": time.time(),
                "pid": process.pid,
                "build": build,
                "b64": payload.get("b64"),
                "count": payload.get("count"),
                "paths": payload.get("paths"),
            }
            append_table(record)
            log(f"RIVEN_TABLE count={record['count']}")
        elif event == "RIVEN_TABLE_ERROR":
            log(f"RIVEN_TABLE error: {payload.get('error')}")
        elif event in {"RIVEN", "RIVEN_OUT", "CHAT", "CHAT_OUT"}:
            counters["received"] += 1
            raw = payload.get("raw") or ""
            if event == "RIVEN":
                record = parse_riven_privmsg(raw)
            elif event == "RIVEN_OUT":
                record = parse_outgoing_riven(raw)
            elif event == "CHAT":
                record = parse_privmsg(raw)
            else:
                record = parse_outgoing_privmsg(raw)
            if not record:
                return
            now = time.time()
            if not recent.add(record, now):
                counters["duplicates"] += 1
                return
            record = {"ts": now, **record}
            append_record(record)
            counters["written"] += 1
            links = len(RIVEN_LINK_RE.findall(record["text"]))
            kind = "RIVEN" if links else "CHAT"
            log(f"{kind} {record['chan']} <{record['nick']}> links={links}")

    script = session.create_script(javascript)
    script.on("message", on_message)
    script.load()
    log(f"output -> {output_path}")

    deadline = time.time() + watch_sec if watch_sec > 0 else None
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        log("stop requested")
    finally:
        log(
            f"summary received={counters['received']} written={counters['written']} "
            f"duplicates={counters['duplicates']}"
        )
        session.detach()
        log("detached")


if __name__ == "__main__":
    main()
