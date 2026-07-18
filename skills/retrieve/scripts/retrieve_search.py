#!/usr/bin/env python3
"""Retrieve passages from the Hermes Amazon Bedrock Knowledge Base."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def _load_boto3() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        return boto3, (BotoCoreError, ClientError)
    except ImportError:
        hermes_home = Path(
            os.environ.get("HERMES_HOME", Path.home() / ".hermes")
        ).expanduser()
        fallback = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
        if fallback.is_file() and Path(sys.executable).resolve() != fallback.resolve():
            os.execv(str(fallback), [str(fallback), *sys.argv])
        raise RuntimeError(
            "boto3를 찾을 수 없습니다. Hermes 가상환경 또는 `pip install boto3`가 필요합니다."
        ) from None


DEFAULT_RESULT_COUNT = 5
PAGE_METADATA_KEY = "x-amz-bedrock-kb-document-page-number"


def config_path() -> Path:
    hermes_home = Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    ).expanduser()
    return hermes_home / "knowledge-base.json"


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"Knowledge Base 설정 파일이 없습니다: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Knowledge Base 설정 파일이 올바른 JSON이 아닙니다: {path}"
        ) from exc

    required = ("region", "knowledge_base_id", "bucket")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(
            "Knowledge Base 설정에 필수 값이 없습니다: " + ", ".join(missing)
        )
    return config


def parse_s3_uri(uri: str) -> tuple[str, str] | None:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    return parsed.netloc, unquote(parsed.path.lstrip("/"))


def title_from_result(result: dict[str, Any], s3_uri: str | None) -> str | None:
    metadata = result.get("metadata") or {}
    title = (
        metadata.get("title")
        or metadata.get("x-amz-bedrock-kb-title")
        or metadata.get("x-amz-bedrock-kb-source-uri")
    )
    if title:
        return Path(str(title)).name
    if s3_uri:
        location = parse_s3_uri(s3_uri)
        if location:
            return Path(location[1]).name
    return None


def display_page(metadata: dict[str, Any]) -> int | str | None:
    raw_page = metadata.get(PAGE_METADATA_KEY)
    if raw_page is None:
        return None
    try:
        return int(raw_page) + 1
    except (TypeError, ValueError):
        return str(raw_page)


def presigned_url(
    s3_client: Any,
    s3_uri: str | None,
    *,
    expires_in: int,
) -> str | None:
    if not s3_uri:
        return None
    location = parse_s3_uri(s3_uri)
    if not location:
        return None
    bucket, key = location
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def retrieve(
    query: str,
    *,
    boto3_module: Any,
    config: dict[str, Any],
    number_of_results: int,
) -> list[dict[str, Any]]:
    region = str(config["region"])
    runtime = boto3_module.client("bedrock-agent-runtime", region_name=region)
    s3_client = boto3_module.client("s3", region_name=region)
    response = runtime.retrieve(
        knowledgeBaseId=str(config["knowledge_base_id"]),
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": number_of_results,
            }
        },
    )

    expires_in = int(config.get("presigned_url_expires_in", 3600))
    results: list[dict[str, Any]] = []
    for item in response.get("retrievalResults", []):
        content = item.get("content") or {}
        location = item.get("location") or {}
        s3_uri = (location.get("s3Location") or {}).get("uri")
        metadata = item.get("metadata") or {}
        results.append(
            {
                "passage": content.get("text"),
                "score": item.get("score"),
                "title": title_from_result(item, s3_uri),
                "page": display_page(metadata),
                "s3_uri": s3_uri,
                "url": presigned_url(
                    s3_client,
                    s3_uri,
                    expires_in=expires_in,
                ),
            }
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hermes Bedrock Knowledge Base에서 관련 문서를 검색합니다."
    )
    parser.add_argument("query", nargs="+", help="검색할 질문 또는 키워드")
    parser.add_argument(
        "-n",
        "--number-of-results",
        type=int,
        default=DEFAULT_RESULT_COUNT,
        help="반환할 최대 결과 수(기본 5)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=config_path(),
        help="Knowledge Base 설정 파일",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.number_of_results <= 100:
        parser.error("--number-of-results는 1에서 100 사이여야 합니다.")
    try:
        boto3_module, aws_errors = _load_boto3()
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    handled_errors = aws_errors + (OSError, RuntimeError, ValueError)
    try:
        results = retrieve(
            " ".join(args.query),
            boto3_module=boto3_module,
            config=load_config(args.config.expanduser()),
            number_of_results=args.number_of_results,
        )
    except handled_errors as exc:
        print(
            json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
