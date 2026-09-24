from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from job_agent.models.job_record import JobRecordInput
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_onboarding import ProfileOnboardingInput, onboard_profile
from job_agent.services.profile_recovery import (
    PENDING_NAME, ProfileRecoveryError, recover_interrupted_profile_switch,
)
from job_agent.services.profile_store import ProfileStoreError, load_profile
from job_agent.services.runtime_config import (
    RuntimeConfigError, RuntimeConfigUpdate, effective_runtime_config, update_runtime_config,
)


CHILD = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from job_agent.services.job_repository import JobRepository
from job_agent.services.profile_onboarding import ProfileOnboardingInput, switch_to_new_profile
from job_agent.services.profile_recovery import recover_interrupted_profile_switch
root = Path(sys.argv[1])
phase = sys.argv[2]
private = root / "private"
profile = private / "profile.json"
output = root / "output"
repository = JobRepository(private / "job_agent.sqlite3")
original = Path.replace
def replace(source, destination):
    result = original(source, destination)
    destination = Path(destination)
    crash = (
        phase == "output" and destination.name.startswith("output-profile-switch-")
        or phase == "old_files" and destination.parent.name.startswith("profile-switch-private-")
        or phase == "new_files" and source.parent.name.startswith(".profile-switch-staging-") and destination == private / "resumes"
        or phase == "profile" and source.parent.name.startswith(".profile-switch-staging-") and destination == profile
        or phase == "during_recovery" and source.parent.name.startswith("profile-switch-private-")
    )
    if crash:
        os._exit(91)
    return result
old_switch = repository.backup_and_clear_for_new_profile
def switch(*args, **kwargs):
    result = old_switch(*args, **kwargs)
    if phase == "committed":
        os._exit(91)
    return result
with patch.object(Path, "replace", replace), patch.object(repository, "backup_and_clear_for_new_profile", switch):
    if phase == "during_recovery":
        recover_interrupted_profile_switch(profile_path=profile, private_dir=private, output_dir=output, repository=repository)
    else:
        request = ProfileOnboardingInput(resume_filename="new.txt", resume_bytes="使用 Python 完成新用户数据分析任务和课程项目。".encode(),display_name="NEW USER",target_roles=["新用户岗位"],confirm_truth=True,confirm_replace=True,mode="replace")
        switch_to_new_profile(request,profile_path=profile,private_dir=private,output_dir=output,repository=repository)
'''


class ProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.profile = self.private / "profile.json"
        self.output = self.root / "output"
        self.repository = JobRepository(self.private / "job_agent.sqlite3")
        onboard_profile(ProfileOnboardingInput(resume_filename="old.txt",resume_bytes="使用 SQL 完成旧用户数据分析任务和课程项目。".encode(), display_name="OLD USER",target_roles=["旧用户岗位"],confirm_truth=True),profile_path=self.profile,private_dir=self.private)
        self.old_bytes = self.profile.read_bytes()
        for name in ("browser-profiles", "browser-sessions"):
            (self.private / name).mkdir()
            (self.private / name / "synthetic.txt").write_text("OLD SESSION",encoding="utf-8")
        self.repository.upsert_job(JobRecordInput(company="OLD COMPANY",title="OLD ROLE",jd_text="synthetic",source="synthetic"))
        self.output.mkdir()
        (self.output / "old.txt").write_text("OLD OUTPUT",encoding="utf-8")
        self.marker = self.private / "backups" / PENDING_NAME

    def tearDown(self):
        self.temp.cleanup()

    def crash(self, phase):
        result = subprocess.run([sys.executable,"-c",CHILD,str(self.root),phase],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,91,result.stderr)

    def recover(self):
        for name in ("browser-profiles", "browser-sessions"):
            (self.private / name).mkdir(exist_ok=True)
        return recover_interrupted_profile_switch(profile_path=self.profile, private_dir=self.private,output_dir=self.output,repository=self.repository)

    def assert_old_user(self):
        self.assertEqual(self.profile.read_bytes(),self.old_bytes)
        self.assertEqual(load_profile(self.profile).person.display_name,"OLD USER")
        with closing(sqlite3.connect(self.repository.path)) as connection:
            self.assertEqual(connection.execute("SELECT company,title FROM jobs").fetchall(),[("OLD COMPANY","OLD ROLE")])
        self.assertEqual((self.output / "old.txt").read_text(encoding="utf-8"),"OLD OUTPUT")
        for name in ("browser-profiles", "browser-sessions"):
            self.assertEqual((self.private / name / "synthetic.txt").read_text(encoding="utf-8"),"OLD SESSION")

    def test_crash_at_each_uncommitted_publication_step_restores_original_user(self):
        # Each successful recovery leaves the workspace reusable for another switch.
        for phase in ("output", "old_files", "new_files", "profile"):
            with self.subTest(phase=phase):
                self.crash(phase)
                with self.assertRaises(ProfileStoreError):
                    load_profile(self.profile)
                self.assertEqual(self.recover(),"rolled_back")
                self.assert_old_user()
                self.assertIsNone(self.recover())

    def test_crash_after_database_commit_keeps_new_user(self):
        self.crash("committed")
        self.assertEqual(self.recover(),"committed")
        self.assertEqual(load_profile(self.profile).person.display_name,"NEW USER")
        self.assertEqual(self.repository.stats().jobs,0)
        self.assertFalse((self.output / "old.txt").exists())
        self.assertIsNone(self.recover())

    def test_recovery_interrupted_again_is_repeatable(self):
        self.crash("profile")
        self.crash("during_recovery")
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.recover(),"rolled_back")
        self.assert_old_user()

    def test_recreated_empty_output_after_interrupted_recovery_is_preserved(self):
        self.output.replace(self.root / "fixture-output")
        self.crash("profile")
        from job_agent.services import profile_recovery
        with patch.object(profile_recovery,"finish_record",side_effect=OSError("interrupted after restoration")):
            with self.assertRaises(ProfileRecoveryError):
                self.recover()
        self.assertTrue(self.marker.exists())
        self.output.mkdir()
        self.assertEqual(self.recover(),"rolled_back")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.profile.read_bytes(), self.old_bytes)
        self.assertEqual(self.repository.stats().jobs, 1)

    def test_tampered_profile_backup_blocks_recovery_and_preserves_current_files(self):
        self.crash("profile")
        record = json.loads(self.marker.read_text(encoding="utf-8"))
        backup = self.marker.parent / f"profile-switch-{record['suffix']}.json"
        backup.write_text("damaged",encoding="utf-8")
        current = self.profile.read_bytes()
        with self.assertRaisesRegex(ProfileRecoveryError,"校验失败"):
            self.recover()
        self.assertEqual(self.profile.read_bytes(),current)
        self.assertTrue(self.marker.exists())

    def test_path_escape_record_is_rejected_without_touching_files(self):
        self.crash("profile")
        record = json.loads(self.marker.read_text(encoding="utf-8"))
        record["old_private_names"].append("../outside")
        self.marker.write_text(json.dumps(record),encoding="utf-8")
        current = self.profile.read_bytes()
        with self.assertRaisesRegex(ProfileRecoveryError,"文件名无效"):
            self.recover()
        self.assertEqual(self.profile.read_bytes(),current)

    def test_missing_database_is_not_recreated_during_recovery(self):
        self.crash("profile")
        self.repository.path.replace(self.root / "preserved.sqlite3")
        with self.assertRaises(ProfileRecoveryError):
            self.recover()
        self.assertFalse(self.repository.path.exists())

    def test_corrupt_config_is_not_replaced_by_defaults_or_settings_update(self):
        path = self.private / "app-settings.json"
        corrupt = b'{"ai":broken synthetic config'
        path.write_bytes(corrupt)
        with self.assertRaises(RuntimeConfigError):
            effective_runtime_config(path)
        with self.assertRaises(RuntimeConfigError):
            update_runtime_config(path,RuntimeConfigUpdate(ai_provider="local",search_provider="none",map_provider="none"))
        self.assertEqual(path.read_bytes(),corrupt)

    def test_corrupt_profile_update_preserves_original_bytes(self):
        corrupt = b'{"person": broken synthetic profile'
        self.profile.write_bytes(corrupt)
        with self.assertRaises(ProfileStoreError):
            onboard_profile(ProfileOnboardingInput(resume_filename="",resume_bytes=b"",display_name="New name"),profile_path=self.profile,private_dir=self.private)
        self.assertEqual(self.profile.read_bytes(),corrupt)

    def test_corrupt_database_initialize_preserves_original_bytes(self):
        path = self.private / "broken.sqlite3"
        corrupt = b'Synthetic broken database; must not reset'
        path.write_bytes(corrupt)
        with self.assertRaises(sqlite3.DatabaseError):
            JobRepository(path).initialize()
        self.assertEqual(path.read_bytes(),corrupt)


if __name__ == "__main__":
    unittest.main()
