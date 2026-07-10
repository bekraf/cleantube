import threading


class DaemonStatus:
    """Thread-safe snapshot of the daemon's in-memory scheduling state.

    The daemon writes wall-clock facts here (last/next scan, next download
    slot, the download currently in flight) that live only on its monotonic
    deadlines and would otherwise be invisible to the web portal.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {}

    def update(self, **fields) -> None:
        with self._lock:
            self._data.update(fields)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)
