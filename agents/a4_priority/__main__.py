"""Container entrypoint: `python -m agents.a4_priority`."""

from __future__ import annotations

import os

from agents.a4_priority import PriorityAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = PriorityAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "a4-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
