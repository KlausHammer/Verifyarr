"""Remove every mkdtemp directory a test left behind (they reached 3.7 GB in /tmp)."""
import shutil
import tempfile

_created = []
_mkdtemp = tempfile.mkdtemp


def _tracked_mkdtemp(*args, **kwargs):
    path = _mkdtemp(*args, **kwargs)
    _created.append(path)
    return path


tempfile.mkdtemp = _tracked_mkdtemp


def pytest_sessionfinish(session, exitstatus):
    for path in _created:
        shutil.rmtree(path, ignore_errors=True)
