#!/usr/bin/env python3
"""常驻 ngrok 隧道：将本地 HTTP 服务暴露到公网，并在日志中输出可访问 URL。

依赖:
  pip3 install pyngrok -i https://mirrors.aliyun.com/pypi/simple/

认证（免费账号需 token）:
  export NGROK_AUTHTOKEN=your_token

用法:
  python3 tools/ngrok_tunnel_daemon.py
  python3 tools/ngrok_tunnel_daemon.py --port 8767
  python3 tools/ngrok_tunnel_daemon.py --addr localhost:8767 --metadata outfit-dev

日志中会输出形如:
  ngrok public url: https://xxxx.ngrok-free.app -> http://localhost:8767
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_shutdown = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    logger.info("received signal %s, shutting down", signum)
    _shutdown = True


def _tunnel_public_url(tunnel: Any) -> str:
    url = getattr(tunnel, "public_url", None)
    if url:
        return str(url)
    text = str(tunnel)
    if "https://" in text:
        start = text.index("https://")
        end = text.find('"', start)
        if end > start:
            return text[start:end]
    return text


def _connect_once(addr: str, metadata: str) -> Any:
    from pyngrok import ngrok

    return ngrok.connect(addr, metadata=metadata)


def _disconnect_all() -> None:
    from pyngrok import ngrok

    try:
        ngrok.disconnect()
    except Exception:
        logger.exception("ngrok disconnect failed")
    try:
        ngrok.kill()
    except Exception:
        logger.debug("ngrok kill skipped or failed", exc_info=True)


def run_daemon(
    addr: str,
    metadata: str,
    reconnect_interval: float,
) -> int:
    global _shutdown
    tunnel: Optional[Any] = None

    while not _shutdown:
        try:
            tunnel = _connect_once(addr, metadata)
            public = _tunnel_public_url(tunnel)
            logger.info(
                "ngrok tunnel ready | public_url=%s | local=%s",
                public,
                addr,
            )
            logger.info(
                "Pls click the link %s -> %s",
                public,
                f"http://{addr}" if "://" not in addr else addr,
            )
        except Exception:
            logger.exception("failed to start ngrok tunnel for %s", addr)
            if reconnect_interval <= 0 or _shutdown:
                return 1
            logger.info(
                "retry in %.1fs",
                reconnect_interval,
            )
            time.sleep(reconnect_interval)
            continue

        while not _shutdown:
            time.sleep(1.0)

        break

    if tunnel is not None:
        _disconnect_all()
    logger.info("ngrok tunnel daemon stopped")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep ngrok tunnel alive and log public URL.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8767,
        help="Local port to expose (default: 8767)",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Local host (default: localhost)",
    )
    parser.add_argument(
        "--addr",
        default=None,
        help="Override host:port, e.g. localhost:8767",
    )
    parser.add_argument(
        "--metadata",
        default="outfit_rec ngrok tunnel",
        help="ngrok tunnel metadata",
    )
    parser.add_argument(
        "--reconnect-interval",
        type=float,
        default=10.0,
        help="Seconds before retry if connect fails (0 = no retry)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [ngrok-daemon] %(message)s",
        stream=sys.stdout,
        force=True,
    )

    addr = args.addr or f"{args.host}:{args.port}"
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "starting ngrok daemon | target=%s | metadata=%s",
        addr,
        args.metadata,
    )
    if not __import__("os").environ.get("NGROK_AUTHTOKEN"):
        logger.warning(
            "NGROK_AUTHTOKEN not set; free ngrok may require auth token",
        )

    return run_daemon(
        addr=addr,
        metadata=args.metadata,
        reconnect_interval=args.reconnect_interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
