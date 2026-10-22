"""Enough of typing for annotated code to run on MicroPython, which has no typing module.

supie ships this with MicroPython functions only. Every name exists and does
nothing, so hints such as Optional[int] or list[str] | None evaluate harmlessly.
Its own annotations use only builtins, since it is the typing module.
"""

from __future__ import annotations

TYPE_CHECKING = False


class _Hint:
    """Stands in for any type hint: subscripting, calling and | all return it."""

    def __getitem__(self, item: object) -> _Hint:
        """Optional[int] and the like."""
        return self

    def __call__(self, *args: object, **kwargs: object) -> _Hint:
        """TypeVar("T") and the like."""
        return self

    def __or__(self, other: object) -> _Hint:
        """int | None, with the hint on the left."""
        return self

    def __ror__(self, other: object) -> _Hint:
        """int | None, with the hint on the right."""
        return self


class _Base:
    """Stands in for a base class such as Generic or Protocol, subscripted or not."""

    def __getitem__(self, item: object) -> type:
        """Generic[T] is a plain base class."""
        return object


_hint = _Hint()
Generic = _Base()
TypedDict = dict
NamedTuple = tuple


class Protocol:
    """Base class for protocols; does nothing."""


def cast(kind: object, value: object) -> object:
    """Return value unchanged."""
    return value


def overload(fn: object) -> object:
    """Return fn unchanged."""
    return fn


def final(obj: object) -> object:
    """Return obj unchanged."""
    return obj


def runtime_checkable(cls: object) -> object:
    """Return cls unchanged."""
    return cls


def NewType(name: str, kind: object) -> object:
    """A NewType is the identity function at runtime."""
    return lambda value: value


def __getattr__(name: str) -> _Hint:
    """Any other name from typing, such as Any, Optional or Callable."""
    return _hint
