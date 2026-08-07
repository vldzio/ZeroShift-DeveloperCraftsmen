"""Fixture code sample for Part 1: payment processor.

DynamoDB writes + KMS decrypt + SNS publish. Includes a class attribute
holding a client, which exercises the extractor's ability to follow attribute
accesses.
"""
import boto3


class PaymentProcessor:
    def __init__(self):
        self.table = boto3.resource("dynamodb").Table("payments-ledger")
        self.kms = boto3.client("kms")
        self.sns = boto3.client("sns")

    def process(self, event):
        card_payload = self.kms.decrypt(CiphertextBlob=event["encryptedCard"])
        card = card_payload["Plaintext"].decode("utf-8")
        txn_id = self._settle(card, event["amount"])
        self.table.put_item(
            Item={
                "txnId": txn_id,
                "amount": event["amount"],
                "status": "SETTLED",
            }
        )
        self.sns.publish(
            TopicArn="arn:aws:sns:us-east-1:111111111111:payment-events",
            Subject="Payment settled",
            Message=txn_id,
        )
        return {"txnId": txn_id}

    def _settle(self, card, amount):
        return f"txn-{amount}-{hash(card) % 100000}"


def lambda_handler(event, context):
    return PaymentProcessor().process(event)
