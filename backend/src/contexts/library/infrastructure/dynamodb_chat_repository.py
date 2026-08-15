from collections.abc import Sequence

import boto3
from boto3.dynamodb.conditions import Key

from src.config import settings
from src.contexts.library.domain.chat import ChatMessage
from src.contexts.library.domain.repository import ChatRepository
from src.contexts.library.infrastructure.chat_mapper import from_item, to_item
from src.contexts.library.infrastructure.keys import pk_book, CHAT_PREFIX


class DynamoDbChatRepository(ChatRepository):
    def __init__(self, table_name: str = settings.table_name) -> None:
        self.table_name = table_name
        self.dynamodb = boto3.resource("dynamodb", endpoint_url=settings.aws_endpoint_url or None)
        self.table = self.dynamodb.Table(table_name)

    def list_messages(self, book_id: str, limit: int = 50) -> list[ChatMessage]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(pk_book(book_id)) & Key("SK").begins_with(CHAT_PREFIX),
            ScanIndexForward=False,  # Descending to get latest messages first
            Limit=limit,
        )
        
        items = response.get("Items", [])
        
        # If we hit the limit, we want to return the oldest first in our subset?
        # Typically chat UI expects chronological order: oldest to newest. 
        # But we fetched newest first to get the last N messages.
        # So we reverse them before returning.
        return [from_item(i) for i in reversed(items)]

    def save_turn(self, user_msg: ChatMessage, assistant_msg: ChatMessage) -> None:
        with self.table.batch_writer() as batch:
            batch.put_item(Item=to_item(user_msg))
            batch.put_item(Item=to_item(assistant_msg))

    def clear(self, book_id: str) -> int:
        # Need to query all chat items and delete them
        pk = pk_book(book_id)

        # We need to paginate to get all CHAT items
        last_evaluated_key = None
        deleted = 0

        with self.table.batch_writer() as batch:
            while True:
                kwargs = {
                    "KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(CHAT_PREFIX),
                }
                if last_evaluated_key:
                    kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = self.table.query(**kwargs)
                items = response.get("Items", [])

                for item in items:
                    batch.delete_item(Key={"PK": pk, "SK": item["SK"]})
                    deleted += 1

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

        return deleted
