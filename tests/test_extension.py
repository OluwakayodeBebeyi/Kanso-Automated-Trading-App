import json
from pathlib import Path


def test_extension_manifest_is_local_read_only_only():
    manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["host_permissions"] == ["http://127.0.0.1:8766/*"]
    assert "tabs" not in manifest.get("permissions", [])
    assert "<all_urls>" not in manifest["host_permissions"]
