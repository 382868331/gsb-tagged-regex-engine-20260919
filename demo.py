"""Demo for regex_engine: one normal result and one real failure.

Runs in well under 8 seconds.  Everything printed is computed at run
time by the engine (no re module involved in matching).
"""

import sys
import time

from regex_engine import RegexError, compile as rx_compile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass


def show(label, prog, text):
    m = prog.fullmatch(text)
    print(f'  {label}: pattern={prog.pattern!r} text={text!r}')
    if m is None:
        print(f'    -> no match (states processed: {prog.last_state_count})')
    else:
        print(f'    -> match, spans={m.spans} '
              f'(states processed: {m.states_processed})')
    return m


def main():
    t0 = time.perf_counter()

    print('1. Normal fullmatch results (deterministic priority + captures)')
    show('overlapping branches', rx_compile('(a|ab)(c|bcd)'), 'abcd')
    show('left branch preferred ', rx_compile('(a|aa)*'), 'aaaa')
    show('capture kept in loop ', rx_compile('(a(b)?)+'), 'aba')
    show('empty vs absent group', rx_compile('(a)|(b)'), 'b')
    show('dot matches newline   ', rx_compile('a.b'), 'a\nb')

    print()
    print('2. Nullable repeat is rejected at compile time')
    for pattern in ('(a?)*', '(a*)+'):
        try:
            rx_compile(pattern)
        except RegexError as exc:
            print(f'  compile({pattern!r}) -> RegexError: {exc}')
        else:
            print(f'  compile({pattern!r}) -> UNEXPECTEDLY ACCEPTED')

    print()
    print('3. A real match failure, and linear state-count growth')
    prog = rx_compile('(a|aa)+b')
    print(f'  pattern={prog.pattern!r} (NFA states: {prog.nfa_state_count})')
    counts = {}
    for n in (250, 500, 1000, 2000):
        result = prog.fullmatch('a' * n)
        counts[n] = prog.last_state_count
        print(f"  fullmatch('a' * {n}) -> {result} "
              f'(states processed: {counts[n]})')
    ratio = counts[2000] / counts[1000]
    print(f'  count(2000)/count(1000) = {ratio:.2f} '
          f'(linear growth, no exponential backtracking)')

    print()
    print(f'demo finished in {time.perf_counter() - t0:.2f}s')


if __name__ == '__main__':
    main()
