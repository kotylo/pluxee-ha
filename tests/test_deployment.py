"""Verify the deployment shell script keeps backups out of HA discovery."""

import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

from homeassistant.loader import _get_custom_components


async def test_installer_preserves_backups_without_shadowing_integration(
    hass, tmp_path, monkeypatch
):
    """A dot-prefixed backup still has a discoverable manifest in HA."""
    components = tmp_path / "custom_components"
    staging = components / ".pluxee-staging-test"
    backups = tmp_path / ".pluxee-deployments"
    destination_backup = backups / "backup-test"
    legacy_name = ".pluxee-backup-test"

    def component(path, version):
        path.mkdir(parents=True)
        (path / "__init__.py").write_text("", encoding="utf-8")
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "domain": "pluxee",
                    "name": "Pluxee",
                    "version": version,
                    "documentation": "https://example.com/pluxee",
                    "codeowners": [],
                    "requirements": [],
                    "dependencies": [],
                }
            ),
            encoding="utf-8",
        )

    component(components / "pluxee", "0.3.1")
    component(components / legacy_name, "0.3.0")
    component(staging / "pluxee", "0.3.2")

    source = (Path(__file__).parents[1] / "copy-to-server.ps1").read_text(
        encoding="utf-8"
    )
    install_script = re.search(r"\$installScript = @'\n(.*?)\n'@", source, re.S)
    assert install_script is not None
    # Execute the actual installer, with every remote path in pytest's temp dir.
    subprocess.run(
        ["sh", "-s", "--", str(components), str(staging), str(destination_backup)],
        input=install_script[1],
        text=True,
        capture_output=True,
        check=True,
    )

    assert (backups / legacy_name / "manifest.json").is_file()
    assert json.loads((destination_backup / "manifest.json").read_text())["version"] == "0.3.1"
    assert not staging.exists()
    assert not (components / legacy_name).exists()

    # Use HA's actual discovery rather than assuming hidden folders are ignored.
    root = ModuleType("custom_components")
    root.__path__ = [str(components)]
    monkeypatch.setitem(sys.modules, "custom_components", root)
    integrations = await hass.async_add_executor_job(_get_custom_components, hass)
    assert integrations["pluxee"].pkg_path == "custom_components.pluxee"
    assert integrations["pluxee"].manifest["version"] == "0.3.2"
