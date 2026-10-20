import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from build_lock import BuildBusy, BuildLock


class BuildLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".market_terminal.lock"

    def child_attempt(self):
        code = """
import sys
from build_lock import BuildBusy, BuildLock
try:
    with BuildLock(sys.argv[1]):
        print('acquired')
except BuildBusy:
    print('busy')
    raise SystemExit(3)
"""
        return subprocess.run([sys.executable, "-c", code, str(self.path)],
                              cwd=Path(__file__).resolve().parent,
                              capture_output=True, text=True, timeout=5)

    def test_same_process_independent_handles_conflict_and_release(self):
        with BuildLock(self.path):
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
            with self.assertRaises(BuildBusy):
                BuildLock(self.path).acquire()
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        with BuildLock(self.path):
            pass

    def test_independent_process_is_busy_while_held_and_acquires_after_release(self):
        with BuildLock(self.path):
            result = self.child_attempt()
            self.assertEqual((result.returncode, result.stdout.strip()), (3, "busy"),
                             result.stderr)
        result = self.child_attempt()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "acquired"),
                         result.stderr)

    def test_process_death_releases_lock_without_removing_file(self):
        # Pipe handshake avoids sleeps and proves the child holds the OS lock.
        code = """
import sys
from build_lock import BuildLock
with BuildLock(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path)],
                                 cwd=Path(__file__).resolve().parent,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(BuildBusy):
                BuildLock(self.path).acquire()
            child.kill()
            child.wait(timeout=5)
            with BuildLock(self.path):
                self.assertTrue(self.path.is_file())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_lock_file_inode_stays_in_place_across_releases(self):
        lock = BuildLock(self.path)
        lock.acquire()
        inode = self.path.stat().st_ino
        lock.release()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_ino, inode)
        with lock:
            self.assertEqual(self.path.stat().st_ino, inode)
        lock.release()  # Release is safe after context-manager cleanup.

    def test_exception_releases_lock_and_reentry_does_not_unlock_owner(self):
        with self.assertRaisesRegex(ValueError, "example"):
            with BuildLock(self.path):
                raise ValueError("example")
        lock = BuildLock(self.path)
        with lock:
            with self.assertRaisesRegex(RuntimeError, "already acquired"):
                lock.acquire()
            with self.assertRaises(BuildBusy):
                BuildLock(self.path).acquire()
        with BuildLock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
