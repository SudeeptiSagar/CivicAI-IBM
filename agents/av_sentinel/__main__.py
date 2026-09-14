"""Container entrypoint: `python -m agents.av_sentinel`."""

from __future__ import annotations

import os

from agents.av_sentinel.agent import Sentinel
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    sentinel = Sentinel(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "sentinel-1"))
    sentinel.run_forever()


if __name__ == "__main__":
    main()
