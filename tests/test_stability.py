"""离线检查记录格式，不将输入声明升级为硬件验收。"""
import copy
import unittest

from embedded_auto.firmware.stability import evaluate_records


class StabilityRecordTests(unittest.TestCase):
    def records(self):
        return [{"run_id": f"fake-{n}", "target_id": "fake", "device_id": "fake-board",
                 "sha256": "a" * 64, "erase_start": 0x08000000, "erase_size": 1024,
                 "duration_ms": 100, "success": True, "verified": True,
                 "boot_confirmed": True, "cleanup_succeeded": True} for n in range(20)]

    def test_well_formed_records_are_not_hardware_evidence(self):
        result = evaluate_records(self.records())
        self.assertTrue(result.success)
        self.assertTrue(result.data["record_checks_passed"])
        self.assertFalse(result.data["hardware_stability_verified"])

    def test_missing_duplicate_mismatch_and_failure_stop(self):
        cases = []
        records = self.records()
        cases.append(records[:-1])
        for key, value in (("run_id", records[0]["run_id"]), ("sha256", "b" * 64),
                           ("cleanup_succeeded", False), ("duration_ms", -1)):
            candidate = copy.deepcopy(records)
            candidate[1][key] = value
            cases.append(candidate)
        for records in cases:
            with self.subTest(records=records[1]):
                self.assertFalse(evaluate_records(records).success)


if __name__ == "__main__":
    unittest.main()
