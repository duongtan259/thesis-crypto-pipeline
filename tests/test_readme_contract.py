import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_local_links_resolve() -> None:
    readme = (ROOT / "README.md").read_text()
    local_targets = [
        target
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
        if "://" not in target
    ]

    assert local_targets
    assert [target for target in local_targets if not (ROOT / target).exists()] == []


def test_readme_records_the_controlled_cloud_reconciliation() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "cloud_e2e_validation_20260905.json" in readme
    assert "16,000" in readme
    assert "Bronze and Silver" in readme


def test_readme_does_not_claim_the_legacy_fabric_guide_has_private_steps() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "managed private endpoint, and dashboard steps are documented" not in readme


def test_readme_uses_the_actual_compose_service_name() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "--profile local up generator-local" in readme
