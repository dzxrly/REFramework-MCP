# Versioned schemas

The Python annotations and Pydantic models are the source of the C2 1.0.0
contract. The schema export script produces one JSON bundle containing all 18
Tool input/output schemas, Bridge envelopes, AccessPlan 1.0, stable references,
and the snapshot manifest.

The SHA-256 file freezes the canonical bundle. CI checks the digest. The
Package workflow creates tool-contracts-v1.json and uploads it as a GitHub
Actions artifact; generated package files are not maintained by hand.
