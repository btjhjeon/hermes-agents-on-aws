#!/usr/bin/env python3
"""Upload local documents and synchronize the Hermes Bedrock Knowledge Base."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None

    class ClientError(Exception):
        """Fallback type used until boto3 is installed."""


LOGGER = logging.getLogger("add_content")
DEFAULT_STATE_PATH = Path("assets/hermes-deployment.json")
DEFAULT_CONTENTS_DIR = Path("contents")
SUPPORTED_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".md",
    ".pdf",
    ".txt",
    ".xls",
    ".xlsx",
}
CONTENT_TYPES = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".htm": "text/html",
    ".html": "text/html",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}
TERMINAL_INGESTION_STATES = {"COMPLETE", "FAILED", "STOPPED"}


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    region: str
    bucket: str
    knowledge_base_id: str
    data_source_id: str
    document_prefix: str = "docs/"


def load_config(path: Path) -> KnowledgeBaseConfig:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"배포 상태 파일이 없습니다: {path}. installer.py를 먼저 실행하세요."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"배포 상태 파일이 올바른 JSON이 아닙니다: {path}") from exc

    values = {
        "region": state.get("region"),
        "bucket": state.get("knowledge_base_bucket"),
        "knowledge_base_id": state.get("knowledge_base_id"),
        "data_source_id": state.get("knowledge_base_data_source_id"),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(
            "배포 상태에 Knowledge Base 정보가 없습니다: "
            + ", ".join(missing)
            + ". --disable-knowledge-base 없이 설치했는지 확인하세요."
        )

    prefix = str(state.get("knowledge_base_document_prefix") or "docs/")
    prefix = prefix.strip("/")
    return KnowledgeBaseConfig(
        region=str(values["region"]),
        bucket=str(values["bucket"]),
        knowledge_base_id=str(values["knowledge_base_id"]),
        data_source_id=str(values["data_source_id"]),
        document_prefix=f"{prefix}/" if prefix else "",
    )


def iter_documents(contents_dir: Path) -> Iterator[Path]:
    if not contents_dir.is_dir():
        raise ValueError(f"문서 디렉터리가 없습니다: {contents_dir}")

    for root, directory_names, file_names in os.walk(
        contents_dir, followlinks=False
    ):
        root_path = Path(root)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".")
            and not (root_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = root_path / name
            if (
                name.startswith(".")
                or path.is_symlink()
                or path.suffix.lower() not in SUPPORTED_EXTENSIONS
            ):
                continue
            yield path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def s3_key_for(path: Path, contents_dir: Path, prefix: str) -> str:
    relative = path.relative_to(contents_dir)
    return str(PurePosixPath(prefix) / PurePosixPath(relative.as_posix()))


def remote_sha256(s3_client: Any, bucket: str, key: str) -> str | None:
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    metadata = response.get("Metadata", {})
    return metadata.get("sha256") or metadata.get("source-sha256")


def upload_document(
    s3_client: Any,
    *,
    path: Path,
    bucket: str,
    key: str,
    digest: str,
) -> None:
    content_type = CONTENT_TYPES.get(path.suffix.lower())
    if not content_type:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    parameters: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Metadata": {"sha256": digest},
        "ContentType": content_type,
        "ServerSideEncryption": "AES256",
    }
    if path.suffix.lower() == ".pdf":
        parameters["ContentDisposition"] = "inline"

    LOGGER.info("업로드: %s -> s3://%s/%s", path, bucket, key)
    with path.open("rb") as file_handle:
        s3_client.put_object(Body=file_handle, **parameters)


def list_remote_keys(s3_client: Any, bucket: str, prefix: str) -> Iterator[str]:
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if key and key != prefix:
                yield key


def delete_remote_keys(s3_client: Any, bucket: str, keys: set[str]) -> int:
    sorted_keys = sorted(keys)
    for start in range(0, len(sorted_keys), 1000):
        batch = sorted_keys[start : start + 1000]
        LOGGER.info("S3에서 로컬에 없는 문서 %d개 삭제", len(batch))
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            details = ", ".join(
                f"{item.get('Key')}: {item.get('Code')}" for item in errors
            )
            raise RuntimeError(f"S3 문서 삭제 실패: {details}")
    return len(sorted_keys)


def start_ingestion(bedrock_client: Any, config: KnowledgeBaseConfig) -> str:
    response = bedrock_client.start_ingestion_job(
        knowledgeBaseId=config.knowledge_base_id,
        dataSourceId=config.data_source_id,
        description="Synchronized by add_content.py",
    )
    job_id = response["ingestionJob"]["ingestionJobId"]
    LOGGER.info("Knowledge Base ingestion 시작: %s", job_id)
    return job_id


def wait_for_ingestion(
    bedrock_client: Any,
    config: KnowledgeBaseConfig,
    job_id: str,
    *,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        response = bedrock_client.get_ingestion_job(
            knowledgeBaseId=config.knowledge_base_id,
            dataSourceId=config.data_source_id,
            ingestionJobId=job_id,
        )
        job = response["ingestionJob"]
        status = job["status"]
        if status != last_status:
            LOGGER.info("Ingestion 상태: %s", status)
            last_status = status
        if status in TERMINAL_INGESTION_STATES:
            if status != "COMPLETE":
                reasons = ", ".join(job.get("failureReasons", [])) or "원인 미상"
                raise RuntimeError(f"Ingestion {status}: {reasons}")
            statistics = job.get("statistics", {})
            LOGGER.info("Knowledge Base 동기화 완료: %s", statistics)
            return
        time.sleep(10)
    raise TimeoutError(f"Ingestion 완료 대기 시간 초과: {job_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="contents/ 문서를 S3에 업로드하고 Hermes Knowledge Base를 동기화합니다."
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--contents-dir", type=Path, default=DEFAULT_CONTENTS_DIR)
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="S3에는 있지만 로컬 contents/에는 없는 문서를 삭제",
    )
    parser.add_argument(
        "--force-sync",
        action="store_true",
        help="업로드나 삭제가 없어도 ingestion 실행",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="ingestion을 시작한 뒤 완료를 기다리지 않음",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="ingestion 완료 대기 시간(초, 기본 1800)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if boto3 is None:
        raise RuntimeError(
            "boto3를 찾을 수 없습니다. `python3 -m pip install -r requirements.txt`를 "
            "먼저 실행하세요."
        )
    config = load_config(args.state_path)
    s3_client = boto3.client("s3", region_name=config.region)
    bedrock_client = boto3.client("bedrock-agent", region_name=config.region)

    local_keys: set[str] = set()
    uploaded = 0
    skipped = 0
    for path in iter_documents(args.contents_dir):
        key = s3_key_for(path, args.contents_dir, config.document_prefix)
        local_keys.add(key)
        digest = sha256_file(path)
        if remote_sha256(s3_client, config.bucket, key) == digest:
            LOGGER.info("변경 없음: %s", path)
            skipped += 1
            continue
        upload_document(
            s3_client,
            path=path,
            bucket=config.bucket,
            key=key,
            digest=digest,
        )
        uploaded += 1

    deleted = 0
    if args.delete_missing:
        remote_keys = set(
            list_remote_keys(
                s3_client, config.bucket, config.document_prefix
            )
        )
        deleted = delete_remote_keys(
            s3_client, config.bucket, remote_keys - local_keys
        )

    LOGGER.info(
        "문서 처리 완료: 업로드 %d, 변경 없음 %d, 삭제 %d",
        uploaded,
        skipped,
        deleted,
    )
    if not (uploaded or deleted or args.force_sync):
        LOGGER.info("변경된 문서가 없어 ingestion을 생략합니다.")
        return 0

    job_id = start_ingestion(bedrock_client, config)
    if not args.no_wait:
        wait_for_ingestion(
            bedrock_client,
            config,
            job_id,
            timeout_seconds=args.timeout,
        )
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout은 1 이상이어야 합니다.")
    try:
        sys.exit(run(args))
    except (ClientError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        LOGGER.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
