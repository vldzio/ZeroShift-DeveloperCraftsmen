"""Fixture code sample for Part 1: analytics batch job.

Exercises Athena + S3 + DynamoDB SDK calls, plus a call through a helper
function to demonstrate the AST extractor following simple assignments.
"""
import boto3

athena = boto3.client("athena")
s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")

RESULTS_TABLE = ddb.Table("analytics-results")


def lambda_handler(event, context):
    query_id = _start_query(event["sql"])
    _wait_for_query(query_id)
    _persist_result(event["reportId"], query_id)


def _start_query(sql):
    resp = athena.start_query_execution(
        QueryString=sql,
        ResultConfiguration={"OutputLocation": "s3://analytics-athena-results/"},
    )
    return resp["QueryExecutionId"]


def _wait_for_query(query_id):
    while True:
        resp = athena.get_query_execution(QueryExecutionId=query_id)
        state = resp["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return state


def _persist_result(report_id, query_id):
    rows = athena.get_query_results(QueryExecutionId=query_id)
    s3.put_object(
        Bucket="analytics-report-archive",
        Key=f"reports/{report_id}.json",
        Body=str(rows).encode("utf-8"),
    )
    RESULTS_TABLE.put_item(Item={"reportId": report_id, "queryId": query_id})
