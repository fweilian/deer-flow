"""Gateway launcher that configures Windows event loop policy before Uvicorn starts."""

from __future__ import annotations

import asyncio
import os
import sys


def _configure_windows_event_loop_policy() -> None:
    if sys.platform != "win32":
        return

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:
        return

    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, policy_cls):
        return

    asyncio.set_event_loop_policy(policy_cls())


def main() -> None:
    _configure_windows_event_loop_policy()

    import uvicorn

    host = os.getenv("DEERFLOW_GATEWAY_HOST", "0.0.0.0")
    port = int(os.getenv("DEERFLOW_GATEWAY_PORT", "8001"))
    reload_enabled = os.getenv("DEERFLOW_GATEWAY_RELOAD", "").lower() in {"1", "true", "yes", "on"}

    uvicorn.run(
        "app.gateway.app:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
