import asyncio, time
from fastapi import FastAPI
import httpx

app = FastAPI()

# 1) 同步 def + time.sleep：FastAPI 把它丢进线程池执行
@app.get("/blocking-sync")
def blocking_sync():
    time.sleep(1)
    return {"ok": True}

# 2) async def，但内部仍然用阻塞版 time.sleep —— 最常见的坑
@app.get("/blocking-async")
async def blocking_async():
    time.sleep(1)            # 阻塞了唯一的事件循环线程！
    return {"ok": True}

# 3) async def + await asyncio.sleep —— 正确姿势
@app.get("/nonblocking-async")
async def nonblocking_async():
    await asyncio.sleep(1)   # 让出控制权，事件循环去处理别的请求
    return {"ok": True}

async def measure(path, n):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        await asyncio.gather(*[client.get(path) for _ in range(n)])
        elapsed = time.perf_counter() - start
    print(f"{path:<22} {n:>3} 并发，总耗时 {elapsed:5.2f}s")

async def main():
    for n in (10, 100, 200):
        await measure("/blocking-sync", n)
        await measure("/nonblocking-async", n)

asyncio.run(main())
