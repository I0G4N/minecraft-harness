"""Exercise console protocol with a FIFO fixture; never launch Minecraft/Java."""

import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest


class ConsoleControlTests(unittest.TestCase):
    def test_save_waits_for_new_exact_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / "control"
            control.mkdir()
            data = root / "data"
            (data / "logs").mkdir(parents=True)
            fifo = control / "console"
            os.mkfifo(fifo)
            descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            (control / "java.pid").write_text(str(os.getpid()))
            (control / "started").touch()
            old = time.time() - 5
            os.utime(control / "started", (old, old))
            log = data / "logs/latest.log"
            log.write_text('[00:00:01] [Server thread/INFO]: Done (1.0s)! For help, type "help"\n'
                           '[00:00:02] [Server thread/INFO]: Saved the game\n')
            env = {**os.environ, "HARNESS_CONTROL_DIR": str(control), "HARNESS_DATA_DIR": str(data)}
            helper = Path(__file__).resolve().parent / "harness-control"
            subprocess.run(["/bin/sh", str(helper), "ready"], env=env, check=True)
            process = subprocess.Popen(["/bin/sh", str(helper), "save"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([descriptor], [], [], 5)[0])
                self.assertEqual(os.read(descriptor, 4096), b"save-all flush\n")
                with log.open("a") as stream:
                    stream.write('[00:00:03] [Server thread/INFO]: <player1> Saved the game\n')
                time.sleep(0.1)
                self.assertIsNone(process.poll(), "Old or chat acknowledgements must not satisfy save")
                with log.open("a") as stream:
                    stream.write('[00:00:04] [Server thread/INFO]: Saved the game\n')
                out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, err.decode())
                self.assertEqual(out, b"Saved the game\n")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertFalse((control / "save.lock").exists())

    def test_previous_boot_log_does_not_indicate_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / "console")
            (root / "java.pid").write_text(str(os.getpid()))
            (root / "logs").mkdir()
            log = root / "logs/latest.log"
            log.write_text('[00:00:01] [Server thread/INFO]: Done (1.0s)! For help, type "help"\n')
            past = time.time() - 5
            os.utime(log, (past, past))
            (root / "started").touch()
            helper = Path(__file__).resolve().parent / "harness-control"
            result = subprocess.run(["/bin/sh", str(helper), "ready"], env={**os.environ, "HARNESS_CONTROL_DIR": str(root), "HARNESS_DATA_DIR": str(root)}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)




class ConsoleCancellationTests(unittest.TestCase):
    def test_terminated_remote_save_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / 'control'
            control.mkdir()
            data = root / 'data'
            (data / 'logs').mkdir(parents=True)
            fifo = control / 'console'
            os.mkfifo(fifo)
            descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            (control / 'java.pid').write_text(str(os.getpid()))
            (control / 'started').touch()
            old = time.time() - 5
            os.utime(control / 'started', (old, old))
            (data / 'logs/latest.log').write_text('[00:00:01] [Server thread/INFO]: Done (1.0s)! For help, type "help"\n')
            helper = Path(__file__).resolve().parent / 'harness-control'
            environment = {**os.environ, 'HARNESS_CONTROL_DIR':str(control), 'HARNESS_DATA_DIR':str(data)}
            process = subprocess.Popen(['/bin/sh',str(helper),'save'], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([descriptor], [], [], 5)[0])
                self.assertEqual(os.read(descriptor,4096), b'save-all flush\n')
                self.assertTrue((control / 'save.lock').is_dir())
                process.terminate()
                process.communicate(timeout=4)
                self.assertNotEqual(process.returncode,0)
                self.assertFalse((control / 'save.lock').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
