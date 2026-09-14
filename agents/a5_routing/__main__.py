"""Container entrypoint: `python -m agents.a5_routing`."""

from __future__ import annotations

import os

from agents.a5_routing import RoutingAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = RoutingAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "a5-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
