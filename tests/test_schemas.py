from __future__ import annotations

import unittest

from srhd_modkit.schemas import list_schemas, validate_schema_document


class SchemaTests(unittest.TestCase):
    def test_audit_schema_accepts_core_report_and_rejects_missing_field(self) -> None:
        document = {
            "schema": "srhd-modkit-audit-v1",
            "target": "fixture",
            "profile": "dev",
            "coverage_complete": True,
            "summary": {},
            "checks": [],
            "issues": [],
        }
        self.assertTrue(validate_schema_document(document)["valid"])
        del document["target"]
        result = validate_schema_document(document)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "schema-required")

    def test_public_contract_schemas_are_packaged(self) -> None:
        names = {item["name"] for item in list_schemas()}
        self.assertTrue(
            {
                "srhd-modkit-audit-v1",
                "srhd-modkit-release-v1",
                "srhd-modkit-project-v1",
                "srhd-modkit-modset-v1",
            }.issubset(names)
        )


if __name__ == "__main__":
    unittest.main()
