"""Usage: python scripts/shim_connect_probe.py N HOGS[,HOGS...] [burst]

Evidence for failopen_shim.DEFAULT_CONNECT_TIMEOUT_S (docs/BUGS.md P010-2).
Connect-time to a local aiohttp server under synthetic CPU load.
Server and client are separate processes; load = K busy-loop processes.
Client uses httpx (same stack as the shim) with a fresh connection per call."""
import asyncio, multiprocessing as mp, socket, sys, time, json, statistics
import httpx
from aiohttp import web

def serve(q):
    async def h(r): return web.Response(text="ok")
    async def main():
        app = web.Application(); app.router.add_route("*", "/{p:.*}", h)
        run = web.AppRunner(app); await run.setup()
        s = web.TCPSite(run, "127.0.0.1", 0); await s.start()
        q.put(s._server.sockets[0].getsockname()[1])
        await asyncio.Event().wait()
    asyncio.run(main())

def spin():
    while True: pass

async def measure(port, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        out.append((time.perf_counter() - t) * 1000)
        w.close()
        await asyncio.sleep(0.005)
    return out

async def measure_httpx(port, n):
    # full httpx request, connect timeout generous; time the whole call
    out = []
    async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0), trust_env=False) as c:
        for _ in range(n):
            t = time.perf_counter()
            await c.get(f"http://127.0.0.1:{port}/x", timeout=httpx.Timeout(10))
            out.append((time.perf_counter() - t) * 1000)
            await asyncio.sleep(0.005)
    return out

def pct(v, p): v = sorted(v); return v[min(len(v)-1, int(p/100*len(v)))]

async def one(port):
    t = time.perf_counter()
    _, w = await asyncio.open_connection("127.0.0.1", port)
    d = (time.perf_counter() - t) * 1000
    w.close()
    return d


async def bursts(port, nb, width):
    out = []
    for _ in range(nb):
        out += await asyncio.gather(*[one(port) for _ in range(width)])
        await asyncio.sleep(0.02)
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]); levels = [int(x) for x in sys.argv[2].split(",")]
    q = mp.Queue(); srv = mp.Process(target=serve, args=(q,), daemon=True); srv.start(); port = q.get()
    res = {}
    for k in levels:
        hogs = [mp.Process(target=spin, daemon=True) for _ in range(k)]
        [h.start() for h in hogs]; time.sleep(2)
        import os; la = os.getloadavg()[0]
        if len(sys.argv) > 3 and sys.argv[3] == "burst":  # n bursts of 50 concurrent connects
            v = asyncio.run(bursts(port, n, 50)); [h.terminate() for h in hogs]; time.sleep(1)
            print(json.dumps({"hogs": k, "load1": round(la, 1), "n": len(v), "p99": round(pct(v, 99), 2),
                              "max": round(max(v), 2), "over200": sum(x > 200 for x in v)}), flush=True)
            continue
        a = asyncio.run(measure(port, n)); b = asyncio.run(measure_httpx(port, n))
        [h.terminate() for h in hogs]; time.sleep(1)
        row = {"hogs": k, "load1_at_end": round(la, 1), "n": n}
        for name, v in (("tcp_connect", a), ("httpx_request", b)):
            row[name] = {"p50": round(pct(v,50),2), "p99": round(pct(v,99),2), "p999": round(pct(v,99.9),2), "max": round(max(v),2),
                         "over200": sum(x > 200 for x in v), "over500": sum(x > 500 for x in v), "over1000": sum(x > 1000 for x in v)}
        print(json.dumps(row), flush=True)
