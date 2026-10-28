"""The roman case's oracle: the story's written spec, checked independently of the tests the run wrote.

Run as `python3 -I roman_oracle.py <checkout>`; prints one JSON object: {"spec": [[name, ok], ...],
"extra": [[name, ok], ...]}. "extra" is beyond the written spec (types, case) and is reported, not held to."""

from __future__ import annotations

import importlib
import json
import sys

VALUES = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
          (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]  # fmt: skip


def reference(n: int) -> str:
    out = ""
    for value, numeral in VALUES:
        while n >= value:
            out, n = out + numeral, n - value
    return out


def raises(
    f: object, *args: object, exc: type[BaseException] | tuple[type[BaseException], ...] = ValueError
) -> bool:
    try:
        f(*args)  # type: ignore[operator]
    except exc:
        return True
    except Exception:
        return False
    return False


def main(checkout: str) -> None:
    sys.path.insert(0, checkout)
    roman = importlib.import_module("sandbox.roman")
    spec: list[tuple[str, bool]] = []
    extra: list[tuple[str, bool]] = []

    def check(into: list[tuple[str, bool]], name: str, fn: object) -> None:
        try:
            ok = bool(fn())  # type: ignore[operator]
        except Exception:
            ok = False
        into.append((name, ok))

    check(spec, "to_roman matches the reference for 1..3999",
          lambda: all(roman.to_roman(n) == reference(n) for n in range(1, 4000)))  # fmt: skip
    check(spec, "from_roman inverts it for 1..3999",
          lambda: all(roman.from_roman(reference(n)) == n for n in range(1, 4000)))  # fmt: skip
    for n in (0, -1, 4000, 10**6):
        check(spec, f"to_roman({n}) raises ValueError", lambda n=n: raises(roman.to_roman, n))
    for s in ("", "IIII", "VV", "IC", "IL", "XM", "VX", "MMMM", "ABC", "MCMC", "IIV", "XXXX"):
        check(spec, f"from_roman({s!r}) raises ValueError", lambda s=s: raises(roman.from_roman, s))
    for v in (3.5, "5", True, None):
        check(
            extra,
            f"to_roman({v!r}) raises",
            lambda v=v: raises(roman.to_roman, v, exc=(ValueError, TypeError)),
        )
    for v in ("iv", " IV", 5, None):
        check(extra, f"from_roman({v!r}) raises",
              lambda v=v: raises(roman.from_roman, v, exc=(ValueError, TypeError)))  # fmt: skip
    print(json.dumps({"spec": spec, "extra": extra}))


if __name__ == "__main__":
    main(sys.argv[1])
