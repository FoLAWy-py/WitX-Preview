"""What every rule shares: the refusal and the canonical form of a value."""

import json


class Rejected(Exception):
    """A request that the rules refuse. ``code`` is stable and safe to show to the model."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
