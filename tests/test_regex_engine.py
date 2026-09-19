"""Tests for regex_engine.  Cross-checks use re.fullmatch(..., re.DOTALL)
on the commonly supported subset; the engine itself never uses re."""

import random
import re
import unittest

from regex_engine import (
    MAX_GROUP_DEPTH, MAX_GROUPS, MAX_INPUT_LEN, MAX_PATTERN_LEN,
    RegexError, compile as rx_compile, fullmatch as rx_fullmatch,
)


def re_spans(pattern, text, ngroups):
    """re.fullmatch spans as a tuple, (-1, -1) mapped to None; None if no match."""
    m = re.fullmatch(pattern, text, re.DOTALL)
    if m is None:
        return None
    out = []
    for g in range(ngroups + 1):
        span = m.span(g)
        out.append(None if span == (-1, -1) else span)
    return tuple(out)


class PriorityTests(unittest.TestCase):
    def test_left_branch_preferred(self):
        # Left branch 'a' is tried first; with two iterations the last
        # completed capture of group 1 is the second 'a'.
        m = rx_fullmatch('(a|aa)*', 'aa')
        self.assertIsNotNone(m)
        self.assertEqual(m.span(1), (1, 2))
        self.assertEqual(m.spans, re_spans('(a|aa)*', 'aa', 1))

    def test_left_branch_priority_on_fallback(self):
        # 'ab' branch is preferred but cannot lead to a full match of
        # 'ab' followed by 'b'; the surviving path uses the 'a' branch.
        m = rx_fullmatch('(ab|a)b', 'ab')
        self.assertEqual(m.span(1), (0, 1))
        self.assertEqual(m.spans, re_spans('(ab|a)b', 'ab', 1))

    def test_surviving_branch_capture(self):
        # Higher-priority path dies, lower-priority branch wins the match.
        m = rx_fullmatch('(a|ab)(c|bcd)', 'abcd')
        self.assertEqual(m.spans, ((0, 4), (0, 1), (1, 4)))
        self.assertEqual(m.spans, re_spans('(a|ab)(c|bcd)', 'abcd', 2))

    def test_a_aa_star(self):
        for text in ('', 'a', 'aa', 'aaa', 'aaaa', 'aaaaa'):
            m = rx_fullmatch('(a|aa)*', text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.spans, re_spans('(a|aa)*', text, 1), text)
        self.assertEqual(rx_fullmatch('(a|aa)*', 'aaaa').span(1), (3, 4))

    def test_quantifier_prefers_entering_body(self):
        # Greedy ? enters the body when possible.
        m = rx_fullmatch('(a?)?', 'a')
        self.assertEqual(m.span(1), (0, 1))
        m = rx_fullmatch('(a?)?', '')
        self.assertEqual(m.span(1), (0, 0))
        self.assertEqual(m.spans, re_spans('(a?)?', '', 1))


class CaptureHistoryTests(unittest.TestCase):
    def test_plus_keeps_completed_capture(self):
        # (a(b)?)+ on 'aba': group 2 completed in iteration 1 and is kept
        # even though the last iteration did not enter it.
        m = rx_fullmatch('(a(b)?)+', 'aba')
        self.assertEqual(m.span(1), (2, 3))
        self.assertEqual(m.span(2), (1, 2))
        self.assertEqual(m.spans, re_spans('(a(b)?)+', 'aba', 2))

    def test_group_never_entered_is_none(self):
        m = rx_fullmatch('(a(b)?)+', 'a')
        self.assertEqual(m.span(1), (0, 1))
        self.assertIsNone(m.span(2))

    def test_empty_group_span_is_not_none(self):
        m = rx_fullmatch('()', '')
        self.assertEqual(m.span(0), (0, 0))
        self.assertEqual(m.span(1), (0, 0))  # empty span, distinct from None
        m = rx_fullmatch('a()b', 'ab')
        self.assertEqual(m.span(1), (1, 1))

    def test_unparticipated_alternation_group(self):
        m = rx_fullmatch('(a)|(b)', 'a')
        self.assertEqual(m.span(1), (0, 1))
        self.assertIsNone(m.span(2))
        m = rx_fullmatch('(a)|(b)', 'b')
        self.assertIsNone(m.span(1))
        self.assertEqual(m.span(2), (0, 1))
        self.assertEqual(m.spans, re_spans('(a)|(b)', 'b', 2))

    def test_inner_group_survives_later_iterations(self):
        m = rx_fullmatch('((a)|b)+', 'ab')
        self.assertEqual(m.span(1), (1, 2))
        self.assertEqual(m.span(2), (0, 1))
        self.assertEqual(m.spans, re_spans('((a)|b)+', 'ab', 2))


class SyntaxTests(unittest.TestCase):
    def test_dot_matches_newline_and_astral(self):
        self.assertIsNotNone(rx_fullmatch('a.b', 'a\nb'))
        self.assertIsNotNone(rx_fullmatch('.', '\n'))
        self.assertIsNotNone(rx_fullmatch('.', '🙂'))  # one code point
        self.assertIsNone(rx_fullmatch('..', 'a'))

    def test_empty_pattern(self):
        m = rx_fullmatch('', '')
        self.assertEqual(m.span(0), (0, 0))
        self.assertIsNone(rx_fullmatch('', 'a'))

    def test_escaped_metacharacters_are_literal(self):
        self.assertIsNotNone(rx_fullmatch('\\^\\$', '^$'))
        self.assertIsNotNone(rx_fullmatch('\\*\\+\\?', '*+?'))
        self.assertIsNotNone(rx_fullmatch('\\[\\]\\{\\}', '[]{}'))
        self.assertIsNotNone(rx_fullmatch('\\.\\|\\\\', '.|\\'))
        self.assertIsNotNone(rx_fullmatch('\\(\\)', '()'))

    def test_literal_newline_in_pattern(self):
        self.assertIsNotNone(rx_fullmatch('a\nb', 'a\nb'))

    def test_fullmatch_must_consume_everything(self):
        self.assertIsNone(rx_fullmatch('ab', 'abc'))
        self.assertIsNone(rx_fullmatch('ab', 'xab'))
        self.assertIsNotNone(rx_fullmatch('ab', 'ab'))


class RejectionTests(unittest.TestCase):
    def test_nullable_repeat_rejected(self):
        for pattern in ('(a?)*', '(a*)+', '(a?)+', '(a*)*', '()*', '()+',
                        '(a|b?)*', '((a)?)*', '(a*)+(b)?'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_question_mark_on_nullable_body_allowed(self):
        self.assertIsNotNone(rx_fullmatch('(a?)?', ''))
        self.assertIsNotNone(rx_fullmatch('(a*)?', 'aaa'))
        self.assertIsNotNone(rx_fullmatch('()?', ''))

    def test_stacked_quantifiers_rejected(self):
        for pattern in ('a**', 'a*+', 'a+*', 'a??', 'a?*', '(a)*?'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_empty_alternation_branch_rejected(self):
        for pattern in ('a|', '|a', 'a||b', '(a|)', '(|a)', '(a|b|)'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_reserved_characters_must_be_escaped(self):
        for pattern in ('[', ']', '{', '}', '^', '$', '[abc]', 'a{2}',
                        '^a', 'a$'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_unsupported_escapes_rejected(self):
        for pattern in ('\\d', '\\w', '\\s', '\\n', '\\t', '\\b', '\\A',
                        '\\1', 'a\\'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_structural_errors_rejected(self):
        for pattern in ('(', ')', '(a', 'a)', '*a', '+', '?', '(*)'):
            with self.assertRaises(RegexError, msg=pattern):
                rx_compile(pattern)

    def test_limits(self):
        with self.assertRaises(RegexError):
            rx_compile('a' * (MAX_PATTERN_LEN + 1))
        with self.assertRaises(RegexError):
            rx_compile('(' * (MAX_GROUP_DEPTH + 1) + ')' * (MAX_GROUP_DEPTH + 1))
        with self.assertRaises(RegexError):
            rx_compile('()' * (MAX_GROUPS + 1))
        p = rx_compile('a*')
        with self.assertRaises(ValueError):
            p.fullmatch('a' * (MAX_INPUT_LEN + 1))


class StateCountTests(unittest.TestCase):
    def test_state_count_reported(self):
        p = rx_compile('(a|aa)+b')
        m = p.fullmatch('aab')
        self.assertIsNotNone(m)
        self.assertGreater(m.states_processed, 0)
        self.assertEqual(m.states_processed, p.last_state_count)
        self.assertIsNone(p.fullmatch('aabx'))
        self.assertGreater(p.last_state_count, 0)

    def test_long_failing_string_state_count_is_linear(self):
        # Fixed pattern, growing failing input: processed-state count must
        # grow linearly with input length, not exponentially.
        p = rx_compile('(a|aa)+b')
        counts = {}
        for n in (250, 500, 1000, 2000):
            self.assertIsNone(p.fullmatch('a' * n))
            counts[n] = p.last_state_count
        self.assertLess(counts[250], counts[500])
        self.assertLess(counts[500], counts[1000])
        self.assertLess(counts[1000], counts[2000])
        # Doubling the input roughly doubles the work (linear), and the
        # per-character cost stays bounded by the NFA size.
        self.assertLess(counts[2000] / counts[1000], 2.5)
        self.assertLess(counts[2000], 4 * p.nfa_state_count * 2001)


class RandomCrossCheckTests(unittest.TestCase):
    """Fixed-seed random patterns from the supported subset, cross-checked
    against re.fullmatch(pattern, text, re.DOTALL)."""

    LITERALS = ['a', 'b', 'c', '\n', '.', '*', '(', '$']

    def _emit_literal(self, ch):
        return '\\' + ch if ch in '.|()*+?[]{}^$\\' else ch

    def _atom(self, rng, depth, budget):
        if depth < 3 and budget[0] > 0 and rng.random() < 0.35:
            budget[0] -= 1
            body, nullable = self._alt(rng, depth + 1, budget)
            return '(' + body + ')', nullable
        if rng.random() < 0.2:
            return '.', False
        return self._emit_literal(rng.choice(self.LITERALS)), False

    def _rep(self, rng, depth, budget):
        s, nullable = self._atom(rng, depth, budget)
        if rng.random() < 0.5:
            return s, nullable
        quant = rng.choice('*+?')
        if quant in '*+' and nullable:
            quant = '?'  # nullable operands may only take '?'
        return s + quant, True

    def _cat(self, rng, depth, budget):
        parts = []
        all_nullable = True
        for _ in range(rng.randint(0, 3)):
            s, nullable = self._rep(rng, depth, budget)
            parts.append(s)
            all_nullable = all_nullable and nullable
        return ''.join(parts), all_nullable

    def _alt(self, rng, depth, budget):
        n = rng.randint(1, 3)
        branches = []
        any_nullable = False
        for _ in range(n):
            s, nullable = self._cat(rng, depth, budget)
            if n > 1 and s == '':
                s, nullable = 'a', False  # empty alternation branch: invalid
            branches.append(s)
            any_nullable = any_nullable or nullable
        return '|'.join(branches), any_nullable

    def test_random_cross_check(self):
        rng = random.Random(20260920)
        checked = 0
        for _ in range(400):
            budget = [12]  # remaining capture-group budget
            pattern, _ = self._alt(rng, 0, budget)
            prog = rx_compile(pattern)  # generator only emits valid syntax
            for _ in range(3):
                text = ''.join(rng.choice('abc\n') for _ in range(rng.randint(0, 6)))
                ours = prog.fullmatch(text)
                expected = re_spans(pattern, text, prog.groups)
                if expected is None:
                    self.assertIsNone(ours, (pattern, text))
                else:
                    self.assertIsNotNone(ours, (pattern, text))
                    self.assertEqual(ours.spans, expected, (pattern, text))
                checked += 1
        self.assertGreater(checked, 1000)


if __name__ == '__main__':
    unittest.main()
