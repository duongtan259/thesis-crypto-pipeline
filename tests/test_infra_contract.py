from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_private_endpoints_have_private_dns_zone_groups() -> None:
    bicep = (ROOT / "infra" / "main.bicep").read_text()

    assert "privatelink.servicebus.windows.net" in bicep
    assert "privatelink.vaultcore.azure.net" in bicep
    assert bicep.count("privateDnsZoneGroups") >= 2
    assert bicep.count("virtualNetworkLinks") >= 2


def test_aci_workflow_uses_managed_identity_and_private_subnet() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    assert "--registry-password" not in workflow
    assert "EVENTHUB_CONNECTION_STRING" not in workflow
    assert "--assign-identity" in workflow
    assert "--acr-identity" in workflow
    assert "--subnet" in workflow
    assert "EVENTHUB_NAMESPACE" in workflow


def test_deployment_requires_manual_dispatch_to_preserve_cost_shutdown() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    trigger_block = workflow.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in trigger_block
    assert "\n  push:" not in trigger_block


def test_setup_does_not_write_to_a_private_key_vault_from_the_callers_network() -> None:
    setup = (ROOT / "scripts" / "setup_azure.sh").read_text()

    assert "az keyvault secret set" not in setup
    assert "eventhub-namespace" not in setup


def test_connection_string_mode_does_not_claim_automatic_key_vault_fetch() -> None:
    paths = [
        ROOT / "generator" / "config.py",
        ROOT / "generator" / "main.py",
        ROOT / "generator" / "publisher" / "eventhub.py",
    ]
    text = "\n".join(path.read_text() for path in paths)

    assert "from Key Vault" not in text
    assert "fetched at startup" not in text


def test_example_environment_exposes_both_supported_event_hub_auth_modes() -> None:
    example = (ROOT / ".env.example").read_text()

    assert "EVENTHUB_CONNECTION_STRING=" in example
    assert "EVENTHUB_NAMESPACE=" in example
