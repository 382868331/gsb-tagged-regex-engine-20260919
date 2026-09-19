"""Demo for regex_engine: a successful match with captures, a real
failure, nullable-repeat rejection, and linear state-count growth.

Run:  python demo.py
"""

import regex_engine as rx


def show(pattern, text):
    pat = rx.compile(pattern)
    m = pat.fullmatch(text)
    print(f"  pattern={pattern!r} text={text!r}")
    if m is None:
        print(f"    -> NO MATCH (states processed: {pat.state_count})")
    else:
        print(f"    -> match, spans={m.spans} (states processed: {m.state_count})")
        for g in range(1, pat.groups + 1):
            print(f"       group {g}: span={m.span(g)} text={m.group(g)!r}")
    return m


def main():
    print("1) Normal match with captures (overlapping alternation branches)")
    print("   '(a|ab)(c|bcd)(d*)' prefers the left branch but backs off in")
    print("   priority order until the whole input is consumed:")
    show("(a|ab)(c|bcd)(d*)", "abcd")
    print()

    print("2) Greedy star over alternation: '(a|aa)*' on 'aaaa'")
    print("   each iteration prefers the left branch 'a':")
    show("(a|aa)*", "aaaa")
    print()

    print("3) '(a(b)?)+' on 'aba': the last iteration captures bare 'a',")
    print("   group 2 keeps its earlier completed capture 'b':")
    show("(a(b)?)+", "aba")
    print()

    print("4) A real failure: '(a|aa)+b' cannot match 'aaaa'")
    print("   (no 'b' at the end; fullmatch must consume everything):")
    show("(a|aa)+b", "aaaa")
    print()

    print("5) Nullable repeat is rejected at compile time:")
    for bad in ("(a?)*", "(a*)+"):
        try:
            rx.compile(bad)
            print(f"   {bad!r}: NOT rejected (unexpected!)")
        except rx.RegexError as e:
            print(f"   {bad!r}: RegexError: {e}")
    print()

    print("6) Failing strings of growing length: processed-state count")
    print("   grows linearly (no exponential backtracking):")
    pat = rx.compile("(a|aa)+b")
    prev = None
    for n in (100, 200, 400, 800, 1600):
        assert pat.fullmatch("a" * n) is None
        ratio = "" if prev is None else f"  (x{pat.state_count / prev:.2f})"
        print(f"   len={n:5d}  states={pat.state_count:6d}{ratio}")
        prev = pat.state_count


if __name__ == "__main__":
    main()
