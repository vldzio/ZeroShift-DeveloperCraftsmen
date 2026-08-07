from pathlib import Path

from part1.code_analyzer.static_extractor import _to_camel, extract_actions, extract_full

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "code_samples"


def test_to_camel_snake_to_camel():
    assert _to_camel("put_object") == "PutObject"
    assert _to_camel("list_objects_v2") == "ListObjectsV2"
    assert _to_camel("send_email") == "SendEmail"
    assert _to_camel("get_query_execution") == "GetQueryExecution"


def test_extract_from_news_summarizer():
    source = (FIXTURES / "news_summarizer.py").read_text(encoding="utf-8")
    actions = set(extract_actions(source))
    assert "s3:ListObjectsV2" in actions
    assert "s3:GetObject" in actions
    assert "s3:PutObject" in actions
    assert "ses:SendEmail" in actions
    assert "ssm:GetParameter" in actions


def test_extract_from_analytics_batch():
    """Analytics batch uses boto3.resource and RESULTS_TABLE = ddb.Table(...)."""
    source = (FIXTURES / "analytics_batch.py").read_text(encoding="utf-8")
    actions = set(extract_actions(source))
    assert "athena:StartQueryExecution" in actions
    assert "athena:GetQueryExecution" in actions
    assert "athena:GetQueryResults" in actions
    assert "s3:PutObject" in actions
    assert "dynamodb:PutItem" in actions


def test_extract_from_payment_processor():
    """Payment processor uses self.<attr> = boto3.client / .Table(...)."""
    source = (FIXTURES / "payment_processor.py").read_text(encoding="utf-8")
    actions = set(extract_actions(source))
    assert "kms:Decrypt" in actions
    assert "dynamodb:PutItem" in actions
    assert "sns:Publish" in actions


def test_extract_ignores_non_boto3_calls():
    source = """
import boto3

def unrelated():
    return "hello".upper()

s3 = boto3.client("s3")
s3.put_object(Bucket="b", Key="k", Body="v")
    """
    actions = set(extract_actions(source))
    assert actions == {"s3:PutObject"}


def test_extract_full_returns_line_numbers():
    source = """import boto3
s3 = boto3.client("s3")
s3.get_object(Bucket="b", Key="k")
"""
    calls = extract_full(source)
    assert len(calls) == 1
    assert calls[0].iam_action == "s3:GetObject"
    assert calls[0].line == 3
