import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def reasoning_table(monkeypatch):
    monkeypatch.setenv("ZEROSHIFT_AGENT_REASONING_TABLE", "zeroshift-agent-reasoning")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="zeroshift-agent-reasoning",
            KeySchema=[
                {"AttributeName": "agentExecutionId", "KeyType": "HASH"},
                {"AttributeName": "stepId#timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "agentExecutionId", "AttributeType": "S"},
                {"AttributeName": "stepId#timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb


def test_reasoning_logger_writes_ordered_rows(reasoning_table):
    from agents.common.reasoning_logger import ReasoningLogger

    logger = ReasoningLogger(agent_name="test_agent", correlation_id="cid-1")
    logger.log_step(node="a", summary="first")
    logger.log_step(node="b", summary="second", tool_name="do_thing", tool_result={"ok": True})
    logger.log_step(node="c", summary="third", next_node="END")

    items = reasoning_table.Table("zeroshift-agent-reasoning").query(
        KeyConditionExpression="agentExecutionId = :e",
        ExpressionAttributeValues={":e": logger.agent_execution_id},
        ScanIndexForward=True,
    )["Items"]

    assert len(items) == 3
    assert items[0]["node"] == "a"
    assert items[1]["node"] == "b"
    assert items[2]["node"] == "c"
    assert items[0]["stepIndex"] == 1
    assert items[1]["stepIndex"] == 2
    assert items[1]["toolName"] == "do_thing"


def test_reasoning_logger_truncates_large_tool_results(reasoning_table):
    from agents.common.reasoning_logger import ReasoningLogger

    logger = ReasoningLogger(agent_name="test_agent")
    huge = {"data": "x" * 20_000}
    logger.log_step(node="a", summary="huge", tool_result=huge)

    items = reasoning_table.Table("zeroshift-agent-reasoning").query(
        KeyConditionExpression="agentExecutionId = :e",
        ExpressionAttributeValues={":e": logger.agent_execution_id},
    )["Items"]
    assert items[0]["toolResult"]["_truncated"] is True
