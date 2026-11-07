"""The artifact store a workflow's composer reads and writes, and the in-process sessions it holds."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

# airlie.config imports airlie.flow.spec, so this package cannot import the story state at load.
if TYPE_CHECKING:
    from airlie.story.state import StoryState


class Store(Protocol):
    """Named artifacts. A name is absent while its value is None, {}, [], False or "". A dotted name
    (`built.01`) is a key inside a dict artifact."""

    def has(self, name: str) -> bool: ...
    def get(self, name: str) -> Any: ...
    def put(self, name: str, value: Any) -> None: ...
    def save(self) -> None: ...
    def emit(self, kind: str, **fields: Any) -> None: ...


class MemoryStore:
    """A dict-backed store for tests; `events` records every emit."""

    def __init__(self, loose: Mapping[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(loose or {})
        self.events: list[tuple[str, dict[str, Any]]] = []

    def has(self, name: str) -> bool:
        return bool(self._values.get(name))

    def get(self, name: str) -> Any:
        return self._values[name]

    def put(self, name: str, value: Any) -> None:
        self._values[name] = value

    def save(self) -> None:
        pass

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


class StateStore:
    """A store over a StoryState: its fields are the artifacts, `<field>.<key>` reaches into a dict field,
    `feedback.<name>` (carried loop feedback) and `package.<layer>` (a layer's ready package, until it is
    built: a run that stops between the two asks for it again, as the driver does) are kept here in memory
    and never saved, and `loose` names (`card`, `repo`) are read from the mapping given, ahead of a state
    field of the same name."""

    MEMORY = ("feedback", "package")

    def __init__(self, st: StoryState, *, loose: Mapping[str, Any]) -> None:
        self.st = st
        self._fields = frozenset(f.name for f in dataclasses.fields(st)) - {"path"}
        self._loose = dict(loose)
        self._memory: dict[str, dict[str, Any]] = {head: {} for head in self.MEMORY}

    def has(self, name: str) -> bool:
        try:
            return bool(self.get(name))
        except KeyError:
            return False

    def get(self, name: str) -> Any:
        head, _, key = name.partition(".")
        if head in self._memory and key:
            return self._memory[head][key]
        if not key:
            if head in self._loose:  # a loose name wins over a state field of the same name (`card`)
                return self._loose[head]
            if head not in self._fields:
                raise KeyError(name)
            return getattr(self.st, head)
        return self._dict_field(name, head)[key]

    def put(self, name: str, value: Any) -> None:
        head, _, key = name.partition(".")
        if head in self._memory and key:
            self._memory[head][key] = value
        elif not key:
            if head in self._loose or head not in self._fields:  # a loose name is read-only: `get` reads
                raise KeyError(name)  # the mapping, so a write to the state field would never be read back
            setattr(self.st, head, value)
        else:
            self._dict_field(name, head)[key] = value

    def _dict_field(self, name: str, head: str) -> dict[str, Any]:
        container = getattr(self.st, head) if head in self._fields else None
        if not isinstance(container, dict):
            raise KeyError(name)
        return container

    def save(self) -> None:
        self.st.save()

    def emit(self, kind: str, **fields: Any) -> None:
        self.st.emit(kind, **fields)


class Sessions:
    """Live objects (a conversation's drafts, say) held by name for one run; never saved."""

    def __init__(self) -> None:
        self._held: dict[str, Any] = {}

    def get(self, name: str) -> Any | None:
        return self._held.get(name)

    def put(self, name: str, value: Any) -> None:
        self._held[name] = value

    def pop(self, name: str) -> Any | None:
        return self._held.pop(name, None)

    def clear(self) -> None:
        self._held.clear()
