from __future__ import annotations

import unittest
from unittest.mock import patch

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

    def test_combinators_and_unsupported_keywords_are_not_ignored(self) -> None:
        document = {"schema": "fixture", "value": 2}
        schema = {
            "type": "object",
            "properties": {
                "value": {"oneOf": [{"const": 1}, {"minimum": 3}]},
            },
        }
        with patch("srhd_modkit.schemas.load_schema", return_value=schema):
            result = validate_schema_document(document)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "schema-oneof")

        with patch(
            "srhd_modkit.schemas.load_schema",
            return_value={"type": "object", "format": "unknown"},
        ):
            unsupported = validate_schema_document(document)
        self.assertFalse(unsupported["valid"])
        self.assertEqual(unsupported["errors"][0]["code"], "schema-keyword-unsupported")

        with patch(
            "srhd_modkit.schemas.load_schema",
            return_value={
                "type": "object",
                "properties": {
                    "value": {
                        "anyOf": [
                            {"type": "integer", "format": "hidden-in-branch"},
                            {"type": "string"},
                        ]
                    }
                },
            },
        ):
            nested = validate_schema_document(document)
        self.assertFalse(nested["valid"])
        self.assertIn("schema-keyword-unsupported", {item["code"] for item in nested["errors"]})
        self.assertNotIn("schema-anyof", {item["code"] for item in nested["errors"]})


if __name__ == "__main__":
    unittest.main()
