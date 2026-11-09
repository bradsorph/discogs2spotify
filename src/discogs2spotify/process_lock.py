import fcntl
from pathlib import Path

from .errors import SyncError


class SyncProcessLock:
    def __init__(self, state_path):
        state_path = Path(state_path).expanduser().resolve()
        self.path = state_path.with_name(state_path.name + ".lock")
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise SyncError(
                "Another synchronization process is already running ({}).".format(
                    self.path
                )
            )

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
