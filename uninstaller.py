#!/usr/bin/env python3
"""Remove resources created by installer.py."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ModuleNotFoundError as exc:
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment,misc]
    NoCredentialsError = Exception  # type: ignore[assignment,misc]
    BOTO_IMPORT_ERROR: Optional[ModuleNotFoundError] = exc
else:
    BOTO_IMPORT_ERROR = None


PROJECT_NAME = "hermes"
REGION = "us-west-2"
MANAGED_BY = "hermes-agent-installer"
STATE_PATH = Path("assets/hermes-deployment.json")
DEPLOYMENT_INFO_PATH = Path("assets/hermes-deployment-info.md")

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


def _tag_map(tags: Iterable[Dict[str, str]]) -> Dict[str, str]:
    return {
        tag["Key"]: tag["Value"]
        for tag in tags
        if tag.get("Key") is not None and tag.get("Value") is not None
    }


class HermesUninstaller:
    def __init__(
        self,
        *,
        region: str = REGION,
        project: str = PROJECT_NAME,
        state_path: Path = STATE_PATH,
        deployment_info_path: Path = DEPLOYMENT_INFO_PATH,
    ):
        _require_boto3()
        self.region = region
        self.project = project
        self.state_path = state_path
        self.deployment_info_path = deployment_info_path
        self.state = self._load_state()
        self.session = boto3.Session(region_name=region)  # type: ignore[union-attr]
        self.ec2 = self.session.client("ec2")
        self.elbv2 = self.session.client("elbv2")
        self.cf = self.session.client("cloudfront")
        self.iam = self.session.client("iam")
        self.s3 = self.session.client("s3")
        self.aoss = self.session.client("opensearchserverless")
        self.bedrock_agent = self.session.client("bedrock-agent")
        self.sts = self.session.client("sts")
        self.errors: List[str] = []

        try:
            self.account_id = self.sts.get_caller_identity()["Account"]
        except NoCredentialsError as exc:
            raise RuntimeError("AWS 자격 증명을 찾을 수 없습니다.") from exc

        self._validate_state()

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            logger.warning(
                "상태 파일이 없습니다. Hermes 관리 태그를 기준으로 리소스를 찾습니다: %s",
                self.state_path,
            )
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"배포 상태 파일을 읽을 수 없습니다: {self.state_path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"배포 상태 파일 형식이 올바르지 않습니다: {self.state_path}")
        return data

    def _validate_state(self) -> None:
        for key, expected in (
            ("region", self.region),
            ("project", self.project),
            ("account_id", self.account_id),
        ):
            actual = self.state.get(key)
            if actual and actual != expected:
                raise RuntimeError(
                    f"상태 파일의 {key}={actual!r}가 현재 값 {expected!r}와 다릅니다."
                )

    def run(self) -> bool:
        start = time.time()
        logger.info("=" * 64)
        logger.info("Hermes Agent AWS standalone uninstall")
        logger.info(
            "Project: %s | Region: %s | Account: %s",
            self.project,
            self.region,
            self.account_id,
        )
        logger.info("=" * 64)

        self._run_step("CloudFront 삭제", self.delete_cloudfront)
        self._run_step("CloudFront VPC Origin 삭제", self.delete_vpc_origin)
        self._run_step("ALB와 Target Group 삭제", self.delete_load_balancer)
        self._run_step("EC2 종료", self.terminate_instances)
        self._run_step(
            "Bedrock Knowledge Base 삭제", self.delete_knowledge_base
        )
        self._run_step(
            "OpenSearch Serverless 삭제", self.delete_opensearch
        )
        self._run_step(
            "Knowledge Base S3 Bucket 삭제",
            self.delete_knowledge_base_bucket,
        )
        self._run_step("Bedrock VPC Endpoint 삭제", self.delete_vpc_endpoints)
        self._run_step("NAT Gateway 삭제", self.delete_nat_gateways)
        self._run_step("Subnet과 Route Table 삭제", self.delete_subnets_and_routes)
        self._run_step("Security Group 삭제", self.delete_security_groups)
        self._run_step("Internet Gateway와 VPC 삭제", self.delete_vpc)
        self._run_step("IAM Role과 Instance Profile 삭제", self.delete_iam)
        self._run_step(
            "Knowledge Base IAM Role 삭제",
            self.delete_knowledge_base_iam,
        )
        self._run_step("NAT Elastic IP 해제", self.release_elastic_ips)

        elapsed = (time.time() - start) / 60
        if self.errors:
            logger.error("=" * 64)
            logger.error(
                "삭제가 일부 완료되지 않았습니다 (%.2f분, %d개 오류)",
                elapsed,
                len(self.errors),
            )
            for error in self.errors:
                logger.error("  - %s", error)
            logger.error("상태 파일을 보존합니다: %s", self.state_path)
            logger.error("=" * 64)
            return False

        for path in (self.state_path, self.deployment_info_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("로컬 파일 삭제 실패 %s: %s", path, exc)
        logger.info("=" * 64)
        logger.info("Hermes Agent 인프라 삭제 완료 (%.2f분)", elapsed)
        logger.info("=" * 64)
        return True

    def _run_step(self, label: str, operation) -> None:
        logger.info("%s", label)
        try:
            operation()
        except Exception as exc:  # Continue cleanup and report every failure.
            message = f"{label}: {exc}"
            self.errors.append(message)
            logger.warning("  %s", message)

    def _managed_filters(self) -> List[Dict[str, Any]]:
        return [
            {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
            {"Name": "tag:Project", "Values": [self.project]},
        ]

    def _is_managed(self, tags: Iterable[Dict[str, str]]) -> bool:
        values = _tag_map(tags)
        return (
            values.get("ManagedBy") == MANAGED_BY
            and values.get("Project") == self.project
        )

    def _managed_vpc(self) -> Optional[Dict[str, Any]]:
        vpc_id = self.state.get("vpc_id")
        if vpc_id:
            try:
                vpcs = self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
            except ClientError as exc:
                if _error_code(exc) == "InvalidVpcID.NotFound":
                    return None
                raise
            if vpcs:
                if not self._is_managed(vpcs[0].get("Tags", [])):
                    raise RuntimeError(
                        f"VPC {vpc_id}에 Hermes 관리 태그가 없어 삭제를 거부합니다."
                    )
                return vpcs[0]

        vpcs = self.ec2.describe_vpcs(Filters=self._managed_filters())["Vpcs"]
        if len(vpcs) > 1:
            raise RuntimeError(
                f"관리 대상 VPC가 여러 개입니다: {[v['VpcId'] for v in vpcs]}"
            )
        return vpcs[0] if vpcs else None

    # ------------------------------------------------------------------
    # CloudFront and ALB
    # ------------------------------------------------------------------
    def _cloudfront_comment(self) -> str:
        return f"Hermes Agent {self.project}"

    def _cloudfront_ids(self) -> List[str]:
        distribution_id = self.state.get("distribution_id")
        if distribution_id:
            return [distribution_id]
        ids: List[str] = []
        paginator = self.cf.get_paginator("list_distributions")
        for page in paginator.paginate():
            for item in page.get("DistributionList", {}).get("Items", []):
                if item.get("Comment") == self._cloudfront_comment():
                    ids.append(item["Id"])
        return ids

    def _assert_managed_cloudfront(self, distribution_id: str) -> None:
        arn = (
            f"arn:aws:cloudfront::{self.account_id}:"
            f"distribution/{distribution_id}"
        )
        tags = self.cf.list_tags_for_resource(Resource=arn)["Tags"].get(
            "Items", []
        )
        if not self._is_managed(tags):
            raise RuntimeError(
                f"CloudFront {distribution_id}에 Hermes 관리 태그가 없어 "
                "삭제를 거부합니다."
            )

    def delete_cloudfront(self) -> None:
        distribution_ids = self._cloudfront_ids()
        if not distribution_ids:
            logger.info("  삭제할 CloudFront 없음")
            return
        for distribution_id in distribution_ids:
            try:
                response = self.cf.get_distribution_config(Id=distribution_id)
            except ClientError as exc:
                if _error_code(exc) == "NoSuchDistribution":
                    continue
                raise

            self._assert_managed_cloudfront(distribution_id)
            config = response["DistributionConfig"]
            if config.get("Comment") != self._cloudfront_comment():
                raise RuntimeError(
                    f"CloudFront {distribution_id}의 Comment가 예상과 달라 삭제를 거부합니다."
                )
            if config.get("Enabled", True):
                config["Enabled"] = False
                self.cf.update_distribution(
                    Id=distribution_id,
                    IfMatch=response["ETag"],
                    DistributionConfig=config,
                )
                logger.info("  CloudFront 비활성화: %s", distribution_id)
            self._wait_cloudfront_deployed(distribution_id)

            response = self.cf.get_distribution_config(Id=distribution_id)
            self.cf.delete_distribution(
                Id=distribution_id, IfMatch=response["ETag"]
            )
            logger.info("  CloudFront 삭제: %s", distribution_id)

    def _wait_cloudfront_deployed(
        self, distribution_id: str, timeout_seconds: int = 1200
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            distribution = self.cf.get_distribution(Id=distribution_id)[
                "Distribution"
            ]
            if distribution["Status"] == "Deployed":
                return
            time.sleep(15)
        raise TimeoutError(
            f"CloudFront 비활성화 전파 시간 초과: {distribution_id}"
        )

    def _vpc_origin_ids(self) -> List[str]:
        vpc_origin_id = self.state.get("vpc_origin_id")
        if vpc_origin_id:
            return [vpc_origin_id]
        ids: List[str] = []
        expected_name = f"{self.project}-alb-vpc-origin"
        marker: Optional[str] = None
        while True:
            params: Dict[str, Any] = {}
            if marker:
                params["Marker"] = marker
            response = self.cf.list_vpc_origins(**params)["VpcOriginList"]
            ids.extend(
                item["Id"]
                for item in response.get("Items", [])
                if item["Name"] == expected_name
            )
            if not response.get("IsTruncated"):
                return ids
            marker = response.get("NextMarker")

    def _assert_managed_vpc_origin(self, vpc_origin_arn: str) -> None:
        tags = self.cf.list_tags_for_resource(
            Resource=vpc_origin_arn
        )["Tags"].get("Items", [])
        if not self._is_managed(tags):
            raise RuntimeError(
                f"VPC origin {vpc_origin_arn}에 Hermes 관리 태그가 없어 "
                "삭제를 거부합니다."
            )

    def delete_vpc_origin(self) -> None:
        vpc_origin_ids = self._vpc_origin_ids()
        if not vpc_origin_ids:
            logger.info("  삭제할 CloudFront VPC origin 없음")
            return
        for vpc_origin_id in vpc_origin_ids:
            try:
                response = self.cf.get_vpc_origin(Id=vpc_origin_id)
            except ClientError as exc:
                if _error_code(exc) == "EntityNotFound":
                    continue
                raise
            self._assert_managed_vpc_origin(
                response["VpcOrigin"]["Arn"]
            )
            # Deploying 상태에서는 삭제할 수 없고, distribution 삭제 직후에는
            # 참조 해제가 전파될 때까지 CannotDeleteEntityWhileInUse가
            # 발생하므로 재시도합니다.
            deadline = time.time() + 1200
            while True:
                try:
                    self.cf.delete_vpc_origin(
                        Id=vpc_origin_id,
                        IfMatch=response["ETag"],
                    )
                    logger.info(
                        "  CloudFront VPC origin 삭제 요청: %s",
                        vpc_origin_id,
                    )
                    break
                except ClientError as exc:
                    if _error_code(exc) == "EntityNotFound":
                        break
                    if _error_code(exc) not in (
                        "CannotDeleteEntityWhileInUse",
                        "IllegalDelete",
                        "PreconditionFailed",
                    ) or time.time() >= deadline:
                        raise
                    time.sleep(15)
                    response = self.cf.get_vpc_origin(Id=vpc_origin_id)
            self._wait_vpc_origin_deleted(vpc_origin_id)

    def _wait_vpc_origin_deleted(
        self, vpc_origin_id: str, timeout_seconds: int = 1200
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                self.cf.get_vpc_origin(Id=vpc_origin_id)
            except ClientError as exc:
                if _error_code(exc) == "EntityNotFound":
                    logger.info(
                        "  CloudFront VPC origin 삭제 완료: %s",
                        vpc_origin_id,
                    )
                    return
                raise
            time.sleep(15)
        raise TimeoutError(
            f"CloudFront VPC origin 삭제 대기 시간 초과: {vpc_origin_id}"
        )

    def delete_load_balancer(self) -> None:
        alb_arn = self.state.get("alb_arn")
        if not alb_arn:
            try:
                lbs = self.elbv2.describe_load_balancers(
                    Names=[f"alb-{self.project}"[:32]]
                )["LoadBalancers"]
            except ClientError as exc:
                if _error_code(exc) == "LoadBalancerNotFound":
                    lbs = []
                else:
                    raise
            alb_arn = lbs[0]["LoadBalancerArn"] if lbs else None

        if alb_arn:
            try:
                tags = self.elbv2.describe_tags(ResourceArns=[alb_arn])[
                    "TagDescriptions"
                ][0]["Tags"]
                if not self._is_managed(tags):
                    raise RuntimeError(
                        f"ALB {alb_arn}에 Hermes 관리 태그가 없어 삭제를 거부합니다."
                    )
                self.elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
                self.elbv2.get_waiter("load_balancers_deleted").wait(
                    LoadBalancerArns=[alb_arn],
                    WaiterConfig={"Delay": 15, "MaxAttempts": 60},
                )
                logger.info("  ALB 삭제: %s", alb_arn)
            except ClientError as exc:
                if _error_code(exc) != "LoadBalancerNotFound":
                    raise

        target_group_arn = self.state.get("target_group_arn")
        if not target_group_arn:
            try:
                groups = self.elbv2.describe_target_groups(
                    Names=[f"tg-{self.project}"[:32]]
                )["TargetGroups"]
            except ClientError as exc:
                if _error_code(exc) == "TargetGroupNotFound":
                    groups = []
                else:
                    raise
            target_group_arn = (
                groups[0]["TargetGroupArn"] if groups else None
            )
        if target_group_arn:
            try:
                tags = self.elbv2.describe_tags(
                    ResourceArns=[target_group_arn]
                )["TagDescriptions"][0]["Tags"]
                if not self._is_managed(tags):
                    raise RuntimeError(
                        "Target Group에 Hermes 관리 태그가 없어 삭제를 거부합니다."
                    )
                self.elbv2.delete_target_group(
                    TargetGroupArn=target_group_arn
                )
                logger.info("  Target Group 삭제: %s", target_group_arn)
            except ClientError as exc:
                if _error_code(exc) != "TargetGroupNotFound":
                    raise

    # ------------------------------------------------------------------
    # EC2 and VPC resources
    # ------------------------------------------------------------------
    def terminate_instances(self) -> None:
        instance_ids: List[str] = []
        state_instance_id = self.state.get("instance_id")
        if state_instance_id:
            try:
                reservations = self.ec2.describe_instances(
                    InstanceIds=[state_instance_id]
                )["Reservations"]
            except ClientError as exc:
                if _error_code(exc) == "InvalidInstanceID.NotFound":
                    reservations = []
                else:
                    raise
        else:
            reservations = self.ec2.describe_instances(
                Filters=self._managed_filters()
                + [{
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                }]
            )["Reservations"]

        for reservation in reservations:
            for instance in reservation["Instances"]:
                if instance["State"]["Name"] == "terminated":
                    continue
                if not self._is_managed(instance.get("Tags", [])):
                    raise RuntimeError(
                        f"EC2 {instance['InstanceId']}에 Hermes 관리 태그가 없습니다."
                    )
                instance_ids.append(instance["InstanceId"])
        if not instance_ids:
            logger.info("  종료할 EC2 없음")
            return
        self.ec2.terminate_instances(InstanceIds=instance_ids)
        self.ec2.get_waiter("instance_terminated").wait(
            InstanceIds=instance_ids,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
        logger.info("  EC2 종료 완료: %s", ", ".join(instance_ids))

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
            "knowledge_base": f"{self.project}-knowledge-base",
        }

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
                f"Knowledge Base {knowledge_base_id}가 관리 대상이 아닙니다."
            )
        return knowledge_base

    def _knowledge_base_ids(self) -> List[str]:
        knowledge_base_id = self.state.get("knowledge_base_id")
        if knowledge_base_id:
            try:
                self._managed_knowledge_base(knowledge_base_id)
                return [knowledge_base_id]
            except ClientError as exc:
                if _error_code(exc) != "ResourceNotFoundException":
                    raise

        ids: List[str] = []
        name = self._knowledge_base_names()["knowledge_base"]
        paginator = self.bedrock_agent.get_paginator(
            "list_knowledge_bases"
        )
        for page in paginator.paginate():
            for item in page.get("knowledgeBaseSummaries", []):
                if item.get("name") != name:
                    continue
                self._managed_knowledge_base(item["knowledgeBaseId"])
                ids.append(item["knowledgeBaseId"])
        return ids

    def _stop_ingestion_jobs(
        self, knowledge_base_id: str, data_source_id: str
    ) -> None:
        active_ids: List[str] = []
        paginator = self.bedrock_agent.get_paginator(
            "list_ingestion_jobs"
        )
        for page in paginator.paginate(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        ):
            for job in page.get("ingestionJobSummaries", []):
                if job.get("status") in (
                    "COMPLETE",
                    "FAILED",
                    "STOPPED",
                ):
                    continue
                job_id = job["ingestionJobId"]
                active_ids.append(job_id)
                try:
                    self.bedrock_agent.stop_ingestion_job(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=data_source_id,
                        ingestionJobId=job_id,
                    )
                    logger.info("  ingestion job 중지 요청: %s", job_id)
                except ClientError as exc:
                    if _error_code(exc) not in (
                        "ConflictException",
                        "ResourceNotFoundException",
                    ):
                        raise

        deadline = time.time() + 300
        pending = set(active_ids)
        while pending and time.time() < deadline:
            for job_id in list(pending):
                try:
                    job = self.bedrock_agent.get_ingestion_job(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=data_source_id,
                        ingestionJobId=job_id,
                    )["ingestionJob"]
                except ClientError as exc:
                    if _error_code(exc) == "ResourceNotFoundException":
                        pending.remove(job_id)
                        continue
                    raise
                if job["status"] in ("COMPLETE", "FAILED", "STOPPED"):
                    pending.remove(job_id)
            if pending:
                time.sleep(10)
        if pending:
            raise TimeoutError(
                f"ingestion job 중지 대기 시간 초과: {sorted(pending)}"
            )

    def _wait_data_source_deleted(
        self,
        knowledge_base_id: str,
        data_source_id: str,
        timeout_seconds: int = 600,
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                data_source = self.bedrock_agent.get_data_source(
                    knowledgeBaseId=knowledge_base_id,
                    dataSourceId=data_source_id,
                )["dataSource"]
            except ClientError as exc:
                if _error_code(exc) == "ResourceNotFoundException":
                    return
                raise
            if data_source["status"] == "DELETE_UNSUCCESSFUL":
                reasons = "; ".join(
                    data_source.get("failureReasons", [])
                )
                raise RuntimeError(
                    "Knowledge Base data source DELETE_UNSUCCESSFUL: "
                    f"{reasons or 'unknown'}"
                )
            time.sleep(10)
        raise TimeoutError(
            f"Knowledge Base data source 삭제 대기 시간 초과: "
            f"{data_source_id}"
        )

    def _wait_knowledge_base_deleted(
        self, knowledge_base_id: str, timeout_seconds: int = 600
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                knowledge_base = self.bedrock_agent.get_knowledge_base(
                    knowledgeBaseId=knowledge_base_id
                )["knowledgeBase"]
            except ClientError as exc:
                if _error_code(exc) == "ResourceNotFoundException":
                    return
                raise
            if knowledge_base["status"] == "DELETE_UNSUCCESSFUL":
                reasons = "; ".join(
                    knowledge_base.get("failureReasons", [])
                )
                raise RuntimeError(
                    "Knowledge Base DELETE_UNSUCCESSFUL: "
                    f"{reasons or 'unknown'}"
                )
            time.sleep(10)
        raise TimeoutError(
            f"Knowledge Base 삭제 대기 시간 초과: {knowledge_base_id}"
        )

    def delete_knowledge_base(self) -> None:
        knowledge_base_ids = self._knowledge_base_ids()
        if not knowledge_base_ids:
            logger.info("  삭제할 Knowledge Base 없음")
            return

        for knowledge_base_id in knowledge_base_ids:
            knowledge_base = self._managed_knowledge_base(
                knowledge_base_id
            )
            if knowledge_base["status"] == "DELETING":
                self._wait_knowledge_base_deleted(knowledge_base_id)
                continue

            data_source_ids: List[str] = []
            paginator = self.bedrock_agent.get_paginator(
                "list_data_sources"
            )
            for page in paginator.paginate(
                knowledgeBaseId=knowledge_base_id
            ):
                data_source_ids.extend(
                    item["dataSourceId"]
                    for item in page.get("dataSourceSummaries", [])
                )

            for data_source_id in data_source_ids:
                try:
                    data_source = self.bedrock_agent.get_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=data_source_id,
                    )["dataSource"]
                except ClientError as exc:
                    if _error_code(exc) == "ResourceNotFoundException":
                        continue
                    raise
                if data_source["status"] == "DELETING":
                    self._wait_data_source_deleted(
                        knowledge_base_id, data_source_id
                    )
                    continue
                self._stop_ingestion_jobs(
                    knowledge_base_id, data_source_id
                )
                for attempt in range(10):
                    try:
                        self.bedrock_agent.delete_data_source(
                            knowledgeBaseId=knowledge_base_id,
                            dataSourceId=data_source_id,
                        )
                        logger.info(
                            "  Knowledge Base data source 삭제 요청: %s",
                            data_source_id,
                        )
                        break
                    except ClientError as exc:
                        if _error_code(exc) == "ResourceNotFoundException":
                            break
                        if (
                            _error_code(exc) == "ConflictException"
                            and attempt < 9
                        ):
                            time.sleep(10)
                            continue
                        raise
                self._wait_data_source_deleted(
                    knowledge_base_id, data_source_id
                )

            for attempt in range(10):
                try:
                    self.bedrock_agent.delete_knowledge_base(
                        knowledgeBaseId=knowledge_base_id
                    )
                    logger.info(
                        "  Bedrock Knowledge Base 삭제 요청: %s",
                        knowledge_base_id,
                    )
                    break
                except ClientError as exc:
                    if _error_code(exc) == "ResourceNotFoundException":
                        break
                    if (
                        _error_code(exc) == "ConflictException"
                        and attempt < 9
                    ):
                        current = self.bedrock_agent.get_knowledge_base(
                            knowledgeBaseId=knowledge_base_id
                        )["knowledgeBase"]
                        if current["status"] == "DELETING":
                            break
                        time.sleep(10)
                        continue
                    raise
            else:
                raise RuntimeError(
                    "Knowledge Base 삭제 요청을 완료하지 못했습니다: "
                    f"{knowledge_base_id}"
                )
            self._wait_knowledge_base_deleted(knowledge_base_id)

    def _managed_collection(self) -> Optional[Dict[str, Any]]:
        collection_id = self.state.get("opensearch_collection_id")
        params: Dict[str, Any]
        if collection_id:
            params = {"ids": [collection_id]}
        else:
            params = {
                "names": [self._knowledge_base_names()["collection"]]
            }
        details = self.aoss.batch_get_collection(**params).get(
            "collectionDetails", []
        )
        if not details and collection_id:
            details = self.aoss.batch_get_collection(
                names=[self._knowledge_base_names()["collection"]]
            ).get("collectionDetails", [])
        if not details:
            return None
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
                f"OpenSearch collection {detail['id']}가 관리 대상이 아닙니다."
            )
        return detail

    def delete_opensearch(self) -> None:
        collection = self._managed_collection()
        if collection:
            if collection["status"] not in ("DELETING", "DELETED"):
                self.aoss.delete_collection(id=collection["id"])
                logger.info(
                    "  OpenSearch collection 삭제 요청: %s",
                    collection["id"],
                )
            deadline = time.time() + 1200
            while time.time() < deadline:
                current = self.aoss.batch_get_collection(
                    ids=[collection["id"]]
                ).get("collectionDetails", [])
                if not current:
                    break
                time.sleep(15)
            else:
                raise TimeoutError(
                    "OpenSearch collection 삭제 대기 시간 초과: "
                    f"{collection['id']}"
                )

        names = self._knowledge_base_names()
        try:
            policy_name = (
                self.state.get("opensearch_access_policy")
                or names["access_policy"]
            )
            detail = self.aoss.get_access_policy(
                name=policy_name,
                type="data",
            )["accessPolicyDetail"]
            expected_description = (
                f"Hermes {self.project} Knowledge Base data access"
            )
            if detail.get("description") != expected_description:
                raise RuntimeError(
                    f"OpenSearch data access policy {policy_name}이 "
                    "관리 대상이 아닙니다."
                )
            self.aoss.delete_access_policy(
                name=policy_name,
                type="data",
            )
            logger.info("  OpenSearch data access policy 삭제")
        except ClientError as exc:
            if _error_code(exc) != "ResourceNotFoundException":
                raise

        for state_key, fallback, policy_type in (
            (
                "opensearch_network_policy",
                names["network_policy"],
                "network",
            ),
            (
                "opensearch_encryption_policy",
                names["encryption_policy"],
                "encryption",
            ),
        ):
            try:
                policy_name = self.state.get(state_key) or fallback
                detail = self.aoss.get_security_policy(
                    name=policy_name,
                    type=policy_type,
                )["securityPolicyDetail"]
                expected_description = (
                    f"Hermes {self.project} {policy_type} policy"
                )
                if detail.get("description") != expected_description:
                    raise RuntimeError(
                        f"OpenSearch {policy_type} policy {policy_name}이 "
                        "관리 대상이 아닙니다."
                    )
                self.aoss.delete_security_policy(
                    name=policy_name,
                    type=policy_type,
                )
                logger.info(
                    "  OpenSearch %s policy 삭제", policy_type
                )
            except ClientError as exc:
                if _error_code(exc) != "ResourceNotFoundException":
                    raise

    def delete_knowledge_base_bucket(self) -> None:
        bucket_name = (
            self.state.get("knowledge_base_bucket")
            or self._knowledge_base_names()["bucket"]
        )
        try:
            tags = self.s3.get_bucket_tagging(Bucket=bucket_name).get(
                "TagSet", []
            )
        except ClientError as exc:
            if _error_code(exc) in ("NoSuchBucket", "404", "NotFound"):
                logger.info("  삭제할 Knowledge Base S3 bucket 없음")
                return
            if _error_code(exc) == "NoSuchTagSet":
                tags = []
            else:
                raise
        if not self._is_managed(tags):
            raise RuntimeError(
                f"S3 bucket {bucket_name}이 관리 대상이 아닙니다."
            )

        objects = self.s3.get_paginator("list_objects_v2")
        for page in objects.paginate(Bucket=bucket_name):
            current = [
                {"Key": item["Key"]}
                for item in page.get("Contents", [])
            ]
            for offset in range(0, len(current), 1000):
                self.s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={
                        "Objects": current[offset : offset + 1000]
                    },
                )

        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in (
                    page.get("Versions", [])
                    + page.get("DeleteMarkers", [])
                )
            ]
            for offset in range(0, len(objects), 1000):
                self.s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": objects[offset : offset + 1000]},
                )

        uploads = self.s3.get_paginator("list_multipart_uploads")
        for page in uploads.paginate(Bucket=bucket_name):
            for upload in page.get("Uploads", []):
                self.s3.abort_multipart_upload(
                    Bucket=bucket_name,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )

        self.s3.delete_bucket(Bucket=bucket_name)
        logger.info("  Knowledge Base S3 bucket 삭제: %s", bucket_name)

    def delete_knowledge_base_iam(self) -> None:
        role_name = (
            self.state.get("knowledge_base_role_name")
            or self._knowledge_base_names()["role"]
        )
        try:
            role = self.iam.get_role(RoleName=role_name)["Role"]
        except ClientError as exc:
            if _error_code(exc) == "NoSuchEntity":
                logger.info("  삭제할 Knowledge Base IAM Role 없음")
                return
            raise
        if not self._is_managed(role.get("Tags", [])):
            raise RuntimeError(
                f"IAM Role {role_name}이 관리 대상이 아닙니다."
            )
        for policy in self.iam.list_attached_role_policies(
            RoleName=role_name
        )["AttachedPolicies"]:
            self.iam.detach_role_policy(
                RoleName=role_name,
                PolicyArn=policy["PolicyArn"],
            )
        for policy_name in self.iam.list_role_policies(
            RoleName=role_name
        )["PolicyNames"]:
            self.iam.delete_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
            )
        self.iam.delete_role(RoleName=role_name)
        logger.info("  Knowledge Base IAM Role 삭제: %s", role_name)

    def delete_vpc_endpoints(self) -> None:
        vpc = self._managed_vpc()
        if not vpc:
            return
        endpoints = self.ec2.describe_vpc_endpoints(Filters=[
            {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
            *self._managed_filters(),
        ])["VpcEndpoints"]
        pending_ids = [
            endpoint["VpcEndpointId"]
            for endpoint in endpoints
            if endpoint["State"] not in ("deleted", "failed")
        ]
        if not pending_ids:
            return
        deletable_ids = [
            endpoint["VpcEndpointId"]
            for endpoint in endpoints
            if endpoint["State"] not in ("deleted", "deleting", "failed")
        ]
        if deletable_ids:
            self.ec2.delete_vpc_endpoints(VpcEndpointIds=deletable_ids)
        deadline = time.time() + 600
        while time.time() < deadline:
            current = self.ec2.describe_vpc_endpoints(Filters=[
                {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
                *self._managed_filters(),
            ])["VpcEndpoints"]
            if not [
                endpoint
                for endpoint in current
                if endpoint["State"] not in ("deleted", "failed")
            ]:
                logger.info("  VPC Endpoint 삭제 완료")
                return
            time.sleep(10)
        raise TimeoutError("VPC Endpoint 삭제 대기 시간 초과")

    def delete_nat_gateways(self) -> None:
        vpc = self._managed_vpc()
        if not vpc:
            return
        gateways = self.ec2.describe_nat_gateways(Filters=[
            {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
            *self._managed_filters(),
        ])["NatGateways"]
        pending_ids = []
        for gateway in gateways:
            for address in gateway.get("NatGatewayAddresses", []):
                allocation_id = address.get("AllocationId")
                if allocation_id:
                    self.state.setdefault(
                        "nat_eip_allocation_id", allocation_id
                    )
            if gateway["State"] not in ("deleted", "deleting", "failed"):
                self.ec2.delete_nat_gateway(
                    NatGatewayId=gateway["NatGatewayId"]
                )
                logger.info(
                    "  NAT Gateway 삭제 요청: %s",
                    gateway["NatGatewayId"],
                )
            if gateway["State"] not in ("deleted", "failed"):
                pending_ids.append(gateway["NatGatewayId"])

        if not pending_ids:
            return
        deadline = time.time() + 900
        while time.time() < deadline:
            current = self.ec2.describe_nat_gateways(
                NatGatewayIds=pending_ids
            )["NatGateways"]
            if all(
                gateway["State"] in ("deleted", "failed")
                for gateway in current
            ):
                logger.info("  NAT Gateway 삭제 완료")
                return
            time.sleep(15)
        raise TimeoutError("NAT Gateway 삭제 대기 시간 초과")

    def delete_subnets_and_routes(self) -> None:
        vpc = self._managed_vpc()
        if not vpc:
            return
        vpc_id = vpc["VpcId"]
        remaining_subnets: set[str] = set()
        for attempt in range(12):
            subnets = self.ec2.describe_subnets(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                *self._managed_filters(),
            ])["Subnets"]
            remaining_subnets = {
                subnet["SubnetId"] for subnet in subnets
            }
            if not remaining_subnets:
                break
            for subnet_id in list(remaining_subnets):
                try:
                    self.ec2.delete_subnet(SubnetId=subnet_id)
                    logger.info("  Subnet 삭제 요청: %s", subnet_id)
                except ClientError as exc:
                    if _error_code(exc) == "InvalidSubnetID.NotFound":
                        remaining_subnets.discard(subnet_id)
                    elif _error_code(exc) != "DependencyViolation":
                        raise
            if attempt < 11:
                time.sleep(10)
        remaining_subnets = {
            subnet["SubnetId"]
            for subnet in self.ec2.describe_subnets(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                *self._managed_filters(),
            ])["Subnets"]
        }
        if remaining_subnets:
            raise RuntimeError(
                f"Subnet 의존성이 남아 있습니다: {sorted(remaining_subnets)}"
            )

        remaining_tables: set[str] = set()
        for attempt in range(6):
            tables = self.ec2.describe_route_tables(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                *self._managed_filters(),
            ])["RouteTables"]
            remaining_tables = {
                table["RouteTableId"]
                for table in tables
                if not any(
                    association.get("Main")
                    for association in table.get("Associations", [])
                )
            }
            if not remaining_tables:
                return
            for table in tables:
                table_id = table["RouteTableId"]
                if table_id not in remaining_tables:
                    continue
                for association in table.get("Associations", []):
                    association_id = association.get(
                        "RouteTableAssociationId"
                    )
                    if association_id and not association.get("Main"):
                        try:
                            self.ec2.disassociate_route_table(
                                AssociationId=association_id
                            )
                        except ClientError as exc:
                            if (
                                _error_code(exc)
                                != "InvalidAssociationID.NotFound"
                            ):
                                raise
                try:
                    self.ec2.delete_route_table(RouteTableId=table_id)
                    logger.info("  Route Table 삭제: %s", table_id)
                except ClientError as exc:
                    if _error_code(exc) == "InvalidRouteTableID.NotFound":
                        remaining_tables.discard(table_id)
                    elif _error_code(exc) != "DependencyViolation":
                        raise
            if attempt < 5:
                time.sleep(5)
        remaining_tables = {
            table["RouteTableId"]
            for table in self.ec2.describe_route_tables(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                *self._managed_filters(),
            ])["RouteTables"]
            if not any(
                association.get("Main")
                for association in table.get("Associations", [])
            )
        }
        if remaining_tables:
            raise RuntimeError(
                f"Route Table 의존성이 남아 있습니다: {sorted(remaining_tables)}"
            )

    def delete_security_groups(self) -> None:
        vpc = self._managed_vpc()
        if not vpc:
            return
        groups = self.ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
            *self._managed_filters(),
        ])["SecurityGroups"]
        # CloudFront가 VPC origin용으로 자동 생성한 service-managed SG는
        # Hermes 태그가 없지만, VPC origin 삭제 후에는 orphan으로 남아
        # VPC 삭제를 막으므로 함께 정리합니다.
        groups += self.ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
            {
                "Name": "group-name",
                "Values": ["CloudFront-VPCOrigins-Service-SG*"],
            },
        ])["SecurityGroups"]
        for group in groups:
            if group.get("IpPermissions"):
                try:
                    self.ec2.revoke_security_group_ingress(
                        GroupId=group["GroupId"],
                        IpPermissions=group["IpPermissions"],
                    )
                except ClientError:
                    pass

        remaining = {group["GroupId"] for group in groups}
        for attempt in range(12):
            for group_id in list(remaining):
                try:
                    self.ec2.delete_security_group(GroupId=group_id)
                    remaining.remove(group_id)
                    logger.info("  Security Group 삭제: %s", group_id)
                except ClientError as exc:
                    if _error_code(exc) == "InvalidGroup.NotFound":
                        remaining.remove(group_id)
                    elif _error_code(exc) != "DependencyViolation":
                        raise
            if not remaining:
                return
            if attempt < 11:
                time.sleep(10)
        raise RuntimeError(
            f"Security Group 의존성이 남아 있습니다: {sorted(remaining)}"
        )

    def delete_vpc(self) -> None:
        vpc = self._managed_vpc()
        if not vpc:
            return
        vpc_id = vpc["VpcId"]
        gateways = self.ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["InternetGateways"]
        for gateway in gateways:
            if not self._is_managed(gateway.get("Tags", [])):
                raise RuntimeError(
                    f"Internet Gateway {gateway['InternetGatewayId']}가 관리 대상이 아닙니다."
                )
            self.ec2.detach_internet_gateway(
                InternetGatewayId=gateway["InternetGatewayId"],
                VpcId=vpc_id,
            )
            self.ec2.delete_internet_gateway(
                InternetGatewayId=gateway["InternetGatewayId"]
            )
            logger.info(
                "  Internet Gateway 삭제: %s",
                gateway["InternetGatewayId"],
            )

        for attempt in range(6):
            try:
                self.ec2.delete_vpc(VpcId=vpc_id)
                logger.info("  VPC 삭제: %s", vpc_id)
                return
            except ClientError as exc:
                if _error_code(exc) == "InvalidVpcID.NotFound":
                    return
                if _error_code(exc) == "DependencyViolation" and attempt < 5:
                    time.sleep(10)
                    continue
                dependencies = self._remaining_vpc_dependencies(vpc_id)
                raise RuntimeError(
                    f"VPC 삭제 실패 ({_error_code(exc)}). 남은 리소스: {dependencies}"
                ) from exc

    def _remaining_vpc_dependencies(self, vpc_id: str) -> Dict[str, List[str]]:
        return {
            "subnets": [
                item["SubnetId"]
                for item in self.ec2.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )["Subnets"]
            ],
            "network_interfaces": [
                item["NetworkInterfaceId"]
                for item in self.ec2.describe_network_interfaces(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )["NetworkInterfaces"]
            ],
            "security_groups": [
                item["GroupId"]
                for item in self.ec2.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )["SecurityGroups"]
                if item["GroupName"] != "default"
            ],
        }

    def release_elastic_ips(self) -> None:
        allocation_ids = set()
        if self.state.get("nat_eip_allocation_id"):
            allocation_ids.add(self.state["nat_eip_allocation_id"])
        addresses = self.ec2.describe_addresses(
            Filters=self._managed_filters()
        )["Addresses"]
        allocation_ids.update(
            address["AllocationId"]
            for address in addresses
            if address.get("AllocationId")
        )
        for allocation_id in allocation_ids:
            try:
                self.ec2.release_address(AllocationId=allocation_id)
                logger.info("  Elastic IP 해제: %s", allocation_id)
            except ClientError as exc:
                if _error_code(exc) != "InvalidAllocationID.NotFound":
                    raise

    # ------------------------------------------------------------------
    # IAM
    # ------------------------------------------------------------------
    def delete_iam(self) -> None:
        profile_name = self.state.get("profile_name") or (
            f"{self.project}-bedrock-profile"
        )
        role_name = self.state.get("role_name") or (
            f"{self.project}-bedrock-role"
        )

        try:
            profile = self.iam.get_instance_profile(
                InstanceProfileName=profile_name
            )["InstanceProfile"]
            if not self._is_managed(profile.get("Tags", [])):
                raise RuntimeError(
                    f"Instance Profile {profile_name}가 관리 대상이 아닙니다."
                )
            for role in profile.get("Roles", []):
                self.iam.remove_role_from_instance_profile(
                    InstanceProfileName=profile_name,
                    RoleName=role["RoleName"],
                )
            self.iam.delete_instance_profile(
                InstanceProfileName=profile_name
            )
            logger.info("  Instance Profile 삭제: %s", profile_name)
        except ClientError as exc:
            if _error_code(exc) != "NoSuchEntity":
                raise

        try:
            role = self.iam.get_role(RoleName=role_name)["Role"]
            if not self._is_managed(role.get("Tags", [])):
                raise RuntimeError(f"IAM Role {role_name}이 관리 대상이 아닙니다.")
            for policy in self.iam.list_attached_role_policies(
                RoleName=role_name
            )["AttachedPolicies"]:
                self.iam.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy["PolicyArn"],
                )
            for policy_name in self.iam.list_role_policies(
                RoleName=role_name
            )["PolicyNames"]:
                self.iam.delete_role_policy(
                    RoleName=role_name, PolicyName=policy_name
                )
            self.iam.delete_role(RoleName=role_name)
            logger.info("  IAM Role 삭제: %s", role_name)
        except ClientError as exc:
            if _error_code(exc) != "NoSuchEntity":
                raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="installer.py가 생성한 AWS 리소스 전체 삭제"
    )
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument("--state-path", default=str(STATE_PATH))
    parser.add_argument(
        "--deployment-info-path", default=str(DEPLOYMENT_INFO_PATH)
    )
    parser.add_argument(
        "--yes", action="store_true", help="확인 프롬프트 없이 삭제"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _require_boto3()

    if not args.yes:
        print("Hermes Agent 전용 AWS 인프라를 모두 삭제합니다.")
        print(
            "대상: CloudFront, CloudFront VPC Origin, ALB, EC2, "
            "Bedrock Knowledge Base, OpenSearch Serverless, S3, "
            "VPC Endpoint, NAT, VPC, IAM"
        )
        answer = input("계속하시겠습니까? (yes/no): ").strip().lower()
        if answer != "yes":
            print("취소되었습니다.")
            return

    try:
        uninstaller = HermesUninstaller(
            region=args.region,
            project=args.project_name,
            state_path=Path(args.state_path),
            deployment_info_path=Path(args.deployment_info_path),
        )
        if not uninstaller.run():
            sys.exit(1)
    except (ClientError, RuntimeError, TimeoutError) as exc:
        logger.error("삭제 실패: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
