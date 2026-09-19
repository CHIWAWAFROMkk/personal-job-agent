import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from job_agent.services.profile_store import (
    load_profile,
    save_profile,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)
from tests.helpers import sample_profile


class AtomicStorageTests(unittest.TestCase):
    def test_concurrent_writers_publish_complete_independent_files(self) -> None:
        for kind in ("text", "bytes", "json", "profile"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "shared.json"
                path.write_text("original", encoding="utf-8")
                # An unrelated legacy temporary file must never be reused or removed.
                legacy = path.with_suffix(".json.tmp")
                legacy.write_text("unrelated", encoding="utf-8")
                barrier = threading.Barrier(8, timeout=10)
                replacements = []
                arrived = set()
                replace = Path.replace

                def synchronized_replace(source, destination):
                    if source not in arrived:
                        arrived.add(source)
                        replacements.append(source)
                        barrier.wait()
                    return replace(source, destination)

                def write(index):
                    content = f"synthetic-{index}-" * 4096
                    if kind == "text":
                        return write_text_atomic(content, path)
                    if kind == "bytes":
                        return write_bytes_atomic(content.encode("utf-8"), path)
                    if kind == "json":
                        return write_json_atomic({"content": content}, path)
                    profile = sample_profile()
                    profile.person.display_name = content
                    return save_profile(
                        profile, path, overwrite=True, create_backup=False
                    )

                with patch.object(Path, "replace", synchronized_replace):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        results = list(pool.map(write, range(8)))

                self.assertEqual(results, [path.resolve()] * 8)
                self.assertEqual(len(set(replacements)), 8)
                self.assertTrue(all(item.parent == path.parent for item in replacements))
                if kind == "profile":
                    actual = load_profile(path).person.display_name
                elif kind == "json":
                    actual = json.loads(path.read_text(encoding="utf-8"))["content"]
                else:
                    actual = path.read_bytes().decode("utf-8")
                self.assertIn(actual, {f"synthetic-{i}-" * 4096 for i in range(8)})
                self.assertEqual(legacy.read_text(encoding="utf-8"), "unrelated")
                self.assertEqual(set(path.parent.iterdir()), {path, legacy})

    def test_failed_replace_keeps_original_and_removes_own_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "original.txt"
            path.write_bytes(b"original")
            with patch.object(Path, "replace", side_effect=OSError("synthetic failure")):
                with self.assertRaisesRegex(OSError, "synthetic failure"):
                    write_text_atomic("replacement", path)
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_encoding_keeps_original_and_removes_own_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "original.txt"
            path.write_bytes(b"original")
            with self.assertRaises(UnicodeEncodeError):
                write_text_atomic("synthetic \u6d4b\u8bd5", path, encoding="ascii")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    @unittest.skipUnless(os.name == "nt", "Windows replacement retry policy")
    def test_persistent_permission_failure_has_bounded_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "original.txt"
            path.write_bytes(b"original")
            with (
                patch.object(Path, "replace", side_effect=PermissionError("denied")) as replace,
                patch("job_agent.services.profile_store.time.sleep") as sleep,
            ):
                with self.assertRaises(PermissionError):
                    write_bytes_atomic(b"replacement", path)
            self.assertEqual(replace.call_count, 4)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02, 0.04])
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
