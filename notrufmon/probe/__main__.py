"""python3 -m notrufmon.probe --config /etc/notrufmon/probe.toml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .agent import Probe, load_local_config


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="notrufmon.probe")
    p.add_argument("--config", default="/etc/notrufmon/probe.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    probe = Probe(load_local_config(a.config))

    async def _run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)
        await probe.run(stop)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
