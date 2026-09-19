"""A small deterministic regular-expression engine with capture groups.

Only full-match is supported.  A pattern is compiled into an ordered
Thompson NFA with capture tags, and the NFA is simulated with a
priority-ordered state set: at every input position each NFA state keeps
only the highest-priority path reaching it, and the epsilon closure is
processed in priority order with de-duplication.  No exponential
backtracking is used and the ``re`` module is never touched here.

Supported syntax:

* Unicode literal characters
* escaped metacharacters: ``\\. \\| \\( \\) \\* \\+ \\? \\[ \\] \\{ \\} \\^ \\$ \\\\``
* ``.``   -- any code point, including newline
* concatenation
* ``a|b`` -- alternation, the left branch is preferred
* ``(...)`` -- capturing group, numbered by left-parenthesis order
* greedy ``*``, ``+``, ``?`` -- entering the body is preferred

Empty patterns and empty groups are allowed.  ``[]{}^$`` are reserved and
must be escaped to be literal.  Other escapes, empty alternation
branches, stacked quantifiers, character classes and any other
extensions are rejected.  A ``*`` or ``+`` whose operand can match the
empty string (e.g. ``(a?)*``) is rejected at compile time; ``?`` may be
applied to a nullable body.

Limits: pattern <= 256 chars, group nesting depth <= 32, capture
groups <= 16, input <= 2000 code points.
"""

__all__ = [
    'RegexError', 'Match', 'Pattern', 'compile', 'fullmatch',
    'MAX_PATTERN_LEN', 'MAX_GROUP_DEPTH', 'MAX_GROUPS', 'MAX_INPUT_LEN',
]

MAX_PATTERN_LEN = 256
MAX_GROUP_DEPTH = 32
MAX_GROUPS = 16
MAX_INPUT_LEN = 2000

_ESCAPABLE = frozenset('.|()*+?[]{}^$\\')
_RESERVED = frozenset('[]{}^$')
_QUANTIFIERS = frozenset('*+?')


class RegexError(ValueError):
    """Raised when a pattern is invalid or uses unsupported syntax."""


# ---------------------------------------------------------------------------
# Parser: pattern string -> AST
#
# AST nodes are tuples:
#   ('lit', ch)            literal code point
#   ('dot',)               any code point
#   ('cat', (children))    concatenation, possibly empty
#   ('alt', (branches))    alternation, left branch preferred
#   ('rep', child, quant)  quant in '*', '+', '?'
#   ('grp', index, child)  capturing group, index starts at 1
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, pattern):
        if not isinstance(pattern, str):
            raise TypeError('pattern must be a str')
        if len(pattern) > MAX_PATTERN_LEN:
            raise RegexError(
                f'pattern too long: {len(pattern)} > {MAX_PATTERN_LEN}')
        self._s = pattern
        self._i = 0
        self._depth = 0
        self.ngroups = 0

    def parse(self):
        node = self._alt()
        if self._i != len(self._s):
            raise RegexError(f'unmatched ")" at offset {self._i}')
        return node

    def _peek(self):
        return self._s[self._i] if self._i < len(self._s) else ''

    def _alt(self):
        branches = [self._cat()]
        while self._peek() == '|':
            self._i += 1
            branches.append(self._cat())
        if len(branches) == 1:
            return branches[0]
        for branch in branches:
            if branch == ('cat', ()):
                raise RegexError('empty alternation branch')
        return ('alt', tuple(branches))

    def _cat(self):
        items = []
        while self._i < len(self._s) and self._peek() not in '|)':
            items.append(self._rep())
        return ('cat', tuple(items))

    def _rep(self):
        atom = self._atom()
        ch = self._peek()
        if ch in _QUANTIFIERS:
            self._i += 1
            if self._peek() in _QUANTIFIERS:
                raise RegexError(f'stacked quantifier at offset {self._i}')
            return ('rep', atom, ch)
        return atom

    def _atom(self):
        ch = self._peek()
        if ch == '(':
            self._i += 1
            self._depth += 1
            if self._depth > MAX_GROUP_DEPTH:
                raise RegexError(
                    f'group nesting too deep: > {MAX_GROUP_DEPTH}')
            self.ngroups += 1
            if self.ngroups > MAX_GROUPS:
                raise RegexError(f'too many groups: > {MAX_GROUPS}')
            index = self.ngroups
            body = self._alt()
            if self._peek() != ')':
                raise RegexError('unclosed "("')
            self._i += 1
            self._depth -= 1
            return ('grp', index, body)
        if ch == '.':
            self._i += 1
            return ('dot',)
        if ch == '\\':
            self._i += 1
            esc = self._peek()
            if esc == '':
                raise RegexError('trailing backslash')
            if esc not in _ESCAPABLE:
                raise RegexError(f'unsupported escape "\\{esc}"')
            self._i += 1
            return ('lit', esc)
        if ch in _QUANTIFIERS:
            raise RegexError(
                f'quantifier "{ch}" without operand at offset {self._i}')
        if ch in _RESERVED:
            raise RegexError(f'reserved character "{ch}" must be escaped')
        self._i += 1
        return ('lit', ch)


# ---------------------------------------------------------------------------
# Nullable analysis and repeat validation
# ---------------------------------------------------------------------------

def _nullable(node):
    kind = node[0]
    if kind == 'lit' or kind == 'dot':
        return False
    if kind == 'cat':
        return all(_nullable(c) for c in node[1])
    if kind == 'alt':
        return any(_nullable(b) for b in node[1])
    if kind == 'grp':
        return _nullable(node[2])
    if kind == 'rep':
        return True if node[2] in '*?' else _nullable(node[1])
    raise AssertionError(f'unknown node {kind!r}')


def _check_repeats(node):
    kind = node[0]
    if kind == 'rep':
        child, quant = node[1], node[2]
        if quant in '*+' and _nullable(child):
            raise RegexError(
                f'"{quant}" applied to an operand that can match empty')
        _check_repeats(child)
    elif kind in ('cat', 'alt'):
        for child in node[1]:
            _check_repeats(child)
    elif kind == 'grp':
        _check_repeats(node[2])


# ---------------------------------------------------------------------------
# NFA construction (ordered Thompson construction with capture tags)
# ---------------------------------------------------------------------------

class _State:
    __slots__ = ('kind', 'ch', 'slot', 'out1', 'out2', 'idx')

    def __init__(self, kind, ch=None, slot=None):
        self.kind = kind      # 'char' | 'any' | 'split' | 'jmp' | 'save' | 'match'
        self.ch = ch
        self.slot = slot
        self.out1 = None      # 'split': out1 is the higher-priority branch
        self.out2 = None
        self.idx = -1


def _patch(outs, target):
    for state, field in outs:
        setattr(state, field, target)


class _Builder:
    def __init__(self):
        self.states = []

    def _new(self, kind, ch=None, slot=None):
        state = _State(kind, ch=ch, slot=slot)
        state.idx = len(self.states)
        self.states.append(state)
        return state

    def emit(self, node):
        """Return (start_state, dangling_outs) for the fragment of `node`."""
        kind = node[0]
        if kind == 'lit':
            s = self._new('char', ch=node[1])
            return s, [(s, 'out1')]
        if kind == 'dot':
            s = self._new('any')
            return s, [(s, 'out1')]
        if kind == 'cat':
            first = None
            outs = []
            for child in node[1]:
                cstart, couts = self.emit(child)
                if first is None:
                    first = cstart
                _patch(outs, cstart)
                outs = couts
            if first is None:  # empty concatenation: a no-op epsilon jump
                s = self._new('jmp')
                return s, [(s, 'out1')]
            return first, outs
        if kind == 'alt':
            # right-associative chain of splits: leftmost branch first
            frags = [self.emit(b) for b in node[1]]
            start, outs = frags[-1]
            for bstart, bouts in reversed(frags[:-1]):
                s = self._new('split')
                s.out1 = bstart
                s.out2 = start
                start = s
                outs = bouts + outs
            return start, outs
        if kind == 'grp':
            index = node[1]
            open_s = self._new('save', slot=2 * index)
            cstart, couts = self.emit(node[2])
            open_s.out1 = cstart
            close_s = self._new('save', slot=2 * index + 1)
            _patch(couts, close_s)
            return open_s, [(close_s, 'out1')]
        if kind == 'rep':
            child, quant = node[1], node[2]
            cstart, couts = self.emit(child)
            s = self._new('split')
            s.out1 = cstart  # greedy: entering the body is preferred
            if quant == '?':
                return s, couts + [(s, 'out2')]
            if quant == '*':
                _patch(couts, s)
                return s, [(s, 'out2')]
            # quant == '+'
            _patch(couts, s)
            return cstart, [(s, 'out2')]
        raise AssertionError(f'unknown node {kind!r}')


# ---------------------------------------------------------------------------
# Compiled pattern and priority-ordered NFA simulation
# ---------------------------------------------------------------------------

def _add_state(out, seen, state, tags, pos):
    """Epsilon-closure in priority order; returns states-processed count.

    Depth-first, following out1 before out2, so the first time a state is
    reached is always via the highest-priority path.  `tags` tuples are
    immutable, so each path keeps its own capture history.
    """
    count = 0
    stack = [(state, tags)]
    while stack:
        st, tg = stack.pop()
        if st.idx in seen:
            continue
        seen.add(st.idx)
        count += 1
        kind = st.kind
        if kind == 'split':
            stack.append((st.out2, tg))
            stack.append((st.out1, tg))
        elif kind == 'jmp':
            stack.append((st.out1, tg))
        elif kind == 'save':
            new_tags = list(tg)
            new_tags[st.slot] = pos
            stack.append((st.out1, tuple(new_tags)))
        else:  # 'char', 'any', 'match'
            out.append((st, tg))
    return count


class Match:
    """Result of a successful fullmatch.

    `span(i)` returns the (start, end) code-point span of group i as a
    tuple, or None if group i never participated on the successful path.
    Group 0 is the whole match.  An empty participated span (e.g. (0, 0))
    is distinct from None.
    """

    __slots__ = ('_spans', 'states_processed')

    def __init__(self, spans, states_processed):
        self._spans = tuple(spans)
        self.states_processed = states_processed

    @property
    def spans(self):
        """Tuple of spans for groups 0..n (None for non-participating)."""
        return self._spans

    def span(self, group=0):
        return self._spans[group]

    def groups(self):
        """Spans of capture groups 1..n (None if never participated)."""
        return self._spans[1:]

    def __repr__(self):
        return f'<Match spans={self._spans!r}>'


class Pattern:
    """A compiled regular expression; use fullmatch() to match."""

    __slots__ = ('pattern', 'groups', 'nfa_state_count',
                 '_start', '_nslots', 'last_state_count')

    def __init__(self, pattern, start, nfa_state_count, ngroups):
        self.pattern = pattern
        self.groups = ngroups
        self.nfa_state_count = nfa_state_count
        self._start = start
        self._nslots = 2 * (ngroups + 1)
        # Number of NFA states processed during the most recent fullmatch
        # (closure visits plus successful consume steps).
        self.last_state_count = 0

    def fullmatch(self, text):
        """Match the entire string; return a Match or None."""
        if not isinstance(text, str):
            raise TypeError('text must be a str')
        if len(text) > MAX_INPUT_LEN:
            raise ValueError(
                f'input too long: {len(text)} > {MAX_INPUT_LEN}')
        none_tags = (None,) * self._nslots
        count = 0
        clist = []
        count += _add_state(clist, set(), self._start, none_tags, 0)
        for pos in range(len(text) + 1):
            if pos == len(text):
                # The list is priority-ordered: the first 'match' state is
                # the highest-priority accepting path.
                for st, tags in clist:
                    if st.kind == 'match':
                        spans = [
                            None if tags[2 * g] is None or tags[2 * g + 1] is None
                            else (tags[2 * g], tags[2 * g + 1])
                            for g in range(self.groups + 1)
                        ]
                        self.last_state_count = count
                        return Match(spans, count)
                self.last_state_count = count
                return None
            ch = text[pos]
            nlist = []
            nseen = set()
            for st, tags in clist:
                if st.kind == 'char':
                    if st.ch == ch:
                        count += 1
                        count += _add_state(nlist, nseen, st.out1, tags, pos + 1)
                elif st.kind == 'any':
                    count += 1
                    count += _add_state(nlist, nseen, st.out1, tags, pos + 1)
                # 'match' states before the end of input are dropped:
                # fullmatch must consume the whole string.
            if not nlist:
                self.last_state_count = count
                return None
            clist = nlist
        return None  # unreachable

    def __repr__(self):
        return f'<Pattern {self.pattern!r} groups={self.groups}>'


def compile(pattern):
    """Compile a pattern string into a Pattern; raise RegexError if invalid."""
    parser = _Parser(pattern)
    ast = parser.parse()
    _check_repeats(ast)
    builder = _Builder()
    open_s = builder._new('save', slot=0)
    start, outs = builder.emit(ast)
    open_s.out1 = start
    close_s = builder._new('save', slot=1)
    _patch(outs, close_s)
    match_s = builder._new('match')
    close_s.out1 = match_s
    return Pattern(pattern, open_s, len(builder.states), parser.ngroups)


def fullmatch(pattern, text):
    """Compile `pattern` and full-match `text`; return a Match or None."""
    return compile(pattern).fullmatch(text)
