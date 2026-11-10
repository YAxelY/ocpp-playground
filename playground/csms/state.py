"""What the CSMS knows: its policy (the knobs) and one record per charging station."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class Policy:
    """CSMS behaviour that the dashboard can change. Every field is a PLAYGROUND DECISION, not an OCPP rule."""
    versions: list = field(default_factory=lambda: ["2.1", "2.0.1", "1.6"])   # enabled, in preference order
    # Registration: what the CSMS answers to BootNotification (each answer is the CSMS's own decision:
    # 1.6 §4.2 · B01/B02/B03). "pending_then_*" answers Pending for `pending_for` seconds, then the final status.
    registration_mode: str = "pending_then_accept"   # accept | reject | pending | pending_then_accept | pending_then_reject
    pending_for: int = 20               # seconds the CSMS keeps a new station Pending (from its first Pending answer)
    heartbeat_interval: int = 10        # seconds, sent with Accepted
    retry_interval: int = 5             # seconds, sent with Pending / Rejected: next BootNotification not before
    # What the CSMS does while a station is Pending (all allowed by B02.FR.01 / 1.6 §4.2), in this order:
    pending_actions: list = field(default_factory=lambda: ["get_variables", "get_base_report", "set_variables",
                                                           "trigger_boot"])
    pending_action_delay: int = 2       # seconds after the first Pending answer
    unreadable_frames: str = "answer"   # "answer" with id "-1" | "drop"  (both allowed: 2.x Part 4 §4.2.3)
    duplicate_ids: str = "reject"       # "reject" a reused message id | "allow"
    authorize_detail: str = "full"      # "full" status | "accepted_or_invalid" (hide Blocked/Expired)
    fault_bad_heartbeat_response: bool = False   # inject a schema-invalid HeartbeatResponse
    # Optional CSMS behaviour that the specifications allow (MAY) — see docs/conformance.md
    report_after_accept: bool = False   # ask for a full report right after Accepted (B05/B07 · 1.6 GetConfiguration)
    cache_expiry_minutes: int = 0       # 0 = omit; else idTokenInfo.cacheExpiryDateTime / idTagInfo.expiryDate (C10)
    tokens: dict = field(default_factory=lambda: {
        "AXEL-0001": "Accepted", "FLEET-0042": "Accepted", "BLOCKED-01": "Blocked", "EXPIRED-01": "Expired"})

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, data: dict) -> None:
        for key, value in data.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = max(0, int(value))
            setattr(self, key, value)


@dataclass
class Transaction:
    id: str
    evse_id: int | None = None
    state: str = "Started"            # Started | Updated | Ended (last eventType)
    charging_state: str | None = None
    id_token: str | None = None
    last_seq: int = -1
    seq_gaps: int = 0                 # seqNo jumps the CSMS noticed (data completeness, Part 0 §2.8.2)
    energy_wh: float = 0.0
    started_at: str | None = None
    ended_at: str | None = None
    stopped_reason: str | None = None
    offline_events: int = 0


@dataclass
class StationRecord:
    id: str
    version: str | None = None
    registration: str = "Unknown"     # our answer to the last BootNotification
    pending_since: float | None = None  # first Pending answer of this registration (pending_then_* modes)
    pending_flow: bool = False        # the while-Pending actions have been started
    boots: int = 0
    online: bool = False
    last_seen: float | None = None
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    connectors: dict = field(default_factory=dict)        # "evse/connector" -> status
    transactions: dict = field(default_factory=dict)      # transactionId -> Transaction
    variables: dict = field(default_factory=dict)         # "Component.Variable" -> value (from reports)
    streams: dict = field(default_factory=dict)           # stream id -> {"meta":…, "values": deque}
    allowed_once: set = field(default_factory=set)        # actions the CSMS triggered while not Accepted
    seen_ids: deque = field(default_factory=lambda: deque(maxlen=200))
    sent_results: dict = field(default_factory=dict)      # message id -> action of CALLRESULTs we sent
    errors: deque = field(default_factory=lambda: deque(maxlen=20))
    security_events: deque = field(default_factory=lambda: deque(maxlen=20))
    availability: dict = field(default_factory=dict)       # 2.1 NotifyEvent: "EVSE 1" / "ChargingStation" -> state
    deprecated_noted: bool = False

    def seen(self) -> None:
        self.last_seen = time.time()

    def snapshot(self) -> dict:
        return {
            "version": self.version, "registration": self.registration, "boots": self.boots,
            "pending_since_ms": int(self.pending_since * 1000) if self.pending_since else None,
            "online": self.online, "last_seen_ms": int(self.last_seen * 1000) if self.last_seen else None,
            "model": self.model, "vendor": self.vendor, "firmware": self.firmware,
            "connectors": dict(self.connectors), "variables": dict(self.variables),
            "transactions": [asdict(t) for t in list(self.transactions.values())[-5:]],
            "streams": {str(k): {"meta": v["meta"], "values": list(v["values"])[-40:]} for k, v in self.streams.items()},
            "allowed_once": sorted(self.allowed_once),
            "security_events": list(self.security_events), "availability": dict(self.availability),
        }
