"""Generate an exact-N-agent real Minecraft deployment from checked-in templates.

The checked-in paper Compose file and paper configuration describe four player
agents plus the independent Observer runner.  This module clones the single
agent block for any configured count from 1 through 16 instead of maintaining a
growing static file.  It never starts Docker or a Supervisor; it only renders
inspectable Compose, experiment-config and token-environment artifacts.

Four players keep the paper-aligned protocol.  Other player counts render the
strict lifecycle, because ``experiment.profile=paper_aligned`` deliberately
requires exactly four players.  The Observer is not a game player and therefore
does not get an identity/launcher/bot Compose triple; when
``observer.mode=llm`` the Supervisor still creates its independent runner
container dynamically (one extra runner next to the N player runners).
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import validate_config

MAX_AGENTS = 16
BOT_PORT_BASE = 3100
WORKER_SANDBOX_RUNTIME = "runsc"
PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

# Keep the first four worker subnets stable: a generated N=4 paper deployment is
# then equivalent to the checked-in static paper deployment.  Counts above four
# use a separate unused /24 range.  Existing host networks occupy 172.17-172.30.
_STATIC_WORKER_SUBNETS = {
    1: "172.24.0.0/16",
    2: "172.25.0.0/16",
    3: "172.26.0.0/16",
    4: "172.29.0.0/16",
}


class DeploymentGenerationError(ValueError):
    """A requested deployment cannot be rendered safely."""


def validate_agent_count(agents: int) -> int:
    if type(agents) is not int or not 1 <= agents <= MAX_AGENTS:
        raise DeploymentGenerationError(
            f"agents must be an integer from 1 to {MAX_AGENTS}")
    return agents


def validate_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise DeploymentGenerationError(
            "project must be lowercase letters, digits, underscores or hyphens, "
            "start with a letter or digit, and be at most 63 characters")
    return project


def resolve_mode(agents: int, mode: str = "auto") -> str:
    validate_agent_count(agents)
    if mode not in {"auto", "paper", "strict"}:
        raise DeploymentGenerationError("mode must be auto, paper or strict")
    if mode == "auto":
        return "paper" if agents == 4 else "strict"
    if mode == "paper" and agents != 4:
        raise DeploymentGenerationError(
            "paper-aligned mode is a four-player protocol; use --mode strict or "
            "--mode auto for another agent count")
    return mode


def agent_ids(agents: int) -> list[str]:
    validate_agent_count(agents)
    return [f"player{i}" for i in range(1, agents + 1)]


def bot_port(index: int) -> int:
    return BOT_PORT_BASE + index


def viewer_port(index: int, agents: int) -> int:
    # Four players use 3105-3108 exactly like the static paper deployment.
    return BOT_PORT_BASE + agents + index


def worker_subnet(index: int) -> str:
    validate_agent_count(index)
    if index in _STATIC_WORKER_SUBNETS:
        return _STATIC_WORKER_SUBNETS[index]
    # 172.31.0.0/24 through 172.31.11.0/24 are outside the ranges already used
    # on this host and do not overlap each other.
    return f"172.31.{index - 5}.0/24"


def _ip(subnet: str, host: int) -> str:
    prefix = subnet.split("/", 1)[0]
    parts = prefix.split(".")
    parts[-1] = str(host)
    return ".".join(parts)


def _load_yaml(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    path = Path(value)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DeploymentGenerationError(f"template is not a YAML object: {path}")
    return data


def _required_service(services: dict[str, Any], name: str) -> dict[str, Any]:
    service = services.get(name)
    if not isinstance(service, dict):
        raise DeploymentGenerationError(f"compose template has no {name} service")
    return service


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment")
    if not isinstance(environment, dict):
        raise DeploymentGenerationError("compose template service has no environment object")
    return environment


def _worker_network(project: str, index: int, template: dict[str, Any]) -> dict[str, Any]:
    source = template.get("networks", {}).get("worker1")
    if not isinstance(source, dict):
        raise DeploymentGenerationError("compose template has no worker1 network")
    network = copy.deepcopy(source)
    network["name"] = f"{project}_worker{index}"
    ipam = network.setdefault("ipam", {})
    ipam["config"] = [{"subnet": worker_subnet(index)}]
    return network


def _required_network(template: dict[str, Any], name: str) -> dict[str, Any]:
    network = template.get("networks", {}).get(name)
    if not isinstance(network, dict):
        raise DeploymentGenerationError(f"compose template has no {name} network")
    return copy.deepcopy(network)


def _bot_mode_environment(environment: dict[str, str], mode: str) -> None:
    if mode == "paper":
        environment["LIFECYCLE"] = "paper_aligned"
        environment["LEFTOVER_POLICY"] = "allow"
        environment["CHAT_DELIVERY"] = "program_output"
        environment["PAPER_SINGLE_PROGRAM"] = "true"
        return
    environment["LIFECYCLE"] = "strict"
    environment["LEFTOVER_POLICY"] = "reset"
    environment["CHAT_DELIVERY"] = "events"
    environment["PAPER_SINGLE_PROGRAM"] = "false"
    # The legacy strict Compose used a 10-minute per-program ceiling.
    environment["MAX_TIMEOUT_MS"] = "600000"


def render_compose(template: dict[str, Any] | str | Path, agents: int,
                   project: str = "minecraft-paper", mode: str = "auto") -> dict[str, Any]:
    """Return a Compose document with exactly ``agents`` identity/launcher/bot triples.

    Player-facing services inherit ``restart: unless-stopped`` so a transient
    container/daemon failure does not permanently remove the bot stack from a
    long run; the managed server deliberately stays ``restart: 'no'`` because the
    Supervisor owns server recovery and must not race a container auto-restart.
    """
    validate_agent_count(agents)
    validate_project(project)
    mode = resolve_mode(agents, mode)
    source = _load_yaml(template)
    services = source.get("services")
    if not isinstance(services, dict):
        raise DeploymentGenerationError("compose template has no services object")
    server = _required_service(services, "server")
    identity_source = _required_service(services, "identity1")
    launcher_source = _required_service(services, "launcher1")
    bot_source = _required_service(services, "bot1")

    rendered: dict[str, Any] = {"name": project, "services": {}, "networks": {}}
    rendered_server = copy.deepcopy(server)
    rendered_server["container_name"] = f"{project}-server"
    # A fresh server data directory is initialized from the checked-in template;
    # let the server entrypoint override max-players for N > 4 (the Observer is
    # not a game player).
    _environment(rendered_server)["MAX_PLAYERS"] = str(agents)
    rendered["services"]["server"] = rendered_server
    rendered["networks"]["world"] = _required_network(source, "world")
    rendered["networks"]["control"] = _required_network(source, "control")

    for index in range(1, agents + 1):
        player = f"player{index}"
        subnet = worker_subnet(index)
        identity_ip = _ip(subnet, 1)
        worker_ip = _ip(subnet, 2)
        viewer = viewer_port(index, agents)
        token = f"${{WORKER{index}_TOKEN:?Set unique WORKER{index}_TOKEN}}"

        identity = copy.deepcopy(identity_source)
        identity.pop("container_name", None)
        identity_environment = _environment(identity)
        identity_environment["ALLOWED_USERNAME"] = player
        identity_environment["VIEWER_PROXY_PORT"] = str(viewer)
        identity_environment["VIEWER_TARGET"] = worker_ip
        identity["networks"] = {
            "control": {},
            f"worker{index}": {"ipv4_address": identity_ip},
            "world": {},
        }
        identity["ports"] = [f"127.0.0.1:{viewer}:{viewer}"]
        identity["depends_on"] = ["server"]
        identity["restart"] = "unless-stopped"
        rendered["services"][f"identity{index}"] = identity

        launcher = copy.deepcopy(launcher_source)
        launcher.pop("container_name", None)
        launcher_environment = _environment(launcher)
        launcher_environment["WORKER_AGENT_ID"] = player
        launcher_environment["WORKER_HOST_TOKEN"] = token
        launcher_environment["WORKER_HOST_ID"] = (
            f"${{HARNESS_PROJECT:-{project}}}-{player}")
        launcher_environment["WORKER_NETWORK"] = f"{project}_worker{index}"
        launcher_environment["WORKER_MC_HOST"] = identity_ip
        launcher_environment["WORKER_VIEWER_PORT"] = str(viewer)
        launcher_environment["WORKER_IP"] = worker_ip
        launcher["networks"] = ["control"]
        launcher["depends_on"] = [f"identity{index}"]
        launcher["restart"] = "unless-stopped"
        rendered["services"][f"launcher{index}"] = launcher

        bot = copy.deepcopy(bot_source)
        bot.pop("container_name", None)
        bot_environment = _environment(bot)
        bot_environment["ALLOWED_AGENT_ID"] = player
        bot_environment["ALLOWED_USERNAME"] = player
        bot_environment["TOKEN"] = (
            f"${{BOT{index}_ADMIN_TOKEN:?Set BOT{index}_ADMIN_TOKEN}}")
        bot_environment["AGENT_TOKENS_JSON"] = (
            f"${{BOT{index}_AGENT_TOKENS_JSON:-}}")
        bot_environment["WORKER_ENDPOINTS_JSON"] = json.dumps(
            {player: {"endpoint": f"tcp://launcher{index}:3901",
                      "token": f"${{WORKER{index}_TOKEN}}"}},
            separators=(",", ":"))
        _bot_mode_environment(bot_environment, mode)
        bot["ports"] = [f"127.0.0.1:{bot_port(index)}:3001"]
        bot["networks"] = ["control"]
        bot["depends_on"] = [f"launcher{index}"]
        bot["restart"] = "unless-stopped"
        rendered["services"][f"bot{index}"] = bot

        rendered["networks"][f"worker{index}"] = _worker_network(project, index, source)

    return rendered


def render_config(template: dict[str, Any] | str | Path, agents: int,
                  project: str = "minecraft-paper", mode: str = "auto",
                  *, runner_mode: str = "container",
                  server_data_dir: str | Path | None = None,
                  config_path: str | Path | None = None,
                  seed: int | None = None,
                  wall_time_minutes: float | None = None,
                  total_model_calls: int | None = None,
                  max_cost_usd: float | None = None) -> dict[str, Any]:
    """Return a validated config for the same exact-N deployment."""
    validate_agent_count(agents)
    validate_project(project)
    mode = resolve_mode(agents, mode)
    if runner_mode not in {"container", "process", "inprocess"}:
        raise DeploymentGenerationError(
            "runner_mode must be container, process or inprocess")
    data = _load_yaml(template)
    identifiers = agent_ids(agents)
    data.setdefault("experiment", {})["name"] = project
    data["experiment"]["profile"] = "paper_aligned" if mode == "paper" else "strict"
    if seed is not None:
        data["experiment"]["seed"] = seed
    data.setdefault("agents", {}).update(count=agents, ids=identifiers)
    runtime = data.setdefault("runtime", {})
    runtime["lifecycle"] = "paper_aligned" if mode == "paper" else "strict"
    runtime["runner_mode"] = runner_mode
    runtime["urls"] = {player: f"http://127.0.0.1:{bot_port(index)}"
                       for index, player in enumerate(identifiers, 1)}
    runtime["token_envs"] = {player: f"BOT{index}_ADMIN_TOKEN"
                              for index, player in enumerate(identifiers, 1)}
    runtime["viewer_ports"] = {player: viewer_port(index, agents)
                                for index, player in enumerate(identifiers, 1)}
    collaboration = data.setdefault("collaboration", {})
    if mode == "paper":
        runtime["leftover_policy"] = "allow"
        collaboration["chat_delivery"] = "program_output"
        data["agents"].update(paper_single_program=True,
                              paper_tight_interface=True,
                              paper_cli_output=True,
                              paper_shell_cli=True)
    else:
        # Render the strict Compose lifecycle and the matching strict agent
        # semantics; the paper-only flags belong to the four-player profile.
        runtime["leftover_policy"] = "reset"
        collaboration["chat_delivery"] = "events"
        data["agents"].update(paper_single_program=False,
                              paper_tight_interface=False,
                              paper_cli_output=False,
                              paper_shell_cli=False)
        # The strict Compose bot container accepts programs up to 10 minutes.
        runtime["program_timeout_max_s"] = min(
            runtime.get("program_timeout_max_s", 600), 600)
    server = data.setdefault("server", {})
    server["managed_container"] = f"{project}-server"
    if server_data_dir is not None:
        root = Path(server_data_dir).resolve()
        base = Path(config_path).resolve().parent if config_path is not None else Path.cwd()
        try:
            relative = os.path.relpath(root, base)
        except ValueError:
            relative = str(root)
        server["world_dir"] = str(Path(relative) / "world")
        server["log_path"] = str(Path(relative) / "logs" / "latest.log")
    budgets = data.setdefault("budgets", {})
    if wall_time_minutes is not None:
        budgets["wall_time_minutes"] = wall_time_minutes
    if total_model_calls is not None:
        budgets["total_model_calls"] = total_model_calls
    if max_cost_usd is not None:
        budgets["max_cost_usd"] = max_cost_usd
    return validate_config(data)


def render_env(agents: int, project: str = "minecraft-paper", *,
               server_data_dir: str | Path = "./server-data",
               seed: int = 42, server_container: str | None = None,
               uid: int | None = None, gid: int | None = None,
               api_key: str | None = None, accept_eula: bool = False,
               worker_sandbox_runtime: str | None = WORKER_SANDBOX_RUNTIME) -> str:
    """Return a token-bearing .env document for exactly ``agents`` player bots.

    Tokens are generated locally and never printed by the CLI.  Callers are
    responsible for writing the result with owner-only permissions.
    """
    validate_agent_count(agents)
    validate_project(project)
    if type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63:
        raise DeploymentGenerationError("seed must be a signed 64-bit integer")
    for label, value in (("server_data_dir", str(server_data_dir)),
                         ("worker_sandbox_runtime", worker_sandbox_runtime),
                         ("api_key", api_key)):
        if isinstance(value, str) and any(character in value for character in "\r\n\0"):
            raise DeploymentGenerationError(f"{label} must not contain newlines or NUL")
    if server_container is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", server_container):
        raise DeploymentGenerationError("server_container is not a Docker container name")
    values: list[tuple[str, str]] = [
        ("ACCEPT_EULA", "true" if accept_eula else "false"),
        ("MC_SEED", str(seed)),
        ("MC_SERVER_CONTAINER", server_container or f"{project}-server"),
        ("HARNESS_PROJECT", project),
        ("GAME_CHAT", "true"),
        ("SERVER_DATA_DIR", str(Path(server_data_dir).resolve())),
        ("MC_UID", str(uid if uid is not None else os.getuid())),
        ("MC_GID", str(gid if gid is not None else os.getgid())),
        ("JAVA_IMAGE", "eclipse-temurin:17-jre-jammy"),
        ("NODE_IMAGE", "node:22-bookworm-slim"),
    ]
    if worker_sandbox_runtime is not None:
        values.append(("WORKER_SANDBOX_RUNTIME", worker_sandbox_runtime))
    for index in range(1, agents + 1):
        values.append((f"WORKER{index}_TOKEN", secrets.token_hex(32)))
        values.append((f"BOT{index}_ADMIN_TOKEN", secrets.token_hex(32)))
        values.append((f"BOT{index}_AGENT_TOKENS_JSON",
                       json.dumps({f"player{index}": secrets.token_hex(32)},
                                  separators=(",", ":"))))
    if api_key:
        values.append(("DEEPSEEK_API_KEY", api_key))
    lines = ["# Generated for an exact-agent-count deployment; do not commit."]
    lines.extend(f"{key}={value}" for key, value in values)
    return "\n".join(lines) + "\n"


def write_private(path: str | Path, text: str) -> None:
    """Atomically write a sensitive file with mode 0600."""
    _write(path, text, 0o600)


def write_text(path: str | Path, text: str) -> None:
    _write(path, text, 0o644)


def _write(path: str | Path, text: str, mode: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".",
                                     dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
