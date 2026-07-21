#!/usr/bin/env python3
"""Deploy a standalone Hermes Agent stack on AWS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shlex
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.exceptions import ClientError, NoCredentialsError
except ModuleNotFoundError as exc:  # Keep --help available before dependencies are installed.
    boto3 = None  # type: ignore[assignment]
    SigV4Auth = None  # type: ignore[assignment,misc]
    AWSRequest = None  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]
    NoCredentialsError = Exception  # type: ignore[assignment,misc]
    BOTO_IMPORT_ERROR: Optional[ModuleNotFoundError] = exc
else:
    BOTO_IMPORT_ERROR = None


PROJECT_NAME = "hermes"
REGION = "us-west-2"
INSTANCE_TYPE = "t3.medium"
DASHBOARD_PORT = 9119
VOLUME_SIZE = 40
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
DEFAULT_VPC_CIDRS = tuple(f"10.{octet}.0.0/16" for octet in range(20, 31))
AMI_PARAMETER = (
    "/aws/service/ami-amazon-linux-latest/"
    "al2023-ami-kernel-default-x86_64"
)
MANAGED_BY = "hermes-agent-installer"
CUSTOM_HEADER_NAME = "X-Origin-Verify-Hermes"
STATE_PATH = Path("assets/hermes-deployment.json")
DEPLOYMENT_INFO_PATH = Path("assets/hermes-deployment-info.md")
CONTENTS_PATH = Path("contents")
RETRIEVE_SKILL_PATH = Path("skills/retrieve")
CLOUDFRONT_CACHE_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CLOUDFRONT_ALL_VIEWER_EXCEPT_HOST_POLICY_ID = (
    "b689b0a8-53d0-40ab-baf2-68738e2966ac"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _require_boto3() -> None:
    if BOTO_IMPORT_ERROR is not None:
        raise SystemExit(
            "boto3가 필요합니다. 먼저 `python -m pip install -r requirements.txt`를 실행하세요."
        )


def _error_code(exc: Exception) -> str:
    try:
        return exc.response["Error"]["Code"]  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError):
        return ""


def _resource_tags(
    project: str,
    deployment_id: str,
    *,
    name: Optional[str] = None,
    extra: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    tags = {
        "ManagedBy": MANAGED_BY,
        "Project": project,
        "DeploymentId": deployment_id,
    }
    if name:
        tags["Name"] = name
    if extra:
        tags.update(extra)
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def _tag_map(tags: Iterable[Dict[str, str]]) -> Dict[str, str]:
    return {
        tag["Key"]: tag["Value"]
        for tag in tags
        if tag.get("Key") is not None and tag.get("Value") is not None
    }


def _resource_tag_map(
    project: str, deployment_id: str, *, name: Optional[str] = None
) -> Dict[str, str]:
    return _tag_map(
        _resource_tags(project, deployment_id, name=name)
    )


def _aoss_resource_tags(
    project: str, deployment_id: str, *, name: Optional[str] = None
) -> List[Dict[str, str]]:
    return [
        {"key": item["Key"], "value": item["Value"]}
        for item in _resource_tags(
            project, deployment_id, name=name
        )
    ]


def _aoss_principal_arn(caller_arn: str) -> str:
    """Convert an STS assumed-role ARN to the IAM role ARN AOSS accepts."""
    marker = ":assumed-role/"
    if marker not in caller_arn:
        return caller_arn
    prefix, resource = caller_arn.split(marker, 1)
    role_name = resource.rsplit("/", 1)[0]
    partition, account_id = prefix.split(":")[1], prefix.split(":")[4]
    return f"arn:{partition}:iam::{account_id}:role/{role_name}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_project_name(project: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,18}[a-z0-9]", project):
        raise ValueError(
            "--project-name은 소문자로 시작하는 3-20자의 영문 소문자, 숫자, "
            "하이픈 조합이어야 합니다."
        )
    return project


class DeploymentState:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"배포 상태 파일을 읽을 수 없습니다: {path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(f"배포 상태 파일 형식이 올바르지 않습니다: {path}")
            self.data = loaded

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.save()

    def remove(self, *keys: str) -> None:
        changed = False
        for key in keys:
            if key in self.data:
                del self.data[key]
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.path)
        os.chmod(self.path, 0o600)


class HermesInstaller:
    def __init__(
        self,
        *,
        region: str = REGION,
        project: str = PROJECT_NAME,
        state_path: Path = STATE_PATH,
    ):
        _require_boto3()
        self.region = region
        self.project = _validate_project_name(project)
        self.state = DeploymentState(state_path)
        self.session = boto3.Session(region_name=region)  # type: ignore[union-attr]
        self.ec2 = self.session.client("ec2")
        self.iam = self.session.client("iam")
        self.elbv2 = self.session.client("elbv2")
        self.cf = self.session.client("cloudfront")
        self.s3 = self.session.client("s3")
        self.aoss = self.session.client("opensearchserverless")
        self.bedrock_agent = self.session.client("bedrock-agent")
        self.ssm = self.session.client("ssm")
        self.sts = self.session.client("sts")

        try:
            identity = self.sts.get_caller_identity()
            self.account_id = identity["Account"]
            self.caller_principal_arn = _aoss_principal_arn(identity["Arn"])
            self.partition = identity["Arn"].split(":", 2)[1]
        except NoCredentialsError as exc:
            raise RuntimeError("AWS 자격 증명을 찾을 수 없습니다.") from exc

        self._initialize_state()
        self.deployment_id = self.state.data["deployment_id"]

    def _initialize_state(self) -> None:
        existing = self.state.data
        for key, expected in (
            ("region", self.region),
            ("project", self.project),
            ("account_id", self.account_id),
        ):
            actual = existing.get(key)
            if actual and actual != expected:
                raise RuntimeError(
                    f"상태 파일의 {key}={actual!r}가 현재 값 {expected!r}와 다릅니다. "
                    "다른 --state-path를 사용하세요."
                )

        self.state.update(
            schema_version=3,
            deployment_id=existing.get("deployment_id") or uuid.uuid4().hex[:12],
            project=self.project,
            region=self.region,
            account_id=self.account_id,
            managed_by=MANAGED_BY,
        )

    def run(
        self,
        *,
        vpc_cidr: Optional[str] = None,
        dashboard_oauth_client_id: Optional[str] = None,
        model_id: str = DEFAULT_MODEL_ID,
        key_name: Optional[str] = None,
        instance_type: str = INSTANCE_TYPE,
        ami_id: Optional[str] = None,
        volume_size: int = VOLUME_SIZE,
        enable_cloudfront: bool = True,
        enable_knowledge_base: bool = True,
        install_browser: bool = True,
        deployment_info_path: Path = DEPLOYMENT_INFO_PATH,
    ) -> Dict[str, Any]:
        start = time.time()
        existing_instance = self._find_existing_instance()
        stored_cloudfront_mode = self.state.data.get("cloudfront_enabled")
        stored_knowledge_base_mode = self.state.data.get(
            "knowledge_base_enabled"
        )
        if (
            existing_instance
            and stored_cloudfront_mode is not None
            and bool(stored_cloudfront_mode) != enable_cloudfront
        ):
            raise RuntimeError(
                "기존 배포의 CloudFront 모드와 요청 값이 다릅니다. "
                "모드를 바꾸려면 기존 배포를 삭제한 뒤 다시 설치하세요."
            )
        if stored_knowledge_base_mode is True and not enable_knowledge_base:
            raise RuntimeError(
                "기존 배포에 Knowledge Base 리소스가 있습니다. 비활성화하려면 "
                "기존 배포를 삭제한 뒤 다시 설치하세요."
            )
        self._reject_legacy_route53_deployment()
        dashboard_auth = self._resolve_dashboard_auth(
            dashboard_oauth_client_id,
            model_id=model_id,
            existing_instance=existing_instance,
        )

        logger.info("1) 전용 VPC와 네트워크 구성")
        network = self._ensure_vpc_networking(vpc_cidr)
        if (
            existing_instance
            and existing_instance["vpc_id"] != network["vpc_id"]
        ):
            raise RuntimeError(
                "관리 대상 EC2와 관리 대상 VPC가 서로 다릅니다. 자동 복구를 "
                "중단합니다."
            )

        knowledge_base: Dict[str, Any] = {
            "knowledge_base_enabled": enable_knowledge_base,
            "knowledge_base_bucket": None,
            "knowledge_base_role_name": None,
            "knowledge_base_role_arn": None,
            "opensearch_collection_id": None,
            "opensearch_collection_arn": None,
            "opensearch_collection_endpoint": None,
            "opensearch_collection_name": None,
            "opensearch_index_name": None,
            "knowledge_base_id": None,
            "knowledge_base_arn": None,
            "knowledge_base_data_source_id": None,
            "knowledge_base_ingestion_job_id": None,
        }
        if enable_knowledge_base:
            logger.info("2) Knowledge Base S3 Bucket 구성")
            bucket_name = self._ensure_knowledge_base_bucket()
            logger.info("3) Knowledge Base Service Role 구성")
            kb_role = self._ensure_knowledge_base_role(bucket_name)
            knowledge_base.update(
                knowledge_base_bucket=bucket_name,
                **kb_role,
            )

        logger.info("4) IAM Role과 Instance Profile 구성")
        iam = self._ensure_iam(
            knowledge_base_bucket=knowledge_base.get(
                "knowledge_base_bucket"
            )
        )

        logger.info("5) Security Group 구성")
        security_groups = self._ensure_security_groups(
            network["vpc_id"],
            enable_cloudfront=enable_cloudfront,
        )

        logger.info("6) Bedrock Runtime VPC Endpoint 구성")
        endpoint_id = self._ensure_bedrock_endpoint(
            network["vpc_id"],
            network["private_subnets"],
            security_groups["endpoint_sg_id"],
        )
        agent_endpoint_id = None
        if enable_knowledge_base:
            logger.info("7) Bedrock Agent Runtime VPC Endpoint 구성")
            agent_endpoint_id = self._ensure_interface_endpoint(
                network["vpc_id"],
                network["private_subnets"],
                security_groups["endpoint_sg_id"],
                service_suffix="bedrock-agent-runtime",
                state_key="bedrock_agent_endpoint_id",
            )

            logger.info("8) OpenSearch Serverless Collection 구성")
            collection = self._ensure_opensearch_collection(
                knowledge_base["knowledge_base_role_arn"]
            )
            knowledge_base.update(collection)
            self._ensure_knowledge_base_role(
                knowledge_base["knowledge_base_bucket"],
                collection_arn=collection["opensearch_collection_arn"],
            )

            logger.info("9) OpenSearch Vector Index 구성")
            self._ensure_vector_index_with_temporary_access(
                collection["opensearch_collection_endpoint"],
                collection["opensearch_index_name"],
                knowledge_base["knowledge_base_role_arn"],
            )

            logger.info("10) Bedrock Knowledge Base와 Data Source 구성")
            kb = self._ensure_knowledge_base(
                role_arn=knowledge_base["knowledge_base_role_arn"],
                collection_arn=collection["opensearch_collection_arn"],
                index_name=collection["opensearch_index_name"],
                bucket_name=knowledge_base["knowledge_base_bucket"],
            )
            knowledge_base.update(kb)

            logger.info("11) 로컬 Knowledge Base 문서 동기화")
            ingestion_job_id = self._sync_initial_knowledge_content(
                bucket_name=knowledge_base["knowledge_base_bucket"],
                knowledge_base_id=knowledge_base["knowledge_base_id"],
                data_source_id=knowledge_base[
                    "knowledge_base_data_source_id"
                ],
            )
            knowledge_base[
                "knowledge_base_ingestion_job_id"
            ] = ingestion_job_id
            self.state.update(**knowledge_base)

        logger.info("12) Hermes Agent UserData 렌더링")
        # Knowledge Base retrieve skill 파일은 SSM으로 설치합니다. user data는
        # 16KB 제한이 있어 skill 파일을 인라인하면 초과하기 때문입니다.
        user_data = self._render_user_data(
            dashboard_oauth_client_id=dashboard_auth["oauth_client_id"],
            dashboard_public_url=self._stored_dashboard_public_url(),
            model_id=model_id,
            install_browser=install_browser,
        )

        logger.info("13) Private Subnet에 EC2 생성")
        resolved_ami = ami_id or self._resolve_ami_id()
        instance = self._ensure_instance(
            existing=existing_instance,
            subnet_id=network["private_subnets"][0],
            sg_id=security_groups["ec2_sg_id"],
            profile_name=iam["profile_name"],
            user_data=user_data,
            key_name=key_name,
            instance_type=instance_type,
            ami_id=resolved_ami,
            volume_size=volume_size,
        )
        if existing_instance:
            logger.info("14) 기존 EC2 Hermes bootstrap 복구")
            self._reconcile_instance_bootstrap_via_ssm(
                instance["instance_id"],
                model_id=model_id,
                install_browser=install_browser,
            )
        if enable_knowledge_base:
            logger.info("14) EC2에 Knowledge Base retrieve skill 설치")
            self._install_retrieve_skill_via_ssm(
                instance["instance_id"], knowledge_base
            )

        logger.info("15) Internal ALB, Target Group, HTTP Listener 구성")
        load_balancer = self._ensure_load_balancer(
            vpc_id=network["vpc_id"],
            private_subnets=network["private_subnets"],
            alb_sg_id=security_groups["alb_sg_id"],
            instance_id=instance["instance_id"],
            origin_header_value=dashboard_auth["origin_header"],
            enable_cloudfront=enable_cloudfront,
        )

        cloudfront: Dict[str, Optional[str]] = {
            "vpc_origin_id": None,
            "distribution_id": None,
            "domain_name": None,
        }
        if enable_cloudfront:
            logger.info("16) CloudFront VPC Origin 구성")
            vpc_origin = self._ensure_vpc_origin(load_balancer["alb_arn"])
            logger.info("17) CloudFront Distribution 구성")
            cloudfront = {
                **vpc_origin,
                **self._ensure_cloudfront(
                    load_balancer["dns_name"],
                    vpc_origin["vpc_origin_id"],
                    dashboard_auth["origin_header"],
                ),
            }
            self._wait_for_cloudfront_deployed(
                cloudfront["distribution_id"]
            )
        else:
            logger.info("16) CloudFront 비활성화됨")

        dashboard_public_url = (
            f"https://{cloudfront['domain_name']}"
            if cloudfront["domain_name"]
            else None
        )
        oauth_client_id = dashboard_auth["oauth_client_id"]
        dashboard_ready_for_start = bool(
            oauth_client_id and dashboard_public_url
        )
        if dashboard_ready_for_start:
            logger.info("18) Nous OAuth Dashboard 설정")
            self._configure_dashboard_oauth_via_ssm(
                instance["instance_id"],
                oauth_client_id=oauth_client_id,
                public_url=dashboard_public_url,
            )
            self.state.remove(
                "dashboard_username",
                "dashboard_password",
                "dashboard_password_hash",
                "dashboard_secret",
            )
        elif existing_instance:
            logger.info("18) OAuth 설정 전까지 기존 Dashboard 중지")
            self._disable_dashboard_via_ssm(instance["instance_id"])

        outputs: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "deployment_id": self.deployment_id,
            "region": self.region,
            "account_id": self.account_id,
            **network,
            **iam,
            **security_groups,
            "bedrock_endpoint_id": endpoint_id,
            "bedrock_agent_endpoint_id": agent_endpoint_id,
            **knowledge_base,
            **instance,
            **load_balancer,
            **cloudfront,
            "dashboard_auth_provider": "nous",
            "dashboard_oauth_client_id": oauth_client_id,
            "dashboard_public_url": dashboard_public_url,
            "dashboard_oauth_callback_url": (
                f"{dashboard_public_url}/auth/callback"
                if dashboard_public_url
                else None
            ),
            "dashboard_oauth_configured": dashboard_ready_for_start,
            "model_id": model_id,
            "ami_id": resolved_ami,
            "cloudfront_enabled": enable_cloudfront,
        }
        self.state.update(**outputs)
        self._write_deployment_info(deployment_info_path, outputs)

        check_url = f"https://{cloudfront['domain_name']}"
        ready = (
            check_application_ready(check_url)
            if dashboard_ready_for_start
            else False
        )
        self.state.update(application_ready=ready)

        elapsed = (time.time() - start) / 60
        logger.info("=" * 64)
        logger.info("Hermes Agent 배포 완료 (%.2f분)", elapsed)
        logger.info("접속 URL: %s", check_url)
        if dashboard_ready_for_start:
            logger.info("Dashboard 인증: Nous OAuth")
        else:
            logger.info(
                "OAuth callback을 Nous Portal에 등록한 뒤 "
                "--dashboard-oauth-client-id로 재실행하세요: %s/auth/callback",
                check_url,
            )
        logger.info("배포 정보: %s", deployment_info_path)
        logger.info("=" * 64)
        return outputs

    def _resolve_dashboard_auth(
        self,
        oauth_client_id: Optional[str],
        *,
        model_id: str,
        existing_instance: Optional[Dict[str, str]],
    ) -> Dict[str, Optional[str]]:
        state = self.state.data
        stored_client_id = state.get("dashboard_oauth_client_id")
        stored_header = state.get("origin_header")

        if oauth_client_id and not re.fullmatch(
            r"agent:[A-Za-z0-9._:-]+", oauth_client_id
        ):
            raise ValueError(
                "--dashboard-oauth-client-id는 Nous Portal이 발급한 "
                "'agent:...' 형식이어야 합니다."
            )
        if (
            stored_client_id
            and oauth_client_id
            and oauth_client_id != stored_client_id
        ):
            logger.info(
                "  Dashboard OAuth client ID를 요청 값으로 갱신합니다: %s",
                oauth_client_id,
            )
        if existing_instance and state.get("model_id") not in (None, model_id):
            raise RuntimeError(
                "기존 EC2의 모델 설정과 --model-id가 다릅니다. 재설치 후 변경하세요."
            )

        resolved_client_id = oauth_client_id or stored_client_id
        resolved_header = stored_header or secrets.token_hex(24)
        self.state.update(
            dashboard_auth_provider="nous",
            dashboard_oauth_client_id=resolved_client_id,
            origin_header=resolved_header,
            model_id=model_id,
        )
        return {
            "oauth_client_id": resolved_client_id,
            "origin_header": resolved_header,
        }

    def _stored_dashboard_public_url(self) -> Optional[str]:
        domain_name = self.state.data.get("domain_name")
        if not domain_name:
            return None
        return f"https://{domain_name}"

    # ------------------------------------------------------------------
    # VPC and networking
    # ------------------------------------------------------------------
    def _ensure_vpc_networking(self, requested_cidr: Optional[str]) -> Dict[str, Any]:
        vpc = self._find_managed_vpc()
        if vpc:
            vpc_id = vpc["VpcId"]
            cidr = vpc["CidrBlock"]
            if requested_cidr and requested_cidr != cidr:
                raise RuntimeError(
                    f"기존 VPC CIDR은 {cidr}이며 요청한 {requested_cidr}와 다릅니다."
                )
            logger.info("  VPC 재사용: %s (%s)", vpc_id, cidr)
        else:
            cidr = self._select_vpc_cidr(requested_cidr)
            name = f"vpc-for-{self.project}"
            vpc_id = self.ec2.create_vpc(
                CidrBlock=cidr,
                TagSpecifications=[{
                    "ResourceType": "vpc",
                    "Tags": _resource_tags(
                        self.project, self.deployment_id, name=name
                    ),
                }],
            )["Vpc"]["VpcId"]
            self.ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
            logger.info("  VPC 생성: %s (%s)", vpc_id, cidr)

        self.ec2.modify_vpc_attribute(
            VpcId=vpc_id, EnableDnsSupport={"Value": True}
        )
        self.ec2.modify_vpc_attribute(
            VpcId=vpc_id, EnableDnsHostnames={"Value": True}
        )
        self.state.update(vpc_id=vpc_id, vpc_cidr=cidr)

        availability_zones = [
            zone["ZoneName"]
            for zone in self.ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )["AvailabilityZones"]
        ][:2]
        if len(availability_zones) < 2:
            raise RuntimeError(f"{self.region}에서 사용 가능한 AZ를 2개 찾지 못했습니다.")

        igw_id = self._ensure_internet_gateway(vpc_id)
        public_rt_id = self._ensure_route_table(vpc_id, "public")
        self._ensure_route(public_rt_id, gateway_id=igw_id)

        public_subnets = self._ensure_subnets(
            vpc_id=vpc_id,
            vpc_cidr=cidr,
            availability_zones=availability_zones,
            tier="public",
            offsets=(0, 1),
            route_table_id=public_rt_id,
            map_public_ip=True,
        )

        nat_gateway_id, eip_allocation_id = self._ensure_nat_gateway(
            vpc_id, public_subnets[0]
        )
        private_rt_id = self._ensure_route_table(vpc_id, "private")
        self._ensure_route(private_rt_id, nat_gateway_id=nat_gateway_id)
        private_subnets = self._ensure_subnets(
            vpc_id=vpc_id,
            vpc_cidr=cidr,
            availability_zones=availability_zones,
            tier="private",
            offsets=(10, 11),
            route_table_id=private_rt_id,
            map_public_ip=False,
        )

        result = {
            "vpc_id": vpc_id,
            "vpc_cidr": cidr,
            "availability_zones": availability_zones,
            "internet_gateway_id": igw_id,
            "public_route_table_id": public_rt_id,
            "private_route_table_id": private_rt_id,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
            "nat_gateway_id": nat_gateway_id,
            "nat_eip_allocation_id": eip_allocation_id,
        }
        self.state.update(**result)
        return result

    def _find_managed_vpc(self) -> Optional[Dict[str, Any]]:
        state_vpc_id = self.state.data.get("vpc_id")
        if state_vpc_id:
            try:
                vpcs = self.ec2.describe_vpcs(VpcIds=[state_vpc_id])["Vpcs"]
                if vpcs:
                    self._assert_managed_tags(vpcs[0].get("Tags", []), "VPC")
                    return vpcs[0]
            except ClientError as exc:
                if _error_code(exc) != "InvalidVpcID.NotFound":
                    raise

        vpcs = self.ec2.describe_vpcs(Filters=[
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
        ])["Vpcs"]
        if len(vpcs) > 1:
            raise RuntimeError(
                f"관리 대상 VPC가 여러 개입니다: {[v['VpcId'] for v in vpcs]}"
            )
        return vpcs[0] if vpcs else None

    def _assert_managed_tags(
        self, tags: Iterable[Dict[str, str]], resource_label: str
    ) -> None:
        values = _tag_map(tags)
        if values.get("ManagedBy") != MANAGED_BY or values.get("Project") != self.project:
            raise RuntimeError(
                f"{resource_label}에 Hermes 관리 태그가 없어 재사용을 거부합니다."
            )

    def _select_vpc_cidr(self, requested: Optional[str]) -> str:
        existing_networks: List[ipaddress.IPv4Network] = []
        for vpc in self.ec2.describe_vpcs()["Vpcs"]:
            for association in vpc.get("CidrBlockAssociationSet", []):
                block = association.get("CidrBlock")
                if block:
                    existing_networks.append(ipaddress.ip_network(block))
            if not vpc.get("CidrBlockAssociationSet") and vpc.get("CidrBlock"):
                existing_networks.append(ipaddress.ip_network(vpc["CidrBlock"]))

        candidates = (requested,) if requested else DEFAULT_VPC_CIDRS
        for candidate in candidates:
            try:
                network = ipaddress.ip_network(candidate, strict=True)
            except ValueError as exc:
                raise ValueError(f"올바르지 않은 --vpc-cidr: {candidate}") from exc
            if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen > 20:
                raise ValueError("--vpc-cidr은 /20 이상의 여유가 있는 IPv4 CIDR이어야 합니다.")
            if not any(network.overlaps(existing) for existing in existing_networks):
                return str(network)

        if requested:
            raise RuntimeError(f"요청한 VPC CIDR이 기존 VPC와 겹칩니다: {requested}")
        raise RuntimeError(
            "사용 가능한 기본 VPC CIDR을 찾지 못했습니다. --vpc-cidr로 지정하세요."
        )

    def _ensure_internet_gateway(self, vpc_id: str) -> str:
        attached = self.ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["InternetGateways"]
        if attached:
            igw = attached[0]
            self._assert_managed_tags(igw.get("Tags", []), "Internet Gateway")
            return igw["InternetGatewayId"]

        name = f"igw-{self.project}"
        igw_id = self.ec2.create_internet_gateway(
            TagSpecifications=[{
                "ResourceType": "internet-gateway",
                "Tags": _resource_tags(
                    self.project, self.deployment_id, name=name
                ),
            }]
        )["InternetGateway"]["InternetGatewayId"]
        self.ec2.attach_internet_gateway(
            InternetGatewayId=igw_id, VpcId=vpc_id
        )
        logger.info("  Internet Gateway 생성: %s", igw_id)
        self.state.update(internet_gateway_id=igw_id)
        return igw_id

    def _ensure_route_table(self, vpc_id: str, tier: str) -> str:
        tables = self.ec2.describe_route_tables(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
            {"Name": "tag:Tier", "Values": [tier]},
        ])["RouteTables"]
        if tables:
            return tables[0]["RouteTableId"]

        name = f"{tier}-rt-{self.project}"
        route_table_id = self.ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[{
                "ResourceType": "route-table",
                "Tags": _resource_tags(
                    self.project,
                    self.deployment_id,
                    name=name,
                    extra={"Tier": tier},
                ),
            }],
        )["RouteTable"]["RouteTableId"]
        logger.info("  %s Route Table 생성: %s", tier, route_table_id)
        return route_table_id

    def _ensure_route(
        self,
        route_table_id: str,
        *,
        gateway_id: Optional[str] = None,
        nat_gateway_id: Optional[str] = None,
    ) -> None:
        table = self.ec2.describe_route_tables(
            RouteTableIds=[route_table_id]
        )["RouteTables"][0]
        desired_key = "GatewayId" if gateway_id else "NatGatewayId"
        desired_value = gateway_id or nat_gateway_id
        existing = next(
            (
                route
                for route in table.get("Routes", [])
                if route.get("DestinationCidrBlock") == "0.0.0.0/0"
            ),
            None,
        )
        if existing and existing.get(desired_key) == desired_value:
            return

        params: Dict[str, str] = {
            "RouteTableId": route_table_id,
            "DestinationCidrBlock": "0.0.0.0/0",
            desired_key: str(desired_value),
        }
        if existing:
            self.ec2.replace_route(**params)
        else:
            self.ec2.create_route(**params)

    def _ensure_subnets(
        self,
        *,
        vpc_id: str,
        vpc_cidr: str,
        availability_zones: List[str],
        tier: str,
        offsets: tuple[int, int],
        route_table_id: str,
        map_public_ip: bool,
    ) -> List[str]:
        subnets: List[str] = []
        subnet_networks = list(ipaddress.ip_network(vpc_cidr).subnets(new_prefix=24))
        for slot, (zone, offset) in enumerate(
            zip(availability_zones, offsets), start=1
        ):
            found = self.ec2.describe_subnets(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
                {"Name": "tag:Project", "Values": [self.project]},
                {"Name": "tag:Tier", "Values": [tier]},
                {"Name": "tag:Slot", "Values": [str(slot)]},
            ])["Subnets"]
            if found:
                subnet = found[0]
                if subnet["AvailabilityZone"] != zone:
                    raise RuntimeError(
                        f"{tier} subnet {subnet['SubnetId']}의 AZ가 예상과 다릅니다."
                    )
                subnet_id = subnet["SubnetId"]
            else:
                name = f"{tier}-subnet-{self.project}-{slot}"
                subnet_id = self.ec2.create_subnet(
                    VpcId=vpc_id,
                    CidrBlock=str(subnet_networks[offset]),
                    AvailabilityZone=zone,
                    TagSpecifications=[{
                        "ResourceType": "subnet",
                        "Tags": _resource_tags(
                            self.project,
                            self.deployment_id,
                            name=name,
                            extra={"Tier": tier, "Slot": str(slot)},
                        ),
                    }],
                )["Subnet"]["SubnetId"]
                self.ec2.get_waiter("subnet_available").wait(SubnetIds=[subnet_id])
                logger.info(
                    "  %s Subnet 생성: %s (%s)", tier, subnet_id, zone
                )

            self.ec2.modify_subnet_attribute(
                SubnetId=subnet_id,
                MapPublicIpOnLaunch={"Value": map_public_ip},
            )
            self._associate_route_table(route_table_id, subnet_id)
            subnets.append(subnet_id)
        return subnets

    def _associate_route_table(self, route_table_id: str, subnet_id: str) -> None:
        tables = self.ec2.describe_route_tables(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
        )["RouteTables"]
        for table in tables:
            for association in table.get("Associations", []):
                if association.get("SubnetId") != subnet_id:
                    continue
                if table["RouteTableId"] == route_table_id:
                    return
                association_id = association.get("RouteTableAssociationId")
                if association_id:
                    self.ec2.replace_route_table_association(
                        AssociationId=association_id,
                        RouteTableId=route_table_id,
                    )
                    return
        self.ec2.associate_route_table(
            RouteTableId=route_table_id, SubnetId=subnet_id
        )

    def _ensure_nat_gateway(
        self, vpc_id: str, public_subnet_id: str
    ) -> tuple[str, str]:
        gateways = self.ec2.describe_nat_gateways(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
            {"Name": "state", "Values": ["pending", "available"]},
        ])["NatGateways"]
        if gateways:
            gateway = gateways[0]
            nat_gateway_id = gateway["NatGatewayId"]
            self._wait_for_nat_gateway(nat_gateway_id)
            allocation_id = gateway["NatGatewayAddresses"][0]["AllocationId"]
            return nat_gateway_id, allocation_id

        addresses = self.ec2.describe_addresses(Filters=[
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
            {"Name": "tag:Purpose", "Values": ["nat"]},
        ])["Addresses"]
        if addresses:
            allocation_id = addresses[0]["AllocationId"]
        else:
            allocation_id = self.ec2.allocate_address(
                Domain="vpc",
                TagSpecifications=[{
                    "ResourceType": "elastic-ip",
                    "Tags": _resource_tags(
                        self.project,
                        self.deployment_id,
                        name=f"nat-eip-{self.project}",
                        extra={"Purpose": "nat"},
                    ),
                }],
            )["AllocationId"]
            self.state.update(nat_eip_allocation_id=allocation_id)

        nat_gateway_id = self.ec2.create_nat_gateway(
            SubnetId=public_subnet_id,
            AllocationId=allocation_id,
            TagSpecifications=[{
                "ResourceType": "natgateway",
                "Tags": _resource_tags(
                    self.project,
                    self.deployment_id,
                    name=f"nat-{self.project}",
                ),
            }],
        )["NatGateway"]["NatGatewayId"]
        self.state.update(nat_gateway_id=nat_gateway_id)
        logger.info("  NAT Gateway 생성 및 대기: %s", nat_gateway_id)
        self._wait_for_nat_gateway(nat_gateway_id)
        return nat_gateway_id, allocation_id

    def _wait_for_nat_gateway(
        self, nat_gateway_id: str, timeout_seconds: int = 900
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            gateways = self.ec2.describe_nat_gateways(
                NatGatewayIds=[nat_gateway_id]
            )["NatGateways"]
            if not gateways:
                raise RuntimeError(f"NAT Gateway를 찾을 수 없습니다: {nat_gateway_id}")
            state = gateways[0]["State"]
            if state == "available":
                return
            if state == "failed":
                message = gateways[0].get("FailureMessage", "unknown error")
                raise RuntimeError(f"NAT Gateway 생성 실패: {message}")
            time.sleep(10)
        raise TimeoutError(f"NAT Gateway 대기 시간 초과: {nat_gateway_id}")

    # ------------------------------------------------------------------
    # IAM and security groups
    # ------------------------------------------------------------------
    def _ensure_iam(
        self, *, knowledge_base_bucket: Optional[str] = None
    ) -> Dict[str, str]:
        role_name = f"{self.project}-bedrock-role"
        profile_name = f"{self.project}-bedrock-profile"
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }
        statements: List[Dict[str, Any]] = [{
            "Sid": "BedrockInference",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:ListFoundationModels",
                "bedrock:GetFoundationModel",
                "bedrock:ListInferenceProfiles",
                "bedrock:GetInferenceProfile",
            ],
            "Resource": "*",
        }]
        if knowledge_base_bucket:
            statements.extend([
                {
                    "Sid": "KnowledgeBaseRetrieve",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:GetKnowledgeBase",
                        "bedrock:Retrieve",
                    ],
                    "Resource": (
                        f"arn:{self.partition}:bedrock:{self.region}:"
                        f"{self.account_id}:knowledge-base/*"
                    ),
                },
                {
                    "Sid": "KnowledgeBaseList",
                    "Effect": "Allow",
                    "Action": "bedrock:ListKnowledgeBases",
                    "Resource": "*",
                },
                {
                    "Sid": "KnowledgeBaseDocuments",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        f"arn:{self.partition}:s3:::"
                        f"{knowledge_base_bucket}/docs/*"
                    ),
                },
            ])
        bedrock_policy = {
            "Version": "2012-10-17",
            "Statement": statements,
        }

        try:
            role = self.iam.get_role(RoleName=role_name)["Role"]
            self._assert_managed_tags(role.get("Tags", []), "IAM Role")
        except ClientError as exc:
            if _error_code(exc) != "NoSuchEntity":
                raise
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Hermes Agent EC2 role for Amazon Bedrock",
                Tags=_resource_tags(self.project, self.deployment_id),
            )["Role"]
            logger.info("  IAM Role 생성: %s", role_name)

        self.iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust_policy),
        )
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName="BedrockAccess",
            PolicyDocument=json.dumps(bedrock_policy),
        )
        managed_policy = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        attached = {
            item["PolicyArn"]
            for item in self.iam.list_attached_role_policies(
                RoleName=role_name
            )["AttachedPolicies"]
        }
        if managed_policy not in attached:
            self.iam.attach_role_policy(
                RoleName=role_name, PolicyArn=managed_policy
            )

        try:
            profile = self.iam.get_instance_profile(
                InstanceProfileName=profile_name
            )["InstanceProfile"]
            self._assert_managed_tags(
                profile.get("Tags", []), "IAM Instance Profile"
            )
        except ClientError as exc:
            if _error_code(exc) != "NoSuchEntity":
                raise
            profile = self.iam.create_instance_profile(
                InstanceProfileName=profile_name,
                Tags=_resource_tags(self.project, self.deployment_id),
            )["InstanceProfile"]
            logger.info("  Instance Profile 생성: %s", profile_name)

        profile_roles = {
            item["RoleName"] for item in profile.get("Roles", [])
        }
        if role_name not in profile_roles:
            if profile_roles:
                raise RuntimeError(
                    f"{profile_name}에 예상하지 않은 Role이 연결되어 있습니다: "
                    f"{sorted(profile_roles)}"
                )
            self.iam.add_role_to_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name
            )

        for attempt in range(20):
            current = self.iam.get_instance_profile(
                InstanceProfileName=profile_name
            )["InstanceProfile"]
            if any(item["RoleName"] == role_name for item in current["Roles"]):
                if attempt >= 2:
                    break
            time.sleep(2)

        result = {
            "role_name": role_name,
            "profile_name": profile_name,
            "profile_arn": current["Arn"],
        }
        self.state.update(**result)
        return result

    def _ensure_security_groups(
        self, vpc_id: str, *, enable_cloudfront: bool
    ) -> Dict[str, str]:
        if not enable_cloudfront:
            raise RuntimeError(
                "CloudFront 없이 public ALB를 구성하는 모드는 지원하지 않습니다."
            )
        ec2_sg_id = self._ensure_security_group(
            vpc_id, f"{self.project}-ec2-sg", "Hermes Agent EC2"
        )
        alb_sg_id = self._ensure_security_group(
            vpc_id, f"{self.project}-alb-sg", "Hermes Agent ALB"
        )
        endpoint_sg_id = self._ensure_security_group(
            vpc_id, f"{self.project}-endpoint-sg", "Hermes Agent VPC endpoints"
        )

        # VPC origin의 CloudFront-managed ENI가 internal ALB에 접근하도록
        # CloudFront origin-facing prefix list에서 HTTP ingress를 허용합니다.
        prefix_list_id = self._cloudfront_prefix_list_id()
        self._authorize_ingress(alb_sg_id, {
            "IpProtocol": "tcp",
            "FromPort": 80,
            "ToPort": 80,
            "PrefixListIds": [{"PrefixListId": prefix_list_id}],
        })
        self._authorize_ingress(ec2_sg_id, {
            "IpProtocol": "tcp",
            "FromPort": DASHBOARD_PORT,
            "ToPort": DASHBOARD_PORT,
            "UserIdGroupPairs": [{"GroupId": alb_sg_id}],
        })
        self._authorize_ingress(endpoint_sg_id, {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "UserIdGroupPairs": [{"GroupId": ec2_sg_id}],
        })
        result = {
            "ec2_sg_id": ec2_sg_id,
            "alb_sg_id": alb_sg_id,
            "endpoint_sg_id": endpoint_sg_id,
        }
        self.state.update(**result)
        return result

    def _ensure_security_group(
        self, vpc_id: str, name: str, description: str
    ) -> str:
        groups = self.ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [name]},
        ])["SecurityGroups"]
        if groups:
            self._assert_managed_tags(groups[0].get("Tags", []), f"SG {name}")
            return groups[0]["GroupId"]
        group_id = self.ec2.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[{
                "ResourceType": "security-group",
                "Tags": _resource_tags(
                    self.project, self.deployment_id, name=name
                ),
            }],
        )["GroupId"]
        logger.info("  Security Group 생성: %s (%s)", name, group_id)
        return group_id

    def _authorize_ingress(
        self, group_id: str, permission: Dict[str, Any]
    ) -> None:
        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=group_id, IpPermissions=[permission]
            )
        except ClientError as exc:
            if _error_code(exc) != "InvalidPermission.Duplicate":
                raise

    def _cloudfront_prefix_list_id(self) -> str:
        try:
            lists = self.ec2.describe_managed_prefix_lists(Filters=[{
                "Name": "prefix-list-name",
                "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
            }])["PrefixLists"]
        except ClientError as exc:
            raise RuntimeError(
                f"CloudFront managed prefix list 조회 실패: {exc}"
            ) from exc
        if not lists:
            raise RuntimeError(
                "CloudFront origin-facing managed prefix list를 찾을 수 "
                "없어 ALB ingress 구성을 중단합니다."
            )
        return lists[0]["PrefixListId"]

    def _ensure_bedrock_endpoint(
        self,
        vpc_id: str,
        private_subnets: List[str],
        endpoint_sg_id: str,
    ) -> str:
        return self._ensure_interface_endpoint(
            vpc_id,
            private_subnets,
            endpoint_sg_id,
            service_suffix="bedrock-runtime",
            state_key="bedrock_endpoint_id",
        )

    def _ensure_interface_endpoint(
        self,
        vpc_id: str,
        private_subnets: List[str],
        endpoint_sg_id: str,
        *,
        service_suffix: str,
        state_key: str,
    ) -> str:
        service_name = f"com.amazonaws.{self.region}.{service_suffix}"
        endpoints = self.ec2.describe_vpc_endpoints(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [service_name]},
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
        ])["VpcEndpoints"]
        if endpoints:
            endpoint_id = endpoints[0]["VpcEndpointId"]
        else:
            endpoint_id = self.ec2.create_vpc_endpoint(
                VpcId=vpc_id,
                ServiceName=service_name,
                VpcEndpointType="Interface",
                SubnetIds=private_subnets,
                SecurityGroupIds=[endpoint_sg_id],
                PrivateDnsEnabled=True,
                TagSpecifications=[{
                    "ResourceType": "vpc-endpoint",
                    "Tags": _resource_tags(
                        self.project,
                        self.deployment_id,
                        name=f"{service_suffix}-{self.project}",
                    ),
                }],
            )["VpcEndpoint"]["VpcEndpointId"]
            logger.info("  %s endpoint 생성: %s", service_suffix, endpoint_id)

        deadline = time.time() + 600
        while time.time() < deadline:
            endpoint = self.ec2.describe_vpc_endpoints(
                VpcEndpointIds=[endpoint_id]
            )["VpcEndpoints"][0]
            state = endpoint["State"]
            if state == "available":
                self.state.update(**{state_key: endpoint_id})
                return endpoint_id
            if state in ("failed", "rejected", "deleted"):
                raise RuntimeError(
                    f"Bedrock endpoint 생성 실패: {endpoint_id} ({state})"
                )
            time.sleep(10)
        raise TimeoutError(f"Bedrock endpoint 대기 시간 초과: {endpoint_id}")

    # ------------------------------------------------------------------
    # Knowledge Base
    # ------------------------------------------------------------------
    def _knowledge_base_names(self) -> Dict[str, str]:
        return {
            "bucket": (
                f"hermes-kb-{self.account_id}-{self.region}-{self.project}"
            ),
            "role": f"{self.project}-knowledge-base-role",
            "collection": f"{self.project}-kb",
            "encryption_policy": f"enc-{self.project}-kb",
            "network_policy": f"net-{self.project}-kb",
            "access_policy": f"data-{self.project}-kb",
            "index": f"{self.project}-kb",
            "knowledge_base": f"{self.project}-knowledge-base",
            "data_source": f"{self.project}-s3-documents",
        }

    def _ensure_knowledge_base_bucket(self) -> str:
        bucket_name = (
            self.state.data.get("knowledge_base_bucket")
            or self._knowledge_base_names()["bucket"]
        )
        exists = True
        try:
            self.s3.head_bucket(Bucket=bucket_name)
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchBucket", "NotFound"):
                exists = False
            else:
                raise

        if not exists:
            params: Dict[str, Any] = {"Bucket": bucket_name}
            if self.region != "us-east-1":
                params["CreateBucketConfiguration"] = {
                    "LocationConstraint": self.region
                }
            self.s3.create_bucket(**params)
            logger.info("  Knowledge Base bucket 생성: %s", bucket_name)
        else:
            try:
                tags = self.s3.get_bucket_tagging(Bucket=bucket_name)[
                    "TagSet"
                ]
            except ClientError as exc:
                if _error_code(exc) == "NoSuchTagSet":
                    tags = []
                else:
                    raise
            if tags:
                self._assert_managed_tags(tags, "Knowledge Base S3 bucket")
            elif not self.state.data.get("knowledge_base_bucket"):
                raise RuntimeError(
                    f"S3 bucket {bucket_name}이 이미 있지만 Hermes 관리 태그가 "
                    "없어 재사용을 거부합니다."
                )

        self.s3.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={
                "TagSet": _resource_tags(
                    self.project,
                    self.deployment_id,
                    name=f"knowledge-base-{self.project}",
                )
            },
        )
        self.s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        self.s3.put_bucket_encryption(
            Bucket=bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [{
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256"
                    },
                }]
            },
        )
        self.s3.put_bucket_ownership_controls(
            Bucket=bucket_name,
            OwnershipControls={
                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
            },
        )
        self.s3.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        self.state.update(knowledge_base_bucket=bucket_name)
        return bucket_name

    def _ensure_knowledge_base_role(
        self,
        bucket_name: str,
        *,
        collection_arn: Optional[str] = None,
    ) -> Dict[str, str]:
        role_name = self._knowledge_base_names()["role"]
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": self.account_id,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:bedrock:{self.region}:"
                            f"{self.account_id}:knowledge-base/*"
                        )
                    },
                },
            }],
        }
        try:
            role = self.iam.get_role(RoleName=role_name)["Role"]
            self._assert_managed_tags(
                role.get("Tags", []), "Knowledge Base IAM Role"
            )
        except ClientError as exc:
            if _error_code(exc) != "NoSuchEntity":
                raise
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Hermes Agent Bedrock Knowledge Base service role",
                Tags=_resource_tags(self.project, self.deployment_id),
            )["Role"]
            logger.info("  Knowledge Base IAM Role 생성: %s", role_name)

        self.iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust_policy),
        )
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName="KnowledgeBaseDocuments",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "s3:ListBucket",
                        "Resource": (
                            f"arn:{self.partition}:s3:::{bucket_name}"
                        ),
                        "Condition": {
                            "StringLike": {"s3:prefix": ["docs", "docs/*"]}
                        },
                    },
                    {
                        "Effect": "Allow",
                        "Action": "s3:GetObject",
                        "Resource": (
                            f"arn:{self.partition}:s3:::{bucket_name}/docs/*"
                        ),
                    },
                ],
            }),
        )
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName="KnowledgeBaseEmbeddingModel",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "bedrock:InvokeModel",
                    "Resource": (
                        f"arn:{self.partition}:bedrock:{self.region}::"
                        f"foundation-model/{EMBEDDING_MODEL_ID}"
                    ),
                }],
            }),
        )
        if collection_arn:
            self.iam.put_role_policy(
                RoleName=role_name,
                PolicyName="KnowledgeBaseVectorStore",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": "aoss:APIAccessAll",
                        "Resource": collection_arn,
                    }],
                }),
            )

        result = {
            "knowledge_base_role_name": role_name,
            "knowledge_base_role_arn": role["Arn"],
        }
        self.state.update(**result)
        return result

    @staticmethod
    def _policy_document(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def _ensure_aoss_security_policy(
        self, *, name: str, policy_type: str, policy: Any
    ) -> None:
        serialized = json.dumps(policy, separators=(",", ":"))
        description = f"Hermes {self.project} {policy_type} policy"
        try:
            detail = self.aoss.get_security_policy(
                name=name, type=policy_type
            )["securityPolicyDetail"]
        except ClientError as exc:
            if _error_code(exc) != "ResourceNotFoundException":
                raise
            self.aoss.create_security_policy(
                name=name,
                type=policy_type,
                description=description,
                policy=serialized,
            )
            return
        if detail.get("description") != description:
            raise RuntimeError(
                f"OpenSearch {policy_type} policy {name}이 Hermes 관리 "
                "policy가 아니어서 재사용을 거부합니다."
            )
        if self._policy_document(detail.get("policy")) != policy:
            self.aoss.update_security_policy(
                name=name,
                type=policy_type,
                policyVersion=detail["policyVersion"],
                description=description,
                policy=serialized,
            )

    def _ensure_aoss_access_policy(
        self, *, name: str, policy: Any
    ) -> None:
        serialized = json.dumps(policy, separators=(",", ":"))
        description = (
            f"Hermes {self.project} Knowledge Base data access"
        )
        try:
            detail = self.aoss.get_access_policy(
                name=name, type="data"
            )["accessPolicyDetail"]
        except ClientError as exc:
            if _error_code(exc) != "ResourceNotFoundException":
                raise
            self.aoss.create_access_policy(
                name=name,
                type="data",
                description=description,
                policy=serialized,
            )
            return
        if detail.get("description") != description:
            raise RuntimeError(
                f"OpenSearch data access policy {name}이 Hermes 관리 "
                "policy가 아니어서 재사용을 거부합니다."
            )
        if self._policy_document(detail.get("policy")) != policy:
            self.aoss.update_access_policy(
                name=name,
                type="data",
                policyVersion=detail["policyVersion"],
                description=description,
                policy=serialized,
            )

    def _aoss_network_policy(self, *, public: bool) -> List[Dict[str, Any]]:
        collection_name = self._knowledge_base_names()["collection"]
        rule: Dict[str, Any] = {
            "Rules": [{
                "ResourceType": "collection",
                "Resource": [f"collection/{collection_name}"],
            }],
            "AllowFromPublic": public,
        }
        if not public:
            rule["SourceServices"] = ["bedrock.amazonaws.com"]
        return [rule]

    def _aoss_data_access_policy(
        self,
        knowledge_base_role_arn: str,
        *,
        include_installer: bool,
    ) -> List[Dict[str, Any]]:
        names = self._knowledge_base_names()
        # Bedrock ingestion은 문서 write 중 index 설정을 갱신하므로 AWS 문서의
        # 최소 3개 권한(Describe/Read/WriteDocument)만으로는 authorization_
        # exception이 발생합니다. index 생성/갱신 권한과 collection describe를
        # 함께 부여해야 ingestion이 성공합니다.
        policy = [{
            "Rules": [
                {
                    "ResourceType": "index",
                    "Resource": [f"index/{names['collection']}/*"],
                    "Permission": [
                        "aoss:CreateIndex",
                        "aoss:UpdateIndex",
                        "aoss:DescribeIndex",
                        "aoss:ReadDocument",
                        "aoss:WriteDocument",
                    ],
                },
                {
                    "ResourceType": "collection",
                    "Resource": [f"collection/{names['collection']}"],
                    "Permission": [
                        "aoss:DescribeCollectionItems",
                    ],
                },
            ],
            "Principal": [knowledge_base_role_arn],
        }]
        if include_installer:
            policy.append({
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{names['collection']}"],
                        "Permission": [
                            "aoss:CreateCollectionItems",
                            "aoss:DeleteCollectionItems",
                            "aoss:UpdateCollectionItems",
                            "aoss:DescribeCollectionItems",
                        ],
                    },
                    {
                        "ResourceType": "index",
                        "Resource": [f"index/{names['collection']}/*"],
                        "Permission": [
                            "aoss:CreateIndex",
                            "aoss:DeleteIndex",
                            "aoss:UpdateIndex",
                            "aoss:DescribeIndex",
                            "aoss:ReadDocument",
                            "aoss:WriteDocument",
                        ],
                    },
                ],
                "Principal": [self.caller_principal_arn],
            })
        return policy

    def _set_aoss_index_management_access(
        self, *, public: bool, knowledge_base_role_arn: str
    ) -> None:
        names = self._knowledge_base_names()
        if public:
            self._ensure_aoss_access_policy(
                name=names["access_policy"],
                policy=self._aoss_data_access_policy(
                    knowledge_base_role_arn,
                    include_installer=True,
                ),
            )
        self._ensure_aoss_security_policy(
            name=names["network_policy"],
            policy_type="network",
            policy=self._aoss_network_policy(public=public),
        )
        if not public:
            self._ensure_aoss_access_policy(
                name=names["access_policy"],
                policy=self._aoss_data_access_policy(
                    knowledge_base_role_arn,
                    include_installer=False,
                ),
            )
        self.state.update(opensearch_network_public=public)
        if public:
            logger.info("  vector index 구성을 위해 AOSS endpoint 임시 공개")
        else:
            logger.info("  AOSS network policy를 Bedrock 전용 private로 복원")

    def _ensure_vector_index_with_temporary_access(
        self,
        collection_endpoint: str,
        index_name: str,
        knowledge_base_role_arn: str,
    ) -> None:
        try:
            self._set_aoss_index_management_access(
                public=True,
                knowledge_base_role_arn=knowledge_base_role_arn,
            )
            self._ensure_vector_index(collection_endpoint, index_name)
        finally:
            self._set_aoss_index_management_access(
                public=False,
                knowledge_base_role_arn=knowledge_base_role_arn,
            )

    def _ensure_opensearch_collection(
        self, knowledge_base_role_arn: str
    ) -> Dict[str, str]:
        names = self._knowledge_base_names()
        collection_name = names["collection"]
        self._ensure_aoss_security_policy(
            name=names["encryption_policy"],
            policy_type="encryption",
            policy={
                "Rules": [{
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"],
                }],
                "AWSOwnedKey": True,
            },
        )
        self._ensure_aoss_security_policy(
            name=names["network_policy"],
            policy_type="network",
            policy=self._aoss_network_policy(public=False),
        )
        self._ensure_aoss_access_policy(
            name=names["access_policy"],
            policy=self._aoss_data_access_policy(
                knowledge_base_role_arn,
                include_installer=False,
            ),
        )
        self.state.update(
            opensearch_encryption_policy=names["encryption_policy"],
            opensearch_network_policy=names["network_policy"],
            opensearch_access_policy=names["access_policy"],
        )

        details = self.aoss.batch_get_collection(
            names=[collection_name]
        ).get("collectionDetails", [])
        if details:
            detail = details[0]
            tags = self.aoss.list_tags_for_resource(
                resourceArn=detail["arn"]
            ).get("tags", [])
            values = {
                item.get("key"): item.get("value") for item in tags
            }
            if (
                values.get("ManagedBy") != MANAGED_BY
                or values.get("Project") != self.project
            ):
                raise RuntimeError(
                    "OpenSearch collection에 Hermes 관리 태그가 없어 "
                    "재사용을 거부합니다."
                )
        else:
            detail = self.aoss.create_collection(
                name=collection_name,
                type="VECTORSEARCH",
                description=f"Hermes Agent Knowledge Base for {self.project}",
                standbyReplicas="DISABLED",
                tags=_aoss_resource_tags(
                    self.project, self.deployment_id
                ),
            )["createCollectionDetail"]
            logger.info("  OpenSearch collection 생성: %s", collection_name)

        deadline = time.time() + 1200
        while time.time() < deadline:
            current = self.aoss.batch_get_collection(
                names=[collection_name]
            ).get("collectionDetails", [])
            if not current:
                time.sleep(10)
                continue
            detail = current[0]
            status = detail["status"]
            endpoint = detail.get("collectionEndpoint")
            if status == "ACTIVE" and endpoint:
                result = {
                    "opensearch_collection_id": detail["id"],
                    "opensearch_collection_arn": detail["arn"],
                    "opensearch_collection_endpoint": endpoint,
                    "opensearch_collection_name": collection_name,
                    "opensearch_index_name": names["index"],
                    "opensearch_encryption_policy": names[
                        "encryption_policy"
                    ],
                    "opensearch_network_policy": names["network_policy"],
                    "opensearch_access_policy": names["access_policy"],
                }
                self.state.update(**result)
                return result
            if status == "FAILED":
                raise RuntimeError(
                    f"OpenSearch collection 생성 실패: {detail.get('failureCode')} "
                    f"{detail.get('failureMessage')}"
                )
            if status == "UPDATE_FAILED":
                raise RuntimeError(
                    "OpenSearch collection 업데이트 실패: "
                    f"{detail.get('failureCode')} "
                    f"{detail.get('failureMessage')}"
                )
            if status == "DELETING":
                raise RuntimeError(
                    f"OpenSearch collection {collection_name}이 삭제 중입니다. "
                    "삭제 완료 후 installer를 다시 실행하세요."
                )
            time.sleep(10)
        raise TimeoutError(
            f"OpenSearch collection ACTIVE 대기 시간 초과: {collection_name}"
        )

    def _aoss_http_request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
    ) -> tuple[int, str]:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("OpenSearch 요청에 사용할 AWS credentials가 없습니다.")
        frozen = credentials.get_frozen_credentials()
        # AOSS 데이터 평면은 모든 SigV4 요청에 x-amz-content-sha256을
        # 요구하며, 없으면 정책과 무관하게 403을 반환합니다.
        headers = {
            "Content-Type": "application/json",
            "X-Amz-Content-SHA256": hashlib.sha256(
                body or b""
            ).hexdigest(),
        }
        aws_request = AWSRequest(  # type: ignore[operator]
            method=method,
            url=url,
            data=body,
            headers=headers,
        )
        SigV4Auth(  # type: ignore[operator]
            frozen, "aoss", self.region
        ).add_auth(aws_request)
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(aws_request.headers.items()),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.getcode(), response.read().decode(
                    "utf-8", errors="replace"
                )
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def _ensure_vector_index(
        self, collection_endpoint: str, index_name: str
    ) -> None:
        endpoint = collection_endpoint.rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"
        url = f"{endpoint}/{index_name}"
        mapping = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 512,
                }
            },
            "mappings": {
                "properties": {
                    "vector_field": {
                        "type": "knn_vector",
                        "dimension": EMBEDDING_DIMENSIONS,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "faiss",
                            "parameters": {
                                "ef_construction": 512,
                                "m": 16,
                            },
                        },
                    },
                    "AMAZON_BEDROCK_TEXT": {
                        "type": "text",
                        "index": True,
                    },
                    "AMAZON_BEDROCK_METADATA": {
                        "type": "text",
                        "index": False,
                    },
                }
            },
        }
        payload = json.dumps(mapping).encode("utf-8")
        deadline = time.time() + 900
        last_status = 0
        last_body = ""
        while time.time() < deadline:
            status, response_body = self._aoss_http_request("GET", url)
            if status == 200:
                logger.info("  OpenSearch vector index 재사용: %s", index_name)
                return
            if status not in (401, 403, 404, 429, 500, 503):
                raise RuntimeError(
                    f"OpenSearch index 조회 실패: HTTP {status}: "
                    f"{response_body}"
                )

            status, response_body = self._aoss_http_request(
                "PUT", url, body=payload
            )
            last_status, last_body = status, response_body
            if status in (200, 201):
                logger.info("  OpenSearch vector index 생성: %s", index_name)
                return
            if status == 400 and "resource_already_exists_exception" in (
                response_body
            ):
                return
            if status not in (401, 403, 429, 500, 503):
                raise RuntimeError(
                    f"OpenSearch index 생성 실패: HTTP {status}: "
                    f"{response_body}"
                )
            time.sleep(10)
        raise TimeoutError(
            f"OpenSearch index policy 전파 대기 시간 초과: "
            f"HTTP {last_status}: {last_body}"
        )

    def _managed_knowledge_base(
        self, knowledge_base_id: str
    ) -> Dict[str, Any]:
        knowledge_base = self.bedrock_agent.get_knowledge_base(
            knowledgeBaseId=knowledge_base_id
        )["knowledgeBase"]
        tags = self.bedrock_agent.list_tags_for_resource(
            resourceArn=knowledge_base["knowledgeBaseArn"]
        ).get("tags", {})
        if (
            tags.get("ManagedBy") != MANAGED_BY
            or tags.get("Project") != self.project
        ):
            raise RuntimeError(
                f"Knowledge Base {knowledge_base_id}에 Hermes 관리 태그가 "
                "없어 재사용을 거부합니다."
            )
        return knowledge_base

    def _find_knowledge_base(self) -> Optional[Dict[str, Any]]:
        state_id = self.state.data.get("knowledge_base_id")
        if state_id:
            try:
                return self._managed_knowledge_base(state_id)
            except ClientError as exc:
                if _error_code(exc) != "ResourceNotFoundException":
                    raise

        name = self._knowledge_base_names()["knowledge_base"]
        paginator = self.bedrock_agent.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for item in page.get("knowledgeBaseSummaries", []):
                if item.get("name") == name:
                    return self._managed_knowledge_base(
                        item["knowledgeBaseId"]
                    )
        return None

    def _wait_knowledge_base_active(
        self, knowledge_base_id: str, timeout_seconds: int = 900
    ) -> Dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            knowledge_base = self.bedrock_agent.get_knowledge_base(
                knowledgeBaseId=knowledge_base_id
            )["knowledgeBase"]
            status = knowledge_base["status"]
            if status == "ACTIVE":
                return knowledge_base
            if status in ("FAILED", "DELETE_UNSUCCESSFUL"):
                reasons = "; ".join(
                    knowledge_base.get("failureReasons", [])
                )
                raise RuntimeError(
                    f"Knowledge Base {status}: {reasons or 'unknown'}"
                )
            if status == "DELETING":
                raise RuntimeError(
                    f"Knowledge Base {knowledge_base_id}가 삭제 중입니다. "
                    "삭제 완료 후 installer를 다시 실행하세요."
                )
            time.sleep(10)
        raise TimeoutError(
            f"Knowledge Base ACTIVE 대기 시간 초과: {knowledge_base_id}"
        )

    def _ensure_knowledge_base(
        self,
        *,
        role_arn: str,
        collection_arn: str,
        index_name: str,
        bucket_name: str,
    ) -> Dict[str, str]:
        name = self._knowledge_base_names()["knowledge_base"]
        knowledge_base = self._find_knowledge_base()
        if knowledge_base:
            storage = knowledge_base["storageConfiguration"][
                "opensearchServerlessConfiguration"
            ]
            if (
                storage["collectionArn"] != collection_arn
                or storage["vectorIndexName"] != index_name
            ):
                raise RuntimeError(
                    "기존 Knowledge Base의 OpenSearch 설정이 현재 배포와 "
                    "다릅니다."
                )
        else:
            params = {
                "name": name,
                "description": f"Hermes Agent documents for {self.project}",
                "roleArn": role_arn,
                "knowledgeBaseConfiguration": {
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": (
                            f"arn:{self.partition}:bedrock:{self.region}::"
                            f"foundation-model/{EMBEDDING_MODEL_ID}"
                        ),
                        "embeddingModelConfiguration": {
                            "bedrockEmbeddingModelConfiguration": {
                                "dimensions": EMBEDDING_DIMENSIONS,
                            }
                        },
                    },
                },
                "storageConfiguration": {
                    "type": "OPENSEARCH_SERVERLESS",
                    "opensearchServerlessConfiguration": {
                        "collectionArn": collection_arn,
                        "vectorIndexName": index_name,
                        "fieldMapping": {
                            "vectorField": "vector_field",
                            "textField": "AMAZON_BEDROCK_TEXT",
                            "metadataField": "AMAZON_BEDROCK_METADATA",
                        },
                    },
                },
                "tags": _resource_tag_map(
                    self.project,
                    self.deployment_id,
                    name=name,
                ),
            }
            last_error: Optional[Exception] = None
            for attempt in range(12):
                try:
                    knowledge_base = self.bedrock_agent.create_knowledge_base(
                        **params
                    )["knowledgeBase"]
                    logger.info(
                        "  Bedrock Knowledge Base 생성: %s",
                        knowledge_base["knowledgeBaseId"],
                    )
                    break
                except ClientError as exc:
                    last_error = exc
                    if _error_code(exc) == "ConflictException":
                        existing = self._find_knowledge_base()
                        if existing:
                            knowledge_base = existing
                            break
                    if (
                        _error_code(exc)
                        in ("ValidationException", "ConflictException")
                        and attempt < 11
                    ):
                        time.sleep(10)
                        continue
                    raise
            else:
                raise RuntimeError(
                    f"Knowledge Base 생성 실패: {last_error}"
                ) from last_error

        knowledge_base = self._wait_knowledge_base_active(
            knowledge_base["knowledgeBaseId"]
        )
        data_source = self._ensure_knowledge_base_data_source(
            knowledge_base["knowledgeBaseId"], bucket_name
        )
        result = {
            "knowledge_base_id": knowledge_base["knowledgeBaseId"],
            "knowledge_base_arn": knowledge_base["knowledgeBaseArn"],
            "knowledge_base_data_source_id": data_source["dataSourceId"],
        }
        self.state.update(**result)
        return result

    def _ensure_knowledge_base_data_source(
        self, knowledge_base_id: str, bucket_name: str
    ) -> Dict[str, Any]:
        data_source_id = self.state.data.get(
            "knowledge_base_data_source_id"
        )
        data_source: Optional[Dict[str, Any]] = None
        if data_source_id:
            try:
                data_source = self.bedrock_agent.get_data_source(
                    knowledgeBaseId=knowledge_base_id,
                    dataSourceId=data_source_id,
                )["dataSource"]
            except ClientError as exc:
                if _error_code(exc) != "ResourceNotFoundException":
                    raise

        name = self._knowledge_base_names()["data_source"]
        if data_source is None:
            paginator = self.bedrock_agent.get_paginator("list_data_sources")
            for page in paginator.paginate(
                knowledgeBaseId=knowledge_base_id
            ):
                for item in page.get("dataSourceSummaries", []):
                    if item.get("name") == name:
                        data_source = self.bedrock_agent.get_data_source(
                            knowledgeBaseId=knowledge_base_id,
                            dataSourceId=item["dataSourceId"],
                        )["dataSource"]
                        break
                if data_source:
                    break

        bucket_arn = f"arn:{self.partition}:s3:::{bucket_name}"
        if data_source:
            status = data_source["status"]
            if status == "DELETING":
                deadline = time.time() + 600
                while time.time() < deadline:
                    try:
                        self.bedrock_agent.get_data_source(
                            knowledgeBaseId=knowledge_base_id,
                            dataSourceId=data_source["dataSourceId"],
                        )
                    except ClientError as exc:
                        if _error_code(exc) == "ResourceNotFoundException":
                            data_source = None
                            break
                        raise
                    time.sleep(10)
                else:
                    raise TimeoutError(
                        "Knowledge Base data source 삭제 대기 시간 초과: "
                        f"{data_source['dataSourceId']}"
                    )
            elif status == "DELETE_UNSUCCESSFUL":
                reasons = "; ".join(
                    data_source.get("failureReasons", [])
                )
                raise RuntimeError(
                    "Knowledge Base data source DELETE_UNSUCCESSFUL: "
                    f"{reasons or 'unknown'}"
                )

        if data_source:
            configured_bucket = data_source["dataSourceConfiguration"][
                "s3Configuration"
            ]["bucketArn"]
            if configured_bucket != bucket_arn:
                raise RuntimeError(
                    "기존 Knowledge Base data source의 S3 bucket이 현재 "
                    "배포와 다릅니다."
                )
        else:
            data_source = self.bedrock_agent.create_data_source(
                knowledgeBaseId=knowledge_base_id,
                name=name,
                description=f"Private documents in s3://{bucket_name}/docs/",
                dataDeletionPolicy="DELETE",
                dataSourceConfiguration={
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": bucket_arn,
                        "inclusionPrefixes": ["docs/"],
                    },
                },
                vectorIngestionConfiguration={
                    "chunkingConfiguration": {
                        "chunkingStrategy": "HIERARCHICAL",
                        "hierarchicalChunkingConfiguration": {
                            "levelConfigurations": [
                                {"maxTokens": 1500},
                                {"maxTokens": 300},
                            ],
                            "overlapTokens": 60,
                        },
                    },
                },
            )["dataSource"]
            logger.info(
                "  Knowledge Base data source 생성: %s",
                data_source["dataSourceId"],
            )

        deadline = time.time() + 600
        while time.time() < deadline:
            current = self.bedrock_agent.get_data_source(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source["dataSourceId"],
            )["dataSource"]
            status = current["status"]
            if status == "AVAILABLE":
                return current
            if status == "DELETE_UNSUCCESSFUL":
                reasons = "; ".join(current.get("failureReasons", []))
                raise RuntimeError(
                    "Knowledge Base data source DELETE_UNSUCCESSFUL: "
                    f"{reasons or 'unknown'}"
                )
            time.sleep(10)
        raise TimeoutError(
            "Knowledge Base data source AVAILABLE 대기 시간 초과: "
            f"{data_source['dataSourceId']}"
        )

    def _wait_ingestion_job(
        self,
        knowledge_base_id: str,
        data_source_id: str,
        ingestion_job_id: str,
        timeout_seconds: int = 1800,
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            job = self.bedrock_agent.get_ingestion_job(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                ingestionJobId=ingestion_job_id,
            )["ingestionJob"]
            status = job["status"]
            if status == "COMPLETE":
                logger.info(
                    "  Knowledge Base ingestion 완료: %s",
                    job.get("statistics", {}),
                )
                return
            if status in ("FAILED", "STOPPED"):
                reasons = "; ".join(job.get("failureReasons", []))
                raise RuntimeError(
                    f"Knowledge Base ingestion {status}: "
                    f"{reasons or 'unknown'}"
                )
            time.sleep(10)
        raise TimeoutError(
            f"Knowledge Base ingestion 대기 시간 초과: {ingestion_job_id}"
        )

    def _sync_initial_knowledge_content(
        self,
        *,
        bucket_name: str,
        knowledge_base_id: str,
        data_source_id: str,
    ) -> Optional[str]:
        if not CONTENTS_PATH.is_dir():
            logger.info("  contents/ 디렉터리가 없어 초기 문서 동기화 생략")
            return None

        supported = {
            ".csv", ".doc", ".docx", ".html", ".md",
            ".pdf", ".txt", ".xls", ".xlsx",
        }
        uploaded = 0
        document_count = 0
        resolved_contents = CONTENTS_PATH.resolve()
        for path in sorted(CONTENTS_PATH.rglob("*")):
            try:
                relative_path = path.resolve().relative_to(
                    resolved_contents
                )
            except ValueError:
                continue
            if (
                not path.is_file()
                or path.is_symlink()
                or any(part.startswith(".") for part in relative_path.parts)
                or relative_path.as_posix() == "README.md"
                or path.suffix.lower() not in supported
            ):
                continue
            document_count += 1
            relative = relative_path.as_posix()
            key = f"docs/{relative}"
            digest = _file_sha256(path)
            try:
                metadata = self.s3.head_object(
                    Bucket=bucket_name, Key=key
                ).get("Metadata", {})
                remote = (
                    metadata.get("sha256")
                    or metadata.get("source-sha256")
                )
            except ClientError as exc:
                if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                    remote = None
                else:
                    raise
            if remote == digest:
                continue
            self.s3.upload_file(
                str(path),
                bucket_name,
                key,
                ExtraArgs={
                    "Metadata": {
                        "sha256": digest,
                    }
                },
            )
            uploaded += 1
            logger.info("  초기 문서 업로드: %s", path)

        if uploaded == 0:
            if document_count == 0:
                logger.info("  Knowledge Base 초기 문서 없음")
                return None
            latest_job: Optional[Dict[str, Any]] = None
            paginator = self.bedrock_agent.get_paginator(
                "list_ingestion_jobs"
            )
            for page in paginator.paginate(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
            ):
                for job in page.get("ingestionJobSummaries", []):
                    if (
                        latest_job is None
                        or job["startedAt"] > latest_job["startedAt"]
                    ):
                        latest_job = job
            if latest_job:
                job_id = latest_job["ingestionJobId"]
                status = latest_job["status"]
                if status == "COMPLETE":
                    logger.info("  Knowledge Base 초기 문서 변경 없음")
                    return job_id
                if status not in ("FAILED", "STOPPED"):
                    logger.info("  기존 ingestion job 대기: %s", job_id)
                    self._wait_ingestion_job(
                        knowledge_base_id, data_source_id, job_id
                    )
                    return job_id
        # AOSS data access policy 갱신은 전파에 몇 분이 걸릴 수 있어 직후의
        # ingestion이 일시적인 authorization_exception으로 실패할 수 있으므로
        # 해당 오류에 한해 대기 후 재시도합니다.
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            job_id = self._start_or_adopt_ingestion_job(
                knowledge_base_id, data_source_id
            )
            self.state.update(knowledge_base_ingestion_job_id=job_id)
            try:
                self._wait_ingestion_job(
                    knowledge_base_id, data_source_id, job_id
                )
                return job_id
            except RuntimeError as exc:
                if (
                    "authorization_exception" not in str(exc)
                    or attempt == max_attempts
                ):
                    raise
                logger.info(
                    "  AOSS 권한 전파 대기 후 ingestion 재시도 (%d/%d)",
                    attempt,
                    max_attempts,
                )
                time.sleep(60)
        raise RuntimeError(
            "Knowledge Base ingestion 재시도를 완료하지 못했습니다."
        )

    def _start_or_adopt_ingestion_job(
        self, knowledge_base_id: str, data_source_id: str
    ) -> str:
        try:
            response = self.bedrock_agent.start_ingestion_job(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                description=(
                    "Initial Hermes installer document synchronization"
                ),
            )
            return response["ingestionJob"]["ingestionJobId"]
        except ClientError as exc:
            if _error_code(exc) != "ConflictException":
                raise
            paginator = self.bedrock_agent.get_paginator(
                "list_ingestion_jobs"
            )
            for page in paginator.paginate(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
            ):
                active = next(
                    (
                        item
                        for item in page.get(
                            "ingestionJobSummaries", []
                        )
                        if item.get("status")
                        not in ("COMPLETE", "FAILED", "STOPPED")
                    ),
                    None,
                )
                if active:
                    job_id = active["ingestionJobId"]
                    logger.info("  기존 ingestion job 대기: %s", job_id)
                    return job_id
            raise

    # ------------------------------------------------------------------
    # EC2 and UserData
    # ------------------------------------------------------------------
    def _render_hermes_config(
        self,
        *,
        model_id: str,
        dashboard_oauth_client_id: Optional[str],
        dashboard_public_url: Optional[str],
    ) -> str:
        dashboard: Dict[str, Any] = {
            "oauth": {"client_id": dashboard_oauth_client_id or ""},
        }
        if dashboard_public_url:
            dashboard["public_url"] = dashboard_public_url
        return json.dumps(
            {
                "model": {
                    "default": model_id,
                    "provider": "bedrock",
                    "base_url": (
                        f"https://bedrock-runtime.{self.region}.amazonaws.com"
                    ),
                },
                "bedrock": {"region": self.region},
                "dashboard": dashboard,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _render_user_data(
        self,
        *,
        dashboard_oauth_client_id: Optional[str],
        dashboard_public_url: Optional[str],
        model_id: str,
        install_browser: bool,
    ) -> str:
        config = self._render_hermes_config(
            model_id=model_id,
            dashboard_oauth_client_id=dashboard_oauth_client_id,
            dashboard_public_url=dashboard_public_url,
        )
        install_options = "--non-interactive --skip-setup"
        if not install_browser:
            install_options += " --skip-browser"

        system_packages = self._hermes_system_packages(install_browser)

        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "exec > >(tee /var/log/hermes-install.log)",
            "exec 2>&1",
            "",
            'echo "=== Hermes Agent installation started ==="',
            f"dnf install -y --allowerasing {' '.join(system_packages)}",
            "timedatectl set-timezone Asia/Seoul || true",
            "",
            "sudo -u ec2-user -H bash -lc "
            "'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | "
            f"bash -s -- {install_options}'",
            "",
            "install -d -m 700 -o ec2-user -g ec2-user /home/ec2-user/.hermes",
            "cat > /home/ec2-user/.hermes/config.yaml <<'HERMES_CONFIG'",
            config,
            "HERMES_CONFIG",
            "chown ec2-user:ec2-user /home/ec2-user/.hermes/config.yaml",
            "chmod 600 /home/ec2-user/.hermes/config.yaml",
            "",
        ]
        lines.extend([
            "cat > /etc/systemd/system/hermes-dashboard.service <<'SERVICE'",
            self._dashboard_systemd_unit().rstrip(),
            "SERVICE",
            "",
            "systemctl daemon-reload",
            "# CloudFront public URL and OAuth callback are configured via SSM.",
            "systemctl disable --now hermes-dashboard.service || true",
            'echo "=== Hermes Agent installation completed ==="',
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _hermes_system_packages(install_browser: bool) -> List[str]:
        # curl은 Amazon Linux 2023에 curl-minimal로 이미 설치되어 있어,
        # curl 패키지를 명시하면 dnf가 파일 충돌로 실패합니다.
        packages = ["git"]
        if install_browser:
            packages.extend([
                "alsa-lib",
                "at-spi2-atk",
                "at-spi2-core",
                "atk",
                "cairo",
                "cups-libs",
                "libdrm",
                "libxkbcommon",
                "mesa-libgbm",
                "nss",
                "pango",
            ])
        return packages

    def _dashboard_systemd_unit(self) -> str:
        return textwrap.dedent(
            f"""\
            [Unit]
            Description=Hermes Agent Dashboard
            After=network-online.target
            Wants=network-online.target

            [Service]
            Type=simple
            User=ec2-user
            Group=ec2-user
            WorkingDirectory=/home/ec2-user
            Environment="HOME=/home/ec2-user"
            Environment="HERMES_HOME=/home/ec2-user/.hermes"
            Environment="AWS_REGION={self.region}"
            Environment="AWS_DEFAULT_REGION={self.region}"
            Environment="PATH=/home/ec2-user/.local/bin:/home/ec2-user/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"
            ExecStart=/home/ec2-user/.local/bin/hermes dashboard --host 0.0.0.0 --port {DASHBOARD_PORT} --no-open
            Restart=always
            RestartSec=10
            StandardOutput=journal
            StandardError=journal
            SyslogIdentifier=hermes-dashboard

            [Install]
            WantedBy=multi-user.target
            """
        )

    def _knowledge_base_install_commands(
        self, knowledge_base: Dict[str, Any]
    ) -> List[str]:
        skill_md = RETRIEVE_SKILL_PATH / "SKILL.md"
        skill_script = (
            RETRIEVE_SKILL_PATH / "scripts" / "retrieve_search.py"
        )
        for path in (skill_md, skill_script):
            if not path.is_file():
                raise RuntimeError(
                    f"Knowledge Base skill 파일이 없습니다: {path}"
                )

        config = {
            "region": self.region,
            "knowledge_base_id": knowledge_base["knowledge_base_id"],
            "data_source_id": knowledge_base[
                "knowledge_base_data_source_id"
            ],
            "bucket": knowledge_base["knowledge_base_bucket"],
            "document_prefix": "docs/",
            "presigned_url_expires_in": 3600,
        }
        files = {
            (
                "/home/ec2-user/.hermes/skills/retrieve/SKILL.md"
            ): skill_md.read_bytes(),
            (
                "/home/ec2-user/.hermes/skills/retrieve/scripts/"
                "retrieve_search.py"
            ): skill_script.read_bytes(),
            (
                "/home/ec2-user/.hermes/knowledge-base.json"
            ): (
                json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8"),
        }
        commands = [
            'echo "=== Installing Hermes Knowledge Base skill ==="',
            # 각 경로 레벨을 ec2-user 소유로 생성합니다. install -d에 -o를 줘도
            # 새로 만들어지는 상위 디렉터리는 root 소유가 되어, 이후 ec2-user로
            # 실행되는 hermes install.sh가 .hermes/bin을 만들지 못합니다.
            (
                "install -d -m 700 -o ec2-user -g ec2-user "
                "/home/ec2-user/.hermes"
            ),
            (
                "install -d -m 755 -o ec2-user -g ec2-user "
                "/home/ec2-user/.hermes/skills "
                "/home/ec2-user/.hermes/skills/retrieve "
                "/home/ec2-user/.hermes/skills/retrieve/scripts"
            ),
        ]
        for destination, content in files.items():
            encoded = base64.b64encode(content).decode("ascii")
            commands.append(
                f"printf '%s' '{encoded}' | base64 --decode > {destination}"
            )
        commands.extend([
            (
                "chmod 644 "
                "/home/ec2-user/.hermes/skills/retrieve/SKILL.md"
            ),
            (
                "chmod 755 /home/ec2-user/.hermes/skills/retrieve/scripts/"
                "retrieve_search.py"
            ),
            "chmod 600 /home/ec2-user/.hermes/knowledge-base.json",
            (
                "chown -R ec2-user:ec2-user "
                "/home/ec2-user/.hermes/skills/retrieve "
                "/home/ec2-user/.hermes/knowledge-base.json"
            ),
            "",
        ])
        return commands

    def _install_retrieve_skill_via_ssm(
        self, instance_id: str, knowledge_base: Dict[str, Any]
    ) -> None:
        commands = [
            item
            for item in self._knowledge_base_install_commands(
                knowledge_base
            )
            if item
        ]
        self._run_ssm_commands(
            instance_id,
            comment="Install Hermes Knowledge Base retrieve skill",
            commands=commands,
        )
        logger.info("  Knowledge Base retrieve skill 설치 완료")

    def _reconcile_instance_bootstrap_via_ssm(
        self,
        instance_id: str,
        *,
        model_id: str,
        install_browser: bool,
    ) -> None:
        install_options = "--non-interactive --skip-setup"
        if not install_browser:
            install_options += " --skip-browser"
        hermes = (
            "sudo -u ec2-user -H env "
            "HOME=/home/ec2-user HERMES_HOME=/home/ec2-user/.hermes "
            "PATH=/home/ec2-user/.local/bin:/home/ec2-user/.hermes/node/bin:"
            "/usr/local/bin:/usr/bin:/bin "
            "/home/ec2-user/.local/bin/hermes"
        )
        commands = [
            "set -euo pipefail",
            (
                "dnf install -y --allowerasing "
                + " ".join(self._hermes_system_packages(install_browser))
            ),
            "timedatectl set-timezone Asia/Seoul || true",
            (
                "install -d -m 700 -o ec2-user -g ec2-user "
                "/home/ec2-user/.hermes"
            ),
            # 이전 실패로 skill만 root 권한으로 설치돼 .hermes가 root 소유일 수
            # 있습니다. install.sh를 ec2-user로 실행하기 전에 소유권을 복구합니다.
            "chown -R ec2-user:ec2-user /home/ec2-user/.hermes",
            (
                f"if ! {hermes} --version >/dev/null 2>&1; then "
                "sudo -u ec2-user -H bash -lc "
                + shlex.quote(
                    "set -o pipefail; "
                    "curl -fsSL "
                    "https://hermes-agent.nousresearch.com/install.sh | "
                    f"bash -s -- {install_options}"
                )
                + "; fi"
            ),
            "test -x /home/ec2-user/.local/bin/hermes",
        ]
        config_values = {
            "model.default": model_id,
            "model.provider": "bedrock",
            "model.base_url": (
                f"https://bedrock-runtime.{self.region}.amazonaws.com"
            ),
            "bedrock.region": self.region,
        }
        commands.extend(
            f"{hermes} config set {shlex.quote(key)} {shlex.quote(value)}"
            for key, value in config_values.items()
        )
        if install_browser:
            commands.append(
                "sudo -u ec2-user -H env HOME=/home/ec2-user "
                "PATH=/home/ec2-user/.hermes/node/bin:/usr/local/bin:"
                "/usr/bin:/bin bash -lc "
                + shlex.quote(
                    "if ! find /home/ec2-user/.cache/ms-playwright "
                    "-maxdepth 1 -type d -name 'chromium-*' "
                    "-print -quit 2>/dev/null | grep -q .; then "
                    "cd /home/ec2-user/.hermes/hermes-agent && "
                    "npx playwright install chromium; fi"
                )
            )
        encoded_unit = base64.b64encode(
            self._dashboard_systemd_unit().encode("utf-8")
        ).decode("ascii")
        commands.extend([
            (
                f"printf '%s' '{encoded_unit}' | base64 --decode > "
                "/etc/systemd/system/hermes-dashboard.service"
            ),
            "chmod 644 /etc/systemd/system/hermes-dashboard.service",
            "systemctl daemon-reload",
        ])
        self._run_ssm_commands(
            instance_id,
            comment="Reconcile Hermes Agent bootstrap",
            commands=commands,
            timeout_seconds=1800,
        )
        logger.info("  기존 EC2 Hermes bootstrap 복구 완료")

    def _wait_for_ssm_online(self, instance_id: str) -> None:
        deadline = time.time() + 900
        while time.time() < deadline:
            information = self.ssm.describe_instance_information(
                Filters=[{
                    "Key": "InstanceIds",
                    "Values": [instance_id],
                }]
            ).get("InstanceInformationList", [])
            if (
                information
                and information[0].get("PingStatus") == "Online"
            ):
                break
            time.sleep(10)
        else:
            raise TimeoutError(
                f"SSM managed instance ONLINE 대기 시간 초과: {instance_id}"
            )

    def _run_ssm_commands(
        self,
        instance_id: str,
        *,
        comment: str,
        commands: List[str],
        timeout_seconds: int = 900,
    ) -> None:
        self._wait_for_ssm_online(instance_id)
        response = self.ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Comment=comment,
            Parameters={
                "commands": commands,
                "executionTimeout": [str(timeout_seconds)],
            },
        )
        command_id = response["Command"]["CommandId"]
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                invocation = self.ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )
            except ClientError as exc:
                if _error_code(exc) == "InvocationDoesNotExist":
                    time.sleep(5)
                    continue
                raise
            status = invocation["Status"]
            if status == "Success":
                return
            if status in (
                "Cancelled",
                "Failed",
                "TimedOut",
                "Cancelling",
            ):
                detail = (
                    invocation.get("StandardErrorContent")
                    or invocation.get("StandardOutputContent")
                    or status
                )
                raise RuntimeError(
                    f"SSM 명령 실패 ({comment}): {detail}"
                )
            time.sleep(5)
        raise TimeoutError(
            f"SSM 명령 시간 초과 ({comment}): {command_id}"
        )

    def _configure_dashboard_oauth_via_ssm(
        self,
        instance_id: str,
        *,
        oauth_client_id: str,
        public_url: str,
    ) -> None:
        hermes = (
            "sudo -u ec2-user -H env "
            "HOME=/home/ec2-user HERMES_HOME=/home/ec2-user/.hermes "
            "PATH=/home/ec2-user/.local/bin:/home/ec2-user/.hermes/node/bin:"
            "/usr/local/bin:/usr/bin:/bin "
            "/home/ec2-user/.local/bin/hermes"
        )
        config_values = {
            "dashboard.oauth.client_id": oauth_client_id,
            "dashboard.public_url": public_url,
            "dashboard.basic_auth.username": "",
            "dashboard.basic_auth.password_hash": "",
            "dashboard.basic_auth.password": "",
            "dashboard.basic_auth.secret": "",
        }
        commands = [
            "set -euo pipefail",
            (
                "for attempt in $(seq 1 180); do "
                "[ -x /home/ec2-user/.local/bin/hermes ] && "
                "[ -f /etc/systemd/system/hermes-dashboard.service ] && break; "
                "sleep 5; done"
            ),
            "test -x /home/ec2-user/.local/bin/hermes",
            "test -f /etc/systemd/system/hermes-dashboard.service",
        ]
        commands.extend(
            f"{hermes} config set {shlex.quote(key)} {shlex.quote(value)}"
            for key, value in config_values.items()
        )
        commands.extend([
            (
                "if [ -f /home/ec2-user/.hermes/.env ]; then "
                "sed -i '/^HERMES_DASHBOARD_BASIC_AUTH_/d' "
                "/home/ec2-user/.hermes/.env; "
                "chown ec2-user:ec2-user /home/ec2-user/.hermes/.env; "
                "chmod 600 /home/ec2-user/.hermes/.env; fi"
            ),
            "systemctl daemon-reload",
            "systemctl enable hermes-dashboard.service",
            "systemctl restart hermes-dashboard.service",
            "systemctl is-active --quiet hermes-dashboard.service",
        ])
        self._run_ssm_commands(
            instance_id,
            comment="Configure Hermes Dashboard Nous OAuth",
            commands=commands,
            timeout_seconds=1200,
        )
        logger.info("  Nous OAuth 설정 및 Dashboard 재시작 완료")

    def _disable_dashboard_via_ssm(self, instance_id: str) -> None:
        self._run_ssm_commands(
            instance_id,
            comment="Disable Dashboard until OAuth is configured",
            commands=[
                "systemctl disable --now hermes-dashboard.service || true",
            ],
        )
        logger.info("  OAuth 미설정 Dashboard 중지 완료")

    def _resolve_ami_id(self) -> str:
        try:
            ami_id = self.ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        except ClientError as exc:
            raise RuntimeError(
                f"Amazon Linux 2023 AMI 조회 실패 ({AMI_PARAMETER}): {exc}"
            ) from exc
        logger.info("  Amazon Linux 2023 AMI: %s", ami_id)
        return ami_id

    def _find_existing_instance(self) -> Optional[Dict[str, str]]:
        filters = [
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
        reservations = self.ec2.describe_instances(Filters=filters)["Reservations"]
        instances = [
            instance
            for reservation in reservations
            for instance in reservation["Instances"]
        ]
        if len(instances) > 1:
            raise RuntimeError(
                f"관리 대상 EC2가 여러 개입니다: {[i['InstanceId'] for i in instances]}"
            )
        if not instances:
            return None
        instance = instances[0]
        return {
            "instance_id": instance["InstanceId"],
            "private_ip": instance.get("PrivateIpAddress", ""),
            "state": instance["State"]["Name"],
            "vpc_id": instance["VpcId"],
        }

    def _ensure_instance(
        self,
        *,
        existing: Optional[Dict[str, str]],
        subnet_id: str,
        sg_id: str,
        profile_name: str,
        user_data: str,
        key_name: Optional[str],
        instance_type: str,
        ami_id: str,
        volume_size: int,
    ) -> Dict[str, str]:
        if existing:
            instance_id = existing["instance_id"]
            if existing["state"] == "stopping":
                self.ec2.get_waiter("instance_stopped").wait(
                    InstanceIds=[instance_id]
                )
                existing["state"] = "stopped"
            if existing["state"] == "stopped":
                self.ec2.start_instances(InstanceIds=[instance_id])
            self.ec2.get_waiter("instance_running").wait(
                InstanceIds=[instance_id]
            )
            details = self.ec2.describe_instances(
                InstanceIds=[instance_id]
            )["Reservations"][0]["Instances"][0]
            logger.info("  EC2 재사용: %s", instance_id)
        else:
            params: Dict[str, Any] = {
                "ImageId": ami_id,
                "InstanceType": instance_type,
                "IamInstanceProfile": {"Name": profile_name},
                "NetworkInterfaces": [{
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "Groups": [sg_id],
                    "AssociatePublicIpAddress": False,
                    "DeleteOnTermination": True,
                }],
                "BlockDeviceMappings": [{
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": volume_size,
                        "VolumeType": "gp3",
                        "Encrypted": True,
                        "DeleteOnTermination": True,
                    },
                }],
                "MetadataOptions": {
                    "HttpTokens": "required",
                    "HttpEndpoint": "enabled",
                },
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": _resource_tags(
                            self.project,
                            self.deployment_id,
                            name=self.project,
                        ),
                    },
                    {
                        "ResourceType": "volume",
                        "Tags": _resource_tags(
                            self.project,
                            self.deployment_id,
                            name=f"{self.project}-data",
                        ),
                    },
                ],
                "UserData": user_data,
                "MinCount": 1,
                "MaxCount": 1,
            }
            if key_name:
                params["KeyName"] = key_name

            last_error: Optional[Exception] = None
            for attempt in range(10):
                try:
                    instance_id = self.ec2.run_instances(**params)[
                        "Instances"
                    ][0]["InstanceId"]
                    break
                except ClientError as exc:
                    last_error = exc
                    if (
                        "Invalid IAM Instance Profile" in str(exc)
                        and attempt < 9
                    ):
                        time.sleep(min(5 * (attempt + 1), 30))
                        continue
                    raise
            else:
                raise RuntimeError(
                    f"EC2 생성 실패: {last_error}"
                ) from last_error

            self.state.update(instance_id=instance_id)
            self.ec2.get_waiter("instance_running").wait(
                InstanceIds=[instance_id]
            )
            details = self.ec2.describe_instances(
                InstanceIds=[instance_id]
            )["Reservations"][0]["Instances"][0]
            logger.info("  EC2 생성: %s", instance_id)

        result = {
            "instance_id": details["InstanceId"],
            "private_ip": details["PrivateIpAddress"],
        }
        self.state.update(**result)
        return result

    # ------------------------------------------------------------------
    # ALB and CloudFront
    # ------------------------------------------------------------------
    def _reject_legacy_route53_deployment(self) -> None:
        legacy_keys = [
            key
            for key in (
                "origin_domain_name",
                "route53_hosted_zone_id",
                "origin_certificate_arn",
                "origin_alias_record_created",
            )
            if self.state.data.get(key)
        ]
        if legacy_keys:
            raise RuntimeError(
                "기존 배포가 Route 53 origin 방식을 사용합니다. 현재 버전은 "
                "CloudFront VPC origin만 지원하므로 이전 버전 uninstaller로 "
                f"삭제한 뒤 다시 설치하세요. (state: {', '.join(legacy_keys)})"
            )

    def _vpc_origin_name(self) -> str:
        return f"{self.project}-alb-vpc-origin"

    def _find_vpc_origin(self) -> Optional[Dict[str, str]]:
        vpc_origin_id = self.state.data.get("vpc_origin_id")
        if vpc_origin_id:
            try:
                vpc_origin = self.cf.get_vpc_origin(Id=vpc_origin_id)[
                    "VpcOrigin"
                ]
                return {
                    "vpc_origin_id": vpc_origin["Id"],
                    "vpc_origin_arn": vpc_origin["Arn"],
                    "status": vpc_origin["Status"],
                    "endpoint_arn": vpc_origin[
                        "VpcOriginEndpointConfig"
                    ]["Arn"],
                }
            except ClientError as exc:
                if _error_code(exc) != "EntityNotFound":
                    raise

        expected_name = self._vpc_origin_name()
        marker: Optional[str] = None
        while True:
            params: Dict[str, Any] = {}
            if marker:
                params["Marker"] = marker
            response = self.cf.list_vpc_origins(**params)["VpcOriginList"]
            for item in response.get("Items", []):
                if item["Name"] != expected_name:
                    continue
                vpc_origin = self.cf.get_vpc_origin(Id=item["Id"])[
                    "VpcOrigin"
                ]
                return {
                    "vpc_origin_id": vpc_origin["Id"],
                    "vpc_origin_arn": vpc_origin["Arn"],
                    "status": vpc_origin["Status"],
                    "endpoint_arn": vpc_origin[
                        "VpcOriginEndpointConfig"
                    ]["Arn"],
                }
            if not response.get("IsTruncated"):
                return None
            marker = response.get("NextMarker")

    def _ensure_vpc_origin(self, alb_arn: str) -> Dict[str, str]:
        existing = self._find_vpc_origin()
        if existing:
            if existing["endpoint_arn"] != alb_arn:
                raise RuntimeError(
                    f"기존 VPC origin {existing['vpc_origin_id']}이 다른 "
                    "resource를 가리켜 재사용을 거부합니다."
                )
            vpc_origin_id = existing["vpc_origin_id"]
            result = {
                "vpc_origin_id": vpc_origin_id,
                "vpc_origin_arn": existing["vpc_origin_arn"],
            }
        else:
            vpc_origin = self.cf.create_vpc_origin(
                VpcOriginEndpointConfig={
                    "Name": self._vpc_origin_name(),
                    "Arn": alb_arn,
                    "HTTPPort": 80,
                    "HTTPSPort": 443,
                    "OriginProtocolPolicy": "http-only",
                },
                Tags={
                    "Items": _resource_tags(
                        self.project, self.deployment_id
                    )
                },
            )["VpcOrigin"]
            vpc_origin_id = vpc_origin["Id"]
            result = {
                "vpc_origin_id": vpc_origin_id,
                "vpc_origin_arn": vpc_origin["Arn"],
            }
            logger.info("  CloudFront VPC origin 생성: %s", vpc_origin_id)
        self.state.update(**result)
        self._wait_for_vpc_origin_deployed(vpc_origin_id)
        return result

    def _wait_for_vpc_origin_deployed(
        self, vpc_origin_id: str, timeout_seconds: int = 1200
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            vpc_origin = self.cf.get_vpc_origin(Id=vpc_origin_id)[
                "VpcOrigin"
            ]
            status = vpc_origin["Status"]
            if status == "Deployed":
                logger.info(
                    "  CloudFront VPC origin 준비 완료: %s", vpc_origin_id
                )
                return
            time.sleep(15)
        raise TimeoutError(
            f"CloudFront VPC origin 배포 대기 시간 초과: {vpc_origin_id}"
        )

    def _ensure_load_balancer(
        self,
        *,
        vpc_id: str,
        private_subnets: List[str],
        alb_sg_id: str,
        instance_id: str,
        origin_header_value: str,
        enable_cloudfront: bool,
    ) -> Dict[str, str]:
        alb_name = f"alb-{self.project}"[:32]
        try:
            load_balancers = self.elbv2.describe_load_balancers(
                Names=[alb_name]
            )["LoadBalancers"]
        except ClientError as exc:
            if _error_code(exc) != "LoadBalancerNotFound":
                raise
            load_balancers = []

        if load_balancers:
            load_balancer = load_balancers[0]
            tags = self.elbv2.describe_tags(
                ResourceArns=[load_balancer["LoadBalancerArn"]]
            )["TagDescriptions"][0]["Tags"]
            self._assert_managed_tags(tags, "ALB")
            if load_balancer["VpcId"] != vpc_id:
                raise RuntimeError("기존 ALB가 현재 Hermes VPC에 속하지 않습니다.")
            if load_balancer["Scheme"] != "internal":
                raise RuntimeError(
                    "기존 ALB가 internet-facing입니다. 이 배포는 CloudFront "
                    "VPC origin + internal ALB 구성만 지원하므로 "
                    "uninstaller로 삭제한 뒤 다시 설치하세요."
                )
        else:
            load_balancer = self.elbv2.create_load_balancer(
                Name=alb_name,
                Subnets=private_subnets,
                SecurityGroups=[alb_sg_id],
                Scheme="internal",
                Type="application",
                IpAddressType="ipv4",
                Tags=_resource_tags(
                    self.project, self.deployment_id, name=alb_name
                ),
            )["LoadBalancers"][0]
            logger.info("  Internal ALB 생성: %s", load_balancer["DNSName"])

        alb_arn = load_balancer["LoadBalancerArn"]
        self.elbv2.get_waiter("load_balancer_available").wait(
            LoadBalancerArns=[alb_arn]
        )
        self.elbv2.modify_load_balancer_attributes(
            LoadBalancerArn=alb_arn,
            Attributes=[
                {"Key": "idle_timeout.timeout_seconds", "Value": "4000"},
                {"Key": "deletion_protection.enabled", "Value": "false"},
            ],
        )

        target_group = self._ensure_target_group(vpc_id)
        target_group_arn = target_group["TargetGroupArn"]
        health = self.elbv2.describe_target_health(
            TargetGroupArn=target_group_arn
        )["TargetHealthDescriptions"]
        registered_ids = {item["Target"]["Id"] for item in health}
        stale_targets = [
            {"Id": target_id, "Port": DASHBOARD_PORT}
            for target_id in registered_ids
            if target_id != instance_id
        ]
        if stale_targets:
            self.elbv2.deregister_targets(
                TargetGroupArn=target_group_arn, Targets=stale_targets
            )
        if instance_id not in registered_ids:
            self.elbv2.register_targets(
                TargetGroupArn=target_group_arn,
                Targets=[{"Id": instance_id, "Port": DASHBOARD_PORT}],
            )

        listener = self._ensure_listener(
            alb_arn,
            target_group_arn,
            origin_header_value,
            enable_cloudfront=enable_cloudfront,
        )
        result = {
            "alb_arn": alb_arn,
            "alb_name": alb_name,
            "alb_dns_name": load_balancer["DNSName"],
            "target_group_arn": target_group_arn,
            "listener_arn": listener["ListenerArn"],
        }
        self.state.update(**result)
        return {
            **result,
            "dns_name": load_balancer["DNSName"],
        }

    def _ensure_target_group(self, vpc_id: str) -> Dict[str, Any]:
        name = f"tg-{self.project}"[:32]
        try:
            groups = self.elbv2.describe_target_groups(Names=[name])[
                "TargetGroups"
            ]
        except ClientError as exc:
            if _error_code(exc) != "TargetGroupNotFound":
                raise
            groups = []
        if groups:
            group = groups[0]
            tags = self.elbv2.describe_tags(
                ResourceArns=[group["TargetGroupArn"]]
            )["TagDescriptions"][0]["Tags"]
            self._assert_managed_tags(tags, "Target Group")
            if group["VpcId"] != vpc_id:
                raise RuntimeError("기존 Target Group이 현재 Hermes VPC와 다릅니다.")
            return group

        group = self.elbv2.create_target_group(
            Name=name,
            Protocol="HTTP",
            Port=DASHBOARD_PORT,
            VpcId=vpc_id,
            TargetType="instance",
            HealthCheckEnabled=True,
            HealthCheckProtocol="HTTP",
            HealthCheckPath="/",
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=10,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=5,
            Matcher={"HttpCode": "200-399"},
            Tags=_resource_tags(
                self.project, self.deployment_id, name=name
            ),
        )["TargetGroups"][0]
        logger.info("  Target Group 생성: %s", name)
        return group

    def _ensure_listener(
        self,
        alb_arn: str,
        target_group_arn: str,
        origin_header_value: str,
        *,
        enable_cloudfront: bool,
    ) -> Dict[str, Any]:
        listeners = self.elbv2.describe_listeners(
            LoadBalancerArn=alb_arn
        )["Listeners"]
        listener = next(
            (
                item
                for item in listeners
                if item["Protocol"] == "HTTP" and item["Port"] == 80
            ),
            None,
        )
        default_actions = (
            [{
                "Type": "fixed-response",
                "FixedResponseConfig": {
                    "StatusCode": "403",
                    "ContentType": "text/plain",
                    "MessageBody": "Access denied",
                },
            }]
            if enable_cloudfront
            else [{"Type": "forward", "TargetGroupArn": target_group_arn}]
        )
        if listener:
            self.elbv2.modify_listener(
                ListenerArn=listener["ListenerArn"],
                DefaultActions=default_actions,
            )
            listener = self.elbv2.describe_listeners(
                ListenerArns=[listener["ListenerArn"]]
            )["Listeners"][0]
        else:
            listener = self.elbv2.create_listener(
                LoadBalancerArn=alb_arn,
                Protocol="HTTP",
                Port=80,
                DefaultActions=default_actions,
                Tags=_resource_tags(self.project, self.deployment_id),
            )["Listeners"][0]

        if enable_cloudfront:
            self._ensure_origin_header_rule(
                listener["ListenerArn"],
                target_group_arn,
                origin_header_value,
            )
        return listener

    def _ensure_origin_header_rule(
        self,
        listener_arn: str,
        target_group_arn: str,
        origin_header_value: str,
    ) -> None:
        rules = self.elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
        condition = {
            "Field": "http-header",
            "HttpHeaderConfig": {
                "HttpHeaderName": CUSTOM_HEADER_NAME,
                "Values": [origin_header_value],
            },
        }
        actions = [{"Type": "forward", "TargetGroupArn": target_group_arn}]
        for rule in rules:
            for item in rule.get("Conditions", []):
                config = item.get("HttpHeaderConfig", {})
                if config.get("HttpHeaderName", "").lower() == (
                    CUSTOM_HEADER_NAME.lower()
                ):
                    self.elbv2.modify_rule(
                        RuleArn=rule["RuleArn"],
                        Conditions=[condition],
                        Actions=actions,
                    )
                    return

        used_priorities = {
            int(rule["Priority"])
            for rule in rules
            if rule.get("Priority", "").isdigit()
        }
        priority = next(
            value for value in range(10, 50001) if value not in used_priorities
        )
        self.elbv2.create_rule(
            ListenerArn=listener_arn,
            Priority=priority,
            Conditions=[condition],
            Actions=actions,
            Tags=_resource_tags(self.project, self.deployment_id),
        )

    def _cloudfront_comment(self) -> str:
        return f"Hermes Agent {self.project}"

    def _find_cloudfront_distribution(self) -> Optional[Dict[str, str]]:
        distribution_id = self.state.data.get("distribution_id")
        if distribution_id:
            try:
                distribution = self.cf.get_distribution(Id=distribution_id)[
                    "Distribution"
                ]
                return {
                    "distribution_id": distribution["Id"],
                    "domain_name": distribution["DomainName"],
                }
            except ClientError as exc:
                if _error_code(exc) != "NoSuchDistribution":
                    raise

        paginator = self.cf.get_paginator("list_distributions")
        for page in paginator.paginate():
            for item in page.get("DistributionList", {}).get("Items", []):
                if item.get("Comment") == self._cloudfront_comment():
                    return {
                        "distribution_id": item["Id"],
                        "domain_name": item["DomainName"],
                    }
        return None

    def _assert_managed_cloudfront(self, distribution_id: str) -> None:
        arn = (
            f"arn:aws:cloudfront::{self.account_id}:"
            f"distribution/{distribution_id}"
        )
        tags = self.cf.list_tags_for_resource(Resource=arn)["Tags"].get(
            "Items", []
        )
        self._assert_managed_tags(tags, "CloudFront")

    def _ensure_cloudfront(
        self,
        alb_dns_name: str,
        vpc_origin_id: str,
        origin_header_value: str,
    ) -> Dict[str, str]:
        existing = self._find_cloudfront_distribution()
        if existing:
            self._assert_managed_cloudfront(existing["distribution_id"])
            current = self.cf.get_distribution_config(
                Id=existing["distribution_id"]
            )
            if (
                current["DistributionConfig"].get("Comment")
                != self._cloudfront_comment()
            ):
                raise RuntimeError(
                    f"CloudFront {existing['distribution_id']}의 Comment가 "
                    "예상과 달라 업데이트를 거부합니다."
                )
            caller_reference = current["DistributionConfig"]["CallerReference"]
        else:
            caller_reference = f"{self.project}-{self.deployment_id}"

        origin_id = f"{self.project}-alb-origin"
        config = {
            "CallerReference": caller_reference,
            "Comment": self._cloudfront_comment(),
            "Enabled": True,
            "IsIPV6Enabled": True,
            "HttpVersion": "http2and3",
            "PriceClass": "PriceClass_All",
            # custom domain을 쓰지 않으므로 빈 Aliases. UpdateDistribution은
            # 전체 config 교체 방식이라 이 필드를 생략하면 IllegalUpdate
            # (Aliases are missing)가 발생합니다.
            "Aliases": {"Quantity": 0, "Items": []},
            "Origins": {
                "Quantity": 1,
                "Items": [{
                    "Id": origin_id,
                    "DomainName": alb_dns_name,
                    "OriginPath": "",
                    "CustomHeaders": {
                        "Quantity": 2,
                        "Items": [
                            {
                                "HeaderName": CUSTOM_HEADER_NAME,
                                "HeaderValue": origin_header_value,
                            },
                            {
                                "HeaderName": "X-Forwarded-Proto",
                                "HeaderValue": "https",
                            },
                        ],
                    },
                    "VpcOriginConfig": {
                        "VpcOriginId": vpc_origin_id,
                        "OriginReadTimeout": 60,
                        "OriginKeepaliveTimeout": 5,
                    },
                    "ConnectionAttempts": 3,
                    "ConnectionTimeout": 10,
                    "OriginShield": {"Enabled": False},
                }],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id,
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": [
                        "GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"
                    ],
                    "CachedMethods": {
                        "Quantity": 2,
                        "Items": ["GET", "HEAD"],
                    },
                },
                "SmoothStreaming": False,
                "Compress": True,
                "CachePolicyId": CLOUDFRONT_CACHE_DISABLED_POLICY_ID,
                "OriginRequestPolicyId": CLOUDFRONT_ALL_VIEWER_EXCEPT_HOST_POLICY_ID,
            },
            "ViewerCertificate": {
                "CloudFrontDefaultCertificate": True,
                "MinimumProtocolVersion": "TLSv1",
                "CertificateSource": "cloudfront",
            },
            "Restrictions": {
                "GeoRestriction": {
                    "RestrictionType": "none",
                    "Quantity": 0,
                }
            },
        }

        if existing:
            # UpdateDistribution은 전체 config 교체 방식이라, 우리가 명시하지
            # 않는 Logging, FieldLevelEncryptionId 등 필수 필드가 누락되면
            # IllegalUpdate/InvalidArgument가 발생합니다. 기존 config를
            # 베이스로 관리 대상 필드만 덮어써서 나머지는 그대로 보존합니다.
            merged_config = self._merge_distribution_config(
                current["DistributionConfig"], config
            )
            distribution = self.cf.update_distribution(
                Id=existing["distribution_id"],
                IfMatch=current["ETag"],
                DistributionConfig=merged_config,
            )["Distribution"]
            logger.info("  CloudFront 업데이트: %s", distribution["Id"])
        else:
            distribution = self.cf.create_distribution_with_tags(
                DistributionConfigWithTags={
                    "DistributionConfig": config,
                    "Tags": {
                        "Items": _resource_tags(
                            self.project, self.deployment_id
                        )
                    },
                }
            )["Distribution"]
            logger.info("  CloudFront 생성: %s", distribution["Id"])

        result = {
            "distribution_id": distribution["Id"],
            "domain_name": distribution["DomainName"],
        }
        self.state.update(**result)
        return result

    @staticmethod
    def _merge_distribution_config(
        base: Dict[str, Any], desired: Dict[str, Any]
    ) -> Dict[str, Any]:
        """기존 distribution config 위에 관리 대상 설정을 덮어씁니다.

        UpdateDistribution은 전체 교체 방식이므로 top-level뿐 아니라
        DefaultCacheBehavior, origin item 내부의 필수 하위 필드
        (FieldLevelEncryptionId, TrustedSigners 등)도 보존해야 합니다.
        dict 값은 한 단계 더 병합하고, 그 외에는 desired가 우선합니다.
        """
        merged = {**base, **desired}
        for key, value in desired.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged[key] = {**base[key], **value}
        # Origins.Items는 origin Id 기준으로 병합해 기존 항목의 필수
        # 하위 필드를 유지합니다.
        base_items = {
            item.get("Id"): item
            for item in base.get("Origins", {}).get("Items", [])
        }
        desired_origins = desired.get("Origins", {}).get("Items")
        if desired_origins is not None:
            merged_items = [
                {**base_items.get(item.get("Id"), {}), **item}
                for item in desired_origins
            ]
            merged["Origins"] = {
                **base.get("Origins", {}),
                **desired["Origins"],
                "Items": merged_items,
                "Quantity": len(merged_items),
            }
        return merged

    def _wait_for_cloudfront_deployed(
        self, distribution_id: str
    ) -> None:
        self.cf.get_waiter("distribution_deployed").wait(
            Id=distribution_id,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
        logger.info("  CloudFront 배포 완료: %s", distribution_id)

    # ------------------------------------------------------------------
    # Local output
    # ------------------------------------------------------------------
    def _write_deployment_info(
        self, path: Path, output: Dict[str, Any]
    ) -> None:
        url = f"https://{output['domain_name']}/"
        if output.get("dashboard_oauth_configured"):
            dashboard_access = textwrap.dedent(f"""\
                - URL: {url}
                - Authentication: Nous OAuth
                - OAuth client ID: `{output["dashboard_oauth_client_id"]}`

                Nous Portal 계정으로 로그인합니다. 등록을 관리하거나 폐기하려면
                https://portal.nousresearch.com/local-dashboards 를 사용하세요.
            """)
        else:
            dashboard_access = textwrap.dedent(f"""\
                - Status: OAuth registration pending
                - Public URL: {url}
                - OAuth callback URL: `{output["dashboard_oauth_callback_url"]}`

                Nous Portal의 https://portal.nousresearch.com/local-dashboards 에서
                위 callback URL로 self-hosted dashboard를 등록한 뒤, 발급된
                `agent:...` client ID를 사용해 같은 옵션으로 다시 실행하세요.

                ```bash
                python3 installer.py \\
                  --region {output["region"]} \\
                  --project-name {output["project"]} \\
                  --dashboard-oauth-client-id agent:<ID>
                ```

                OAuth가 구성되기 전에는 Dashboard 서비스가 중지되어 있습니다.
            """)

        content = textwrap.dedent(f"""\
            # Hermes Agent AWS 배포 정보

            | 항목 | 값 |
            |---|---|
            | Region | {output["region"]} |
            | Deployment ID | {output["deployment_id"]} |
            | VPC | {output["vpc_id"]} ({output["vpc_cidr"]}) |
            | Public Subnets | {", ".join(output["public_subnets"])} |
            | Private Subnets | {", ".join(output["private_subnets"])} |
            | NAT Gateway | {output["nat_gateway_id"]} |
            | Bedrock Endpoint | {output["bedrock_endpoint_id"]} |
            | Bedrock Agent Endpoint | {output.get("bedrock_agent_endpoint_id") or "disabled"} |
            | EC2 | {output["instance_id"]} ({output["private_ip"]}) |
            | Internal ALB | {output["alb_dns_name"]} |
            | CloudFront VPC Origin | {output.get("vpc_origin_id") or "disabled"} |
            | CloudFront | {output.get("distribution_id") or "disabled"} |
            | Bedrock Model | {output["model_id"]} |
            | Knowledge Base | {output.get("knowledge_base_id") or "disabled"} |
            | Knowledge Base S3 | {output.get("knowledge_base_bucket") or "disabled"} |
            | OpenSearch Collection | {output.get("opensearch_collection_id") or "disabled"} |

            ## Dashboard

            {textwrap.indent(dashboard_access.rstrip(), "            ").lstrip()}

            ## EC2 접속

            ```bash
            aws ssm start-session --target {output["instance_id"]} --region {output["region"]}
            ```

            ## 서비스 확인

            ```bash
            sudo systemctl status hermes-dashboard.service
            sudo journalctl -u hermes-dashboard.service -f
            sudo tail -150 /var/log/hermes-install.log
            ```

            ## Knowledge Base

            로컬 `contents/` 문서를 업로드하고 동기화합니다.

            ```bash
            python3 add_content.py
            ```

            Hermes에서는 `/retrieve <질문>`으로 검색할 수 있습니다.
        """)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        logger.info("19) 배포 정보 기록: %s", path)


def check_application_ready(
    url: str, max_attempts: int = 120, wait_seconds: int = 10
) -> bool:
    logger.info("애플리케이션 준비 확인: %s", url)
    start = time.time()
    for attempt in range(1, max_attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if 200 <= response.getcode() < 500:
                    logger.info(
                        "  Hermes Dashboard 응답 확인 (HTTP %d, %.1f분)",
                        response.getcode(),
                        (time.time() - start) / 60,
                    )
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                logger.info(
                    "  Hermes Dashboard 인증 응답 확인 (HTTP %d)", exc.code
                )
                return True
            if attempt == 1 or attempt % 6 == 0:
                logger.info(
                    "  준비 대기 [%d/%d]: HTTP %d",
                    attempt,
                    max_attempts,
                    exc.code,
                )
        except (urllib.error.URLError, OSError) as exc:
            if attempt == 1 or attempt % 6 == 0:
                logger.info(
                    "  준비 대기 [%d/%d]: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
        if attempt < max_attempts:
            time.sleep(wait_seconds)
    logger.warning("Dashboard 준비 확인 시간 초과: %s", url)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hermes Agent 독립형 AWS 배포 "
            "(CloudFront -> ALB -> EC2 + Bedrock Knowledge Base)"
        )
    )
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument(
        "--vpc-cidr",
        default=None,
        help="전용 VPC CIDR (미지정 시 겹치지 않는 기본 /16 자동 선택)",
    )
    parser.add_argument(
        "--dashboard-oauth-client-id",
        default=None,
        help="Nous Portal에서 발급한 Dashboard OAuth client ID (agent:...)",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--key-name", default=None)
    parser.add_argument("--instance-type", default=INSTANCE_TYPE)
    parser.add_argument(
        "--ami-id",
        default=None,
        help="미지정 시 해당 리전의 최신 Amazon Linux 2023 AMI 사용",
    )
    parser.add_argument("--volume-size", type=int, default=VOLUME_SIZE)
    parser.add_argument(
        "--disable-cloudfront",
        action="store_true",
        help="보안상 현재 지원하지 않음 (public ALB HTTP 노출 방지)",
    )
    parser.add_argument(
        "--disable-knowledge-base",
        action="store_true",
        help="S3/OpenSearch Serverless/Bedrock Knowledge Base 생성 생략",
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Hermes browser tool용 Chromium 설치 생략",
    )
    parser.add_argument("--state-path", default=str(STATE_PATH))
    parser.add_argument(
        "--deployment-info-path", default=str(DEPLOYMENT_INFO_PATH)
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.volume_size < 20:
        parser.error("--volume-size는 20 이상이어야 합니다.")
    if args.disable_cloudfront:
        parser.error(
            "--disable-cloudfront는 공개 Dashboard를 HTTP로 노출하므로 지원하지 "
            "않습니다. CloudFront HTTPS를 사용하세요."
        )
    if (
        args.dashboard_oauth_client_id
        and not re.fullmatch(
            r"agent:[A-Za-z0-9._:-]+", args.dashboard_oauth_client_id
        )
    ):
        parser.error(
            "--dashboard-oauth-client-id는 Nous Portal이 발급한 "
            "'agent:...' 형식이어야 합니다."
        )
    _require_boto3()

    try:
        installer = HermesInstaller(
            region=args.region,
            project=args.project_name,
            state_path=Path(args.state_path),
        )
        logger.info("=" * 64)
        logger.info("Hermes Agent AWS standalone deployment")
        logger.info("Project: %s | Region: %s", args.project_name, args.region)
        logger.info("=" * 64)
        installer.run(
            vpc_cidr=args.vpc_cidr,
            dashboard_oauth_client_id=args.dashboard_oauth_client_id,
            model_id=args.model_id,
            key_name=args.key_name,
            instance_type=args.instance_type,
            ami_id=args.ami_id,
            volume_size=args.volume_size,
            enable_cloudfront=not args.disable_cloudfront,
            enable_knowledge_base=not args.disable_knowledge_base,
            install_browser=not args.skip_browser,
            deployment_info_path=Path(args.deployment_info_path),
        )
    except (ClientError, ValueError, RuntimeError, TimeoutError) as exc:
        logger.error("배포 실패: %s", exc)
        logger.info(
            "생성된 리소스 ID는 %s에 보존되었습니다. 문제를 해결한 뒤 같은 명령을 "
            "다시 실행하거나 uninstaller를 실행하세요.",
            args.state_path,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
