import os
from rpython.rtyper.lltypesystem import llmemory, rffi
from rpython.rlib.rposix import is_valid_fd
from rpython.rlib.rarithmetic import widen, ovfcheck_float_to_longlong
from rpython.rlib.objectmodel import keepalive_until_here
from rpython.rtyper.annlowlevel import llhelper

from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.error import exception_from_saved_errno
from pypy.interpreter.gateway import unwrap_spec
from pypy.module.faulthandler import cintf, dumper


class Handler(object):
    def __init__(self, space):
        "NOT_RPYTHON"
        self.space = space
        self._cleanup_()

    def _cleanup_(self):
        self.fatal_error_w_file = None
        self.dump_traceback_later_w_file = None

    def check_err(self, p_err):
        if p_err:
            raise oefmt(self.space.w_RuntimeError, 'faulthandler: %8',
                        rffi.charp2str(p_err))

    def get_fileno_and_file(self, w_file):
        space = self.space
        if space.is_none(w_file):
            w_file = space.sys.get('stderr')
            if space.is_none(w_file):
                raise oefmt(space.w_RuntimeError, "sys.stderr is None")
        elif space.isinstance_w(w_file, space.w_int):
            fd = space.c_int_w(w_file)
            if fd < 0 or not is_valid_fd(fd):
                raise oefmt(space.w_ValueError,
                            "file is not a valid file descriptor")
            return fd, None

        fd = space.c_int_w(space.call_method(w_file, 'fileno'))
        try:
            space.call_method(w_file, 'flush')
        except OperationError as e:
            if e.async(space):
                raise
            pass   # ignore flush() error
        return fd, w_file

    def setup(self):
        dump_callback = llhelper(cintf.DUMP_CALLBACK, dumper._dump_callback)
        self.check_err(cintf.pypy_faulthandler_setup(dump_callback))

    def enable(self, w_file, all_threads):
        fileno, w_file = self.get_fileno_and_file(w_file)
        self.setup()
        self.fatal_error_w_file = w_file
        self.check_err(cintf.pypy_faulthandler_enable(
            rffi.cast(rffi.INT, fileno),
            rffi.cast(rffi.INT, all_threads)))

    def disable(self):
        cintf.pypy_faulthandler_disable()
        self.fatal_error_w_file = None

    def is_enabled(self):
        return bool(widen(cintf.pypy_faulthandler_is_enabled()))

    def dump_traceback(self, w_file, all_threads):
        fileno, w_file = self.get_fileno_and_file(w_file)
        self.setup()
        cintf.pypy_faulthandler_dump_traceback(
            rffi.cast(rffi.INT, fileno),
            rffi.cast(rffi.INT, all_threads),
            llmemory.NULL)
        keepalive_until_here(w_file)

    def dump_traceback_later(self, timeout, repeat, w_file, exit):
        timeout *= 1e6
        try:
            microseconds = ovfcheck_float_to_longlong(timeout)
        except OverflowError:
            raise oefmt(space.w_OverflowError, "timeout value is too large")
        if microseconds <= 0:
            raise oefmt(space.w_ValueError, "timeout must be greater than 0")
        fileno, w_file = self.get_fileno_and_file(w_file)
        self.setup()
        self.check_err(cintf.pypy_faulthandler_dump_traceback_later(
            rffi.cast(rffi.LONGLONG, microseconds),
            rffi.cast(rffi.INT, repeat),
            rffi.cast(rffi.INT, fileno),
            rffi.cast(rffi.INT, exit)))
        self.dump_traceback_later_w_file = w_file

    def cancel_dump_traceback_later(self):
        cintf.pypy_faulthandler_cancel_dump_traceback_later()
        self.dump_traceback_later_w_file = None

    def finish(self):
        cintf.pypy_faulthandler_teardown()
        self._cleanup_()


def finish(space):
    "Finalize the faulthandler logic (called from shutdown())"
    space.fromcache(Handler).finish()


@unwrap_spec(all_threads=int)
def enable(space, w_file=None, all_threads=0):
    "enable(file=sys.stderr, all_threads=True): enable the fault handler"
    space.fromcache(Handler).enable(w_file, all_threads)

def disable(space):
    "disable(): disable the fault handler"
    space.fromcache(Handler).disable()

def is_enabled(space):
    "is_enabled()->bool: check if the handler is enabled"
    return space.wrap(space.fromcache(Handler).is_enabled())

@unwrap_spec(all_threads=int)
def dump_traceback(space, w_file=None, all_threads=0):
    """dump the traceback of the current thread into file
    including all threads if all_threads is True"""
    space.fromcache(Handler).dump_traceback(w_file, all_threads)

@unwrap_spec(timeout=float, repeat=int, exit=int)
def dump_traceback_later(space, timeout, repeat=0, w_file=None, exit=0):
    """dump the traceback of all threads in timeout seconds,
    or each timeout seconds if repeat is True. If exit is True,
    call _exit(1) which is not safe."""
    space.fromcache(Handler).dump_traceback_later(timeout, repeat, w_file, exit)

def cancel_dump_traceback_later(space):
    """cancel the previous call to dump_traceback_later()."""
    space.fromcache(Handler).cancel_dump_traceback_later()


# for tests...

@unwrap_spec(release_gil=int)
def read_null(space, release_gil=0):
    if release_gil:
        cintf.pypy_faulthandler_read_null_releasegil()
    else:
        cintf.pypy_faulthandler_read_null()

@unwrap_spec(release_gil=int)
def sigsegv(space, release_gil=0):
    if release_gil:
        cintf.pypy_faulthandler_sigsegv_releasegil()
    else:
        cintf.pypy_faulthandler_sigsegv()

def sigfpe(space):
    cintf.pypy_faulthandler_sigfpe()

def sigabrt(space):
    cintf.pypy_faulthandler_sigabrt()

@unwrap_spec(levels=int)
def stack_overflow(space, levels=100000000):
    levels = float(levels)
    return space.wrap(cintf.pypy_faulthandler_stackoverflow(levels))
