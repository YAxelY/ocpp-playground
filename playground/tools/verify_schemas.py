"""Compare the schemas the playground uses (the `ocpp` library's) with an official OCA schema folder.

    python -m playground.tools.verify_schemas 2.0.1 ~/OCPP-2.0.1_part3_JSON_schemas
    python -m playground.tools.verify_schemas 1.6   ~/OCPP_1.6_documentation/schemas/json

Differences in descriptions, ids, "$schema", "$comment" and "javaType" are cosmetic and ignored.
"""
from __future__ import annotations

import glob
import json
import os
import sys

from ..protocol.versions import BY_NAME

COSMETIC = {"description", "$comment", "comment", "$id", "id", "$schema", "javaType", "tsEnumNames"}


def _norm(x):
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items() if k not in COSMETIC}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _first_diff(a, b, path=""):
    if type(a) is not type(b):
        return f"{path}: {str(a)[:60]} ≠ {str(b)[:60]}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: only in {'official' if k in a else 'library'}"
            r = _first_diff(a[k], b[k], f"{path}.{k}")
            if r:
                return r
    elif isinstance(a, list):
        if len(a) != len(b):
            extra = set(map(str, b)) - set(map(str, a)) or set(map(str, a)) - set(map(str, b))
            return f"{path}: {len(a)} items (official) vs {len(b)} (library), e.g. {sorted(extra)[:3]}"
        for i, (x, y) in enumerate(zip(a, b)):
            r = _first_diff(x, y, f"{path}[{i}]")
            if r:
                return r
    elif a != b:
        return f"{path}: {a!r} (official) ≠ {b!r} (library)"
    return None


def verify(version_name: str, official_dir: str) -> dict:
    version = BY_NAME[version_name]
    load = lambda p: json.load(open(p, encoding="utf-8-sig"))
    official = {os.path.basename(p)[:-5]: p for p in glob.glob(os.path.join(official_dir, "**", "*.json"), recursive=True)}
    library = {f[:-5]: os.path.join(version.library_schema_dir, f)
               for f in os.listdir(version.library_schema_dir) if f.endswith(".json")}
    report = {"version": version_name, "official": len(official), "library": len(library),
              "identical": 0, "cosmetic": 0, "structural": [], "only_official": sorted(set(official) - set(library)),
              "only_library": sorted(set(library) - set(official))}
    for name in sorted(set(official) & set(library)):
        a, b = load(official[name]), load(library[name])
        if a == b:
            report["identical"] += 1
        elif _norm(a) == _norm(b):
            report["cosmetic"] += 1
        else:
            report["structural"].append((name, _first_diff(_norm(a), _norm(b))))
    return report


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    r = verify(sys.argv[1], os.path.expanduser(sys.argv[2]))
    print(f"OCPP {r['version']}: {r['official']} official schemas, {r['library']} in the library")
    print(f"  identical: {r['identical']} · only cosmetic differences: {r['cosmetic']} · structural: {len(r['structural'])}")
    for name, d in r["structural"]:
        print(f"    {name}: {d}")
    if r["only_official"]:
        print(f"  only in the official set: {', '.join(r['only_official'])}")
    if r["only_library"]:
        print(f"  only in the library: {', '.join(r['only_library'])}")


if __name__ == "__main__":
    main()
