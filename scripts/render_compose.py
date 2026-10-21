#!/usr/bin/env python3
"""Render an exact-N-agent compose file from the paper compose template.

The checked-in paper compose describes four players.  For other N we clone the
server plus the identity/launcher/bot triple and rewrite names, ports, tokens and
networks, so `docker compose up` starts only the requested number of agents.
"""
from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "compose.paper.yaml"


def rewrite(value, index: int):
    if isinstance(value, str):
        text = value
        text = text.replace("player1", f"player{index}")
        text = text.replace("identity1", f"identity{index}")
        text = text.replace("launcher1", f"launcher{index}")
        text = text.replace("worker1", f"worker{index}")
        text = text.replace("WORKER1_TOKEN", f"WORKER{index}_TOKEN")
        text = text.replace("BOT1_ADMIN_TOKEN", f"BOT{index}_ADMIN_TOKEN")
        text = text.replace("BOT1_AGENT_TOKENS_JSON", f"BOT{index}_AGENT_TOKENS_JSON")
        text = text.replace(":3101:", f":{3100 + index}:")
        text = text.replace("3901", str(3900 + index))
        return text
    if isinstance(value, list):
        return [rewrite(item, index) for item in value]
    if isinstance(value, dict):
        return {rewrite(key, index) if isinstance(key, str) else key: rewrite(item, index)
                for key, item in value.items()}
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, choices=range(1, 17), required=True)
    parser.add_argument("--mode", choices=("paper", "strict"), required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "infra" / "compose.local.yaml")
    parser.add_argument("--project", default="minecraft-paper")
    args = parser.parse_args()

    base = yaml.safe_load(TEMPLATE.read_text())
    services = base["services"]
    rendered = {
        "name": args.project,
        "services": {"server": copy.deepcopy(services["server"])},
        "networks": {"world": copy.deepcopy(base["networks"]["world"]),
                     "control": copy.deepcopy(base["networks"]["control"])},
    }
    for index in range(1, args.agents + 1):
        for kind in ("identity", "launcher", "bot"):
            source = services[f"{kind}1"]
            service = copy.deepcopy(source)
            service = rewrite(service, index)
            rendered["services"][f"{kind}{index}"] = service
        network = copy.deepcopy(base["networks"]["worker1"])
        rendered["networks"][f"worker{index}"] = rewrite(network, index)
    if args.mode == "strict":
        for index in range(1, args.agents + 1):
            env = rendered["services"][f"bot{index}"]["environment"]
            env["LIFECYCLE"] = "strict"
            env["LEFTOVER_POLICY"] = "reset"
            env["CHAT_DELIVERY"] = "events"
    args.output.write_text(yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True))
    print(f"wrote {args.output} for {args.agents} agents ({args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
