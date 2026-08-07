"""Fixture code sample for Part 1: a small news-summarizer Lambda.

Demonstrates the SDK-call extraction path: three boto3 clients, a handful of
method calls each. When Part 1 analyzes this file, it should produce a set of
IAM actions that Part 2 can later manage as intent-registry ACTIVE entries.
"""
import json
import boto3

s3 = boto3.client("s3")
ses = boto3.client("ses")
ssm = boto3.client("ssm")


def lambda_handler(event, context):
    articles = _fetch_new_articles()
    summary = _summarize(articles)
    _store_snapshot(summary)
    _send_digest(summary)
    return {"statusCode": 200, "articleCount": len(articles)}


def _fetch_new_articles():
    resp = s3.list_objects_v2(Bucket="news-inbox-daily")
    keys = [obj["Key"] for obj in resp.get("Contents", [])]
    articles = []
    for key in keys:
        obj = s3.get_object(Bucket="news-inbox-daily", Key=key)
        articles.append(json.loads(obj["Body"].read()))
    return articles


def _summarize(articles):
    return {"total": len(articles), "titles": [a.get("title") for a in articles]}


def _store_snapshot(summary):
    s3.put_object(
        Bucket="news-snapshots",
        Key=f"digest/{summary['total']}.json",
        Body=json.dumps(summary),
    )


def _send_digest(summary):
    param = ssm.get_parameter(Name="/news-summarizer/recipient")
    recipient = param["Parameter"]["Value"]
    ses.send_email(
        Source="digest@example.com",
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": f"Daily digest ({summary['total']} articles)"},
            "Body": {"Text": {"Data": json.dumps(summary, indent=2)}},
        },
    )
