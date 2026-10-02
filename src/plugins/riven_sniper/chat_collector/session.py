"""一份一次性连接材料对应一个独立 IRC 会话。"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import ssl
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..platform_identity import game_nick_to_irc, resolve_player_identity
from .config import ALL_SLOTS, PresenceSnapshotPolicy
from .presence_snapshot import (
    PresenceSnapshotScheduler,
    SnapshotRequest,
    load_snapshot_request,
    update_snapshot_request,
)
from .protocol import (
    Heartbeat,
    JoinReport,
    JoinTracker,
    irc_password,
    is_account_id,
    parse_auth_challenge,
    parse_presence,
    parse_privmsg,
)
from .protocol_probe import (
    ProtocolProbeAnalyzer,
    channel_probe_commands,
    normalize_probe_channels,
    protocol_probe_commands,
)
from .runtime import pid_identity


@dataclass(frozen=True)
class Ticket:
    slot: str
    host: str
    port: int
    nick: str
    account_id: str
    nonce: str
    tls_fingerprint_sha256: str = ""
    tls_server_name: str = ""
    game_pid: int | None = None
    game_identity: str | None = None
    ticket_id: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, expected_slot: str | None = None) -> "Ticket":
        slot = str(value.get("slot") or expected_slot or "").strip().upper()
        host = str(value.get("host") or value.get("peer_ip") or "").strip()
        port = int(value.get("port") or value.get("peer_port") or 0)
        nick = str(value.get("nick") or "").strip()
        account_id = str(value.get("account_id") or "").strip().lower()
        nonce = str(value.get("nonce") or value.get("nonce_str") or "").strip()
        if expected_slot and slot != expected_slot.upper():
            raise ValueError(f"票据槽位 {slot!r} 与期望 {expected_slot!r} 不符")
        if slot not in ALL_SLOTS:
            raise ValueError("票据 slot 必须是 A-Q")
        if not host or not 1 <= port <= 65535:
            raise ValueError("票据缺少有效 IRC 地址")
        if not nick or any(character.isspace() for character in nick):
            raise ValueError("票据 nick 无效")
        if not is_account_id(account_id):
            raise ValueError("票据 account_id 必须是 24 位十六进制")
        try:
            nonce.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("票据 nonce 必须是 ASCII") from error
        if not nonce:
            raise ValueError("票据缺少 nonce")
        fingerprint = str(value.get("tls_fingerprint_sha256") or "").replace(":", "").lower()
        if fingerprint and (
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("TLS SHA-256 指纹格式无效")
        game_pid_value = value.get("game_pid")
        game_pid = int(game_pid_value) if game_pid_value else None
        created_at = str(value.get("created_at") or "").strip()
        if created_at:
            try:
                parsed_created_at = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )
            except ValueError as error:
                raise ValueError("票据 created_at 格式无效") from error
            if parsed_created_at.tzinfo is None:
                raise ValueError("票据 created_at 必须包含时区")
        ticket_id = str(value.get("ticket_id") or "").strip()
        if not ticket_id:
            ticket_id = hashlib.sha256(
                f"{slot}\0{host}\0{port}\0{nonce}".encode("utf-8")
            ).hexdigest()[:24]
        return cls(
            slot=slot,
            host=host,
            port=port,
            nick=nick,
            account_id=account_id,
            nonce=nonce,
            tls_fingerprint_sha256=fingerprint,
            tls_server_name=str(value.get("tls_server_name") or "").strip(),
            game_pid=game_pid,
            game_identity=str(value.get("game_identity") or "").strip() or None,
            ticket_id=ticket_id,
            created_at=created_at,
        )

    def created_at_timestamp(self) -> float | None:
        if not self.created_at:
            return None
        return datetime.fromisoformat(
            self.created_at.replace("Z", "+00:00")
        ).timestamp()


@dataclass(frozen=True)
class AuthResult:
    status: str
    response_sent: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.status == "authenticated"


class MessageSinkError(RuntimeError):
    """聊天记录持久化失败，与 socket/协议故障分开报告。"""


class IrcSession:
    """同步 socket 会话；适合由每槽独立 worker 进程持有。"""

    def __init__(
        self,
        ticket: Ticket,
        psk: bytes,
        *,
        plain: bool = False,
        connect_timeout: float = 12.0,
        auth_timeout: float = 30.0,
    ):
        if len(psk) < 64:
            raise ValueError("PSK 至少需要 64 字节")
        self.ticket = ticket
        self.psk = psk
        self.plain = plain
        self.connect_timeout = float(connect_timeout)
        self.auth_timeout = float(auth_timeout)
        self.socket: socket.socket | ssl.SSLSocket | None = None
        self._buffer = b""
        self.privmsg_count = 0
        self.by_channel: dict[str, int] = {}
        self.last_line_at: float | None = None
        self.on_presence: Callable[[dict[str, Any]], None] | None = None
        self._presence_snapshot_scheduler: PresenceSnapshotScheduler | None = None

    def _wire_nick(self) -> str:
        game_nick = self.ticket.nick.replace("\r", "").replace("\n", "").strip()
        return game_nick_to_irc(game_nick)

    def connect_and_auth(self) -> AuthResult:
        response_sent = False
        diagnostics: dict[str, Any] = {
            "registration_user_format": "account_id_0",
            "server_lines": 0,
            "challenge_count": 0,
            "reply_codes": [],
        }

        def result(status: str) -> AuthResult:
            return AuthResult(status, response_sent, dict(diagnostics))

        try:
            raw = socket.create_connection(
                (self.ticket.host, self.ticket.port), timeout=self.connect_timeout
            )
        except OSError as error:
            return result(f"connect_error:{error}")
        try:
            if self.plain:
                self.socket = raw
            else:
                # Warframe IRC 端点不依赖系统 CA。按用户当前方案，证书指纹仅在
                # 票据提供时校验，不作为采集启动的强制条件。
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                server_name = self.ticket.tls_server_name or self.ticket.host
                try:
                    self.socket = context.wrap_socket(raw, server_hostname=server_name)
                except Exception:
                    raw.close()
                    raise
                expected = self.ticket.tls_fingerprint_sha256
                if expected:
                    certificate = self.socket.getpeercert(binary_form=True)
                    actual = hashlib.sha256(certificate).hexdigest()
                    if not hmac.compare_digest(actual, expected):
                        self.close()
                        return result("tls_fingerprint_mismatch")
            if isinstance(self.socket, ssl.SSLSocket):
                diagnostics["tls_version"] = self.socket.version()
                cipher = self.socket.cipher()
                diagnostics["tls_cipher"] = cipher[0] if cipher else ""
            self.socket.settimeout(1.0)
            self._buffer = b""
            deadline = time.monotonic() + self.auth_timeout
            while time.monotonic() < deadline:
                try:
                    line = self._readline(deadline - time.monotonic())
                except TimeoutError:
                    continue
                if line is None:
                    return result("eof")
                self.last_line_at = time.time()
                diagnostics["server_lines"] = int(diagnostics["server_lines"]) + 1
                if line.startswith("PING"):
                    self.send_raw("PONG" + line[4:])
                    continue
                challenge = parse_auth_challenge(line)
                if challenge is not None:
                    diagnostics["challenge_count"] = (
                        int(diagnostics["challenge_count"]) + 1
                    )
                    if response_sent:
                        continue
                    token = irc_password(
                        self.psk, int(time.time()), challenge, self.ticket.nonce
                    )
                    # USER 带一次性凭据；从尝试发送前即按已消费处理，避免
                    # sendall 部分写入后抛错时错误复用 nonce。
                    response_sent = True
                    self.send_registration(token)
                    continue
                parts = line.split()
                code = parts[1] if len(parts) > 1 else ""
                if code.isdigit() and len(code) == 3:
                    reply_codes = diagnostics["reply_codes"]
                    if isinstance(reply_codes, list) and len(reply_codes) < 12:
                        reply_codes.append(code)
                if code in {"001", "002", "003", "004", "005", "251", "396"} or (
                    " MODE " in line and " +xw" in line
                ):
                    return result("authenticated")
                if "Bad Credentials" in line:
                    return result("rejected:Bad Credentials")
                if code in {
                    "464", "465", "466", "491", "433",
                }:
                    return result(f"rejected:{code}")
                if line.startswith("ERROR") and (
                    "E409" in line or "Authentication failure" in line
                ):
                    return result("rejected:E409")
                if line.startswith("ERROR"):
                    return result("server_error")
            return result("auth_timeout")
        except (OSError, ssl.SSLError, ValueError) as error:
            self.close()
            return result(f"session_error:{error}")

    def send_registration(self, token: str) -> None:
        """按游戏实际格式，在一次写入中发送 NICK 与 USER。"""
        if self.socket is None:
            raise OSError("IRC socket 尚未连接")
        nick = self._wire_nick()
        account_id = self.ticket.account_id
        # 当前 PC 客户端的真实出站注册抓包与独立 001 样本均使用
        # ``USER <account_id>_0 0 * <token>``；这里的 ``_0`` 是认证输入的一部分。
        payload = (
            f"NICK {nick}\r\n"
            f"USER {account_id}_0 0 * {token}\r\n"
        )
        self.socket.sendall(payload.encode("utf-8", "replace"))

    def join_channels(
        self,
        channels: list[str] | tuple[str, ...],
        *,
        on_privmsg: Callable[[dict[str, Any]], None],
        should_stop: Callable[[], bool] | None = None,
        batch_size: int = 5,
        batch_gap: float = 2.0,
        settle_timeout: float = 45.0,
    ) -> tuple[JoinTracker, JoinReport]:
        targets = tuple(channels)
        tracker = JoinTracker(targets, own_nick=self._wire_nick())
        self.by_channel = {channel: 0 for channel in targets}
        pending = list(targets)
        next_batch_at = time.monotonic()
        deadline = time.monotonic() + settle_timeout + batch_gap * (
            len(targets) // max(1, batch_size) + 1
        )
        disconnected = False
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                report = tracker.report(settled=True)
                return tracker, JoinReport(
                    "stopped", report.joined, report.missing, report.rejected,
                    "stop",
                )
            now = time.monotonic()
            if pending and now >= next_batch_at:
                current = pending[: max(1, batch_size)]
                del pending[: max(1, batch_size)]
                for channel in current:
                    self.send_raw(f"JOIN {channel}")
                next_batch_at = now + batch_gap
            try:
                line = self._readline(0.5)
            except TimeoutError:
                if not pending and not tracker.report(settled=False).missing:
                    break
                continue
            if line is None:
                disconnected = True
                break
            self._handle_line(line, tracker, on_privmsg, heartbeat=None)
            if not pending and not tracker.report(settled=False).missing:
                break
        report = tracker.report(settled=True)
        if disconnected:
            report = JoinReport(
                "failed" if not report.joined else "degraded",
                report.joined,
                report.missing,
                report.rejected,
                "eof",
            )
        elif report.status == "failed" and not report.rejected:
            report = JoinReport(
                report.status,
                report.joined,
                report.missing,
                report.rejected,
                "timeout",
            )
        return tracker, report

    def listen(
        self,
        tracker: JoinTracker,
        *,
        on_privmsg: Callable[[dict[str, Any]], None],
        should_stop: Callable[[], bool],
        health_callback: Callable[[dict[str, Any]], None] | None = None,
        health_interval: float = 30.0,
        ping_interval: float = 90.0,
        pong_timeout: float = 20.0,
        rejoin_interval: float = 120.0,
        presence_snapshot_policy: PresenceSnapshotPolicy | None = None,
        snapshot_request_path: str | Path | None = None,
    ) -> str:
        heartbeat = Heartbeat(interval=ping_interval, timeout=pong_timeout)
        automatic_snapshot = (
            PresenceSnapshotScheduler(
                tracker.confirmed,
                slot=self.ticket.slot,
                collector_account_id=self.ticket.account_id,
                policy=presence_snapshot_policy,
            )
            if presence_snapshot_policy is not None else None
        )
        self._presence_snapshot_scheduler = automatic_snapshot
        manual_snapshot: PresenceSnapshotScheduler | None = None
        manual_request: SnapshotRequest | None = None
        request_path = Path(snapshot_request_path) if snapshot_request_path else None
        next_request_poll = 0.0
        last_health = 0.0
        last_rejoin = time.monotonic()

        def finish_manual_request(
            status: str,
            *,
            error: str = "",
            result: dict[str, Any] | None = None,
        ) -> None:
            nonlocal manual_request, manual_snapshot
            if manual_request is None or request_path is None:
                return
            update_snapshot_request(
                request_path,
                manual_request,
                status=status,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=error,
                result=result or {},
            )
            manual_request = None
            manual_snapshot = None
            self._presence_snapshot_scheduler = automatic_snapshot

        def finish_session(reason: str) -> str:
            if manual_request is None:
                return reason
            try:
                finish_manual_request(
                    "cancelled" if reason == "stop" else "failed",
                    error=f"session_ended:{reason}",
                    result=(manual_snapshot.health() if manual_snapshot else {}),
                )
            except OSError as error:
                return f"storage_error:手动快照状态写入失败: {error}"
            except (RuntimeError, ValueError) as error:
                return f"internal_error:手动快照状态更新失败: {error}"
            return reason

        try:
            while True:
                if should_stop():
                    try:
                        self.send_raw("QUIT :collector stop")
                    except OSError:
                        pass
                    return finish_session("stop")
                now = time.monotonic()
                if heartbeat.expired(now):
                    return finish_session("pong_timeout")
                token = heartbeat.next_ping(now, self.ticket.slot)
                if token:
                    self.send_raw(f"PING :{token}")
                if now - last_rejoin >= rejoin_interval:
                    for channel in tracker.report(settled=True).missing:
                        self.send_raw(f"JOIN {channel}")
                    last_rejoin = now

                if (
                    request_path is not None
                    and presence_snapshot_policy is not None
                    and manual_request is None
                    and now >= next_request_poll
                    and (
                        automatic_snapshot is None
                        or automatic_snapshot.finished
                    )
                ):
                    next_request_poll = now + 5.0
                    request = load_snapshot_request(request_path)
                    if (
                        request is not None
                        and request.slot == self.ticket.slot
                        and request.status == "armed"
                    ):
                        try:
                            update_snapshot_request(
                                request_path,
                                request,
                                status="running",
                                started_at=datetime.now(timezone.utc).isoformat(),
                                worker_pid=os.getpid(),
                                worker_identity=pid_identity(os.getpid()) or "",
                            )
                        except OSError as error:
                            raise MessageSinkError(
                                f"手动快照状态写入失败: {error}"
                            ) from error
                        invalid_channels = sorted(
                            set(request.channels) - set(tracker.targets)
                        )
                        if not presence_snapshot_policy.manual_enabled:
                            manual_request = request
                            finish_manual_request(
                                "failed", error="manual_snapshot_disabled"
                            )
                        elif invalid_channels:
                            manual_request = request
                            finish_manual_request(
                                "failed",
                                error=f"channels_not_assigned:{','.join(invalid_channels)}",
                            )
                        else:
                            manual_request = request
                            manual_snapshot = PresenceSnapshotScheduler(
                                request.channels,
                                slot=self.ticket.slot,
                                collector_account_id=self.ticket.account_id,
                                policy=replace(
                                    presence_snapshot_policy,
                                    enabled=True,
                                    initial_delay_seconds=0.0,
                                ),
                                started_at=now,
                                stagger_by_slot=False,
                                expire_unjoined=True,
                            )
                            self._presence_snapshot_scheduler = manual_snapshot

                active_snapshot = manual_snapshot
                if active_snapshot is None and automatic_snapshot is not None:
                    if not automatic_snapshot.finished:
                        active_snapshot = automatic_snapshot
                if active_snapshot is not None:
                    command = active_snapshot.next_command(now, tracker.confirmed)
                    if command is not None:
                        self.send_raw(command)
                try:
                    line = self._readline(1.0)
                except TimeoutError:
                    line = ""
                if line is None:
                    return finish_session("eof")
                if line:
                    presence = (
                        parse_presence(line) if active_snapshot is not None else None
                    )
                    self._handle_line(line, tracker, on_privmsg, heartbeat)
                    if active_snapshot is not None:
                        if presence is not None:
                            active_snapshot.observe_presence(presence)
                        event = active_snapshot.observe_line(
                            line,
                            now=time.monotonic(),
                            joined_channels=tracker.confirmed,
                        )
                        if event is not None and self.on_presence is not None:
                            try:
                                self.on_presence({
                                    **event,
                                    "t": datetime.now(timezone.utc).isoformat(),
                                    "slot": self.ticket.slot,
                                })
                            except OSError as error:
                                raise MessageSinkError(str(error)) from error
                if manual_snapshot is not None and manual_snapshot.finished:
                    result = manual_snapshot.health()
                    finish_manual_request(
                        "complete"
                        if result["snapshot_coverage"] == "complete"
                        else "failed",
                        error=str(result.get("snapshot_last_error") or ""),
                        result=result,
                    )
                if health_callback and now - last_health >= health_interval:
                    try:
                        health_callback(self.health(tracker))
                    except OSError:
                        pass
                    last_health = now
        except MessageSinkError as error:
            return finish_session(f"storage_error:{error}")
        except OSError as error:
            return finish_session(f"socket_error:{error}")
        except Exception as error:
            return finish_session(f"internal_error:{error}")

    def probe_online_status(
        self,
        target_irc_nick: str,
        *,
        target_account_id: str = "",
        probe_channels: tuple[str, ...] = (),
        duration_seconds: float,
        query_interval_seconds: float,
        on_event: Callable[[str, str], None],
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """组合探测推送、昵称查询和稳定账号 ID 查询能力。"""
        normalized_probe_channels = normalize_probe_channels(probe_channels)
        analyzer = ProtocolProbeAnalyzer(
            target_irc_nick, target_account_id, normalized_probe_channels
        )
        started = time.monotonic()
        deadline = started + duration_seconds
        auxiliary_interval = (
            max(5.0, query_interval_seconds)
            if normalized_probe_channels else query_interval_seconds
        )
        next_ison_at = started + auxiliary_interval
        next_userhost_at = started + auxiliary_interval
        next_whois_at = started + max(30.0, query_interval_seconds * 3)
        next_identity_at = started + query_interval_seconds
        channel_settled_at = started + 3.0
        channel_sweep_requested = False
        channel_sweep = 0
        channel_command_queue: list[tuple[str, str, str, str]] = []
        next_channel_command_at = started
        heartbeat = Heartbeat(interval=90.0, timeout=60.0)
        line_count = 0
        sent_count = 0

        def emit(direction: str, line: str) -> None:
            try:
                on_event(direction, line)
            except OSError as error:
                raise MessageSinkError(str(error)) from error

        def send(command: str) -> None:
            nonlocal sent_count
            if target_account_id and command == identity_command:
                analyzer.begin_identity_query()
            self.send_raw(command)
            sent_count += 1
            emit("out", command)

        identity_flags = "du" if normalized_probe_channels else "u"
        identity_command = (
            f"WHO {target_account_id}_0 {identity_flags}%tnu,42"
            if target_account_id else ""
        )
        initial_commands = protocol_probe_commands(
            target_irc_nick,
            target_account_id,
            include_delayjoined=bool(normalized_probe_channels),
        )
        for channel in normalized_probe_channels:
            send(f"JOIN {channel}")
        for command in initial_commands[:5]:
            send(command)
        pending_research_commands = [
            command for command in initial_commands[5:]
            if not normalized_probe_channels or command != identity_command
        ]
        next_research_command_at = started + 1.0

        reason = "complete"
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                reason = "stopped"
                break
            now = time.monotonic()
            if heartbeat.expired(now):
                reason = "pong_timeout"
                break
            token = heartbeat.next_ping(now, f"probe-{self.ticket.slot}")
            if token:
                send(f"PING :{token}")
            channel_busy = bool(channel_command_queue) or analyzer.channel_queries_pending()
            if (
                pending_research_commands
                and now >= next_research_command_at
                and not channel_busy
            ):
                send(pending_research_commands.pop(0))
                next_research_command_at = now + 1.0
            if now >= next_ison_at:
                send(f"ISON {target_irc_nick}")
                next_ison_at = now + auxiliary_interval
            if target_account_id and now >= next_userhost_at:
                send(f"USERHOST {target_irc_nick}")
                next_userhost_at = now + auxiliary_interval
            if (
                target_account_id
                and now >= next_identity_at
                and not channel_busy
                and not analyzer.identity_queries_pending()
            ):
                send(identity_command)
                next_identity_at = now + query_interval_seconds
            if (
                channel_sweep_requested
                and now >= channel_settled_at
                and not channel_busy
            ):
                channel_sweep += 1
                channel_command_queue.extend(channel_probe_commands(
                    normalized_probe_channels
                ))
                channel_sweep_requested = False
                next_channel_command_at = now
            if channel_command_queue and now >= next_channel_command_at:
                channel, variant, query_type, command = channel_command_queue.pop(0)
                analyzer.begin_channel_query(
                    channel, variant, query_type, channel_sweep
                )
                send(command)
                next_channel_command_at = now + 0.1
            if now >= next_whois_at:
                send(f"WHOIS {target_irc_nick}")
                next_whois_at = now + max(30.0, query_interval_seconds * 3)
            try:
                line = self._readline(min(1.0, max(0.01, deadline - now)))
            except TimeoutError:
                continue
            if line is None:
                reason = "eof"
                break
            self.last_line_at = time.time()
            line_count += 1
            emit("in", line)
            if line.startswith("PING"):
                send("PONG" + line[4:])
                continue
            heartbeat.observe(line)
            monitor_online_before = analyzer.monitor_online_seen
            ison_online_before = analyzer.ison_online_seen
            ison_current_before = analyzer.ison_current_online
            identity_current_before = analyzer.identity_current_online
            analyzer.observe(line)
            if (
                normalized_probe_channels
                and analyzer.identity_current_online is not None
                and analyzer.identity_current_online != identity_current_before
                and (
                    identity_current_before is not None
                    or analyzer.identity_current_online
                )
            ):
                channel_sweep_requested = True
            if (
                (analyzer.monitor_online_seen and not monitor_online_before)
                or (analyzer.ison_online_seen and not ison_online_before)
            ):
                send(f"WHOIS {target_irc_nick}")
                next_whois_at = time.monotonic() + max(
                    30.0, query_interval_seconds * 3
                )
            if ison_current_before and not analyzer.ison_current_online:
                send(f"WHOWAS {target_irc_nick} 1")

        return {
            "reason": reason,
            "duration_seconds": round(time.monotonic() - started, 3),
            "received_line_count": line_count,
            "sent_command_count": sent_count,
            **analyzer.summary(),
        }

    def health(self, tracker: JoinTracker) -> dict[str, Any]:
        report = tracker.report(settled=True)
        health = {
            "status": "listening" if report.status == "healthy" else report.status,
            "joined": list(report.joined),
            "missing": list(report.missing),
            "rejected": report.rejected,
            "privmsg_count": self.privmsg_count,
            "privmsg_by_channel": dict(self.by_channel),
            "last_line_at": self.last_line_at,
        }
        if self._presence_snapshot_scheduler is not None:
            health.update(self._presence_snapshot_scheduler.health())
        return health

    def send_raw(self, message: str) -> None:
        if self.socket is None:
            raise OSError("IRC socket 尚未连接")
        normalized = message.replace("\r", "").replace("\n", "").strip()
        self.socket.sendall((normalized + "\r\n").encode("utf-8", "replace"))

    def _readline(self, remaining: float) -> str | None:
        if self.socket is None:
            return None
        deadline = time.monotonic() + max(0.01, remaining)
        while b"\r\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("IRC readline deadline")
            self.socket.settimeout(min(1.0, left))
            try:
                chunk = self.socket.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                return None
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\r\n", 1)
        return raw.decode("utf-8", "replace")

    def _handle_line(
        self,
        line: str,
        tracker: JoinTracker,
        on_privmsg: Callable[[dict[str, Any]], None],
        heartbeat: Heartbeat | None,
    ) -> None:
        self.last_line_at = time.time()
        if line.startswith("PING"):
            self.send_raw("PONG" + line[4:])
            return
        if heartbeat:
            heartbeat.observe(line)
        tracker.observe(line)
        presence = parse_presence(line)
        if presence is not None and self.on_presence is not None:
            irc_nick = presence.new_nick or presence.nick
            old_irc_nick = presence.nick if presence.new_nick else ""
            nick, platform = resolve_player_identity(irc_nick, raw=line)
            old_nick = ""
            if old_irc_nick:
                old_nick, _ = resolve_player_identity(
                    old_irc_nick, explicit_platform=platform, raw=line,
                )
            try:
                self.on_presence({
                    "t": datetime.now(timezone.utc).isoformat(),
                    "slot": self.ticket.slot,
                    "type": presence.event_type,
                    "nick": nick,
                    "irc_nick": irc_nick,
                    "old_nick": old_nick,
                    "old_irc_nick": old_irc_nick,
                    "platform": platform,
                    "sender_id": presence.sender_id,
                    "chan": presence.chan,
                    "account": self.ticket.nick,
                    "raw": line,
                })
            except OSError as error:
                raise MessageSinkError(str(error)) from error
        message = parse_privmsg(line)
        if message is None:
            return
        nick, platform = resolve_player_identity(message.nick, raw=line)
        try:
            on_privmsg(
                {
                    "t": datetime.now(timezone.utc).isoformat(),
                    "slot": self.ticket.slot,
                    "nick": nick,
                    "irc_nick": message.nick,
                    "platform": platform,
                    "sender_id": message.sender_id,
                    "chan": message.chan,
                    "text": message.text,
                    "account": self.ticket.nick,
                }
            )
        except OSError as error:
            raise MessageSinkError(str(error)) from error
        self.privmsg_count += 1
        self.by_channel[message.chan] = self.by_channel.get(message.chan, 0) + 1

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

    def __enter__(self) -> "IrcSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
