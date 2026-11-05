"""The official OCPP JSON schemas: lookup, validation, cross-version comparison.

Where the schemas come from
---------------------------
By default, the copies that ship inside the `ocpp` Python library. You can point the playground at
the official OCA files instead (`--schemas 2.0.1=/path/to/OCPP-2.0.1_part3_JSON_schemas`). See
docs/schema-provenance.md for how the two sets compare.

File names
----------
  1.6     <Action>.json         + <Action>Response.json
  2.x     <Action>Request.json  + <Action>Response.json      + <Action>.json for SEND messages (2.1)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from jsonschema import validators
from jsonschema.exceptions import best_match

from .versions import ALL, BY_NAME, OcppVersion

# Failing JSON-schema keyword → logical RPC error (version.code() turns it into the exact string).
# PLAYGROUND DECISION based on the descriptions in the error-code tables; the specs don't list this mapping.
KEYWORD_TO_LOGICAL = {
    "type": "type",                 # "violates data type constraints"
    "required": "occurrence",       # "violates occurrence constraints"
    "minItems": "occurrence", "maxItems": "occurrence",
    "enum": "property",             # "a field contains an invalid value"
    "maxLength": "property", "minLength": "property", "minimum": "property", "maximum": "property",
    "pattern": "property",
    "additionalProperties": "format",   # "syntactically incorrect": unknown field
}

_custom_dirs: dict[str, str] = {}


def use_directory(version_name: str, directory: str) -> None:
    """Load a version's schemas from `directory` (e.g. the official OCA folder) instead of the library."""
    if not any(f.endswith(".json") for f in os.listdir(directory)):
        raise ValueError(f"no .json schemas in {directory}")
    _custom_dirs[version_name] = directory
    store.cache_clear()
    catalogue.cache_clear()


@dataclass
class SchemaViolation:
    logical: str       # e.g. "property"
    code: str          # the exact error-code string for this version
    keyword: str
    path: str
    message: str

    def details(self) -> dict:
        return {"keyword": self.keyword, "path": self.path or "(root)", "message": self.message[:200]}


class SchemaStore:
    """All schemas of one OCPP version."""

    def __init__(self, version: OcppVersion):
        self.version = version
        self.dir = _custom_dirs.get(version.name, version.library_schema_dir)
        self.source = "official" if version.name in _custom_dirs else "ocpp library"
        self.files = sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json"))
        self.response_actions = {f[: -len("Response")] for f in self.files if f.endswith("Response")}
        if version.request_suffix:
            self.request_actions = {f[: -len("Request")] for f in self.files if f.endswith("Request")}
            self.send_actions = {f for f in self.files if not f.endswith(("Request", "Response"))}
        else:                                       # 1.6: the request schema is just <Action>.json
            self.request_actions = {f for f in self.files if not f.endswith("Response")}
            self.send_actions = set()

    def schema_name(self, action: str, kind: str) -> str:
        return {"request": action + self.version.request_suffix, "response": action + "Response", "send": action}[kind]

    def has(self, action: str, kind: str) -> bool:
        return self.schema_name(action, kind) in self.files

    @lru_cache(maxsize=None)
    def load_file(self, name: str) -> dict:
        with open(os.path.join(self.dir, name + ".json"), encoding="utf-8-sig") as f:
            return json.load(f)

    def load(self, action: str, kind: str) -> dict:
        return self.load_file(self.schema_name(action, kind))

    @lru_cache(maxsize=None)
    def _validator(self, name: str):
        schema = self.load_file(name)
        return validators.validator_for(schema)(schema)

    def validate(self, action: str, kind: str, payload: dict) -> SchemaViolation | None:
        """Return the most relevant violation, or None if the payload is valid."""
        error = best_match(self._validator(self.schema_name(action, kind)).iter_errors(payload))
        if error is None:
            return None
        path = ".".join(str(p) for p in error.absolute_path)
        if error.validator == "required" and isinstance(error.validator_value, list) and isinstance(error.instance, dict):
            missing = [r for r in error.validator_value if r not in error.instance]
            if missing:
                path = (path + "." if path else "") + missing[0]
        logical = KEYWORD_TO_LOGICAL.get(error.validator, "format")
        return SchemaViolation(logical, self.version.code(logical), error.validator, path, error.message)

    def messages(self) -> set[tuple[str, str]]:
        """{(action, kind)} for every schema of this version."""
        out = {(a, "request") for a in self.request_actions} | {(a, "response") for a in self.response_actions}
        return out | {(a, "send") for a in self.send_actions}


@lru_cache(maxsize=None)
def store(version: OcppVersion) -> SchemaStore:
    return SchemaStore(version)


def code_for_library_error(error, version: OcppVersion) -> str:
    """Translate an `ocpp` library validation exception into our keyword mapping (same codes both ways)."""
    cause = str((getattr(error, "details", None) or {}).get("cause", ""))
    for needle, keyword in (("is a required property", "required"), ("is not one of", "enum"),
                            ("is not of type", "type"), ("Additional properties", "additionalProperties"),
                            ("is too long", "maxLength"), ("is too short", "minLength"),
                            ("is less than", "minimum"), ("is greater than", "maximum")):
        if needle in cause:
            return version.code(KEYWORD_TO_LOGICAL[keyword])
    return getattr(error, "code", version.code("generic"))


# ---------------------------------------------------------------------------------------------
# Comparison between versions
# ---------------------------------------------------------------------------------------------
# Messages that were renamed or replaced between 1.6 and 2.x (same meaning, different message).
REPLACED_IN_2X = {
    "StartTransaction": (["TransactionEvent"], "2.0.1 Part 0 §2.2.1: Start/StopTransaction replaced by TransactionEvent"),
    "StopTransaction": (["TransactionEvent"], "2.0.1 Part 0 §2.2.1"),
    "RemoteStartTransaction": (["RequestStartTransaction"], "2.0.1 Part 0 §2.8.1: renamed, it's a request to *try* to start"),
    "RemoteStopTransaction": (["RequestStopTransaction"], "renamed like RemoteStartTransaction (2.0.1 Part 0 §2.8.1)"),
    "GetConfiguration": (["GetVariables", "GetBaseReport"], "2.0.1 Part 1 §4: configuration keys became the Device Model"),
    "ChangeConfiguration": (["SetVariables"], "2.0.1 Part 1 §4: Device Model"),
    "GetDiagnostics": (["GetLog"], "2.0.1 Part 2 N01 Retrieve Log Information"),
    "DiagnosticsStatusNotification": (["LogStatusNotification"], "2.0.1 Part 2 N01"),
}
REPLACES_FROM_16 = {new: old for old, (news, _) in REPLACED_IN_2X.items() for new in news}

# Messages the `ocpp` library ships for 1.6 that are NOT in the OCPP 1.6 Edition 2 schema set:
# they come from the later "OCPP 1.6 security" white paper (an extension), checked against the official files.
SECURITY_EXTENSION_16 = {
    "CertificateSigned", "DeleteCertificate", "ExtendedTriggerMessage", "GetInstalledCertificateIds", "GetLog",
    "InstallCertificate", "LogStatusNotification", "SecurityEventNotification", "SignCertificate",
    "SignedFirmwareStatusNotification", "SignedUpdateFirmware",
}


def _fields(schema: dict, node: dict, prefix: str, out: dict, depth: int = 0) -> None:
    """Flatten a schema into {"field.path": {"type", "required", "enum", "maxLength"}}."""
    if depth > 7:
        return
    required = set(node.get("required", []))
    for name, prop in node.get("properties", {}).items():
        if name == "customData":
            continue
        path = f"{prefix}{name}"
        target = prop
        if "$ref" in target:
            target = schema.get("definitions", {}).get(target["$ref"].split("/")[-1], target)
        if target.get("type") == "array" and "items" in target:
            items = target["items"]
            path += "[]"
            if "$ref" in items:
                items = schema.get("definitions", {}).get(items["$ref"].split("/")[-1], items)
            target = {**items, "type": items.get("type", "object")}
        out[path] = {"type": target.get("type"), "required": name in required, "enum": target.get("enum"),
                     "maxLength": target.get("maxLength"), "format": target.get("format")}
        if target.get("type") == "object" or "properties" in target:
            _fields(schema, target, path + ".", out, depth + 1)


def flatten(schema: dict) -> dict:
    out: dict = {}
    _fields(schema, schema, "", out)
    return out


def _collapse(paths) -> list[str]:
    kept: list[str] = []
    for p in sorted(paths):
        if not any(p.startswith(k + ".") or p.startswith(k + "[]") for k in kept):
            kept.append(p)
    return kept


def compare(action: str, kind: str, a: OcppVersion, b: OcppVersion) -> dict:
    """Field-by-field comparison of one message between two versions (used by the UI and the report)."""
    sa, sb = store(a), store(b)
    in_a, in_b = sa.has(action, kind), sb.has(action, kind)
    res = {"action": action, "kind": kind, "a": a.name, "b": b.name, "in_a": in_a, "in_b": in_b,
           "status": "", "rows": [], "added": [], "removed": [], "changed": 0,
           "schema_a": sa.load(action, kind) if in_a else None, "schema_b": sb.load(action, kind) if in_b else None,
           "replaced": replacement_note(action, a, b),
           "ext16": action in SECURITY_EXTENSION_16 and "1.6" in (a.name, b.name)}
    if not (in_a and in_b):
        res["status"] = f"only {a.name}" if in_a else f"only {b.name}" if in_b else "absent"
        fa = flatten(res["schema_a"]) if in_a else {}
        fb = flatten(res["schema_b"]) if in_b else {}
    else:
        fa, fb = flatten(res["schema_a"]), flatten(res["schema_b"])
    for path in sorted(set(fa) | set(fb)):
        x, y = fa.get(path), fb.get(path)
        if x and not y:
            change = "removed"
        elif y and not x:
            change = "added"
        elif x != y:
            change = "changed"
        else:
            change = "same"
        res["rows"].append({"path": path, "a": x, "b": y, "change": change})
    if in_a and in_b:
        res["added"] = _collapse(r["path"] for r in res["rows"] if r["change"] == "added")
        res["removed"] = _collapse(r["path"] for r in res["rows"] if r["change"] == "removed")
        res["changed"] = sum(r["change"] == "changed" for r in res["rows"])
        res["status"] = "same" if not (res["added"] or res["removed"] or res["changed"]) else "changed"
    return res


def replacement_note(action: str, a: OcppVersion, b: OcppVersion) -> str | None:
    if "1.6" not in (a.name, b.name) or a.name == b.name:
        return None
    if action in REPLACED_IN_2X and not store(BY_NAME["2.0.1"]).has(action, "request"):
        news, why = REPLACED_IN_2X[action]
        return f"1.6 {action} → 2.x {' / '.join(news)} ({why})"
    if action in REPLACES_FROM_16:
        old = REPLACES_FROM_16[action]
        return f"2.x {action} ← 1.6 {old} ({REPLACED_IN_2X[old][1]})"
    return None


@lru_cache(maxsize=None)
def catalogue() -> dict:
    """Every (action, kind) of every version, and its status for each version pair."""
    keys = sorted(set().union(*(store(v).messages() for v in ALL)))
    pairs = [("1.6", "2.0.1"), ("2.0.1", "2.1"), ("1.6", "2.1")]
    items = []
    for action, kind in keys:
        item = {"action": action, "kind": kind, "in": {v.name: store(v).has(action, kind) for v in ALL}, "status": {},
                "ext16": action in SECURITY_EXTENSION_16 and store(BY_NAME["1.6"]).has(action, kind)}
        for a, b in pairs:
            c = compare(action, kind, BY_NAME[a], BY_NAME[b])
            item["status"][f"{a}|{b}"] = "renamed" if c["status"].startswith("only") and c["replaced"] else c["status"]
        items.append(item)
    return {"items": items, "pairs": [f"{a}|{b}" for a, b in pairs],
            "sources": {v.name: store(v).source for v in ALL}}
