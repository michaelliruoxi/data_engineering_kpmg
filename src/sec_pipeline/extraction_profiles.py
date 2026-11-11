"""Explicit artifact identities; a set name never upgrades stored payloads."""

LEGACY_PROFILE = "document-set-v4"
CORRECTED_PROFILE = "corrected-layout-v5"
REVIEWED_PROFILE = "reviewed-layout-v6"
CORRECTED_PROFILES = frozenset({CORRECTED_PROFILE, REVIEWED_PROFILE})
_VERSIONS = {
    LEGACY_PROFILE: {
        "facts": "facts-v1", "tables": "tables-v4",
        "reports": "report-v1", "chunks": "paragraph-v1",
    },
    CORRECTED_PROFILE: {
        "facts": "facts-v1", "tables": "tables-v5",
        "reports": "report-v2", "chunks": "paragraph-v1",
    },
    REVIEWED_PROFILE: {
        "facts": "facts-v1", "tables": "tables-v6",
        "reports": "report-v3", "chunks": "paragraph-v2",
    },
}


def versions_for(profile):
    try:
        return dict(_VERSIONS[profile])
    except (KeyError, TypeError) as exc:
        raise ValueError("Unsupported document-set extraction profile.") from exc


def supported_versions(versions):
    return isinstance(versions, dict) and any(versions == value for value in _VERSIONS.values())


def corrected_tables(tables, *, profile=CORRECTED_PROFILE):
    """Allocate new table and issue identities without changing source evidence."""
    from copy import deepcopy
    from dataclasses import replace

    if profile not in CORRECTED_PROFILES:
        raise ValueError("Corrected tables require a corrected extraction profile.")
    version = versions_for(profile)["tables"]
    result = []
    for table in tables:
        if table.extraction_version != "tables-v4" or table.structure.get("schema_version") != 3:
            raise ValueError("Corrected tables require freshly verified schema3 output.")
        structure = deepcopy(table.structure)
        for row in structure["rows"]:
            for cell in row:
                if cell.get("quality_issues"):
                    for issue in cell["quality_issues"]:
                        issue["issue_key"] = version + ":" + issue["issue_key"]
                    cell["quality_issue_keys"] = [issue["issue_key"] for issue in cell["quality_issues"]]
        result.append(replace(table, extraction_version=version, structure=structure))
    return tuple(result)
