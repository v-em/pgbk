import os
import signal
import selectors
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

from .errors import Error


class Control:
    def __init__(self, timeout=21600):
        self.stop = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.reason = "CANCELLED"
        self.guards = []

    def cancel(self, reason="CANCELLED"):
        self.reason = reason
        self.stop.set()

    def check(self):
        if time.monotonic() >= self.deadline:
            self.cancel("DEADLINE_EXCEEDED")
        if self.stop.is_set():
            raise Error(self.reason, "Operation stopped before completion", 7 if self.reason == "CANCELLED" else 5)
        for guard in self.guards:
            guard()

    @staticmethod
    def terminate(child):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                child.wait()

    @contextmanager
    def output(self, argv, env=None):
        """Yield bounded stdout chunks; successful EOF includes a zero exit status."""
        self.check()
        try:
            child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise Error("TOOL_UNAVAILABLE", "Required executable is unavailable", 3) from None
        def chunks():
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                while True:
                    self.check()
                    if not ready.select(.2):
                        continue
                    block = os.read(child.stdout.fileno(), 1024 * 1024)
                    if not block:
                        break
                    yield block
            while child.poll() is None:
                self.check()
                self.stop.wait(.2)
            self.check()
            if child.returncode:
                raise Error("TOOL_FAILED", "Streaming tool failed (native output redacted)", 5,
                            tool=os.path.basename(argv[0]), toolExitCode=child.returncode)
        stream = chunks()
        try:
            yield stream
        finally:
            stream.close()
            self.terminate(child)
            child.stdout.close()

    def inspect_input(self, argv, chunks):
        """Inspect an archive via stdin; allow the inspector to stop after its TOC."""
        self.check()
        with tempfile.TemporaryFile() as output:
            try:
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                raise Error("TOOL_UNAVAILABLE", "Required executable is unavailable", 3) from None
            try:
                os.set_blocking(child.stdin.fileno(), False)
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdin, selectors.EVENT_WRITE)
                    for chunk in chunks:
                        view = memoryview(chunk)
                        while view and child.poll() is None:
                            self.check()
                            if ready.select(.2):
                                try:
                                    view = view[os.write(child.stdin.fileno(), view):]
                                except BlockingIOError:
                                    continue
                                except BrokenPipeError:
                                    break
                        if child.poll() is not None:
                            break
                child.stdin.close()
                while child.poll() is None:
                    self.check()
                    self.stop.wait(.2)
                self.check()
                if child.returncode:
                    raise Error("ARCHIVE_INSPECTION_FAILED", "Cannot inspect streamed archive", 6)
                output.seek(0)
                value = output.read(4 * 1024 * 1024 + 1)
                if len(value) > 4 * 1024 * 1024:
                    raise Error("TOOL_OUTPUT_LIMIT", "Tool metadata output exceeds limit", 5)
                return value.decode('utf-8')
            finally:
                chunks.close()
                self.terminate(child)
                child.stdin.close()

    @contextmanager
    def connection(self, conn):
        """Interrupt blocking libpq queries when the worker loses its execution budget."""
        self.check()
        finished = threading.Event()
        def watch():
            while not finished.wait(0.2):
                try:
                    self.check()
                except Error:
                    # Repetition covers cancellation arriving between two statements.
                    try:
                        conn.cancel_safe(timeout=2)
                    except Exception:
                        pass
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            yield conn
        finally:
            finished.set()
            thread.join(timeout=3)
            self.check()

    def run(self, argv, env=None, cwd=None, capture=False):
        self.check()
        # Files bound RAM use; never log argv, native stderr or credentials.
        with tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdout:
            try:
                child = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr, start_new_session=True)
            except OSError:
                raise Error("TOOL_UNAVAILABLE", "Required executable is unavailable", 3) from None
            try:
                while child.poll() is None:
                    self.check()
                    self.stop.wait(0.2)
                self.check()
            except BaseException:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                except ProcessLookupError:
                    child.wait()
                raise
            if child.returncode:
                # PostgreSQL stderr can include SQL, connection strings, names and passwords.
                # Expose a safe classified error rather than risk echoing tool output.
                stderr.seek(0)
                diagnostic = stderr.read(65536).lower()
                if any(x in diagnostic for x in [b"password authentication failed", b"accessdenied", b"permission denied", b"not permitted"]):
                    raise Error("ACCESS_DENIED", "Upstream tool denied access (native output redacted)", 8)
                raise Error("TOOL_FAILED", "Upstream tool failed (native output redacted)", 5,
                            tool=os.path.basename(argv[0]), toolExitCode=child.returncode)
            if capture:
                stdout.seek(0)
                value = stdout.read(4 * 1024 * 1024 + 1)
                if len(value) > 4 * 1024 * 1024:
                    raise Error("TOOL_OUTPUT_LIMIT", "Tool metadata output exceeds limit", 5)
                return value.decode("utf-8")


def install_signals(control):
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: control.cancel())
