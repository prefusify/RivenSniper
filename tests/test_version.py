import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_version_sources_stay_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project["project"]["version"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package for package in lock["package"] if package["name"] == "rivensniper"
    )
    version_source = (
        ROOT / "src" / "plugins" / "riven_sniper" / "version.py"
    ).read_text(encoding="utf-8")
    runtime_version = re.fullmatch(r'VERSION = "([^"]+)"\s*', version_source).group(1)

    assert project_version == "9.0.2"
    assert locked_project["version"] == project_version
    assert runtime_version == project_version
    assert project["project"]["license"] == "MIT"
