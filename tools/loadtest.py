"""aql-scoreboard の負荷試験。

閲覧者 N 人がブラウザと同じく「応答を受けてから 1.5 秒後に次の取得」を繰り返し、
出題者 1 人が入力画面の取得（1.5 秒ごと）と、30 秒ごとの判定の送信を行う。

使い方: uv run python tools/loadtest.py BASE GAME_ID PASSWORD VIEWERS SECONDS [OUT.json]
  BASE は閲覧画面の URL の前半（例: https://aql-sokuhou.q-koken.com/score）。
  試合は管理者画面で先に作っておく。OUT.json に集計、OUT.json.raw に個々のリクエストを書く。
  複数の部屋は、試合ごとにこのスクリプトを並べて動かす。結果は docs/nonfunctional-test.md
"""

import asyncio
import json
import random
import statistics
import sys
import time

import httpx

BASE, GID, PW = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
VIEWERS, SECONDS = int(sys.argv[4]), float(sys.argv[5])
OUT = sys.argv[6] if len(sys.argv) > 6 else None
POLL, ACTION_EVERY = 1.5, 30.0

records = []  # (kind, start, latency, status, bytes)

# この WSL は名前解決がときどき失敗するので、成功した結果を覚えて使い回す（測りたいのはサーバー側）
import socket
_orig_gai = socket.getaddrinfo
_gai_cache = {}


def _cached_gai(host, port, *args, **kwargs):
    key = (host, port) + args + tuple(sorted(kwargs.items()))
    if key not in _gai_cache:
        for i in range(20):
            try:
                _gai_cache[key] = _orig_gai(host, port, *args, **kwargs)
                break
            except socket.gaierror:
                if i == 19:
                    raise
                time.sleep(0.3)
    return _gai_cache[key]


socket.getaddrinfo = _cached_gai


async def timed(kind, coro):
    t = time.monotonic()
    try:
        r = await coro
        records.append((kind, t, time.monotonic() - t, r.status_code, len(r.content), time.time()))
        return r
    except Exception as e:
        records.append((kind, t, time.monotonic() - t, type(e).__name__, 0, time.time()))
        return None


async def viewer(end):
    # 1人1接続（ブラウザのタブ1つに相当）。開始時刻はばらけさせる
    async with httpx.AsyncClient(timeout=10) as c:
        await asyncio.sleep(random.uniform(0, POLL))
        while time.monotonic() < end:
            await timed("viewer", c.get(f"{BASE}/api/games/{GID}/state"))
            await asyncio.sleep(POLL)


ACTIONS = [
    {"type": "correct", "team": 0, "seat": 0}, {"type": "correct", "team": 1, "seat": 1},
    {"type": "through"}, {"type": "wrong", "team": 0, "seat": 2},
    {"type": "correct", "team": 1, "seat": 3}, {"type": "correct", "team": 0, "seat": 4},
]


async def quizmaster(end):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{BASE}/input/{GID}/login", json={"password": PW})
        assert r.status_code == 200, r.text

        async def poll():
            while time.monotonic() < end:
                await timed("input_poll", c.get(f"{BASE}/input/{GID}/api/state"))
                await asyncio.sleep(POLL)

        async def act():
            i = 0
            await asyncio.sleep(5)
            while time.monotonic() < end:
                body = ACTIONS[i % len(ACTIONS)]
                r = await timed("action", c.post(f"{BASE}/input/{GID}/api/action", json=body))
                if r is not None and r.status_code == 409:  # 試合終了など → リセットして続ける
                    await timed("action", c.post(f"{BASE}/input/{GID}/api/action", json={"type": "reset"}))
                i += 1
                await asyncio.sleep(max(0, min(ACTION_EVERY, end - time.monotonic())))

        await asyncio.gather(poll(), act())


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


async def main():
    t0 = time.monotonic()
    end = t0 + SECONDS
    await asyncio.gather(quizmaster(end), *[viewer(end) for _ in range(VIEWERS)])
    elapsed = time.monotonic() - t0
    summary = {"viewers": VIEWERS, "seconds": round(elapsed, 1), "total": len(records),
               "rps": round(len(records) / elapsed, 2), "kinds": {}}
    for kind in ("viewer", "input_poll", "action"):
        rs = [r for r in records if r[0] == kind]
        ok = [r for r in rs if r[3] == 200]
        lat = [r[2] * 1000 for r in ok]
        errs = {}
        for r in rs:
            if r[3] != 200:
                errs[str(r[3])] = errs.get(str(r[3]), 0) + 1
        summary["kinds"][kind] = {
            "n": len(rs), "ok": len(ok), "errors": errs,
            "p50_ms": round(pct(lat, .5), 1) if lat else None, "p95_ms": round(pct(lat, .95), 1) if lat else None,
            "p99_ms": round(pct(lat, .99), 1) if lat else None, "max_ms": round(max(lat), 1) if lat else None,
            "mean_ms": round(statistics.mean(lat), 1) if lat else None,
            "bytes_avg": round(statistics.mean([r[4] for r in ok])) if ok else None,
        }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if OUT:
        json.dump(summary, open(OUT, "w"), ensure_ascii=False)
        # 個々のリクエスト（種類・終了時刻・応答時間・状態）。時間変化と部屋をまたいだ集計に使う
        json.dump([(r[0], round(r[5], 3), round(r[2] * 1000, 1), r[3]) for r in records], open(OUT + ".raw", "w"))


asyncio.run(main())
