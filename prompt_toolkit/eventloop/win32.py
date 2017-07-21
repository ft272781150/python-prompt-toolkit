"""
Win32 event loop.
"""
from __future__ import unicode_literals

from ..win32_types import SECURITY_ATTRIBUTES
from .base import EventLoop
from .inputhook import InputHookContext

from ctypes import windll, pointer
from ctypes.wintypes import DWORD, BOOL, HANDLE

import msvcrt
import threading

__all__ = (
    'Win32EventLoop',
    'wait_for_handles',
)

WAIT_TIMEOUT = 0x00000102
INFINITE = -1


class Win32EventLoop(EventLoop):
    """
    Event loop for Windows systems.

    :param recognize_paste: When True, try to discover paste actions and turn
        the event into a BracketedPaste.
    """
    def __init__(self, inputhook=None, recognize_paste=True):
        assert inputhook is None or callable(inputhook)

        super(Win32EventLoop, self).__init__()

        self._event = _create_event()
        self._calls_from_executor = []

        self.closed = False
        self._running = False

        # The `Input` object that's currently attached.
        self._input = None
        self._input_ready_cb = None

        # Additional readers.
        self._read_fds = {}  # Maps fd to handler.

        # Create inputhook context.
        self._inputhook_context = InputHookContext(inputhook) if inputhook else None

    def run_until_complete(self, future):
        if self.closed:
            raise Exception('Event loop already closed.')

        try:
            self._running = True

            while not future.done():
                self._run_once()

            # Run one last time, to flush the pending `_calls_from_executor`s.
            if self._calls_from_executor:
                self._run_once()

        finally:
            self._running = False

    def _run_once(self):
        # Call inputhook.
        if self._inputhook_context:
            def ready(wait):
                " True when there is input ready. The inputhook should return control. "
                return bool(self._ready_for_reading(INFINITE if wait else 0))
            self._inputhook_context.call_inputhook(ready)

        # Wait for the next event.
        handle = self._ready_for_reading(INFINITE)

        if self._input and handle == self._input.handle:
            # When stdin is ready, read input and reset timeout timer.
            self._run_task(self._input_ready_cb)

        elif handle == self._event:
            # When the Windows Event has been trigger, process the messages in the queue.
            windll.kernel32.ResetEvent(self._event)
            self._process_queued_calls_from_executor()

        elif handle in self._read_fds:
            callback = self._read_fds[handle]
            self._run_task(callback)

    def _run_task(self, t):
        try:
            t()
        except BaseException as e:
            self.call_exception_handler({
                'exception': e
            })

    def _ready_for_reading(self, timeout=INFINITE):
        """
        Return the handle that is ready for reading or `None` on timeout.
        """
        handles = [self._event]
        if self._input:
            handles.append(self._input.handle)
        handles.extend(self._read_fds.keys())
        return wait_for_handles(handles, timeout)

    def set_input(self, input, input_ready_callback):
        """
        Tell the eventloop to read from this input object.
        """
        from ..input.win32 import Win32Input
        assert isinstance(input, Win32Input)
        previous_values = self._input, self._input_ready_cb

        self._input = input
        self._input_ready_cb = input_ready_callback

        return previous_values

    def remove_input(self):
        """
        Remove the currently attached `Input`.
        """
        if self._input:
            previous_input = self._input
            previous_cb = self._input_ready_cb

            self._input = None
            self._input_ready_cb = None

            return previous_input, previous_cb
        else:
            return None, None

    def close(self):
        self.closed = True

        # Clean up Event object.
        windll.kernel32.CloseHandle(self._event)

        if self._inputhook_context:
            self._inputhook_context.close()

    def run_in_executor(self, callback, _daemon=False):
        """
        Run a long running function in a background thread.
        (This is recommended for code that could block the event loop.)
        Similar to Twisted's ``deferToThread``.
        """
        # Wait until the main thread is idle for an instant before starting the
        # executor. (Like in eventloop/posix.py, we start the executor using
        # `call_from_executor`.)
        def start_executor():
            t = threading.Thread(target=callback)
            if _daemon:
                t.daemon = True
            t.start()
        self.call_from_executor(start_executor)

    def call_from_executor(self, callback, _max_postpone_until=None):
        """
        Call this function in the main event loop.
        Similar to Twisted's ``callFromThread``.
        """
        # Append to list of pending callbacks.
        self._calls_from_executor.append(callback)

        # Set Windows event.
        windll.kernel32.SetEvent(self._event)

    def _process_queued_calls_from_executor(self):
        # Process calls from executor.
        calls_from_executor, self._calls_from_executor = self._calls_from_executor, []
        for c in calls_from_executor:
            self._run_task(c)

    def add_reader(self, fd, callback):
        " Start watching the file descriptor for read availability. "
        h = msvcrt.get_osfhandle(fd)
        self._read_fds[h] = callback

    def remove_reader(self, fd):
        " Stop watching the file descriptor for read availability. "
        h = msvcrt.get_osfhandle(fd)
        if h in self._read_fds:
            del self._read_fds[h]


def wait_for_handles(handles, timeout=INFINITE):
    """
    Waits for multiple handles. (Similar to 'select') Returns the handle which is ready.
    Returns `None` on timeout.

    http://msdn.microsoft.com/en-us/library/windows/desktop/ms687025(v=vs.85).aspx
    """
    assert isinstance(handles, list)
    assert isinstance(timeout, int)

    arrtype = HANDLE * len(handles)
    handle_array = arrtype(*handles)

    ret = windll.kernel32.WaitForMultipleObjects(
        len(handle_array), handle_array, BOOL(False), DWORD(timeout))

    if ret == WAIT_TIMEOUT:
        return None
    else:
        h = handle_array[ret]
        return h


def _create_event():
    """
    Creates a Win32 unnamed Event .

    http://msdn.microsoft.com/en-us/library/windows/desktop/ms682396(v=vs.85).aspx
    """
    return windll.kernel32.CreateEventA(pointer(SECURITY_ATTRIBUTES()), BOOL(True), BOOL(False), None)
