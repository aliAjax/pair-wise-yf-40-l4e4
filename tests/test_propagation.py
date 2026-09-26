import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PropagationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.officer = Actor("quar-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _declare(self, code, parent_id=None):
        data = {"code": code, "origin": "Port-" + code, "destination": "Farm-" + code}
        if parent_id:
            data["parent_id"] = parent_id
        return self.service.create(self.admin, "consignment", data)

    def _inspect(self, entity):
        return self.service.transition(
            self.admin, entity["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )

    def _quarantine(self, entity, sample="S-1"):
        return self.service.transition(
            self.officer, entity["id"], "quarantine",
            {"pest_found": True, "sample_id": sample},
        )

    def _release(self, entity):
        return self.service.transition(
            self.officer, entity["id"], "release",
            {"pest_found": False, "treatment": "certified"},
        )

    def _chain(self):
        a = self._inspect(self._declare("A"))
        b = self._inspect(self._declare("B", parent_id=a["id"]))
        c = self._inspect(self._declare("C", parent_id=b["id"]))
        return a, b, c

    def test_declare_registers_direct_source(self):
        a = self._declare("A")
        b = self._declare("B", parent_id=a["id"])
        self.assertEqual(b["data"]["parent_id"], a["id"])
        with self.assertRaises(ValidationError):
            self._declare("X", parent_id="missing-batch")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "consignment",
                {"id": "self-ref", "code": "S", "origin": "A", "destination": "B",
                 "parent_id": "self-ref"},
            )

    def test_quarantine_blocks_descendants_with_blocking_batch(self):
        a, b, c = self._chain()
        self._quarantine(a)
        for held in (b, c):
            current = self.service.get(held["id"])
            self.assertEqual(current["status"], "pending_review")
            self.assertEqual(current["data"]["blocked_by"], a["id"])
            self.assertEqual(current["data"]["block_reason"], "source_quarantined")
            self.assertEqual(current["data"]["resume_status"], "inspected")
        with self.assertRaises(InvalidTransition):
            self._inspect(self.service.get(b["id"]))

    def test_release_restores_level_by_level(self):
        a, b, c = self._chain()
        self._quarantine(a)
        self.service.transition(self.admin, a["id"], "recheck", {"sample_id": "S-2"})
        self._release(self.service.get(a["id"]))
        for restored in (b, c):
            current = self.service.get(restored["id"])
            self.assertEqual(current["status"], "inspected")
            self.assertNotIn("blocked_by", current["data"])
            self.assertNotIn("resume_status", current["data"])

    def test_destroy_source_restores_descendants(self):
        a, b, c = self._chain()
        self._quarantine(a)
        self.service.transition(
            self.officer, a["id"], "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(self.service.get(b["id"])["status"], "inspected")
        self.assertEqual(self.service.get(c["id"])["status"], "inspected")

    def test_restore_stops_below_still_quarantined_level(self):
        a, b, c = self._chain()
        self._quarantine(b)
        self.assertEqual(self.service.get(c["id"])["data"]["blocked_by"], b["id"])
        self._quarantine(a)
        self.service.transition(self.admin, a["id"], "recheck", {"sample_id": "S-9"})
        self._release(self.service.get(a["id"]))
        self.assertEqual(self.service.get(b["id"])["status"], "quarantined")
        self.assertEqual(self.service.get(c["id"])["status"], "pending_review")
        self.service.transition(self.admin, b["id"], "recheck", {"sample_id": "S-10"})
        self._release(self.service.get(b["id"]))
        self.assertEqual(self.service.get(c["id"])["status"], "inspected")

    def test_repositive_reblocks_restored_descendants(self):
        a, b, c = self._chain()
        self._quarantine(a)
        self.service.transition(self.admin, a["id"], "recheck", {"sample_id": "S-2"})
        self._release(self.service.get(a["id"]))
        self.assertEqual(self.service.get(c["id"])["status"], "inspected")
        self._quarantine(self.service.get(b["id"]), sample="S-3")
        held = self.service.get(c["id"])
        self.assertEqual(held["status"], "pending_review")
        self.assertEqual(held["data"]["blocked_by"], b["id"])
        self.assertEqual(held["data"]["resume_status"], "inspected")

    def test_declared_under_held_source_is_blocked_immediately(self):
        a = self._inspect(self._declare("A"))
        self._quarantine(a)
        b = self._declare("B", parent_id=a["id"])
        self.assertEqual(b["status"], "pending_review")
        self.assertEqual(b["data"]["blocked_by"], a["id"])
        self.assertEqual(b["data"]["resume_status"], "declared")
        self.service.transition(self.admin, a["id"], "recheck", {"sample_id": "S-2"})
        self._release(self.service.get(a["id"]))
        self.assertEqual(self.service.get(b["id"])["status"], "declared")

    def test_timeline_keeps_block_and_restore_history(self):
        a, b, c = self._chain()
        self._quarantine(a)
        self.service.transition(self.admin, a["id"], "recheck", {"sample_id": "S-2"})
        self._release(self.service.get(a["id"]))
        timeline = self.service.audit_log(entity_id=b["id"])
        blocks = [entry for entry in timeline if entry["action"] == "block"]
        restores = [entry for entry in timeline if entry["action"] == "restore"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["actor_id"], "quar-1")
        self.assertEqual(blocks[0]["detail"]["blocked_by"], a["id"])
        self.assertEqual(blocks[0]["detail"]["reason"], "source_quarantined")
        self.assertTrue(blocks[0]["created_at"])
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0]["detail"]["reason"], "source_released")
        self.assertEqual(restores[0]["detail"]["unblocked_by"], a["id"])
        self.assertEqual(restores[0]["to_status"], "inspected")


if __name__ == "__main__":
    unittest.main()
