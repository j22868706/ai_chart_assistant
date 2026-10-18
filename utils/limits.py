"""Local resource limits; capacity is per process, without an unbounded queue."""

import os


def positive_limit(name: str, default: int, ceiling: int) -> int:
    value = os.getenv(name, str(default))
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError(f"{name} must be a positive integer")
    result = int(value)
    if not 1 <= result <= ceiling:
        raise RuntimeError(f"{name} is outside the supported range")
    return result


class Capacity:
    """Called only on the application's event loop; acquire never waits."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active -= 1
