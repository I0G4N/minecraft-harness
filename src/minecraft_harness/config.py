"""Validated, secret-free experiment configuration with bounded failure policies."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml


DEFAULTS = {
    "experiment": {"name": "minecraft", "task": "observe_only", "goal": "Observe the shared world and verify your observations.", "seed": 42,
                   "profile": "strict", "stop_on_success": True},
    "agents": {"count": 4, "max_turns": 30, "context_chars": 48000, "max_no_progress_turns": 5, "paper_single_program": False, "paper_tight_interface": False, "paper_single_program": False,
               "max_model_failures": 5, "max_event_failures": 5, "retry_base_s": 1, "retry_max_s": 30,
               "max_workspace_bytes": 64_000_000, "max_workspace_files": 4096, "max_tool_result_chars": 16000},
    "model": {"kind": "fake", "max_output_tokens": 4096, "timeout_s": 90, "max_retries": 0,
              "max_request_bytes": 1_000_000, "max_response_bytes": 2_000_000},
    "collaboration": {"game_chat": True, "shared_memory": True, "chat_delivery": "events"},
    "runtime": {"mode": "fake", "url": None, "urls": {}, "viewer_ports": {}, "token_env": "MC_RUNTIME_TOKEN",
                "program_timeout_default_s": 60, "program_timeout_max_s": 180,
                "minecraft_version": "1.20.4", "host": "127.0.0.1", "port": 25565,
                "request_timeout_s": 10, "startup_timeout_s": 60, "heartbeat_interval_s": 5, "lease_timeout_s": 30,
                "response_max_bytes": 4 * 1024 ** 2, "lifecycle": "strict", "max_background_jobs": 8,
                "max_background_ttl_ms": 86400000, "leftover_policy": "reset", "recover_session_loss": False},
    "budgets": {"wall_time_minutes": 60, "total_model_calls": 400, "max_cost_usd": None},
    "server": {"world_dir": None, "log_path": None, "save_interval_s": 300, "managed_container": None,
               "control_timeout_s": 90, "startup_timeout_s": 120, "log_interval_s": 1,
               "properties_path": None, "require_fresh_world": False, "log_timezone": "UTC"},
    "storage": {"min_free_disk_bytes": 256 * 1024 ** 2, "max_artifact_bytes": 16 * 1024 ** 3,
                "max_run_bytes": 24 * 1024 ** 3, "max_event_bytes": 4 * 1024 ** 2},
    "supervision": {"poll_interval_s": .2, "evaluation_interval_s": 2, "resource_check_interval_s": 5, "resource_sample_interval_s": 10,
                    "recovery_attempts": 3, "max_recoveries": 20, "recovery_base_s": 1, "recovery_max_s": 30, "shutdown_timeout_s": 30},
    "observer": {"mode": "rules", "interval_s": 60, "allow_infra_requests": True},
    "workspace": {"provision": False, "image": "minecraft-harness-workspace:local", "network": "none",
                  "docs_dir": None},
    "web": {"enabled": False, "search_url": None, "search_key_env": None, "allowed_hosts": [],
            "timeout_s": 15, "max_response_bytes": 262144},
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _number(value, label, *, minimum=0, maximum=None, inclusive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value < minimum if inclusive else value <= minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be finite and {'at least' if inclusive else 'greater than'} {minimum}" + (f" and at most {maximum}" if maximum is not None else ""))
    return value


def _integer(value, label, *, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be an integer >= {minimum}" + (f" and <= {maximum}" if maximum is not None else ""))
    return value


def _endpoint(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an HTTP(S) URL")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{label} has an invalid port") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (port is not None and not 0 < port < 65536):
        raise ValueError(f"{label} must be an HTTP(S) URL without embedded credentials/query")


def _environment_name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{label} must name an environment variable")


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("configuration must be an object")
    result = _merge(DEFAULTS, raw)
    for group in DEFAULTS:
        if not isinstance(result[group], dict):
            raise ValueError(f"{group} must be an object")
    agents, model, rt = result["agents"], result["model"], result["runtime"]
    experiment = result["experiment"]
    if experiment["profile"] not in {"strict", "paper_aligned"}:
        raise ValueError("experiment.profile must be strict or paper_aligned")
    for key in ("stop_on_success",):
        if type(experiment[key]) is not bool:
            raise ValueError("experiment." + key + " must be boolean")
    seed = experiment["seed"]
    if seed is not None and (type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63):
        raise ValueError("experiment.seed must be null (random) or a signed 64-bit integer")
    if rt["lifecycle"] not in {"strict", "paper_aligned"}:
        raise ValueError("runtime.lifecycle must be strict or paper_aligned")
    _integer(rt["max_background_jobs"], "runtime.max_background_jobs", maximum=128)
    _integer(rt["max_background_ttl_ms"], "runtime.max_background_ttl_ms", minimum=10)
    count = agents["count"]
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("agents.count must be an integer from 1 to 8")
    ids = agents.get("ids", [f"player{i + 1}" for i in range(count)])
    if not isinstance(ids, list) or len(ids) != count or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", x) for x in ids) or len(set(ids)) != count:
        raise ValueError("unique Minecraft agent IDs (1–16 alphanumeric/underscore characters) required")
    agents["ids"] = ids
    for group, key in (("agents", "max_turns"), ("agents", "max_no_progress_turns"), ("model", "max_output_tokens"), ("budgets", "total_model_calls")):
        _integer(result[group][key], f"{group}.{key}")
    _integer(agents["context_chars"], "agents.context_chars", minimum=4000)
    if type(agents.get("paper_single_program")) is not bool:
        raise ValueError("agents.paper_single_program must be a boolean")
    if type(agents.get("paper_tight_interface")) is not bool:
        raise ValueError("agents.paper_tight_interface must be a boolean")
    if result["runtime"].get("leftover_policy") not in {"reset", "allow"}:
        raise ValueError("runtime.leftover_policy must be 'reset' or 'allow'")
    if type(result["runtime"].get("recover_session_loss")) is not bool:
        raise ValueError("runtime.recover_session_loss must be a boolean")
    if result["collaboration"].get("chat_delivery") not in {"events", "program_output"}:
        raise ValueError("collaboration.chat_delivery must be 'events' or 'program_output'")
    for key in ("max_model_failures", "max_event_failures"):
        _integer(agents[key], "agents." + key, maximum=100)
    _integer(agents["max_workspace_bytes"], "agents.max_workspace_bytes", maximum=1024 ** 3)
    _integer(agents["max_workspace_files"], "agents.max_workspace_files", maximum=100000)
    _integer(agents["max_tool_result_chars"], "agents.max_tool_result_chars", minimum=1000, maximum=1024 ** 2)
    _number(agents["retry_base_s"], "agents.retry_base_s", inclusive=True, maximum=300)
    _number(agents["retry_max_s"], "agents.retry_max_s", minimum=agents["retry_base_s"], inclusive=True, maximum=300)
    _number(model["timeout_s"], "model.timeout_s", maximum=3600)
    agents.setdefault("model_request_timeout_s", model["timeout_s"] + 5)
    _number(agents["model_request_timeout_s"], "agents.model_request_timeout_s", maximum=3660)
    if type(model["max_retries"]) is not int or model["max_retries"] != 0:
        raise ValueError("model.max_retries must be 0; agent retries reserve and account for each request separately")
    for key in ("max_request_bytes", "max_response_bytes"):
        _integer(model[key], "model." + key, minimum=1024, maximum=16 * 1024 ** 2)
    _number(result["budgets"]["wall_time_minutes"], "budgets.wall_time_minutes")
    if rt["mode"] not in ("fake", "minecraft"):
        raise ValueError("runtime.mode must be fake or minecraft")
    if model["kind"] not in ("fake", "chat_completions"):
        raise ValueError("model.kind must be fake or chat_completions")
    from .evaluation.scoring import _configuration, SUPPORTED_TASKS
    task_name, _, _ = _configuration(result["experiment"]["task"])
    if task_name not in SUPPORTED_TASKS:
        raise ValueError("unsupported experiment task")
    if model.get("api_key"):
        raise ValueError("use model.api_key_env, never put credentials in experiment files")
    if model.get("api_key_env") is not None:
        _environment_name(model["api_key_env"], "model.api_key_env")
    if model["kind"] == "chat_completions":
        if not isinstance(model.get("model"), str) or not model["model"].strip() or not model.get("base_url"):
            raise ValueError("model.model and model.base_url are required")
        _endpoint(model["base_url"], "model.base_url")
    for key in ("urls", "token_envs"):
        if key in rt and (not isinstance(rt[key], dict) or any(agent not in ids for agent in rt[key])):
            raise ValueError(f"runtime.{key} must be an object keyed by configured agent IDs")
    viewers = rt.get("viewer_ports", {})
    if not isinstance(viewers, dict) or any(agent not in ids for agent in viewers):
        raise ValueError("runtime.viewer_ports must be an object keyed by configured agent IDs")
    for agent, port in viewers.items():
        _integer(port, f"runtime.viewer_ports.{agent}", minimum=1, maximum=65535)
    for endpoint in [rt.get("url"), *rt["urls"].values()]:
        if endpoint is not None:
            _endpoint(endpoint, "runtime URL")
    _environment_name(rt["token_env"], "runtime.token_env")
    for agent, env in rt.get("token_envs", {}).items():
        _environment_name(env, f"runtime.token_envs.{agent}")
    for key in ("program_timeout_default_s", "program_timeout_max_s", "request_timeout_s", "startup_timeout_s", "heartbeat_interval_s", "lease_timeout_s"):
        _number(rt[key], "runtime." + key, maximum=3600)
    if rt["program_timeout_default_s"] > rt["program_timeout_max_s"]:
        raise ValueError("invalid program timeout range")
    if rt["heartbeat_interval_s"] * 2 >= rt["lease_timeout_s"]:
        raise ValueError("runtime.lease_timeout_s must exceed twice runtime.heartbeat_interval_s")
    _integer(rt["response_max_bytes"], "runtime.response_max_bytes", minimum=1024, maximum=64 * 1024 ** 2)
    _integer(rt["port"], "runtime.port", maximum=65535)
    if rt["mode"] == "minecraft" and any(not rt.get("url") and not rt["urls"].get(agent) for agent in ids):
        raise ValueError("Minecraft mode requires an external bot runtime URL for every configured agent (see infra)")
    server = result["server"]
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(server['log_timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError('server.log_timezone must be a valid IANA timezone') from None
    for key in ("save_interval_s", "control_timeout_s", "startup_timeout_s", "log_interval_s"):
        _number(server[key], "server." + key, maximum=86400)
    for key in ("world_dir", "log_path"):
        if server[key] is not None and (not isinstance(server[key], str) or not server[key].strip()):
            raise ValueError(f"server.{key} must be a nonempty path string")
    container = server["managed_container"]
    if container is not None and (not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container)):
        raise ValueError("server.managed_container is invalid")
    for key, value in result["storage"].items():
        if key in DEFAULTS["storage"]:
            _integer(value, "storage." + key, minimum=0 if key == "min_free_disk_bytes" else 1024)
    if result["storage"]["max_artifact_bytes"] > result["storage"]["max_run_bytes"]:
        raise ValueError("storage.max_artifact_bytes cannot exceed storage.max_run_bytes")
    supervision = result["supervision"]
    for key in ("poll_interval_s", "evaluation_interval_s", "resource_check_interval_s", "resource_sample_interval_s", "shutdown_timeout_s"):
        _number(supervision[key], "supervision." + key, maximum=86400)
    _integer(supervision["recovery_attempts"], "supervision.recovery_attempts", minimum=0, maximum=100)
    _integer(supervision["max_recoveries"], "supervision.max_recoveries", minimum=0, maximum=1000)
    _number(supervision["recovery_base_s"], "supervision.recovery_base_s", inclusive=True, maximum=3600)
    _number(supervision["recovery_max_s"], "supervision.recovery_max_s", minimum=supervision["recovery_base_s"], inclusive=True, maximum=3600)
    if result["observer"]["mode"] not in {"rules", "llm"}:
        raise ValueError("observer.mode must be rules or llm")
    if result['observer']['mode'] == 'llm' and 'observer' in ids:
        raise ValueError('observer is reserved for the independent Observer agent')
    _number(result["observer"]["interval_s"], "observer.interval_s", maximum=86400)
    if type(result["observer"]["allow_infra_requests"]) is not bool:
        raise ValueError("observer.allow_infra_requests must be boolean")
    observer_context = result["observer"].get("context_chars")
    if observer_context is not None:
        _integer(observer_context, "observer.context_chars", minimum=4000)
    observer_context = result["observer"].get("context_chars", agents["context_chars"])
    if model["kind"] == "chat_completions":
        # A request necessarily carries more than the context budget; a byte
        # limit below the context budget can only fail at runtime, so reject it
        # during configuration instead of losing the Observer mid-run.
        for label, value in (("agents.context_chars", agents["context_chars"]),
                             ("observer.context_chars", observer_context)):
            if value > model["max_request_bytes"]:
                raise ValueError(f"model.max_request_bytes must be at least {label} ({value})")
    for key in ("provision",):
        if type(result["workspace"][key]) is not bool:
            raise ValueError("workspace." + key + " must be boolean")
    if type(result["web"]["enabled"]) is not bool:
        raise ValueError("web.enabled must be boolean")
    if result["web"].get("search_url"):
        _endpoint(result["web"]["search_url"], "web.search_url")
        if urlparse(result['web']['search_url']).scheme != 'https':
            raise ValueError('web.search_url must use HTTPS')
    if result["web"].get("search_key_env"):
        _environment_name(result["web"]["search_key_env"], "web.search_key_env")
    if not isinstance(result['web']['allowed_hosts'], list) or any(not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', host) for host in result['web']['allowed_hosts']):
        raise ValueError('web.allowed_hosts must be a list of explicit HTTPS host names')
    _number(result["web"]["timeout_s"], "web.timeout_s", minimum=1, inclusive=True, maximum=60)
    _integer(result["web"]["max_response_bytes"], "web.max_response_bytes", minimum=1024, maximum=1000000)
    if result['workspace']['network'] != 'none':
        raise ValueError('workspace.network must be none; use the Unix submission broker and web adapter')
    if result['workspace'].get('docs_dir') is not None:
        raise ValueError('workspace.docs_dir is not supported; locked reference documents are prepared automatically')
    if type(server['require_fresh_world']) is not bool:
        raise ValueError('server.require_fresh_world must be boolean')
    if experiment["profile"] == "paper_aligned":
        if count != 4 or rt["lifecycle"] != "paper_aligned":
            raise ValueError("paper_aligned requires four players and runtime.lifecycle=paper_aligned")
        if server["save_interval_s"] != 300:
            raise ValueError("paper_aligned requires server.save_interval_s=300")
        if rt["mode"] == "minecraft":
            if not server["require_fresh_world"] or not server.get("world_dir"):
                raise ValueError("paper_aligned requires verified fresh world evidence")
            if result["observer"]["mode"] != "llm" or not result["workspace"]["provision"]:
                raise ValueError("paper_aligned requires LLM Observer and isolated coding workspaces")
    for key in ("game_chat", "shared_memory"):
        if type(result["collaboration"][key]) is not bool:
            raise ValueError(f"collaboration.{key} must be boolean")
    maximum_cost = result["budgets"].get("max_cost_usd")
    rates = result["budgets"].get("prices_per_million")
    if rates is not None:
        if not isinstance(rates, dict) or not all(key in rates for key in ("input", "output")):
            raise ValueError("prices_per_million requires input and output rates")
        for key in ("input", "output", "cached_input", "reasoning"):
            if key in rates:
                _number(rates[key], "budgets.prices_per_million." + key, inclusive=True)
    if maximum_cost is not None:
        _number(maximum_cost, "budgets.max_cost_usd")
        if rates is None:
            raise ValueError("a positive cost budget requires input/output prices_per_million")
        _integer(result["budgets"].get("max_input_tokens"), "budgets.max_input_tokens")
    return result


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    result = validate_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    for key in ("world_dir", "log_path", "properties_path"):
        if result["server"].get(key):
            p = Path(result["server"][key])
            result["server"][key] = str((path.parent / p).resolve() if not p.is_absolute() else p.resolve())
    return result
