"""Enough of typing for annotated code to run on MicroPython, which has no typing module.

supie ships this with MicroPython functions only. Every name exists and does
nothing, so hints such as Optional[int] or list[str] | None evaluate harmlessly.
"""

TYPE_CHECKING = False


class _Hint:
    """Stands in for any type hint: subscripting, calling and | all return it."""

    def __getitem__(self, item):
        """Optional[int] and the like."""
        return self

    def __call__(self, *args, **kwargs):
        """TypeVar("T") and the like."""
        return self

    def __or__(self, other):
        """int | None, with the hint on the left."""
        return self

    def __ror__(self, other):
        """int | None, with the hint on the right."""
        return self


class _Base:
    """Stands in for a base class such as Generic or Protocol, subscripted or not."""

    def __getitem__(self, item):
        """Generic[T] is a plain base class."""
        return object


_hint = _Hint()
Generic = _Base()
TypedDict = dict
NamedTuple = tuple


class Protocol:
    """Base class for protocols; does nothing."""


def cast(kind, value):
    """Return value unchanged."""
    return value


def overload(fn):
    """Return fn unchanged."""
    return fn


def final(obj):
    """Return obj unchanged."""
    return obj


def runtime_checkable(cls):
    """Return cls unchanged."""
    return cls


def NewType(name, kind):
    """A NewType is the identity function at runtime."""
    return lambda value: value


def __getattr__(name):
    """Any other name from typing, such as Any, Optional or Callable."""
    return _hint
