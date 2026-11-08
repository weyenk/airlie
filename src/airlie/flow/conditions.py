"""A line's `when`: a small expression over artifacts, the card and config.

    expr := term (("and" | "or") term)*      `and` binds tighter than `or`
    term := "not" term | "(" expr ")" | atom
    atom := path "exists" | path ("==" | "!=") literal | path

A path is dotted (`verdict.decision`); a bare path is true when its value is truthy. A literal is a quoted
string, a number, `true`, `false` or a bare word (a string). A path the lookup cannot find makes `exists`
false and any comparison false. Hand-written tokenizer and recursive descent; imports nothing from `spec`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_KEYWORDS = frozenset({"and", "or", "not", "exists"})
_TOKEN = re.compile(r"""(?P<op>==|!=|\(|\))|(?P<str>"[^"]*"|'[^']*')|(?P<word>[^\s()=!"']+)""")


class ConditionError(ValueError):
    """The condition does not parse; the message has the position and what was expected."""


@dataclass(frozen=True)
class _Token:
    kind: str  # op, str, word, end
    text: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ConditionError(f"at {pos}: unexpected {text[pos]!r}")
        kind = m.lastgroup or "word"
        tokens.append(_Token(kind, m.group(kind), pos))
        pos = m.end()
    tokens.append(_Token("end", "", len(text)))
    return tokens


class _Node:
    def names(self) -> frozenset[str]:
        raise NotImplementedError

    def evaluate(self, lookup: Callable[[str], Any]) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class _Atom(_Node):
    path: str
    op: str  # "", "exists", "==", "!="
    literal: Any = None

    def names(self) -> frozenset[str]:
        return frozenset({self.path.split(".")[0]})

    def evaluate(self, lookup: Callable[[str], Any]) -> bool:
        try:
            value = lookup(self.path)
        except KeyError:
            return False
        if self.op == "exists":
            return True
        if self.op == "==":
            return bool(value == self.literal)
        if self.op == "!=":
            return bool(value != self.literal)
        return bool(value)


@dataclass(frozen=True)
class _Not(_Node):
    inner: _Node

    def names(self) -> frozenset[str]:
        return self.inner.names()

    def evaluate(self, lookup: Callable[[str], Any]) -> bool:
        return not self.inner.evaluate(lookup)


@dataclass(frozen=True)
class _Bool(_Node):
    op: str  # "and" | "or"
    parts: tuple[_Node, ...]

    def names(self) -> frozenset[str]:
        return frozenset().union(*(p.names() for p in self.parts))

    def evaluate(self, lookup: Callable[[str], Any]) -> bool:
        results = (p.evaluate(lookup) for p in self.parts)
        return all(results) if self.op == "and" else any(results)


@dataclass(frozen=True)
class Condition:
    text: str
    root: _Node

    @property
    def names(self) -> frozenset[str]:
        """The head of every path the condition reads: `verdict`, `card`, `config`."""
        return self.root.names()

    def evaluate(self, lookup: Callable[[str], Any]) -> bool:
        """`lookup(path)` returns the value of a dotted path or raises KeyError."""
        return self.root.evaluate(lookup)


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.i = 0

    @property
    def tok(self) -> _Token:
        return self.tokens[self.i]

    def is_word(self, *words: str) -> bool:
        return self.tok.kind == "word" and self.tok.text in words

    def is_op(self, *ops: str) -> bool:
        return self.tok.kind == "op" and self.tok.text in ops

    def fail(self, expected: str) -> ConditionError:
        t = self.tok
        found = "end of the condition" if t.kind == "end" else repr(t.text)
        return ConditionError(f"at {t.pos}: expected {expected}, found {found}")

    def expr(self) -> _Node:
        parts = [self.conj()]
        while self.is_word("or"):
            self.i += 1
            parts.append(self.conj())
        return parts[0] if len(parts) == 1 else _Bool("or", tuple(parts))

    def conj(self) -> _Node:
        parts = [self.term()]
        while self.is_word("and"):
            self.i += 1
            parts.append(self.term())
        return parts[0] if len(parts) == 1 else _Bool("and", tuple(parts))

    def term(self) -> _Node:
        if self.is_word("not"):
            self.i += 1
            return _Not(self.term())
        if self.is_op("("):
            self.i += 1
            inner = self.expr()
            if not self.is_op(")"):
                raise self.fail("')'")
            self.i += 1
            return inner
        return self.atom()

    def atom(self) -> _Node:
        if self.tok.kind != "word" or self.tok.text in _KEYWORDS:
            raise self.fail("a path")
        path = self.tok.text
        self.i += 1
        if self.is_word("exists"):
            self.i += 1
            return _Atom(path, "exists")
        if self.is_op("==", "!="):
            op = self.tok.text
            self.i += 1
            return _Atom(path, op, self.literal())
        return _Atom(path, "")

    def literal(self) -> Any:
        t = self.tok
        if t.kind == "str":
            self.i += 1
            return t.text[1:-1]
        if t.kind == "word" and t.text not in _KEYWORDS:
            self.i += 1
            if t.text in ("true", "false"):
                return t.text == "true"
            for cast in (int, float):
                try:
                    return cast(t.text)
                except ValueError:
                    pass
            return t.text
        raise self.fail("a literal (a quoted string, a number, true, false or a word)")


def parse(text: str) -> Condition:
    """Parse `text`; raise ConditionError with the position and what was expected."""
    parser = _Parser(_tokenize(text))
    root = parser.expr()
    if parser.tok.kind != "end":
        raise parser.fail("'and', 'or' or the end of the condition")
    return Condition(text, root)
