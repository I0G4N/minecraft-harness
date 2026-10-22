#!/usr/bin/env python3
"""Render an exact-N-agent deployment without starting Docker or a Supervisor.

Writes three inspectable artifacts from the checked-in templates:

* a Compose file containing exactly N identity/launcher/bot triples,
* a frozen-vetted experiment config using container runner mode,
* an owner-only .env carrying unique per-agent worker/bot tokens.

Four players keep the paper-aligned protocol.  Other counts render the strict
lifecycle.  The independent Observer is not a game player, so it has no bot
triple; the Supervisor creates its runner container dynamically when
observer.mode=llm (N player runners + one Observer runner).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minecraft_harness.deployment_generation import (  # noqa: E402
    MAX_AGENTS, DeploymentGenerationError, render_compose, render_config,
    render_env, render_yaml, resolve_mode, validate_agent_count,
    validate_project, write_private, write_text,
)


def _default_project(agents: int) -> str:
    return f"minecraft-paper-{agents}"


def _paths(args) -> tuple[Path, Path, Path]:
    project = args.project
    compose = Path(args.compose_output) if args.compose_output else (
        ROOT / "infra" / f"compose.{project}.yaml")
    config = Path(args.config_output) if args.config_output else (
        ROOT / "configs" / f"{project}.yaml")
    environment = Path(args.env_output) if args.env_output else (
        ROOT / "infra" / f".env.{project}")
    return compose, config, environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agents", required=True, type=int,
                        help=f"exact number of player agents (1-{MAX_AGENTS})")
    parser.add_argument("--project", default=None,
                        help="Docker Compose project name (default: minecraft-paper-<agents>)")
    parser.add_argument("--mode", choices=("auto", "paper", "strict"), default="auto",
                        help="auto uses paper-aligned for exactly four players, strict otherwise")
    parser.add_argument("--runner-mode", choices=("container", "process", "inprocess"),
                        default="container",
                        help="runtime.runner_mode written into the config (default: container)")
    parser.add_argument("--template-compose", default=str(ROOT / "infra" / "compose.paper.yaml"),
                        help="static four-player Compose template")
    parser.add_argument("--template-config",
                        default=str(ROOT / "configs" / "minecraft-paper-runner-20261022.yaml"),
                        help="paper/container config template")
    parser.add_argument("--compose-output", default=None,
                        help="default: infra/compose.<project>.yaml")
    parser.add_argument("--config-output", default=None,
                        help="default: configs/<project>.yaml")
    parser.add_argument("--env-output", default=None,
                        help="default: infra/.env.<project>")
    parser.add_argument("--server-data", default=str(ROOT / "infra" / "server-data"),
                        help="shared Minecraft server data directory")
    parser.add_argument("--seed", type=int, default=42,
                        help="MC_SEED written to the environment file (default: 42)")
    parser.add_argument("--wall-minutes", type=float, default=None,
                        help="optional budgets.wall_time_minutes override")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="optional budgets.total_model_calls override")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="optional budgets.max_cost_usd override")
    parser.add_argument("--api-key", default=None,
                        help="optional DEEPSEEK_API_KEY value (default: current environment)")
    parser.add_argument("--accept-eula", action="store_true",
                        help="write ACCEPT_EULA=true; only set after accepting the EULA")
    parser.add_argument("--force", action="store_true",
                        help="replace existing generated artifacts")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        agents = validate_agent_count(args.agents)
        project = validate_project(args.project or _default_project(agents))
        mode = resolve_mode(agents, args.mode)
        if mode == "paper" and agents != 4:
            raise DeploymentGenerationError("paper mode requires exactly four agents")
        config_template = Path(args.template_config)
        compose_template = Path(args.template_compose)
        for label, path in (("compose template", compose_template),
                            ("config template", config_template)):
            if not path.is_file():
                raise DeploymentGenerationError(f"{label} does not exist: {path}")
        compose_path, config_path, env_path = _paths(args)
        targets = [compose_path, config_path, env_path]
        if len({p.resolve() for p in targets}) != len(targets):
            raise DeploymentGenerationError("compose, config and env outputs must be distinct")
        existing = [path for path in targets if path.exists()]
        if existing and not args.force:
            raise DeploymentGenerationError(
                "refusing to overwrite existing artifacts without --force: " +
                ", ".join(str(path) for path in existing))

        server_data = Path(args.server_data).resolve()
        config_data = render_config(
            config_template, agents, project, mode,
            runner_mode=args.runner_mode, server_data_dir=server_data,
            config_path=config_path, wall_time_minutes=args.wall_minutes,
            total_model_calls=args.max_calls, max_cost_usd=args.max_cost)
        compose_data = render_compose(compose_template, agents, project, mode)
        environment_text = render_env(
            agents, project, server_data_dir=server_data, seed=args.seed,
            api_key=args.api_key or os.environ.get("DEEPSEEK_API_KEY"),
            accept_eula=args.accept_eula)
        config_text = render_yaml(config_data)
        compose_text = render_yaml(compose_data)

        write_text(compose_path, compose_text)
        write_text(config_path, config_text)
        write_private(env_path, environment_text)
    except (DeploymentGenerationError, ValueError, OSError) as exc:
        print(f"prepare_deployment: {exc}", file=sys.stderr)
        return 2

    observer = (config_data.get("observer") or {}).get("mode") == "llm"
    runners = agents + (1 if observer else 0)
    print(f"agents={agents} mode={mode} project={project} "
          f"runner_mode={config_data['runtime']['runner_mode']}")
    print(f"runner containers per run: {runners}"
          f" ({agents} player + {'1 Observer' if observer else 'rules Observer'})")
    print(f"compose: {compose_path}")
    print(f"config:  {config_path}")
    print(f"env:     {env_path} (mode 0600)")
    print()
    print("start only when the previous stack is stopped and the EULA is accepted:")
    print(f"  docker compose -p {project} -f {compose_path} --env-file {env_path} up -d")
    print("then run the supervisor (from the deployment dir, with a fresh run directory):")
    print(f"  PAPER_ENV_FILE={env_path} PAPER_CONFIG={config_path} "
          "PAPER_RUN_DIR=<run-dir> <deployment>/run-paper.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
