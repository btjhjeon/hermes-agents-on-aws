---
name: retrieve
description: Search the deployment's Amazon Bedrock Knowledge Base and answer from uploaded documents. Use whenever a user asks about internal documents, manuals, policies, reports, procedures, error codes, or other facts that should be grounded in the Knowledge Base, including requests for document summaries, comparisons, supporting sources, or page references.
---

# Knowledge Base Retrieve

Search indexed documents with the bundled script and ground the answer in the
returned passages.

## Workflow

1. Form a concise query that preserves important names, identifiers, dates,
   and error codes.
2. Run the script with its full installed path.
3. Parse the JSON array from stdout.
4. Select passages that directly address the request, using the relevance
   score as supporting signal rather than an absolute confidence measure.
5. Retry with a narrower query, exact identifier, or useful synonym when the
   evidence is weak.
6. Search each topic separately when a request contains unrelated questions.
7. Answer from the selected passages and cite the title and page when
   available.

Do not claim that information came from the Knowledge Base unless it appears
in a retrieved passage.

## Search

Run a basic search:

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "검색할 질문 또는 키워드"
```

The default is five passages. Request more for a broad topic:

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  --number-of-results 10 \
  "검색할 질문 또는 키워드"
```

Use the full installed path regardless of the current working directory.

Example queries:

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "오류 코드 E1001의 원인과 해결 방법"

python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "운영 환경에서 API 인증서를 교체하는 절차"

python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "보안 정책에서 관리자 권한 검토 주기"
```

## Output

Expect a JSON array on stdout:

```json
[
  {
    "passage": "Matched text from the indexed document.",
    "score": 0.87,
    "title": "operations-guide.pdf",
    "page": 12,
    "s3_uri": "s3://example-bucket/docs/operations-guide.pdf",
    "url": "https://example-bucket.s3.amazonaws.com/..."
  }
]
```

Interpret the fields as follows:

- `passage`: matched document text
- `score`: Bedrock relevance score
- `title`: source filename
- `page`: one-based page number
- `s3_uri`: original S3 object URI
- `url`: temporary presigned URL for the source object

Treat fields as optional because Bedrock may omit source metadata. Treat an
empty array as no supporting evidence. Do not invent a document-based answer
when no passage is returned.

The presigned URL normally expires after one hour. Present it only as a
temporary way to open the source.

## Answering

- Ground factual claims in retrieved passages.
- Combine duplicate passages instead of repeating them.
- Distinguish document evidence from general reasoning.
- Quote only text that appears in a passage.
- Cite `title, p. page` when both values exist.
- Cite the title alone when page metadata is unavailable.
- Preserve uncertainty when passages are incomplete or conflicting.
- State clearly when the documents do not contain enough information.
- Include the temporary URL when opening the original source would help.

Use this citation style:

```text
인증서는 만료 30일 전부터 교체할 수 있습니다.
출처: operations-guide.pdf, p. 12
```

## Configuration

Read `~/.hermes/knowledge-base.json`, which the installer creates:

```json
{
  "region": "us-west-2",
  "knowledge_base_id": "KNOWLEDGE_BASE_ID",
  "data_source_id": "DATA_SOURCE_ID",
  "bucket": "KNOWLEDGE_BASE_BUCKET",
  "document_prefix": "docs/",
  "presigned_url_expires_in": 3600
}
```

Use an alternate config only for an explicitly supplied deployment or
diagnostic:

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  --config /path/to/knowledge-base.json \
  "검색할 질문"
```

Do not print, expose, or modify the configuration while answering.

## Authentication

Use the standard AWS credential chain. On the AWS deployment, rely on the EC2
Instance Profile with `bedrock:Retrieve` and source-document `s3:GetObject`
permissions.

Do not request access keys from the user or add static credentials to this
skill, its command arguments, or its configuration.

## Dependencies And Errors

Use the selected Python interpreter first. If it lacks `boto3`, allow the
script to re-execute with:

```text
~/.hermes/hermes-agent/venv/bin/python
```

If neither interpreter provides `boto3`, report the dependency error. Do not
install packages without permission.

Expect failures as a JSON object on stderr with a nonzero exit code:

```json
{
  "error": "Error details"
}
```

Report a concise failure without exposing credentials, environment variables,
raw configuration, or stack traces. Suggest checking the Knowledge Base
configuration, EC2 IAM permissions, and Knowledge Base status when relevant.
