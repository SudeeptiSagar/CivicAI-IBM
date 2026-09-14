"""Container entrypoint: `python -m agents.a3_synthesis`."""

from __future__ import annotations

import os

from agents.a3_synthesis import SynthesisAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = SynthesisAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "a3-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
