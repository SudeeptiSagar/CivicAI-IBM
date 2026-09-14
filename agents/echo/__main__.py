"""Container entrypoint: `python -m agents.echo`."""

from __future__ import annotations

import os

from agents.echo import EchoAgent
from bus.factory import make_bus
from common.logging import configure_logging


def main() -> None:
    configure_logging()
    agent = EchoAgent(make_bus(), consumer=os.environ.get("CIVICAI_AGENT_CONSUMER", "echo-1"))
    agent.run_forever()


if __name__ == "__main__":
    main()
