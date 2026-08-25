#!/usr/bin/env python3
"""Start Nostalgia Line.

    python run.py                 # http://127.0.0.1:8777
    python run.py --host 0.0.0.0  # reachable from the LAN / a container
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Nostalgia Line")
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default 8777)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--reload", action="store_true", help="auto-reload for development")
    args = parser.parse_args()

    if args.config:
        os.environ["NOSTALGIA_CONFIG"] = str(Path(args.config).resolve())

    # Import after NOSTALGIA_CONFIG is set - module import builds the app state.
    import uvicorn

    from nostalgia_line.server import state

    host = args.host or os.getenv("NOSTALGIA_HOST") or state.cfg.server.host
    port = args.port or int(os.getenv("NOSTALGIA_PORT") or state.cfg.server.port)

    print(f"Nostalgia Line -> http://{host}:{port}")
    print(f"  config    {state.config_path}")
    print(f"  channels  {len(state.catalog)} ({len(state.defaults)} default assignments)")
    if not state.configured:
        print("  ! Plex and TMDB are not configured yet - open the Settings tab.")

    uvicorn.run(
        "nostalgia_line.server:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
