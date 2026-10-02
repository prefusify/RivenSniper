"""快速获取压力测试：真实匹配/目标去重/入队，投递终点为内存计数器。

默认 mock；--live 显式启用 WM 请求与私有代理配置。不会连接 QQ/Discord。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import httpx
from nonebot import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.plugins.riven_sniper.config import Config
from src.plugins.riven_sniper.poller import SniperPoller, _HitBatch, _HitCard
from src.plugins.riven_sniper.store import Store
from src.plugins.riven_sniper.wfm_fast import ProxyPool, QueryKey, query_keys
from src.plugins.riven_sniper.wm_proxy import proxy_routes


def fixtures() -> list[QueryKey]:
    weapons = ('torid', 'laetum', 'felarx', 'phenmor', 'strun', 'vectis',
               'phantasma', 'dual_toxocyst', 'latron')
    cc, cd, ms = 'critical_chance', 'critical_damage', 'multishot'
    dmg, fr = 'base_damage_/_melee_damage', 'fire_rate_/_attack_speed'
    triples = [(cc, cd, ms), (dmg, fr, ms), (cd, ms, 'status_chance'),
               (dmg, ms, 'toxin_damage'), (dmg, ms, 'cold_damage'),
               (cc, ms, fr), (cc, cd, fr), (cd, ms, 'punch_through')]
    return [QueryKey(w, tuple(sorted(t))) for w in weapons for t in triples]


def auction(key: QueryKey, aid: str, created: str) -> dict:
    return {'id': aid, 'created': created, 'closed': False, 'private': False,
            'visible': True, 'starting_price': 100, 'buyout_price': 100,
            'is_direct_sell': True, 'owner': {'ingame_name': 'BenchmarkSeller'},
            'item': {'type': 'riven', 'weapon_url_name': key.weapon,
                     'name': 'benchmark', 'mod_rank': 8, 're_rolls': 0,
                     'mastery_level': 8, 'polarity': 'madurai',
                     'attributes': [{'url_name': s, 'positive': True, 'value': 100}
                                    for s in key.positives]}}


def percentile(values, p):
    rows = sorted(values)
    return rows[int((len(rows)-1)*p)] if rows else None


async def run(args) -> dict:
    store = Store(':memory:')
    if args.rules_file:
        configs = json.loads(args.rules_file.read_text(encoding='utf-8'))['configs']
        # 规则所属目标已脱敏；仅复制条件，不连接真实平台或复制账号信息。
        scopes = {}
        for cfg in configs:
            source_scope = cfg['group_id']
            if source_scope not in scopes:
                if cfg.get('platform') == 'discord':
                    target = store.upsert_discord_target(str(10**17 + source_scope), enabled=True)
                    scope = target['scope_id']
                else:
                    scope = source_scope
                    store.upsert_qq_target(scope, 1, enabled=True)
                scopes[source_scope] = scope
                store.set_target_wm_fast_enabled(scope, True)
            scope = scopes[source_scope]
            store.add_config(scope, **{k: cfg[k] for k in (
                'weapon', 'wildcard', 'positives', 'positive_ratings',
                'negatives', 'negative_ratings', 'zero_rerolls')})
        keys = sorted({key for cfg in configs for key in query_keys(cfg)})
    else:
        keys = fixtures()[:args.queries or 72]
        for scope in (101, 102, 103):
            store.upsert_qq_target(scope, 1, enabled=True)
            store.set_target_wm_fast_enabled(scope, scope != 103)
            for key in keys:
                store.add_config(scope, weapon=key.weapon, wildcard=None,
                                 positives=[[s] for s in key.positives],
                                 negatives=[['__any_attribute__']])
    config = Config(wm_fast_interval=args.interval, wm_fast_proxy_config=args.proxy_config,
                    discord_dm_enabled=bool(args.rules_file))
    poller = SniperPoller(store, config)
    deliveries, counts = set(), Counter()
    def sink(item):
        cards = item.payload.cards if isinstance(item.payload, _HitBatch) else (item.payload,)
        for card in cards:
            if isinstance(card, _HitCard):
                pair = (item.target, card.auction['id'])
                counts['duplicates'] += pair in deliveries
                deliveries.add(pair)
                counts[f'target_{item.target}'] += 1
        return True
    poller.enqueue_delivery = sink
    search_seen, recent_seen, latest = {}, {}, {}
    original_fast = poller.fast.on_auctions
    async def on_fast(rows, configs):
        for row in rows:
            search_seen.setdefault(row['id'], time.time())
        await original_fast(rows, configs)
    poller.fast.on_auctions = on_fast
    original_recent = poller.wfm.recent_auctions
    async def recent():
        rows = await original_recent() if args.live else list(latest.values())
        for row in rows:
            recent_seen.setdefault(row['id'], time.time())
        return rows
    poller.wfm.recent_auctions = recent
    if args.live:
        data = json.loads(Path(args.proxy_config).read_text(encoding='utf-8'))
        data['proxies'] = data['proxies'][:args.exits]
        urls, poller.fast.tunnel = proxy_routes(data)
        poller.fast.pool = ProxyPool(urls)
        if poller.fast.tunnel:
            poller.fast.tunnel.start()
    else:
        bodies = {}
        for index, key in enumerate(keys):
            # 498 条存量 + 一条可变化订单，覆盖接近接口上限的解析和基线处理。
            rows = [auction(key, f'old-{index}-{i}', '2020-01-01T00:00:00Z')
                    for i in range(498)]
            bodies[(key.weapon, key.positives)] = json.dumps(rows)[:-1].encode()
        rounds = Counter()
        async def handler(request):
            await asyncio.sleep(.02)
            key = QueryKey(request.url.params['weapon_url_name'],
                           tuple(request.url.params['positive_stats'].split(',')))
            rounds[key] += 1
            sequence = rounds[key] // 5
            aid = f'new-{keys.index(key)}-{sequence}'
            if key not in latest or latest[key]['id'] != aid:
                row = auction(key, aid, datetime.now(timezone.utc).isoformat())
                row['item']['attributes'].append(
                    {'url_name': 'zoom', 'positive': False, 'value': -20})
                latest[key] = row
            content = (b'{"payload":{"auctions":' + bodies[(key.weapon, key.positives)]
                       + b',' + json.dumps(latest[key]).encode() + b']}}')
            return httpx.Response(200, content=content)
        poller.fast.pool = ProxyPool(
            [f'http://mock-{i}' for i in range(args.exits)],
            client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    lag = []
    async def heartbeat():
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(.02)
            lag.append(max(0, loop.time() - start - .02))
    async def ordinary():
        while True:
            try:
                await poller._poll_once()
            except Exception as error:
                counts['ordinary_errors'] += 1
                print(json.dumps({'ordinary_error': type(error).__name__}), flush=True)
            await asyncio.sleep(15 if args.live else 2)
    tasks = [asyncio.create_task(poller.fast.run()), asyncio.create_task(heartbeat()),
             asyncio.create_task(ordinary())]
    started, cpu_started = time.monotonic(), time.process_time()
    report = {}
    try:
        while time.monotonic() - started < args.duration:
            await asyncio.sleep(min(10, args.duration - (time.monotonic() - started)))
            snapshot = poller.fast.snapshot()
            print(json.dumps({'elapsed': round(time.monotonic()-started, 1),
                              'state': snapshot['state'], 'pool': snapshot['pool'],
                              'interval_p95': snapshot['actual_interval_p95'],
                              'deliveries': sum(v for k,v in counts.items() if k.startswith('target_'))}), flush=True)
            if args.live and snapshot['pool'] and snapshot['pool'].get('rate_limited', 0) >= 3:
                counts['stopped_on_rate_limit'] += 1
                break
        snapshot = poller.fast.snapshot()
        common = search_seen.keys() & recent_seen.keys()
        report = {'mode': 'live' if args.live else 'mock', 'query_count': len(keys),
                  'rules_source': 'provided' if args.rules_file else 'fixtures',
                  'rule_count': len(store.list_configs()),
                  'target_platforms': dict(Counter(t['platform'] for t in store.list_targets())),
                  'elapsed_seconds': time.monotonic()-started,
                  'cpu_seconds': time.process_time()-cpu_started,
                  'requested_interval': args.interval,
                  'actual_interval_p95': snapshot['actual_interval_p95'],
                  'event_loop_lag_p95': percentile(lag, .95),
                  'event_loop_lag_max': max(lag, default=0),
                  'pool': snapshot['pool'], 'deliveries': dict(counts),
                  'truncated_queries': sum(q['state']=='truncated' for q in snapshot['queries']),
                  'lead_samples': len(common),
                  'search_lead_seconds': {
                      'p50': percentile([recent_seen[i]-search_seen[i] for i in common], .5),
                      'p95': percentile([recent_seen[i]-search_seen[i] for i in common], .95)}}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await poller.wfm.close()
        store.close()
    return report


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    logger.remove()
    logger.add(sys.stderr, level='WARNING', format='{message}')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--duration', type=float, default=60)
    parser.add_argument('--queries', type=int, choices=range(1,73))
    parser.add_argument('--rules-file', type=Path)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--exits', type=int, default=512)
    parser.add_argument('--proxy-config', default='.runtime/wm_fast_proxy.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.rules_file and args.queries is not None:
        parser.error('--rules-file 使用文件全部规则，不能同时指定 --queries')
    if args.duration <= 0 or not 1 <= args.interval <= 60 or args.exits <= 0:
        parser.error('duration 和 exits 必须大于 0，interval 必须在 1～60 秒')
    result = asyncio.run(run(args))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
