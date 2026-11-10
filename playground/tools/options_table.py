"""Print the optional-behaviour table of docs/conformance.md from playground/station/options.py.

    python -m playground.tools.options_table > /tmp/table.md
"""
from ..station.options import OPTIONS, text


def main() -> None:
    print("| Option | Versions | 2.0.1 / 2.1 name | 1.6 key | Default | Reference |")
    print("|---|---|---|---|---|---|")
    for o in OPTIONS:
        dm = f"`{o.key}`" if o.dm else "implementation choice"
        dm = dm if any(v != "1.6" for v in o.versions) else "—"
        k16 = f"`{o.key16}`" if o.key16 else ("implementation choice" if "1.6" in o.versions else "—")
        refs = " · ".join(r for r in (o.ref2x, o.ref16) if r)
        print(f"| {o.label} | {', '.join(o.versions)} | {dm} | {k16} | `{text(o.default)}` | {refs} |")


if __name__ == "__main__":
    main()
