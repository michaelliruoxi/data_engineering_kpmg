"""Explicit artifact identities; a set name never upgrades stored payloads."""

LEGACY_PROFILE = "document-set-v4"
CORRECTED_PROFILE = "corrected-layout-v5"
_VERSIONS = {
    LEGACY_PROFILE: {
        "facts": "facts-v1", "tables": "tables-v4",
        "reports": "report-v1", "chunks": "paragraph-v1",
    },
    CORRECTED_PROFILE: {
        "facts": "facts-v1", "tables": "tables-v5",
        "reports": "report-v2", "chunks": "paragraph-v1",
    },
}


def versions_for(profile):
    try:
        return dict(_VERSIONS[profile])
    except (KeyError, TypeError) as exc:
        raise ValueError("Unsupported document-set extraction profile.") from exc


def supported_versions(versions):
    return isinstance(versions, dict) and any(versions == value for value in _VERSIONS.values())


def corrected_tables(tables):
    """Allocate new table and issue identities without changing source evidence."""
    from copy import deepcopy
    from dataclasses import replace

    result = []
    for table in tables:
        if table.extraction_version != "tables-v4" or table.structure.get("schema_version") != 3:
            raise ValueError("Corrected tables require freshly verified schema3 output.")
        structure = deepcopy(table.structure)
        for row in structure["rows"]:
            for cell in row:
                if cell.get("quality_issues"):
                    for issue in cell["quality_issues"]:
                        issue["issue_key"] = "tables-v5:" + issue["issue_key"]
                    cell["quality_issue_keys"] = [issue["issue_key"] for issue in cell["quality_issues"]]
        result.append(replace(table, extraction_version="tables-v5", structure=structure))
    return tuple(result)
