"""The official OCPP JSON schemas (shipped inside the `ocpp` library): lookup, validation and version diffs.

Part 3 of each OCPP version contains one schema per message:
  <Action>Request.json / <Action>Response.json   for CALL / CALLRESULT
  <Action>.json                                  for SEND messages (2.1), e.g. NotifyPeriodicEventStream
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from jsonschema import validators
from jsonschema.exceptions import best_match

from .versions import OcppVersion

# How a failing JSON-schema keyword maps to an RPC Framework error code (Part 4 §4.3).
# This mapping is a PLAYGROUND DECISION based on the descriptions in that table; the spec doesn't list it.
KEYWORD_TO_CODE = {
    "type": "TypeConstraintViolation",              # "violates data type constraints"
    "required": "OccurrenceConstraintViolation",    # "violates occurrence constraints"
    "minItems": "OccurrenceConstraintViolation",
    "maxItems": "OccurrenceConstraintViolation",
    "enum": "PropertyConstraintViolation",          # "a field contains an invalid value"
    "maxLength": "PropertyConstraintViolation",
    "minLength": "PropertyConstraintViolation",
    "minimum": "PropertyConstraintViolation",
    "maximum": "PropertyConstraintViolation",
    "pattern": "PropertyConstraintViolation",
    "additionalProperties": "FormatViolation",      # "syntactically incorrect": unknown field
}


def code_for_library_error(error) -> str:
    """Translate an `ocpp` library validation exception into our keyword mapping.

    The library maps e.g. a missing required field to ProtocolError; we keep one consistent
    mapping (KEYWORD_TO_CODE) for both directions.
    """
    cause = str((getattr(error, "details", None) or {}).get("cause", ""))
    for needle, keyword in (("is a required property", "required"), ("is not one of", "enum"),
                            ("is not of type", "type"), ("Additional properties", "additionalProperties"),
                            ("is too long", "maxLength"), ("is too short", "minLength"),
                            ("is less than", "minimum"), ("is greater than", "maximum")):
        if needle in cause:
            return KEYWORD_TO_CODE[keyword]
    return getattr(error, "code", "GenericError")


@dataclass
class SchemaViolation:
    code: str          # RPC Framework error code
    keyword: str       # JSON-schema keyword that failed
    path: str          # where in the payload, e.g. "chargingStation.model"
    message: str

    def details(self) -> dict:
        return {"keyword": self.keyword, "path": self.path or "(root)", "message": self.message[:200]}


class SchemaStore:
    """All schemas of one OCPP version."""

    def __init__(self, version: OcppVersion):
        self.version = version
        self.dir = version.schema_dir
        files = [f[:-5] for f in os.listdir(self.dir) if f.endswith(".json")]
        self.request_actions = {f[: -len("Request")] for f in files if f.endswith("Request")}
        self.response_actions = {f[: -len("Response")] for f in files if f.endswith("Response")}
        self.send_actions = {f for f in files if not f.endswith(("Request", "Response"))}

    def schema_name(self, action: str, kind: str) -> str:
        return {"request": action + "Request", "response": action + "Response", "send": action}[kind]

    @lru_cache(maxsize=None)
    def load(self, name: str) -> dict:
        with open(os.path.join(self.dir, name + ".json"), encoding="utf-8-sig") as f:
            return json.load(f)

    @lru_cache(maxsize=None)
    def _validator(self, name: str):
        schema = self.load(name)
        return validators.validator_for(schema)(schema)

    def validate(self, action: str, kind: str, payload: dict) -> SchemaViolation | None:
        """Return the most relevant violation, or None if the payload is valid."""
        validator = self._validator(self.schema_name(action, kind))
        error = best_match(validator.iter_errors(payload))
        if error is None:
            return None
        path = ".".join(str(p) for p in error.absolute_path)
        if error.validator == "required" and isinstance(error.validator_value, list):
            # report which field is missing
            missing = [r for r in error.validator_value if isinstance(error.instance, dict) and r not in error.instance]
            if missing:
                path = (path + "." if path else "") + missing[0]
        return SchemaViolation(KEYWORD_TO_CODE.get(error.validator, "FormatViolation"), error.validator, path, error.message)


@lru_cache(maxsize=None)
def store(version: OcppVersion) -> SchemaStore:
    return SchemaStore(version)


# ---------------------------------------------------------------------------------------------
# Version diff: what changed in a message schema between two OCPP versions
# ---------------------------------------------------------------------------------------------
def _fields(schema: dict, node: dict, prefix: str, out: dict, depth: int = 0) -> None:
    """Flatten a schema into {"field.path": {"type":…, "required":bool, "enum":[…]}}."""
    if depth > 6:
        return
    required = set(node.get("required", []))
    for name, prop in node.get("properties", {}).items():
        if name == "customData":
            continue
        path = f"{prefix}{name}"
        target = prop
        if prop.get("type") == "array" and "items" in prop:
            target, path = prop["items"], path + "[]"
        if "$ref" in target:
            target = schema["definitions"][target["$ref"].split("/")[-1]]
        out[path] = {"type": target.get("type", prop.get("type")), "required": name in required,
                     "enum": target.get("enum")}
        if target.get("type") == "object" or "properties" in target:
            _fields(schema, target, path + ".", out, depth + 1)


def flatten(schema: dict) -> dict:
    out: dict = {}
    _fields(schema, schema, "", out)
    return out


def _collapse(paths) -> list[str]:
    """Keep only the top-most new/removed fields: 'tariff' instead of 'tariff', 'tariff.currency', …"""
    paths = sorted(paths)
    kept: list[str] = []
    for p in paths:
        if not any(p.startswith(k + ".") or p.startswith(k + "[]") for k in kept):
            kept.append(p)
    return kept


def diff(old: SchemaStore, new: SchemaStore, name: str) -> dict:
    """Compare schema `name` (e.g. "BootNotificationRequest") between two versions."""
    have_old = os.path.exists(os.path.join(old.dir, name + ".json"))
    have_new = os.path.exists(os.path.join(new.dir, name + ".json"))
    result = {"name": name, "only_in": None, "added": [], "removed": [], "required_changed": [],
              "enum_added": [], "enum_removed": [], "type_changed": []}
    if not (have_old and have_new):
        result["only_in"] = new.version.name if have_new else old.version.name if have_old else None
        return result
    a, b = flatten(old.load(name)), flatten(new.load(name))
    result["added"] = _collapse(k for k in b if k not in a)
    result["removed"] = _collapse(k for k in a if k not in b)
    for k in sorted(set(a) & set(b)):
        if a[k]["required"] != b[k]["required"]:
            result["required_changed"].append({"field": k, "from": a[k]["required"], "to": b[k]["required"]})
        if a[k]["type"] != b[k]["type"]:
            result["type_changed"].append({"field": k, "from": a[k]["type"], "to": b[k]["type"]})
        ea, eb = set(a[k]["enum"] or []), set(b[k]["enum"] or [])
        if eb - ea:
            result["enum_added"].append({"field": k, "values": sorted(eb - ea)})
        if ea - eb:
            result["enum_removed"].append({"field": k, "values": sorted(ea - eb)})
    return result


def is_unchanged(d: dict) -> bool:
    return d["only_in"] is None and not any(d[k] for k in ("added", "removed", "required_changed",
                                                         "enum_added", "enum_removed", "type_changed"))
