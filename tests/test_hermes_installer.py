from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hermes_installer", ROOT / "installer.py"
)
assert SPEC and SPEC.loader
installer_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer_module
SPEC.loader.exec_module(installer_module)

ADD_CONTENT_SPEC = importlib.util.spec_from_file_location(
    "add_content", ROOT / "add_content.py"
)
assert ADD_CONTENT_SPEC and ADD_CONTENT_SPEC.loader
add_content_module = importlib.util.module_from_spec(ADD_CONTENT_SPEC)
sys.modules[ADD_CONTENT_SPEC.name] = add_content_module
ADD_CONTENT_SPEC.loader.exec_module(add_content_module)


class FakeState:
    def __init__(self, **values: Any):
        self.data = dict(values)

    def update(self, **values: Any) -> None:
        self.data.update(values)


class FakeS3:
    def __init__(self):
        self.versioning: dict[str, Any] | None = None

    def head_bucket(self, **parameters: Any) -> None:
        return None

    def get_bucket_tagging(self, **parameters: Any) -> dict[str, Any]:
        return {
            "TagSet": [
                {"Key": "ManagedBy", "Value": installer_module.MANAGED_BY},
                {"Key": "Project", "Value": "hermes"},
            ]
        }

    def put_bucket_tagging(self, **parameters: Any) -> None:
        return None

    def put_public_access_block(self, **parameters: Any) -> None:
        return None

    def put_bucket_encryption(self, **parameters: Any) -> None:
        return None

    def put_bucket_ownership_controls(self, **parameters: Any) -> None:
        return None

    def put_bucket_versioning(self, **parameters: Any) -> None:
        self.versioning = parameters


class FakeEC2:
    def __init__(self, permissions: list[dict[str, Any]] | None = None):
        self.permissions = permissions or []
        self.revoked: list[dict[str, Any]] = []

    def describe_managed_prefix_lists(
        self, **parameters: Any
    ) -> dict[str, Any]:
        return {"PrefixLists": []}

    def describe_security_groups(
        self, **parameters: Any
    ) -> dict[str, Any]:
        return {
            "SecurityGroups": [{
                "GroupId": "sg-alb",
                "IpPermissions": self.permissions,
            }]
        }

    def revoke_security_group_ingress(
        self, **parameters: Any
    ) -> None:
        self.revoked.extend(parameters["IpPermissions"])


class FakeELB:
    def __init__(self):
        self.created: dict[str, Any] | None = None

    def describe_listeners(self, **parameters: Any) -> dict[str, Any]:
        return {"Listeners": []}

    def create_listener(self, **parameters: Any) -> dict[str, Any]:
        self.created = parameters
        return {
            "Listeners": [{
                "ListenerArn": "listener",
                "Protocol": parameters["Protocol"],
                "Port": parameters["Port"],
            }]
        }


class FakeCloudFront:
    def __init__(self):
        self.created: dict[str, Any] | None = None
        self.vpc_origin_created: dict[str, Any] | None = None
        self.vpc_origin_status = "Deployed"

    def create_distribution_with_tags(self, **parameters: Any) -> dict[str, Any]:
        self.created = parameters
        return {
            "Distribution": {
                "Id": "DISTRIBUTION",
                "DomainName": "example.cloudfront.net",
            }
        }

    def create_vpc_origin(self, **parameters: Any) -> dict[str, Any]:
        self.vpc_origin_created = parameters
        return {
            "VpcOrigin": {
                "Id": "VPCORIGIN",
                "Arn": "arn:aws:cloudfront::123456789012:vpcorigin/VPCORIGIN",
                "Status": "Deploying",
                "VpcOriginEndpointConfig": parameters[
                    "VpcOriginEndpointConfig"
                ],
            }
        }

    def get_vpc_origin(self, **parameters: Any) -> dict[str, Any]:
        config = (
            self.vpc_origin_created["VpcOriginEndpointConfig"]
            if self.vpc_origin_created
            else {"Arn": "alb-arn"}
        )
        return {
            "VpcOrigin": {
                "Id": parameters["Id"],
                "Arn": (
                    "arn:aws:cloudfront::123456789012:vpcorigin/"
                    + parameters["Id"]
                ),
                "Status": self.vpc_origin_status,
                "VpcOriginEndpointConfig": config,
            },
            "ETag": "etag",
        }

    def list_vpc_origins(self, **parameters: Any) -> dict[str, Any]:
        return {"VpcOriginList": {"IsTruncated": False, "Items": []}}


class HermesInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = installer_module.HermesInstaller.__new__(
            installer_module.HermesInstaller
        )
        self.installer.region = "us-west-2"
        self.installer.project = "hermes"
        self.installer.deployment_id = "deployment"
        self.installer.account_id = "123456789012"
        self.installer.state = FakeState()

    def test_render_config_uses_nous_oauth_without_basic_auth(self) -> None:
        rendered = self.installer._render_hermes_config(
            model_id=installer_module.DEFAULT_MODEL_ID,
            dashboard_oauth_client_id="agent:test",
            dashboard_public_url="https://example.cloudfront.net",
        )
        config = json.loads(rendered)

        self.assertEqual(config["dashboard"]["oauth"]["client_id"], "agent:test")
        self.assertEqual(
            config["dashboard"]["public_url"],
            "https://example.cloudfront.net",
        )
        self.assertNotIn("basic_auth", config["dashboard"])

    def test_pending_user_data_keeps_dashboard_disabled(self) -> None:
        rendered = self.installer._render_user_data(
            dashboard_oauth_client_id=None,
            dashboard_public_url=None,
            model_id=installer_module.DEFAULT_MODEL_ID,
            install_browser=False,
        )

        self.assertIn('"client_id": ""', rendered)
        self.assertIn(
            "systemctl disable --now hermes-dashboard.service", rendered
        )
        self.assertNotIn("password_hash", rendered)

    def test_ssm_migration_sets_oauth_and_removes_basic_provider(self) -> None:
        captured: dict[str, Any] = {}

        def capture(instance_id: str, **parameters: Any) -> None:
            captured["instance_id"] = instance_id
            captured.update(parameters)

        self.installer._run_ssm_commands = capture
        self.installer._configure_dashboard_oauth_via_ssm(
            "i-1234567890abcdef0",
            oauth_client_id="agent:test",
            public_url="https://example.cloudfront.net",
        )
        commands = "\n".join(captured["commands"])

        self.assertIn("dashboard.oauth.client_id agent:test", commands)
        self.assertIn(
            "dashboard.public_url https://example.cloudfront.net", commands
        )
        self.assertIn("dashboard.basic_auth.username ''", commands)
        self.assertIn("HERMES_DASHBOARD_BASIC_AUTH_", commands)
        self.assertIn("systemctl restart hermes-dashboard.service", commands)

    def test_cloudfront_uses_vpc_origin_and_forwarded_scheme(self) -> None:
        cloudfront = FakeCloudFront()
        self.installer.cf = cloudfront
        self.installer._find_cloudfront_distribution = lambda: None

        result = self.installer._ensure_cloudfront(
            "internal-alb.us-west-2.elb.amazonaws.com",
            "VPCORIGIN",
            "origin-secret",
        )
        self.assertEqual(result["domain_name"], "example.cloudfront.net")
        assert cloudfront.created
        distribution = cloudfront.created["DistributionConfigWithTags"][
            "DistributionConfig"
        ]
        origin = distribution["Origins"]["Items"][0]
        self.assertEqual(
            origin["DomainName"],
            "internal-alb.us-west-2.elb.amazonaws.com",
        )
        self.assertEqual(
            origin["VpcOriginConfig"]["VpcOriginId"], "VPCORIGIN"
        )
        self.assertNotIn("CustomOriginConfig", origin)
        headers = origin["CustomHeaders"]["Items"]
        header_map = {
            item["HeaderName"]: item["HeaderValue"] for item in headers
        }
        self.assertEqual(header_map["X-Forwarded-Proto"], "https")
        self.assertEqual(
            header_map[installer_module.CUSTOM_HEADER_NAME], "origin-secret"
        )

    def test_vpc_origin_targets_alb_with_http_only(self) -> None:
        cloudfront = FakeCloudFront()
        self.installer.cf = cloudfront

        result = self.installer._ensure_vpc_origin("alb-arn")

        self.assertEqual(result["vpc_origin_id"], "VPCORIGIN")
        assert cloudfront.vpc_origin_created
        config = cloudfront.vpc_origin_created["VpcOriginEndpointConfig"]
        self.assertEqual(config["Arn"], "alb-arn")
        self.assertEqual(config["OriginProtocolPolicy"], "http-only")
        self.assertEqual(config["HTTPPort"], 80)

    def test_vpc_origin_refuses_foreign_endpoint(self) -> None:
        cloudfront = FakeCloudFront()
        self.installer.cf = cloudfront
        self.installer.state = FakeState(vpc_origin_id="VPCORIGIN")

        with self.assertRaisesRegex(RuntimeError, "다른"):
            self.installer._ensure_vpc_origin("other-alb-arn")

    def test_legacy_route53_state_is_rejected(self) -> None:
        self.installer.state = FakeState(
            origin_domain_name="origin-hermes.example.com",
            route53_hosted_zone_id="Z123ABC",
        )

        with self.assertRaisesRegex(RuntimeError, "Route 53"):
            self.installer._reject_legacy_route53_deployment()

    def test_rejects_non_portal_client_id(self) -> None:
        with self.assertRaises(ValueError):
            self.installer._resolve_dashboard_auth(
                "not-an-agent-id",
                model_id=installer_module.DEFAULT_MODEL_ID,
                existing_instance=None,
            )

    def test_explicit_client_id_rotates_stored_registration(self) -> None:
        self.installer.state = FakeState(
            dashboard_oauth_client_id="agent:old",
            origin_header="origin-secret",
        )
        resolved = self.installer._resolve_dashboard_auth(
            "agent:new",
            model_id=installer_module.DEFAULT_MODEL_ID,
            existing_instance=None,
        )

        self.assertEqual(resolved["oauth_client_id"], "agent:new")
        self.assertEqual(
            self.installer.state.data["dashboard_oauth_client_id"],
            "agent:new",
        )

    def test_alb_listener_uses_plain_http_for_vpc_origin(self) -> None:
        elb = FakeELB()
        self.installer.elbv2 = elb
        self.installer._ensure_origin_header_rule = (
            lambda *args, **kwargs: None
        )

        listener = self.installer._ensure_listener(
            "alb-arn",
            "target-group-arn",
            "origin-secret",
            enable_cloudfront=True,
        )

        self.assertEqual(listener["Protocol"], "HTTP")
        assert elb.created
        self.assertEqual(elb.created["Port"], 80)
        self.assertNotIn("Certificates", elb.created)

    def test_aoss_private_policy_only_allows_bedrock(self) -> None:
        policy = self.installer._aoss_network_policy(public=False)

        self.assertEqual(policy[0]["AllowFromPublic"], False)
        self.assertEqual(
            policy[0]["SourceServices"], ["bedrock.amazonaws.com"]
        )
        self.assertEqual(
            policy[0]["Rules"],
            [{
                "ResourceType": "collection",
                "Resource": ["collection/hermes-kb"],
            }],
        )

    def test_aoss_private_policy_is_restored_after_index_failure(self) -> None:
        policy_changes: list[bool] = []
        self.installer._set_aoss_index_management_access = (
            lambda *, public, knowledge_base_role_arn: (
                policy_changes.append(public)
            )
        )

        def fail_index(endpoint: str, index_name: str) -> None:
            raise RuntimeError("index failed")

        self.installer._ensure_vector_index = fail_index
        with self.assertRaisesRegex(RuntimeError, "index failed"):
            self.installer._ensure_vector_index_with_temporary_access(
                "https://collection.example.com",
                "index",
                "arn:aws:iam::123456789012:role/kb",
            )

        self.assertEqual(policy_changes, [True, False])

    def test_aoss_final_data_policy_excludes_installer(self) -> None:
        self.installer.caller_principal_arn = (
            "arn:aws:iam::123456789012:role/installer"
        )
        role_arn = "arn:aws:iam::123456789012:role/kb"

        final_policy = self.installer._aoss_data_access_policy(
            role_arn, include_installer=False
        )
        temporary_policy = self.installer._aoss_data_access_policy(
            role_arn, include_installer=True
        )

        self.assertEqual(final_policy[0]["Principal"], [role_arn])
        self.assertEqual(len(final_policy), 1)
        self.assertEqual(
            temporary_policy[1]["Principal"],
            [self.installer.caller_principal_arn],
        )

    def test_existing_instance_bootstrap_preserves_config(self) -> None:
        captured: dict[str, Any] = {}

        def capture(instance_id: str, **parameters: Any) -> None:
            captured["instance_id"] = instance_id
            captured.update(parameters)

        self.installer._run_ssm_commands = capture
        self.installer._reconcile_instance_bootstrap_via_ssm(
            "i-1234567890abcdef0",
            model_id=installer_module.DEFAULT_MODEL_ID,
            install_browser=False,
        )
        commands = "\n".join(captured["commands"])

        self.assertIn("if ! sudo -u ec2-user", commands)
        self.assertIn(
            "https://hermes-agent.nousresearch.com/install.sh", commands
        )
        self.assertIn("config set model.provider bedrock", commands)
        self.assertIn("hermes-dashboard.service", commands)
        self.assertNotIn("cat > /home/ec2-user/.hermes/config.yaml", commands)

    def test_cloudfront_prefix_list_lookup_fails_closed(self) -> None:
        self.installer.ec2 = FakeEC2()

        with self.assertRaises(RuntimeError):
            self.installer._cloudfront_prefix_list_id()

    def test_knowledge_bucket_enables_versioning(self) -> None:
        s3 = FakeS3()
        self.installer.s3 = s3
        self.installer.state = FakeState(knowledge_base_bucket="bucket")

        bucket = self.installer._ensure_knowledge_base_bucket()

        self.assertEqual(bucket, "bucket")
        assert s3.versioning
        self.assertEqual(
            s3.versioning["VersioningConfiguration"],
            {"Status": "Enabled"},
        )

    def test_legacy_source_hash_metadata_is_read(self) -> None:
        class LegacyMetadataS3:
            def head_object(
                self, **parameters: Any
            ) -> dict[str, Any]:
                return {"Metadata": {"source-sha256": "legacy-digest"}}

        self.assertEqual(
            add_content_module.remote_sha256(
                LegacyMetadataS3(), "bucket", "docs/example.md"
            ),
            "legacy-digest",
        )

    def test_new_upload_uses_canonical_sha256_metadata(self) -> None:
        class CapturingS3:
            def __init__(self):
                self.parameters: dict[str, Any] | None = None

            def put_object(self, **parameters: Any) -> None:
                self.parameters = parameters

        s3 = CapturingS3()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.md"
            path.write_text("example", encoding="utf-8")
            add_content_module.upload_document(
                s3,
                path=path,
                bucket="bucket",
                key="docs/example.md",
                digest="new-digest",
            )

        assert s3.parameters
        self.assertEqual(
            s3.parameters["Metadata"], {"sha256": "new-digest"}
        )


if __name__ == "__main__":
    unittest.main()
