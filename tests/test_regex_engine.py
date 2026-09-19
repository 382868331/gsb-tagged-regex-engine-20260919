"""Tests for regex_engine.  Cross-checks use re.fullmatch(..., re.DOTALL)
on the commonly supported subset; the engine itself never imports re."""

import random
import re
import unittest

import regex_engine as rx


def spans(pattern, text):
    """Compile+fullmatch, returning the span tuple or None."""
    m = rx.compile(pattern).fullmatch(text)
    return None if m is None else m.spans


def re_spans(pattern, text):
    rm = re.fullmatch(pattern, text, re.DOTALL)
    if rm is None:
        return None
    return tuple(
        None if rm.span(g) == (-1, -1) else rm.span(g)
        for g in range(re.compile(pattern).groups + 1)
    )


class PriorityTests(unittest.TestCase):
    def test_left_branch_preferred(self):
        # Both branches could match "a"; the left one wins.
        self.assertEqual(spans("(a|a)", "a"), ((0, 1), (0, 1)))
        # Left branch 'ab' is tried first and succeeds.
        self.assertEqual(spans("(ab|a)(b?)", "ab"), ((0, 2), (0, 2), (2, 2)))
        # Left branch fails to consume everything; engine falls back in
        # priority order without exponential backtracking.
        self.assertEqual(spans("(a|ab)(c|bcd)", "abcd"),
                         ((0, 4), (0, 1), (1, 4)))

    def test_star_over_alternation(self):
        # Greedy: each iteration prefers the single 'a' (left branch).
        self.assertEqual(spans("(a|aa)*", "aaaa"), ((0, 4), (3, 4)))
        self.assertEqual(spans("(a|aa)*", "aaa"), ((0, 3), (2, 3)))
        # With 'aa' on the left, pairs are preferred.
        self.assertEqual(spans("(aa|a)*", "aaa"), ((0, 3), (2, 3)))

    def test_plus_keeps_last_completed_capture(self):
        # Second iteration matches bare 'a'; group 2 keeps its earlier
        # completed capture 'b' at (1, 2).
        self.assertEqual(spans("(a(b)?)+", "aba"),
                         ((0, 3), (2, 3), (1, 2)))
        self.assertEqual(spans("(a(b)?)+", "abab"),
                         ((0, 4), (2, 4), (3, 4)))

    def test_quantifier_prefers_entering(self):
        self.assertEqual(spans("(a*)(a*)", "aa"), ((0, 2), (0, 2), (2, 2)))
        self.assertEqual(spans("(a+)?(a+)?", "aa"), ((0, 2), (0, 2), None))


class CaptureTests(unittest.TestCase):
    def test_empty_group_vs_unparticipated(self):
        # Empty capture is a real span (i, i), distinct from None.
        self.assertEqual(spans("()", ""), ((0, 0), (0, 0)))
        self.assertEqual(spans("(a())", "a"), ((0, 1), (0, 1), (1, 1)))
        self.assertEqual(spans("(a)|(b)", "a"), ((0, 1), (0, 1), None))
        self.assertEqual(spans("(a)|(b)", "b"), ((0, 1), None, (0, 1)))
        m = rx.compile("(a)|(b)").fullmatch("a")
        self.assertEqual(m.group(2), None)
        self.assertEqual(m.span(2), None)
        m2 = rx.compile("(a())").fullmatch("a")
        self.assertEqual(m2.group(2), "")
        self.assertEqual(m2.span(2), (1, 1))

    def test_group_zero_is_whole_match(self):
        m = rx.compile("a(b)c").fullmatch("abc")
        self.assertEqual(m.span(0), (0, 3))
        self.assertEqual(m.group(0), "abc")
        self.assertEqual(m.group(1), "b")

    def test_group_numbering_by_left_paren(self):
        m = rx.compile("(a(b(c)))").fullmatch("abc")
        self.assertEqual(m.spans, ((0, 3), (0, 3), (1, 3), (2, 3)))

    def test_dot_matches_newline(self):
        self.assertEqual(spans(".", "\n"), ((0, 1),))
        self.assertEqual(spans("a.c", "a\nc"), ((0, 3),))
        self.assertIsNone(spans("a.c", "ac"))  # dot must consume a code point

    def test_unicode_literals(self):
        self.assertEqual(spans("(héllo)", "héllo"), ((0, 5), (0, 5)))
        self.assertEqual(spans("...", "中\n🙂"), ((0, 3),))


class CompileErrorTests(unittest.TestCase):
    def assertRejected(self, pattern):
        with self.assertRaises(rx.RegexError, msg=pattern):
            rx.compile(pattern)

    def test_nullable_repeat_rejected(self):
        for pat in ("(a?)*", "(a*)+", "(a*)*", "()*", "()+", "((a?))*",
                    "(a|)*b*", "(a*b?)*"):
            self.assertRejected(pat)

    def test_question_mark_on_nullable_allowed(self):
        rx.compile("(a?)?")
        rx.compile("(a*)?")
        rx.compile("()?")

    def test_stacked_quantifiers_rejected(self):
        for pat in ("a**", "a*+", "a?*", "a+*", "(a)?*"):
            self.assertRejected(pat)

    def test_empty_alternation_branch_rejected(self):
        for pat in ("a|", "|a", "a||b", "(a|)", "(|a)", "(a|b|)"):
            self.assertRejected(pat)

    def test_empty_pattern_and_group_allowed(self):
        self.assertEqual(spans("", ""), ((0, 0),))
        self.assertIsNone(spans("", "a"))
        self.assertEqual(spans("(())", ""), ((0, 0), (0, 0), (0, 0)))

    def test_reserved_characters_must_be_escaped(self):
        for pat in ("[", "]", "{", "}", "^", "$", "[abc]", "a{2}"):
            self.assertRejected(pat)
        # Escaped, they are plain literals.
        pat = r"\[\]\{\}\^\$"
        self.assertEqual(spans(pat, "[]{}^$"), ((0, 6),))

    def test_escapes(self):
        self.assertEqual(spans(r"\.\*\+\?\(\)\|\\", ".*+?()|\\"), ((0, 8),))
        for pat in (r"\d", r"\w", r"\s", r"\b", r"\A", "\\"):
            self.assertRejected(pat)

    def test_structural_errors(self):
        for pat in ("(", ")", "(a", "a)", "*a", "+", "?", "(*)"):
            self.assertRejected(pat)

    def test_limits(self):
        self.assertRejected("a" * (rx.MAX_PATTERN_LEN + 1))
        # Nesting past MAX_DEPTH also exceeds MAX_GROUPS (each '(' is a
        # group); either way it must be rejected.
        self.assertRejected("(" * (rx.MAX_DEPTH + 1) + "a" + ")" * (rx.MAX_DEPTH + 1))
        self.assertRejected("()" * (rx.MAX_GROUPS + 1))
        with self.assertRaises(rx.RegexError):
            rx.compile("a").fullmatch("a" * (rx.MAX_INPUT_LEN + 1))
        # Exactly at the limits is fine.
        rx.compile("a" * rx.MAX_PATTERN_LEN)
        rx.compile("(" * rx.MAX_GROUPS + "a" + ")" * rx.MAX_GROUPS)
        rx.compile("a").fullmatch("a" * rx.MAX_INPUT_LEN)


class StateCountTests(unittest.TestCase):
    def test_state_count_grows_linearly_on_failing_input(self):
        # Classic exponential-backtracking trap; here it must stay linear.
        p = rx.compile("(a|aa)+b")
        counts = []
        for n in (100, 200, 400, 800):
            self.assertIsNone(p.fullmatch("a" * n))
            counts.append(p.state_count)
        for c in counts:
            self.assertGreater(c, 0)
        # Doubling the input roughly doubles the processed-state count.
        for small, big in zip(counts, counts[1:]):
            ratio = big / small
            self.assertGreater(ratio, 1.7)
            self.assertLess(ratio, 2.3)

    def test_state_count_reported_on_success(self):
        p = rx.compile("(a|b)+")
        m = p.fullmatch("abba")
        self.assertIsNotNone(m)
        self.assertEqual(m.state_count, p.state_count)
        self.assertGreater(m.state_count, 0)

    def test_match_consumes_entire_input(self):
        self.assertIsNone(spans("a+", "aab"))
        self.assertIsNone(spans("(a|aa)+b", "aaaa"))


class CrossCheckTests(unittest.TestCase):
    """Seeded random cross-validation against re.fullmatch(..., re.DOTALL)
    on the commonly supported subset."""

    ATOMS = ["a", "b", ".", r"\.", r"\*", "(a)", "(b)", "(a|b)", "(a|bb)",
             "(ab|a)", "(a(b)?)", "((a)|b)"]
    NULLABLE_ATOMS = ["(a?)", "(b?)", "()"]

    def gen_pattern(self, rng):
        items = []
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.25:
                atom = rng.choice(self.NULLABLE_ATOMS)
                quants = ["", "?", "?", ""]  # '*'/'+' would be rejected
            else:
                atom = rng.choice(self.ATOMS)
                quants = ["", "", "*", "+", "?"]
            items.append(atom + rng.choice(quants))
        pat = "".join(items)
        if rng.random() < 0.3:
            other = "".join(
                rng.choice(self.ATOMS) for _ in range(rng.randint(1, 3)))
            pat = pat + "|" + other
        return pat

    def test_random_crosscheck(self):
        rng = random.Random(20260920)
        checked = matched = 0
        for _ in range(300):
            pat = self.gen_pattern(rng)
            # Bias half the samples toward a tiny alphabet so that a
            # reasonable share of them actually match.
            alphabet = "ab" if rng.random() < 0.6 else "ab\n.c*"
            text = "".join(rng.choice(alphabet)
                           for _ in range(rng.randint(0, 8)))
            ours = spans(pat, text)
            ref = re_spans(pat, text)
            self.assertEqual(ours, ref, f"{pat!r} on {text!r}")
            checked += 1
            matched += ours is not None
        # Sanity: the sample must actually exercise both outcomes.
        self.assertGreater(matched, 50)
        self.assertGreater(checked - matched, 50)

    def test_engine_does_not_use_re(self):
        import inspect
        import regex_engine
        src = inspect.getsource(regex_engine)
        self.assertNotIn("import re", src)


if __name__ == "__main__":
    unittest.main()
