import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PropagationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, code, parent_id=None):
        data = {"code": code, "origin": "Port-" + code, "destination": "Farm-" + code}
        if parent_id:
            data["parent_id"] = parent_id
        return self.service.create(self.actor, "consignment", data)

    def _chain(self, length=3):
        batch = []
        parent_id = None
        for index in range(length):
            entity = self._create("C-%d" % (index + 1), parent_id)
            batch.append(entity)
            parent_id = entity["id"]
        return batch

    def _inspect(self, entity):
        return self.service.transition(
            self.actor,
            entity["id"],
            "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )

    def _quarantine(self, entity, sample="S-1"):
        return self.service.transition(
            self.actor,
            entity["id"],
            "quarantine",
            {"pest_found": True, "sample_id": sample},
        )

    def test_unknown_parent_rejected(self):
        with self.assertRaises(ValidationError):
            self._create("C-X", parent_id="no-such-batch")

    def test_parent_cycle_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.actor,
                "consignment",
                {
                    "id": "batch-b",
                    "code": "C-B2",
                    "origin": "P",
                    "destination": "F",
                    "parent_id": "batch-b",
                },
            )

    def test_quarantine_blocks_descendants(self):
        root, child, grandchild = self._chain(3)
        self._inspect(root)
        self._inspect(child)
        self._quarantine(root)
        child = self.service.get(child["id"])
        grandchild = self.service.get(grandchild["id"])
        self.assertEqual(child["status"], "pending_review")
        self.assertEqual(grandchild["status"], "pending_review")
        self.assertEqual(child["data"]["blocked_by"], root["id"])
        self.assertEqual(child["data"]["blocked_by_code"], "C-1")
        self.assertEqual(child["data"]["pre_block_status"], "inspected")
        self.assertEqual(grandchild["data"]["pre_block_status"], "declared")
        self.assertIn("C-1", grandchild["data"]["block_reason"])

    def test_restore_after_recheck_and_destroy(self):
        root, child, grandchild = self._chain(3)
        self._inspect(root)
        self._quarantine(root)
        self.assertEqual(self.service.get(grandchild["id"])["status"], "pending_review")
        # source rechecked clean -> inspected, descendants restored level by level
        self.service.transition(
            self.actor, root["id"], "recheck", {"sample_id": "S-2"}
        )
        self.assertEqual(self.service.get(child["id"])["status"], "declared")
        self.assertEqual(self.service.get(grandchild["id"])["status"], "declared")
        self.assertNotIn(
            "blocked_by", self.service.get(grandchild["id"])["data"]
        )
        # quarantine again, then destroy -> descendants restored as well
        self._quarantine(root, sample="S-3")
        self.assertEqual(self.service.get(child["id"])["status"], "pending_review")
        self.service.transition(
            self.actor,
            root["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(self.service.get(child["id"])["status"], "declared")
        self.assertEqual(self.service.get(grandchild["id"])["status"], "declared")

    def test_intermediate_positive_keeps_lower_levels_blocked(self):
        root, child, grandchild = self._chain(3)
        self._inspect(root)
        self._inspect(child)
        self._quarantine(root)
        self._quarantine(self.service.get(child["id"]), sample="S-child")
        # root resolved, but child itself is quarantined
        self.service.transition(
            self.actor, root["id"], "recheck", {"sample_id": "S-2"}
        )
        child = self.service.get(child["id"])
        grandchild = self.service.get(grandchild["id"])
        self.assertEqual(child["status"], "quarantined")
        self.assertEqual(grandchild["status"], "pending_review")
        self.assertEqual(grandchild["data"]["blocked_by"], child["id"])
        # child recheck positive again -> quarantine keeps grandchild pending
        self.service.transition(
            self.actor, child["id"], "recheck", {"sample_id": "S-3"}
        )
        self._quarantine(self.service.get(child["id"]), sample="S-4")
        self.assertEqual(
            self.service.get(grandchild["id"])["status"], "pending_review"
        )
        # child finally destroyed -> grandchild restored
        self.service.transition(
            self.actor,
            child["id"],
            "destroy",
            {"method": "deep burial", "witnessed_by": "W-2"},
        )
        self.assertEqual(self.service.get(grandchild["id"])["status"], "declared")

    def test_restored_descendants_reblocked_on_new_positive(self):
        root, child, grandchild = self._chain(3)
        self._inspect(root)
        self._quarantine(root)
        self.service.transition(
            self.actor, root["id"], "recheck", {"sample_id": "S-2"}
        )
        self.assertEqual(self.service.get(grandchild["id"])["status"], "declared")
        # a restored descendant is found positive later -> its downstream re-blocked
        self._inspect(self.service.get(child["id"]))
        self._quarantine(self.service.get(child["id"]), sample="S-5")
        grandchild = self.service.get(grandchild["id"])
        self.assertEqual(grandchild["status"], "pending_review")
        self.assertEqual(grandchild["data"]["blocked_by"], child["id"])

    def test_timeline_keeps_block_and_restore_history(self):
        root, child, _ = self._chain(3)
        self._inspect(root)
        self._quarantine(root)
        self.service.transition(
            self.actor, root["id"], "recheck", {"sample_id": "S-2"}
        )
        timeline = self.service.audit_log(entity_id=child["id"])
        actions = [entry["action"] for entry in timeline]
        self.assertEqual(actions, ["create", "block", "restore"])
        block_entry = timeline[1]
        self.assertEqual(block_entry["actor_id"], "admin")
        self.assertTrue(block_entry["created_at"])
        self.assertEqual(block_entry["detail"]["blocked_by"], root["id"])
        self.assertIn("quarantined", block_entry["detail"]["reason"])
        restore_entry = timeline[2]
        self.assertEqual(restore_entry["to_status"], "declared")
        self.assertIn("resolved", restore_entry["detail"]["reason"])


if __name__ == "__main__":
    unittest.main()
