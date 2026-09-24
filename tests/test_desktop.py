from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.desktop import (
    SingleInstanceLock,
    default_user_data_root,
    desktop_smoke_test,
    prepare_user_workspace,
)


class DesktopTests(unittest.TestCase):
    def test_packaged_app_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-local-") as directory:
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": directory, "JOB_AGENT_PROJECT_ROOT": ""},
                clear=False,
            ):
                self.assertEqual(
                    default_user_data_root(frozen=True),
                    (Path(directory) / "PersonalJobAgent").resolve(),
                )

    def test_explicit_data_directory_has_priority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-explicit-") as directory:
            expected = Path(directory).resolve()
            self.assertEqual(
                default_user_data_root(explicit=expected, frozen=True), expected
            )

    def test_job_agent_data_dir_env_var_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-datadir-") as directory:
            expected = Path(directory).resolve()
            with patch.dict(os.environ, {"JOB_AGENT_DATA_DIR": str(expected), "JOB_AGENT_PROJECT_ROOT": ""}, clear=False):
                self.assertEqual(default_user_data_root(frozen=True), expected)

    def test_workspace_and_smoke_test_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-smoke-") as directory:
            root = prepare_user_workspace(Path(directory))
            report_path = desktop_smoke_test(root)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["database_created"])
            self.assertTrue(payload["settings_api_ready"])
            self.assertTrue(payload["openai_sdk_ready"])
            self.assertTrue((root / "data/private").is_dir())
            self.assertTrue((root / "data/output").is_dir())

    def test_single_instance_lock_prevents_duplicate_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-lock-") as directory:
            lock_path = Path(directory) / ".instance_lock"
            pid_path = Path(directory) / ".instance_lock.pid"
            lock1 = SingleInstanceLock(lock_path)
            try:
                ok1, pid1 = lock1.acquire()
                self.assertTrue(ok1)
                self.assertEqual(pid1, os.getpid())
                self.assertTrue(lock_path.is_file())
                self.assertTrue(pid_path.is_file())
                self.assertEqual(pid_path.read_text(encoding="utf-8").strip(), str(os.getpid()))

                # Second lock on same path should fail and report holding pid
                lock2 = SingleInstanceLock(lock_path)
                ok2, pid2 = lock2.acquire()
                self.assertFalse(ok2)
                self.assertEqual(pid2, os.getpid())
            finally:
                lock1.release()

            # Now second lock should succeed
            try:
                ok3, pid3 = lock2.acquire()
                self.assertTrue(ok3)
                self.assertEqual(pid3, os.getpid())
            finally:
                lock2.release()

    def test_single_instance_lock_context_manager(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-agent-lock-ctx-") as directory:
            lock_path = Path(directory) / ".instance_lock"
            with SingleInstanceLock(lock_path):
                # Another acquire attempt should fail while context is active
                lock2 = SingleInstanceLock(lock_path)
                ok, pid = lock2.acquire()
                self.assertFalse(ok)
                self.assertEqual(pid, os.getpid())

            # After exit, another lock can be acquired
            with SingleInstanceLock(lock_path):
                self.assertTrue(lock_path.is_file())

    def test_single_instance_lock_multiprocess_exclusivity(self) -> None:
        """验证跨真实进程的单实例排他性：
        1. 同目录互斥：进程 1 持有锁期间，独立的进程 2 获取失败并准确报出进程 1 的 PID；
        2. 不同目录并行：独立的进程 3 在另一个数据目录能顺利获取锁；
        3. 退出后重启：进程 1 正常退出后，进程 4 能顺利获取该目录的锁；
        4. 异常崩溃恢复：持有锁的进程被强制终止后，OS 内核自动释放文件锁，新进程能接管获取。
        """
        code_hold = (
            "import sys, os, time\n"
            "from pathlib import Path\n"
            "from job_agent.desktop import SingleInstanceLock\n"
            "lock = SingleInstanceLock(Path(sys.argv[1]) / '.instance_lock')\n"
            "ok, pid = lock.acquire()\n"
            "if ok:\n"
            "    sys.stdout.write(f'ACQUIRED {pid}\\n')\n"
            "    sys.stdout.flush()\n"
            "    sys.stdin.readline()\n"
            "    lock.release()\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    sys.stdout.write(f'FAILED {pid}\\n')\n"
            "    sys.stdout.flush()\n"
            "    sys.exit(1)\n"
        )
        code_try = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from job_agent.desktop import SingleInstanceLock\n"
            "lock = SingleInstanceLock(Path(sys.argv[1]) / '.instance_lock')\n"
            "ok, pid = lock.acquire()\n"
            "if ok:\n"
            "    lock.release()\n"
            "    sys.stdout.write(json.dumps({'ok': True, 'pid': pid}))\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    sys.stdout.write(json.dumps({'ok': False, 'pid': pid}))\n"
            "    sys.exit(1)\n"
        )

        project_src = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = project_src + os.pathsep + env.get("PYTHONPATH", "")

        with tempfile.TemporaryDirectory(prefix="agent-mp-lock1-") as dir1, \
             tempfile.TemporaryDirectory(prefix="agent-mp-lock2-") as dir2:
            # 1. 启动真实子进程 P1 持有 dir1 的锁
            p1 = subprocess.Popen(
                [sys.executable, "-c", code_hold, dir1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                line = p1.stdout.readline().strip()
                self.assertTrue(line.startswith("ACQUIRED"), f"进程 1 必须成功获取锁: {line}")
                p1_pid = int(line.split()[1])
                self.assertGreater(p1_pid, 0)

                # 2. 启动独立子进程 P2 尝试获取同一 dir1 的锁 -> 必须失败并报告 P1 的 PID
                p2 = subprocess.run(
                    [sys.executable, "-c", code_try, dir1],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(p2.returncode, 1, "同一目录被占用时进程 2 必须返回非 0 退出码")
                p2_res = json.loads(p2.stdout)
                self.assertFalse(p2_res["ok"])
                self.assertEqual(p2_res["pid"], p1_pid, "进程 2 必须识别出正在持有锁的进程 1 PID")

                # 3. 启动独立子进程 P3 获取不同目录 dir2 的锁 -> 必须成功（不同目录并行）
                p3 = subprocess.run(
                    [sys.executable, "-c", code_try, dir2],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(p3.returncode, 0, "不同数据目录的进程必须可以并行获取锁")
                p3_res = json.loads(p3.stdout)
                self.assertTrue(p3_res["ok"])

                # 4. 正常退出进程 P1
                p1.stdin.write("\n")
                p1.stdin.flush()
                p1.wait(timeout=5)

                # 5. 启动独立子进程 P4 尝试获取 dir1 -> 必须成功（正常退出后可重启）
                p4 = subprocess.run(
                    [sys.executable, "-c", code_try, dir1],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(p4.returncode, 0, "前序进程退出后新进程必须能重新获取锁")
                p4_res = json.loads(p4.stdout)
                self.assertTrue(p4_res["ok"])
            finally:
                for pipe in (p1.stdin, p1.stdout, p1.stderr):
                    try:
                        if pipe:
                            pipe.close()
                    except Exception:
                        pass
                if p1.poll() is None:
                    p1.kill()
                    p1.wait()

        # 6. 测试异常强杀崩溃恢复：内核锁自动解开
        with tempfile.TemporaryDirectory(prefix="agent-mp-crash-") as dir_crash:
            p_crash = subprocess.Popen(
                [sys.executable, "-c", code_hold, dir_crash],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                line = p_crash.stdout.readline().strip()
                self.assertTrue(line.startswith("ACQUIRED"))
                # 模拟非正常崩溃：直接 kill 强杀进程
                p_crash.kill()
                p_crash.wait()

                # 强杀后启动新进程接管
                p_recover = subprocess.run(
                    [sys.executable, "-c", code_try, dir_crash],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(p_recover.returncode, 0, "进程异常崩溃后，OS 内核释放文件锁，新进程必须能立即接管")
                p_recover_res = json.loads(p_recover.stdout)
                self.assertTrue(p_recover_res["ok"])
            finally:
                for pipe in (p_crash.stdin, p_crash.stdout, p_crash.stderr):
                    try:
                        if pipe:
                            pipe.close()
                    except Exception:
                        pass
                if p_crash.poll() is None:
                    p_crash.kill()
                    p_crash.wait()


if __name__ == "__main__":
    unittest.main()
