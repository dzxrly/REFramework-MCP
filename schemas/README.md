# Versioned schemas

scripts/export_schemas.py writes the MCP, Bridge, access-plan, and snapshot
schemas to one JSON bundle. tool-contracts-v1.sha256 locks the canonical output;
update both files when a public contract changes.
