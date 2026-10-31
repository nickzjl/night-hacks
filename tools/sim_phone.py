"""Pretend to be the iPhone app: stream a JPEG and fake telemetry to the backend.

  .venv/bin/python tools/sim_phone.py path/to/image.jpg [--host 127.0.0.1] [--seconds 10]
"""
import argparse
import asyncio
import json
import math
import time

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    jpeg = open(args.image, "rb").read()

    async with websockets.connect(f"ws://{args.host}:8765/ws/phone", max_size=None) as ws:
        async def reader():
            async for message in ws:
                data = json.loads(message)
                if data["type"] != "idle":
                    print("mac ->", data)

        task = asyncio.create_task(reader())
        start = time.monotonic()
        while time.monotonic() - start < args.seconds:
            t = time.monotonic() - start
            distance = 1.4 + math.sin(t) * 1.0  # sweep 0.4 m .. 2.4 m
            await ws.send(jpeg)
            await ws.send(json.dumps({"type": "telemetry", "distance": distance, "level": 0, "points": 400, "orientation": "up", "fps": 30}))
            await asyncio.sleep(0.3)
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
