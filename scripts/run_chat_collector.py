"""独立聊天采集器操作入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")

from src.plugins.riven_sniper.chat_collector.config import (  # noqa: E402
    ALL_SLOTS,
    CollectorConfigError,
    load_presence_snapshot_policy,
    load_shards,
    topology_for_mode,
)
from src.plugins.riven_sniper.chat_collector.presence_snapshot import (  # noqa: E402
    SnapshotRequest,
    arm_snapshot_request,
    cancel_snapshot_request,
)
from src.plugins.riven_sniper.chat_collector.protocol_probe import (  # noqa: E402
    PROBE_PLATFORMS,
    ProtocolProbeRequest,
    arm_protocol_probe,
)
from src.plugins.riven_sniper.chat_collector.runtime import (  # noqa: E402
    pid_matches,
    read_json,
)
from src.plugins.riven_sniper.chat_collector.supervisor import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    CollectorLayout,
    collector_topology,
    collector_status,
    default_shards_path,
    initialize_layout,
    request_stop,
    run_slot,
    run_supervisor,
    set_collector_mode,
    validate_layout,
)


def _layout(value: str) -> CollectorLayout:
    return CollectorLayout(Path(value).resolve())


_SNAPSHOT_PENDING_WORKER_STATUSES = {
    "authenticating", "joining", "listening", "degraded", "reconnecting",
}


def _cancel_stale_snapshot_request(
    layout: CollectorLayout,
    status: dict,
) -> tuple[str, str] | None:
    current = read_json(layout.snapshot_request_path)
    current_status = str((current or {}).get("status") or "")
    if current_status not in {"armed", "running"}:
        return None
    assert current is not None
    request = SnapshotRequest.from_dict(current)
    if current_status == "running":
        owner_alive = pid_matches(
            int(current.get("worker_pid") or 0),
            str(current.get("worker_identity") or ""),
        )
        reason = "stale_running_worker_exited"
    else:
        owner_state = status.get("slots", {}).get(request.slot, {})
        owner_alive = bool(
            owner_state.get("process_alive")
            and owner_state.get("status") in _SNAPSHOT_PENDING_WORKER_STATUSES
        )
        reason = "stale_armed_worker_not_running"
    if owner_alive:
        return None
    cancel_snapshot_request(
        layout.snapshot_request_path,
        request,
        expected_status=current_status,
        reason=reason,
    )
    return request.request_id, reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warframe 多拓扑独立 IRC 采集器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser, *, shards: bool = False) -> None:
        target.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
        if shards:
            target.add_argument("--shards", default="")

    initialize = subparsers.add_parser("init", help="创建运行目录与账号模板")
    common(initialize)
    validate = subparsers.add_parser("validate", help="验证账号、分片与 PSK")
    common(validate, shards=True)
    run = subparsers.add_parser("run", aliases=["start"], help="前台运行监督器")
    common(run, shards=True)
    run.add_argument("--plain", action="store_true", help=argparse.SUPPRESS)
    status = subparsers.add_parser("status", help="查看监督器与当前槽位状态")
    common(status)
    stop = subparsers.add_parser("stop", help="请求监督器和 worker 正常退出")
    common(stop)
    mode = subparsers.add_parser("mode", help="查看或切换 4/17 槽采集模式")
    common(mode)
    mode.add_argument("value", nargs="?", choices=["4", "17"])
    probe = subparsers.add_parser(
        "probe", help="装载下一张指定槽票据执行的在线查询协议探测"
    )
    common(probe)
    probe.add_argument("--slot", default="B", choices=list(ALL_SLOTS))
    probe.add_argument("--target-nick", required=True)
    probe.add_argument("--target-id", default="")
    probe.add_argument(
        "--channel",
        action="append",
        default=[],
        help="加入并执行普通 WHO 与 WHO d 对照的频道，可重复",
    )
    probe.add_argument("--platform", default="windows", choices=PROBE_PLATFORMS)
    probe.add_argument("--duration", type=float, default=600.0)
    probe.add_argument("--interval", type=float, default=10.0)
    probe_status = subparsers.add_parser("probe-status", help="查看当前协议探测请求与结果")
    common(probe_status)
    snapshot = subparsers.add_parser(
        "snapshot", help="让运行中的指定槽执行一次 Delayed-WHOX 成员快照"
    )
    common(snapshot, shards=True)
    snapshot.add_argument("--slot", required=True, choices=list(ALL_SLOTS))
    snapshot.add_argument(
        "--channel", action="append", default=[], help="指定频道，可重复"
    )
    snapshot.add_argument(
        "--channels", nargs="+", default=[], help="一次指定一个或多个频道"
    )
    snapshot_status = subparsers.add_parser(
        "snapshot-status", help="查看当前手动成员快照请求"
    )
    common(snapshot_status)
    slot = subparsers.add_parser("slot", help=argparse.SUPPRESS)
    common(slot, shards=True)
    slot.add_argument("--slot", required=True, choices=list(ALL_SLOTS))
    slot.add_argument("--mode", default="", choices=["4", "17"])
    slot.add_argument("--run-id", default="standalone", help=argparse.SUPPRESS)
    slot.add_argument("--plain", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    layout = _layout(args.runtime_root)
    if args.command == "init":
        print(json.dumps(initialize_layout(layout), ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate":
        selected = collector_topology(layout)
        shards_path = Path(args.shards).resolve() if args.shards else None
        problems = validate_layout(
            layout, shards_path=shards_path, topology=selected,
        )
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
        return int(bool(problems))
    if args.command in {"run", "start"}:
        try:
            selected = collector_topology(layout)
            shards_path = Path(args.shards).resolve() if args.shards else None
            return run_supervisor(
                layout,
                script_path=Path(__file__).resolve(),
                shards_path=shards_path,
                topology=selected,
                plain=args.plain,
            )
        except (CollectorConfigError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "slot":
        selected = (
            topology_for_mode(args.mode)
            if args.mode else collector_topology(layout)
        )
        shards_path = Path(args.shards).resolve() if args.shards else None
        return run_slot(
            args.slot,
            layout,
            shards_path=shards_path,
            topology=selected,
            plain=args.plain,
            run_id=args.run_id,
        )
    if args.command == "stop":
        request_stop(layout)
        print(json.dumps({"ok": True, "stop_requested": True}, ensure_ascii=False))
        return 0
    if args.command == "mode":
        try:
            selected = (
                set_collector_mode(layout, args.value)
                if args.value else collector_topology(layout)
            )
        except (CollectorConfigError, OSError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps({
            "mode": selected.mode,
            "slots": list(selected.slots),
            "accounts_path": str(layout.accounts_path_for(selected)),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "probe":
        try:
            selected = collector_topology(layout)
            if args.slot not in selected.slots:
                raise ValueError(
                    f"槽 {args.slot} 不属于当前 {selected.mode} 槽模式"
                )
            request = ProtocolProbeRequest.create(
                slot=args.slot,
                target_nick=args.target_nick,
                target_platform=args.platform,
                target_account_id=args.target_id,
                probe_channels=args.channel,
                duration_seconds=args.duration,
                query_interval_seconds=args.interval,
            )
            layout.ensure()
            arm_protocol_probe(layout.protocol_probe_path, request)
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(request.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "probe-status":
        print(json.dumps(
            read_json(layout.protocol_probe_path) or {"status": "not_armed"},
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if args.command == "snapshot":
        try:
            selected = collector_topology(layout)
            if args.slot not in selected.slots:
                raise ValueError(
                    f"槽 {args.slot} 不属于当前 {selected.mode} 槽模式"
                )
            shards_path = (
                Path(args.shards).resolve()
                if args.shards else default_shards_path(selected)
            )
            shards = load_shards(shards_path, topology=selected)
            policy = load_presence_snapshot_policy(shards_path)
            if not policy.manual_enabled:
                raise ValueError("当前分片配置已关闭手动 presence 快照")
            requested = tuple(args.channel) + tuple(args.channels)
            channels = requested or shards[args.slot]
            request = SnapshotRequest.create(slot=args.slot, channels=channels)
            invalid = sorted(set(request.channels) - set(shards[args.slot]))
            if invalid:
                raise ValueError(
                    f"槽 {args.slot} 未分配这些频道: {', '.join(invalid)}"
                )
            status = collector_status(layout, selected)
            layout.ensure()
            recovered = _cancel_stale_snapshot_request(layout, status)
            if recovered is not None:
                print(
                    f"已取消失效的手动快照请求 {recovered[0]}: {recovered[1]}",
                    file=sys.stderr,
                )
            state = status["slots"][args.slot]
            if not state.get("process_alive"):
                raise ValueError(f"槽 {args.slot} 的 worker 当前未运行")
            if state.get("status") not in {
                "joining", "listening", "degraded", "reconnecting",
            }:
                raise ValueError(
                    f"槽 {args.slot} 当前状态 {state.get('status')} 不接受手动快照"
                )
            arm_snapshot_request(layout.snapshot_request_path, request)
        except (CollectorConfigError, OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(request.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "snapshot-status":
        print(json.dumps(
            read_json(layout.snapshot_request_path) or {"status": "not_armed"},
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    print(json.dumps(collector_status(layout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
