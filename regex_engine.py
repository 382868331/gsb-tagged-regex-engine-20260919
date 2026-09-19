"""A small regular-expression engine with capture groups (full match only).

Supported syntax:
  - Unicode literal characters
  - escaped metacharacters (\\\\ . * + ? ( ) | [ ] { } ^ $)
  - '.' matching any code point, including newline
  - concatenation, alternation '|', capture groups '(...)'
  - greedy quantifiers '*', '+', '?'

Empty patterns and empty groups '()' are allowed.  The characters
[]{}^$ are reserved and must be escaped to be used as literals.
Empty alternation branches, stacked quantifiers, character classes,
unsupported escapes and any other extensions are rejected.

Matching uses an ordered Thompson NFA with capture tags (a Pike VM):
the epsilon closure is processed in priority order and each state is
kept at most once per input position (highest-priority path wins), so
there is no exponential backtracking.  Alternation prefers the left
branch and quantifiers prefer entering the body (greedy).

The matching engine itself never touches the ``re`` module.
"""

from __future__ import annotations

__all__ = [
    "RegexError",
    "Pattern",
    "Match",
    "compile",
    "fullmatch",
    "MAX_PATTERN_LEN",
    "MAX_DEPTH",
    "MAX_GROUPS",
    "MAX_INPUT_LEN",
]

MAX_PATTERN_LEN = 256
MAX_DEPTH = 32
MAX_GROUPS = 16
MAX_INPUT_LEN = 2000


class RegexError(Exception):
    """Raised for invalid patterns, unsupported syntax or oversized input."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class _Node:
    __slots__ = ()


class _Literal(_Node):
    __slots__ = ("ch",)

    def __init__(self, ch: str):
        self.ch = ch


class _Dot(_Node):
    __slots__ = ()


class _Concat(_Node):
    __slots__ = ("children",)

    def __init__(self, children: list):
        self.children = children


class _Alt(_Node):
    __slots__ = ("branches",)

    def __init__(self, branches: list):
        self.branches = branches


class _Group(_Node):
    __slots__ = ("index", "child")

    def __init__(self, index: int, child: _Node):
        self.index = index
        self.child = child


class _Repeat(_Node):
    __slots__ = ("child", "kind")

    def __init__(self, child: _Node, kind: str):
        self.child = child
        self.kind = kind  # one of '*', '+', '?'


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_ESCAPABLE = frozenset(".*+?()|[]{}^$\\")
_RESERVED = frozenset("[]{}^$")
_QUANTIFIERS = frozenset("*+?")


class _Parser:
    def __init__(self, pattern: str):
        self.pattern = pattern
        self.pos = 0
        self.depth = 0
        self.n_groups = 0

    def _error(self, msg: str) -> RegexError:
        return RegexError(f"{msg} (at position {self.pos} in {self.pattern!r})")

    def _peek(self) -> str:
        if self.pos < len(self.pattern):
            return self.pattern[self.pos]
        return ""

    def parse(self):
        node = self._parse_alt()
        if self.pos != len(self.pattern):
            raise self._error(f"unexpected {self.pattern[self.pos]!r}")
        return node, self.n_groups

    def _parse_alt(self) -> _Node:
        first = self._parse_concat()
        if self._peek() != "|":
            # A lone empty concatenation (empty pattern or '()') is fine.
            return first
        branches = [first]
        if _is_empty(first):
            raise self._error("empty alternation branch")
        while self._peek() == "|":
            self.pos += 1
            branch = self._parse_concat()
            if _is_empty(branch):
                raise self._error("empty alternation branch")
            branches.append(branch)
        return _Alt(branches)

    def _parse_concat(self) -> _Node:
        items = []
        while self.pos < len(self.pattern) and self.pattern[self.pos] not in "|)":
            items.append(self._parse_repeat())
        return _Concat(items)

    def _parse_repeat(self) -> _Node:
        atom = self._parse_atom()
        ch = self._peek()
        if ch in _QUANTIFIERS:
            self.pos += 1
            node = _Repeat(atom, ch)
            if self._peek() in _QUANTIFIERS:
                raise self._error("stacked quantifiers are not supported")
            return node
        return atom

    def _parse_atom(self) -> _Node:
        ch = self._peek()
        if ch == "":
            raise self._error("unexpected end of pattern")
        if ch in _QUANTIFIERS:
            raise self._error(f"quantifier {ch!r} has no operand")
        if ch == ".":
            self.pos += 1
            return _Dot()
        if ch == "(":
            self.pos += 1
            self.n_groups += 1
            if self.n_groups > MAX_GROUPS:
                raise self._error(f"more than {MAX_GROUPS} capture groups")
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise self._error(f"parentheses nested deeper than {MAX_DEPTH}")
            index = self.n_groups
            child = self._parse_alt()
            if self._peek() != ")":
                raise self._error("unclosed group")
            self.pos += 1
            self.depth -= 1
            return _Group(index, child)
        if ch == "\\":
            self.pos += 1
            esc = self._peek()
            if esc == "":
                raise self._error("trailing backslash")
            if esc not in _ESCAPABLE:
                raise self._error(f"unsupported escape '\\{esc}'")
            self.pos += 1
            return _Literal(esc)
        if ch in _RESERVED:
            raise self._error(f"reserved character {ch!r} must be escaped")
        self.pos += 1
        return _Literal(ch)


def _is_empty(node: _Node) -> bool:
    return isinstance(node, _Concat) and not node.children


def _nullable(node: _Node) -> bool:
    """Compute nullability, rejecting '*'/'+' over a nullable operand."""
    if isinstance(node, (_Literal, _Dot)):
        return False
    if isinstance(node, _Concat):
        return all(_nullable(c) for c in node.children)
    if isinstance(node, _Alt):
        return any(_nullable(b) for b in node.branches)
    if isinstance(node, _Group):
        return _nullable(node.child)
    if isinstance(node, _Repeat):
        child_nullable = _nullable(node.child)
        if node.kind in "*+" and child_nullable:
            raise RegexError(
                f"quantifier {node.kind!r} applied to an operand that can "
                "match the empty string"
            )
        if node.kind == "+":
            return child_nullable  # always False here, kept for clarity
        return True  # '*' and '?' are nullable
    raise AssertionError(f"unknown node {node!r}")


# ---------------------------------------------------------------------------
# NFA
# ---------------------------------------------------------------------------

_CHAR = 0   # consume one code point; ch is None for '.'
_SPLIT = 1  # ordered epsilon transitions (outs in priority order)
_SAVE = 2   # record current position into capture slot `slot`
_MATCH = 3  # accept


class _State:
    __slots__ = ("kind", "ch", "slot", "outs")

    def __init__(self, kind: int, ch=None, slot=None):
        self.kind = kind
        self.ch = ch
        self.slot = slot
        self.outs: list = []


class _Compiler:
    """Thompson construction with ordered splits and capture tags."""

    def __init__(self):
        self.states: list[_State] = []

    def _emit(self, kind: int, ch=None, slot=None) -> _State:
        st = _State(kind, ch, slot)
        self.states.append(st)
        return st

    @staticmethod
    def _patch(dangling, target: _State) -> None:
        for st, idx in dangling:
            st.outs[idx] = target

    def build(self, node: _Node):
        """Return (start_state, dangling_out_pointers)."""
        if isinstance(node, _Literal):
            st = self._emit(_CHAR, ch=node.ch)
            st.outs = [None]
            return st, [(st, 0)]
        if isinstance(node, _Dot):
            st = self._emit(_CHAR, ch=None)
            st.outs = [None]
            return st, [(st, 0)]
        if isinstance(node, _Concat):
            if not node.children:
                # Empty fragment: a plain epsilon jump.
                st = self._emit(_SPLIT)
                st.outs = [None]
                return st, [(st, 0)]
            start, dangling = self.build(node.children[0])
            for child in node.children[1:]:
                nxt, nxt_dangling = self.build(child)
                self._patch(dangling, nxt)
                dangling = nxt_dangling
            return start, dangling
        if isinstance(node, _Alt):
            split = self._emit(_SPLIT)
            split.outs = [None] * len(node.branches)
            dangling = []
            for i, branch in enumerate(node.branches):
                start, sub = self.build(branch)
                split.outs[i] = start  # left branch first => higher priority
                dangling.extend(sub)
            return split, dangling
        if isinstance(node, _Group):
            open_st = self._emit(_SAVE, slot=2 * node.index)
            open_st.outs = [None]
            start, dangling = self.build(node.child)
            close_st = self._emit(_SAVE, slot=2 * node.index + 1)
            close_st.outs = [None]
            open_st.outs[0] = start
            self._patch(dangling, close_st)
            return open_st, [(close_st, 0)]
        if isinstance(node, _Repeat):
            if node.kind == "*":
                split = self._emit(_SPLIT)
                split.outs = [None, None]
                start, dangling = self.build(node.child)
                split.outs[0] = start  # prefer entering the body (greedy)
                self._patch(dangling, split)
                return split, [(split, 1)]
            if node.kind == "+":
                start, dangling = self.build(node.child)
                split = self._emit(_SPLIT)
                split.outs = [start, None]  # prefer looping (greedy)
                self._patch(dangling, split)
                return start, [(split, 1)]
            # '?'
            split = self._emit(_SPLIT)
            split.outs = [None, None]
            start, dangling = self.build(node.child)
            split.outs[0] = start  # prefer taking the optional part
            return split, dangling + [(split, 1)]
        raise AssertionError(f"unknown node {node!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Match:
    """Result of a successful full match.

    Captures are code-point spans.  A group that never participated on
    the winning path is ``None``; an empty capture is a span ``(i, i)``.
    """

    __slots__ = ("_spans", "_text", "state_count")

    def __init__(self, spans: tuple, text: str, state_count: int):
        self._spans = spans
        self._text = text
        self.state_count = state_count

    @property
    def spans(self) -> tuple:
        """Tuple of spans; index 0 is the whole match, then groups 1..n."""
        return self._spans

    def span(self, group: int = 0):
        """(start, end) code-point span of `group`, or None if it did not
        participate.  Group 0 is the whole match."""
        return self._spans[group]

    def group(self, group: int = 0):
        """Matched substring for `group`, or None if it did not participate."""
        sp = self._spans[group]
        if sp is None:
            return None
        return self._text[sp[0]:sp[1]]

    def __repr__(self):
        return f"Match(spans={self._spans!r})"


class Pattern:
    """A compiled pattern.  Use :func:`compile` to create one."""

    def __init__(self, pattern: str, n_groups: int, start: _State, n_states: int):
        self.pattern = pattern
        self.groups = n_groups
        self.n_states = n_states
        self._start = start
        self._last_state_count = 0

    @property
    def state_count(self) -> int:
        """Number of NFA states processed by the most recent fullmatch call.

        This counts every state added to the ordered epsilon closures
        across all input positions, and grows linearly with input length.
        """
        return self._last_state_count

    def fullmatch(self, text: str) -> Match | None:
        """Match the entire `text`; return a Match or None."""
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        if len(text) > MAX_INPUT_LEN:
            raise RegexError(f"input longer than {MAX_INPUT_LEN} code points")

        n_caps = 2 * (self.groups + 1)
        count = 0  # states processed across all epsilon closures

        def add_thread(state: _State, caps: list, pos: int,
                       lst: list, seen: set) -> None:
            # Iterative epsilon closure in priority order; each state is
            # kept at most once per input position (first = highest priority).
            nonlocal count
            stack = [(state, caps)]
            while stack:
                st, cp = stack.pop()
                if id(st) in seen:
                    continue
                seen.add(id(st))
                count += 1
                if st.kind in (_CHAR, _MATCH):
                    lst.append((st, cp))
                elif st.kind == _SAVE:
                    new_caps = list(cp)
                    new_caps[st.slot] = pos
                    stack.append((st.outs[0], new_caps))
                else:  # _SPLIT: push in reverse so outs[0] is processed first
                    for nxt in reversed(st.outs):
                        stack.append((nxt, cp))

        seen: set = set()
        clist: list = []
        add_thread(self._start, [None] * n_caps, 0, clist, seen)

        for i, ch in enumerate(text):
            nlist: list = []
            seen = set()
            for st, caps in clist:
                if st.kind == _CHAR and (st.ch is None or st.ch == ch):
                    add_thread(st.outs[0], caps, i + 1, nlist, seen)
            clist = nlist
            if not clist:
                break

        self._last_state_count = count
        for st, caps in clist:
            if st.kind == _MATCH:
                spans = tuple(
                    None if caps[2 * g] is None or caps[2 * g + 1] is None
                    else (caps[2 * g], caps[2 * g + 1])
                    for g in range(self.groups + 1)
                )
                return Match(spans, text, count)
        return None


def compile(pattern: str) -> Pattern:
    """Compile `pattern` into a :class:`Pattern`.

    Raises RegexError for unsupported syntax, nullable operands under
    '*'/'+', or exceeded limits.
    """
    if not isinstance(pattern, str):
        raise TypeError("pattern must be a str")
    if len(pattern) > MAX_PATTERN_LEN:
        raise RegexError(f"pattern longer than {MAX_PATTERN_LEN} characters")
    ast, n_groups = _Parser(pattern).parse()
    _nullable(ast)  # validates that '*'/'+' never wrap a nullable operand
    compiler = _Compiler()
    save0 = compiler._emit(_SAVE, slot=0)
    save0.outs = [None]
    start, dangling = compiler.build(ast)
    save1 = compiler._emit(_SAVE, slot=1)
    save1.outs = [None]
    match_st = compiler._emit(_MATCH)
    save0.outs[0] = start
    compiler._patch(dangling, save1)
    save1.outs[0] = match_st
    return Pattern(pattern, n_groups, save0, len(compiler.states))


def fullmatch(pattern: str, text: str) -> Match | None:
    """Compile `pattern` and full-match it against `text`."""
    return compile(pattern).fullmatch(text)
