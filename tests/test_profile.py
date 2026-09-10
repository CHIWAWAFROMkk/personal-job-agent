import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from job_agent.models.profile import Profile
from job_agent.services.profile_store import ProfileStoreError, load_profile, save_profile
from tests.helpers import sample_profile


class ProfileTests(unittest.TestCase):
    def test_application_context_excludes_contact_and_unconfirmed_facts(self) -> None:
        context = sample_profile().application_context()

        self.assertNotIn("person", context)
        facts = context["experiences"][0]["facts"]
        self.assertEqual([fact["id"] for fact in facts], ["fact-sql-analysis"])
        self.assertEqual([skill["name"] for skill in context["skills"]], ["SQL"])
        self.assertNotIn("private@example.com", json.dumps(context))

    def test_unknown_fact_reference_is_rejected(self) -> None:
        payload = sample_profile().model_dump(mode="json")
        payload["skills"][0]["evidence_fact_ids"] = ["fact-does-not-exist"]

        with self.assertRaises(ValidationError):
            Profile.model_validate(payload)

    def test_store_refuses_unrequested_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            save_profile(sample_profile(), path)

            with self.assertRaises(ProfileStoreError):
                save_profile(sample_profile(), path)
            loaded = load_profile(path)
            self.assertEqual(loaded.skills[0].name, "SQL")

    def test_confirm_all_pending_claims(self) -> None:
        confirmed, count = sample_profile().confirm_all_pending_claims()

        self.assertEqual(count, 2)
        context = confirmed.application_context()
        fact_ids = [
            fact["id"]
            for experience in context["experiences"]
            for fact in experience["facts"]
        ]
        self.assertIn("fact-pending-tableau", fact_ids)
        self.assertIn("Tableau", [skill["name"] for skill in context["skills"]])


if __name__ == "__main__":
    unittest.main()
