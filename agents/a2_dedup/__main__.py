"""Container entrypoint: `python -m agents.a2_dedup`."""

from __future__ import annotations

import os

from agents.a2_dedup import DedupAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = DedupAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "a2-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
