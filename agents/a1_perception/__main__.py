"""Container entrypoint: `python -m agents.a1_perception`."""

from __future__ import annotations

import os

from agents.a1_perception import PerceptionAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = PerceptionAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "a1-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
