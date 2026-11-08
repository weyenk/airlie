"""The artifact store a workflow's composer reads and writes, and the in-process sessions it holds."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from airlie.flow.spec import RESERVED, reserved_output

# airlie.config imports airlie.flow.spec, so this package cannot import the story state at load.
if TYPE_CHECKING:
    from airlie.story.state import StoryState


MEMORY = ("feedback", "package")
"""Heads kept in memory only and never saved: carried loop feedback (`feedback.<node>`) and a layer's ready
package (`package.<layer>`). A loop re-run never clears them."""


class Store(Protocol):
    """Named artifacts. A name is absent while its value is None, {}, [], False or "". A dotted name
    (`built.01`) is a key inside a dict artifact. `put(name, None)` clears the name. The done set holds the
    nodes that finished (`<node>`, or `<node>@<item>` inside a bundle item)."""

    def has(self, name: str) -> bool: ...
    def get(self, name: str) -> Any: ...
    def put(self, name: str, value: Any) -> None: ...
    def save(self) -> None: ...
    def emit(self, kind: str, **fields: Any) -> None: ...
    def done(self, name: str) -> bool: ...
    def mark_done(self, name: str) -> None: ...
    def clear_done(self, name: str) -> None: ...


class MemoryStore:
    """A dict-backed store for tests; `events` records every emit."""

    def __init__(self, loose: Mapping[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(loose or {})
        self._done: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def has(self, name: str) -> bool:
        return bool(self._values.get(name))

    def get(self, name: str) -> Any:
        return self._values[name]

    def put(self, name: str, value: Any) -> None:
        if value is None:
            self._values.pop(name, None)
        else:
            self._values[name] = value

    def save(self) -> None:
        pass

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def done(self, name: str) -> bool:
        return name in self._done

    def mark_done(self, name: str) -> None:
        if name not in self._done:
            self._done.append(name)

    def clear_done(self, name: str) -> None:
        if name in self._done:
            self._done.remove(name)


class StateStore:
    """A store over a StoryState: its fields are the artifacts, `<field>.<key>` reaches into a dict field,
    `feedback.<name>` (carried loop feedback) and `package.<layer>` (a layer's ready package, until it is
    built: a run that stops between the two asks for it again, as the driver does) are kept here in memory
    and never saved, and `loose` names (`card`, `repo`) are read from the mapping given, ahead of a state
    field of the same name. Any other name is a workflow's own artifact (design D15): it lives in
    `st.artifacts` under that name (`<name>.<key>` in the dict there), saved with the state.

    `put(name, None)` clears: a state field goes back to its default, an artifact or a key inside a dict is
    removed. The done set is `st.nodes_done`, saved with the state."""

    MEMORY = MEMORY
    RESERVED = RESERVED
    """Names no node may write (`airlie.flow.spec.RESERVED`): the state's own place, the artifact store whole
    and the card's snapshot; `check_workflows` refuses them as outputs too."""

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
            if head in self._fields:
                return getattr(self.st, head)
            if head in self.MEMORY or head in self.RESERVED:
                raise KeyError(name)
            return self.st.artifacts[head]
        return self._dict_field(name, head)[key]

    def put(self, name: str, value: Any) -> None:
        if value is None:
            self._clear(name)
            return
        head, _, key = name.partition(".")
        if head in self._memory and key:
            self._memory[head][key] = value
        elif reserved_output(name):
            raise KeyError(name)
        elif not key:
            if head in self._loose or head in self.MEMORY:
                # a loose name is read-only: `get` reads the mapping, so a write to a state field or an
                # artifact of the same name would never be read back
                raise KeyError(name)
            if head in self._fields:
                setattr(self.st, head, value)
            else:
                self.st.artifacts[head] = value
        elif head in self._fields:
            self._dict_field(name, head)[key] = value
        elif head in self._loose:
            raise KeyError(name)
        else:
            container = self.st.artifacts.setdefault(head, {})
            if not isinstance(container, dict):
                raise KeyError(name)
            container[key] = value

    def _clear(self, name: str) -> None:
        head, _, key = name.partition(".")
        if head in self._memory and key:
            self._memory[head].pop(key, None)
        elif reserved_output(name) or head in self._loose or (head in self.MEMORY and not key):
            raise KeyError(name)
        elif not key and head in self._fields:
            f = next(f for f in dataclasses.fields(self.st) if f.name == head)
            empty = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
            setattr(self.st, head, None if empty is dataclasses.MISSING else empty)
        elif not key:
            self.st.artifacts.pop(head, None)
        else:
            container = getattr(self.st, head) if head in self._fields else self.st.artifacts.get(head)
            if isinstance(container, dict):
                container.pop(key, None)

    def done(self, name: str) -> bool:
        return name in self.st.nodes_done

    def mark_done(self, name: str) -> None:
        if name not in self.st.nodes_done:
            self.st.nodes_done.append(name)

    def clear_done(self, name: str) -> None:
        if name in self.st.nodes_done:
            self.st.nodes_done.remove(name)

    def _dict_field(self, name: str, head: str) -> dict[str, Any]:
        if head in self._fields:
            container = getattr(self.st, head)
        elif head in self._loose or head in self.RESERVED or head in self.MEMORY:
            container = None
        else:
            container = self.st.artifacts.get(head)
        if not isinstance(container, dict):
            raise KeyError(name)
        return container

    def save(self) -> None:
        self.st.save()

    def emit(self, kind: str, **fields: Any) -> None:
        self.st.emit(kind, **fields)


class BundleStore:
    """What one run of a bundle's workflow sees (design: "Bundles and isolation"): the parent's store read by
    name, and written only under the bundle workflow's declared `outputs` (`{item}` filled with the item)
    and the memory heads (the feedback a loop inside it carries). Any other write is a KeyError naming the
    bundle. Events, saves and the done set are the parent's."""

    def __init__(self, parent: Store, *, workflow: str, outputs: Collection[str], item: str | None) -> None:
        self.parent = parent
        self.workflow = workflow
        self.item = item
        self.outputs = frozenset(o if item is None else o.replace("{item}", item) for o in outputs)

    def has(self, name: str) -> bool:
        return self.parent.has(name)

    def get(self, name: str) -> Any:
        return self.parent.get(name)

    def put(self, name: str, value: Any) -> None:
        if name not in self.outputs and name.partition(".")[0] not in MEMORY:
            raise KeyError(f"bundle {self.workflow}: '{name}' is not one of its outputs")
        self.parent.put(name, value)

    def save(self) -> None:
        self.parent.save()

    def emit(self, kind: str, **fields: Any) -> None:
        self.parent.emit(kind, **fields)

    def done(self, name: str) -> bool:
        return self.parent.done(name)

    def mark_done(self, name: str) -> None:
        self.parent.mark_done(name)

    def clear_done(self, name: str) -> None:
        self.parent.clear_done(name)


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
