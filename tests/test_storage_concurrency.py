"""Commands and the monitor must not overwrite each other's persisted state."""
import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from test_mag import Base, mag


class ConcurrentStorage(Base):
    def test_independent_processes_preserve_every_usage_update(self):
        mag.set_current("claude", "main", manual=True)
        script = ("import runpy,sys; m=runpy.run_path(sys.argv[1]); "
                  "[m['record_limits'](sys.argv[2]+'-'+str(n), {'windows':[]}) for n in range(12)]")

        def record(worker):
            subprocess.run([sys.executable, "-c", script, mag.__file__, str(worker)],
                           check=True, capture_output=True, timeout=15)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(record, range(4)))
        self.assertEqual(len(mag.state()["limits"]), 48)
        self.assertTrue(mag.manually_selected("main"))

    def test_simultaneous_atomic_writers_use_independent_files(self):
        path = os.path.join(mag.ROOT, "update.json")
        barrier = threading.Barrier(8)
        replace = mag.os.replace

        def synchronized_replace(source, target):
            barrier.wait(timeout=5)
            replace(source, target)

        with mock.patch.object(mag.os, "replace", side_effect=synchronized_replace):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda n: mag.write_json(path, {"writer": n}), range(8)))
        self.assertIn(mag.read_json(path, {})["writer"], range(8))
        self.assertFalse(any(name.startswith("update.json.") for name in os.listdir(mag.ROOT)))

    def test_parallel_limit_updates_preserve_selection_and_every_account(self):
        mag.set_current("claude", "main", manual=True)
        read_state = mag.state

        def slow_read():
            result = read_state()
            time.sleep(0.005)
            return result

        with mock.patch.object(mag, "state", side_effect=slow_read):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda n: mag.record_limits(str(n), {"windows": []}), range(16)))
        self.assertEqual(set(mag.state()["limits"]), {str(n) for n in range(16)})
        self.assertEqual(mag.get_current("claude"), "main")
        self.assertTrue(mag.manually_selected("main"))

    def test_failed_serialization_keeps_previous_file_and_cleans_temp(self):
        path = os.path.join(mag.ROOT, "config.json")
        mag.write_json(path, {"original": True})
        with self.assertRaises(TypeError):
            mag.write_json(path, {"bad": object()})
        self.assertEqual(mag.read_json(path, {}), {"original": True})
        self.assertFalse(any(name.startswith("config.json.") for name in os.listdir(mag.ROOT)))
