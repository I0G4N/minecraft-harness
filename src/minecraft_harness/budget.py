"""Team-wide durable model-call admission and usage accounting."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
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
        # Guards synchronous state read/modify/write sections that can be
        # entered from different threads (in-process agents and RPC handlers).
        self.state_lock = threading.RLock()
        self.state = recorder.get_state("budget", {})
        if not isinstance(self.state, dict):
            self.state = {}
        for key, default in (("calls", 0), ("input_tokens", 0), ("output_tokens", 0),
                             ("unknown_usage_calls", 0), ("cost_usd", 0.0),
                             ("pending", {}), ("paused_seconds", 0.0), ("web_calls", 0)):
            self.state.setdefault(key, copy.deepcopy(default))
        if not isinstance(self.state.get("pending"), dict):
            self.state["pending"] = {}
        if not isinstance(self.state.get("paused_seconds"), (int, float)) or isinstance(self.state.get("paused_seconds"), bool):
            self.state["paused_seconds"] = 0.0
        if not isinstance(self.state.get("web_calls"), int) or isinstance(self.state.get("web_calls"), bool):
            self.state["web_calls"] = 0
        # A crash during an operator pause must not charge the paused window to
        # the wall budget. The pause start is persisted before the gate closes so
        # a resumed Supervisor can extend the durable deadline by that window.
        paused_at = recorder.get_state("budget_paused_at_unix")
        paused_elapsed = 0.0
        if isinstance(paused_at, (int, float)) and not isinstance(paused_at, bool):
            paused_elapsed = max(0.0, time.time() - float(paused_at))
            recorder.set_state("budget_paused_at_unix", None)
            if paused_elapsed > 0:
                self.state["paused_seconds"] = float(self.state["paused_seconds"]) + paused_elapsed
                recorder.set_state("budget", self.state)
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
        if paused_elapsed > 0:
            saved_deadline = float(saved_deadline) + paused_elapsed
            recorder.set_state("budget_deadline_unix", saved_deadline)
        self.deadline = time.monotonic() + max(0, min(config["wall_time_minutes"] * 60 - recorder.get_state("active_wall_seconds", 0), saved_deadline - time.time()))
        self.paused = False
        self._pause_started = None
        self._paused_seconds = float(self.state["paused_seconds"])
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

    def paused_duration(self):
        """Seconds spent in operator pause in this process plus persisted windows."""
        elapsed = self._paused_seconds
        if self._pause_started is not None:
            elapsed += max(0.0, time.monotonic() - self._pause_started)
        return elapsed

    def pause(self):
        """Freeze the wall deadline; paused time is persisted for crash recovery."""
        if self.paused:
            return
        self.paused = True
        self._pause_started = time.monotonic()
        self.state["paused_seconds"] = self._paused_seconds
        paused_at = time.time()
        try:
            self.recorder.set_state("budget_paused_at_unix", paused_at)
            self.recorder.state_event("budget", self.state, "budget_paused", {
                "paused_seconds": self._paused_seconds, "paused_at_unix": paused_at,
                "deadline_unix": self.recorder.get_state("budget_deadline_unix"),
            }, actor_id="supervisor")
        except Exception:
            # Instrumentation failure must not leave the gate half-paused; the
            # in-memory deadline freeze below is still authoritative.
            pass

    def resume(self):
        """Release a pause and extend the wall deadline by the paused duration."""
        if not self.paused:
            return
        duration = max(0.0, time.monotonic() - self._pause_started) if self._pause_started is not None else 0.0
        self.paused = False
        self._pause_started = None
        self._paused_seconds += duration
        self.deadline += duration
        self.state["paused_seconds"] = self._paused_seconds
        try:
            self.recorder.set_state("budget_paused_at_unix", None)
            saved = self.recorder.get_state("budget_deadline_unix")
            if isinstance(saved, (int, float)) and not isinstance(saved, bool):
                self.recorder.set_state("budget_deadline_unix", float(saved) + duration)
            self.recorder.state_event("budget", self.state, "budget_resumed", {
                "duration_s": duration, "paused_seconds": self._paused_seconds,
                "deadline_unix": self.recorder.get_state("budget_deadline_unix"),
            }, actor_id="supervisor")
        except Exception:
            pass

    def admit_web(self, agent_id, tool="web"):
        """Durably meter one external search/fetch call.

        Returns False (never raises, never stops the run) when the configured
        ``budgets.max_web_calls`` cap is exhausted, so the caller can return a
        bounded "unavailable" tool result without touching the network.
        """
        with self.state_lock:
            cap = self.config.get("max_web_calls")
            current = int(self.state.get("web_calls", 0) or 0)
            if cap is not None:
                try:
                    cap_value = int(cap)
                except (TypeError, ValueError):
                    cap_value = None
                if cap_value is not None and current >= cap_value:
                    return False
            self.state["web_calls"] = current + 1
            payload = {"agent_id": agent_id, "tool": str(tool)[:64],
                       "web_calls": current + 1, "max_web_calls": cap}
            try:
                self.recorder.state_event("budget", self.state, "web_usage", payload,
                                          actor_id=agent_id)
            except Exception:
                self.state["web_calls"] = current
                return False
            return True

    def stop_requested(self):
        if self.paused:
            # Paused time never counts against the wall budget; only an explicit
            # stop (or a hard bound violation) ends the run while paused.
            return self.stopped
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
            with self.state_lock:
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
            with self.state_lock:
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

    async def recover_all_pending(self):
        """Settle every still-pending admission at shutdown.

        An in-flight provider call can outlive the shutdown grace window (for
        example the Observer's final review). Leaving its reservation pending
        makes the terminal budget state look stuck and excludes the worst-case
        call from cost accounting. Charge each as unknown usage once, then clear
        it, matching crash recovery semantics.
        """
        agents = sorted({pending.get("agent_id") for pending in
                         list(self.state.get("pending", {}).values())
                         if isinstance(pending, dict) and pending.get("agent_id")})
        recovered = 0
        for agent in agents:
            recovered += await self.recover_abandoned(agent)
        return recovered

    async def recover_abandoned(self, agent_id):
        """Charge and release reservations left by a dead runner process.

        The request may already have reached the provider, so usage is unknown.
        Charge the admission-time worst-case reservation instead of freezing the
        whole team behind a permanently pending booking.
        """
        recovered = 0
        with self.state_lock:
            for identity, pending in list(self.state["pending"].items()):
                if pending.get("agent_id") != agent_id:
                    continue
                updated = copy.deepcopy(self.state)
                updated["pending"].pop(identity, None)
                updated["unknown_usage_calls"] += 1
                conservative = pending.get("reserved_cost", 0.0)
                if self.config.get("prices_per_million"):
                    updated["cost_usd"] += conservative
                    updated["unknown_cost_calls"] = updated.get("unknown_cost_calls", 0) + 1
                self.recorder.state_event("budget", updated, "model_usage", {
                    "reservation_id": identity, "usage": {}, "normalized_usage": normalize_usage({}),
                    "cost": estimate_cost({}, self.config.get("prices_per_million")),
                    "conservative_cost_usd": conservative,
                    "purpose": pending.get("purpose", "agent_decision"),
                    "role": pending.get("role", "player"), "recovered_runner_lost": True,
                    "budget": updated}, actor_id=agent_id)
                self.state = updated
                recovered += 1
        return recovered
