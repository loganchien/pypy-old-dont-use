#!/usr/bin/env python

from rpython.jit.backend.riscv.test.test_basic import JitRISCVMixin
from rpython.jit.metainterp.test.test_send import SendTests
from rpython.rlib import jit


class TestSend(JitRISCVMixin, SendTests):
    # for the individual tests see
    # ====> ../../../metainterp/test/test_send.py

    def test_call_with_additional_args(self):
        @jit.dont_look_inside
        def externfn(a, b, c, d):
            return a + b * 10 + c * 100 + d * 1000
        def f(a, b, c, d):
            return externfn(a, b, c, d)
        res = self.interp_operations(f, [1, 2, 3, 4])
        assert res == 4321
