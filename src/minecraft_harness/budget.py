"""Team-wide durable model-call admission and usage accounting."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
import time
import uuid

from .usage import estimate_cost, normalize_usage, reservation_cost


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    def __init__(self, config, recorder, *, max_output_tokens=4096):
        self.config, self.recorder = config, recorder
        self.max_output_tokens = max_output_tokens
        self.lock = asyncio.Lock()
        saved_deadline = recorder.get_state("budget_deadline_unix")
        if saved_deadline is None:
            origin = time.time()
            manifest = recorder.run_dir / "manifest.json"
            if manifest.exists():
                created = json.loads(manifest.read_text()).get("created_at")
                if created:
                    parsed = datetime.fromisoformat(created)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    origin = min(origin, parsed.timestamp())
            saved_deadline = origin + config["wall_time_minutes"] * 60
            recorder.set_state("budget_deadline_unix", saved_deadline)
        self.deadline = time.monotonic() + max(0, min(config["wall_time_minutes"] * 60 - recorder.get_state("active_wall_seconds", 0), saved_deadline - time.time()))
        self.state = recorder.get_state("budget", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "unknown_usage_calls": 0, "cost_usd": 0.0, "pending": {}})
        self.stopped = bool(self.state.get("bound_exceeded"))
        self.admission = asyncio.Event()
        self.admission.set()
        # An interrupted request still consumed a call and may have been billed.
        # Charge its admission-time worst-case reservation so a restart keeps the
        # budget conservative without freezing every later paid request.
        if self.state["pending"]:
            for identity, pending in list(self.state["pending"].items()):
                updated = copy.deepcopy(self.state)
                conservative = pending.get("reserved_cost", 0.0)
                updated["unknown_usage_calls"] += 1
                updated["pending"].pop(identity)
                if updated.get("unknown_cost_calls") is None:
                    updated["unknown_cost_calls"] = 0
                updated["cost_usd"] += conservative
                self.recorder.state_event("budget", updated, "model_usage", {"reservation_id": identity,
                    "usage": {}, "normalized_usage": normalize_usage({}), "cost": estimate_cost({}, self.config.get("prices_per_million")),
                    "conservative_cost_usd": conservative,
                    "purpose": pending.get("purpose", "agent_decision"), "role": pending.get("role", "player"),
                    "recovered_pending": True, "budget": updated}, actor_id=pending["agent_id"])
                self.state = updated

    def _save(self):
        self.recorder.set_state("budget", self.state)

    def stop_requested(self):
        return self.stopped or time.monotonic() >= self.deadline

    def validate_request(self, request_bytes):
        bound = self.config.get("max_input_tokens")
        # max_input_tokens is a token bound while request_bytes is bytes. Use a
        # deliberately conservative 4 bytes/token floor so a request that could
        # exceed the reserved input bound is rejected before admission.
        if self.config.get("max_cost_usd") is not None and bound is not None and request_bytes > bound * 4:
            raise BudgetExhausted("serialized request exceeds the conservative input reservation bound")

    async def reserve(self, agent_id, *, purpose="agent_decision", role="player"):
        if purpose not in {"agent_decision", "observer_review", "context_compaction"} or role not in {"player", "observer"}:
            raise ValueError("unsupported model role or purpose")
        await self.admission.wait()
        async with self.lock:
            if self.stop_requested() or self.state["calls"] >= self.config["total_model_calls"]:
                raise BudgetExhausted("team wall-time or model-call budget exhausted")
            reservation = 0.0
            maximum = self.config.get("max_cost_usd")
            if maximum is not None:
                # Unknown earlier usage was already charged at its admission-time
                # worst-case reservation, so a paid run survives transient network
                # failures instead of freezing after the first one.
                rates = self.config["prices_per_million"]
                reservation = reservation_cost(self.config["max_input_tokens"], self.max_output_tokens, rates)
                if self.state["cost_usd"] + sum(v["reserved_cost"] for v in self.state["pending"].values()) + reservation > maximum:
                    raise BudgetExhausted("cost reservation exceeds remaining team budget")
            request_id = uuid.uuid4().hex
            updated = copy.deepcopy(self.state)
            updated["calls"] += 1
            updated["pending"][request_id] = {"agent_id": agent_id, "reserved_cost": reservation, "purpose": purpose, "role": role}
            try:
                self.recorder.state_event("budget", updated, "model_admitted", {"reservation_id": request_id, "purpose": purpose, "role": role}, actor_id=agent_id)
            except Exception:
                self.stopped = True
                raise
            self.state = updated
            return request_id

    async def settle(self, reservation_id, usage):
        async with self.lock:
            updated = copy.deepcopy(self.state)
            pending = updated["pending"].pop(reservation_id, None)
            if pending is None:
                return
            usage = usage if isinstance(usage, dict) else {}
            normalized = normalize_usage(usage)
            inp, out = normalized["input_tokens"], normalized["output_tokens"]
            estimated = estimate_cost(normalized, self.config.get("prices_per_million"))
            # Provider-reported cost can be known even when token usage is not.
            # Unknown usage is charged at the admission-time worst-case
            # reservation: conservative, bounded and never a reason to freeze the
            # whole run (long sessions will hit at least one transient failure).
            conservative = 0.0
            if estimated["cost_usd"] is not None:
                updated["cost_usd"] += estimated["cost_usd"]
            elif self.config.get("prices_per_million"):
                conservative = pending.get("reserved_cost", 0.0)
                updated["cost_usd"] += conservative
                updated["unknown_cost_calls"] = updated.get("unknown_cost_calls", 0) + 1
            if isinstance(inp, bool) or isinstance(out, bool) or not isinstance(inp, int) or not isinstance(out, int) or inp < 0 or out < 0:
                updated["unknown_usage_calls"] += 1
            else:
                updated["input_tokens"] += inp
                updated["output_tokens"] += out
                if (self.config.get("max_input_tokens") and inp > self.config["max_input_tokens"]) or out > self.max_output_tokens:
                    self.stopped = True
                    updated["bound_exceeded"] = {"input_tokens": inp, "output_tokens": out,
                        "max_input_tokens": self.config.get("max_input_tokens"), "max_output_tokens": self.max_output_tokens}
            try:
                self.recorder.state_event("budget", updated, "model_usage", {"reservation_id": reservation_id,
                    "usage": usage, "normalized_usage": normalized, "cost": estimated,
                    "conservative_cost_usd": conservative,
                    "purpose": pending.get("purpose", "agent_decision"), "role": pending.get("role", "player"), "budget": updated}, actor_id=pending["agent_id"])
            except Exception:
                self.stopped = True
                raise
            self.state = updated
