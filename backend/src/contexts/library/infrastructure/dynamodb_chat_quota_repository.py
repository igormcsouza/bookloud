import boto3
from botocore.exceptions import ClientError

from src.config import settings
from src.contexts.library.domain.repository import ChatQuotaRepository
from src.contexts.library.infrastructure.keys import pk_user, sk_chat_quota


class DynamoDbChatQuotaRepository(ChatQuotaRepository):
    def __init__(self, table_name: str = settings.table_name) -> None:
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb", endpoint_url=settings.aws_endpoint_url or None)
        self.table = self.dynamodb.Table(table_name)

    def increment_and_check(self, user_id: str, date: str, limit: int) -> bool:
        """PLANS/phase-7.md §4.7. Atomic increment with a condition.
        Returns True if successful, False if quota exceeded.
        """
        try:
            self.table.update_item(
                Key={
                    "PK": pk_user(user_id),
                    "SK": sk_chat_quota(date),
                },
                UpdateExpression="ADD #c :one",
                ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
                ExpressionAttributeNames={
                    "#c": "count",
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": limit,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_count(self, user_id: str, date: str) -> int:
        response = self.table.get_item(
            Key={
                "PK": pk_user(user_id),
                "SK": sk_chat_quota(date),
            }
        )
        item = response.get("Item")
        if not item:
            return 0
        return int(item.get("count", 0))
