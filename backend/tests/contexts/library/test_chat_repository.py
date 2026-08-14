import boto3
import pytest
from moto import mock_aws

from src.contexts.library.domain.chat import ChatMessage, ChatRole
from src.contexts.library.infrastructure.dynamodb_chat_repository import DynamoDbChatRepository

@pytest.fixture
def table_name():
    return "test-chat-table"

@pytest.fixture
def dynamodb_table(table_name):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"}
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )
        yield table

@pytest.fixture
def repo(dynamodb_table, table_name, monkeypatch):
    monkeypatch.setattr("src.config.settings.table_name", table_name)
    monkeypatch.setattr("src.config.settings.aws_endpoint_url", "")
    return DynamoDbChatRepository(table_name=table_name)

def test_save_turn_and_list_messages(repo):
    u_msg = ChatMessage("b1", "1", "u1", ChatRole.USER, "hi", 0, "2026-08-12T10:00:00.000000+00:00")
    a_msg = ChatMessage("b1", "2", "u1", ChatRole.ASSISTANT, "hello", 0, "2026-08-12T10:00:01.000000+00:00")
    
    repo.save_turn(u_msg, a_msg)
    
    msgs = repo.list_messages("b1")
    assert len(msgs) == 2
    assert msgs[0].role == ChatRole.USER
    assert msgs[1].role == ChatRole.ASSISTANT
    
def test_list_messages_limit(repo):
    for i in range(10):
        # Need distinct padded created_at so they sort properly
        t = f"2026-08-12T10:00:{i:02d}.000000+00:00"
        m = ChatMessage("b1", str(i), "u1", ChatRole.USER, str(i), 0, t)
        m_a = ChatMessage("b1", f"{i}a", "u1", ChatRole.ASSISTANT, str(i), 0, t.replace(f"{i:02d}", f"{i+10:02d}"))
        repo.save_turn(m, m_a)
        
    msgs = repo.list_messages("b1", limit=5)
    assert len(msgs) == 5
    # Should be the most recent 5, ordered chronologically
    assert msgs[-1].role == ChatRole.ASSISTANT
    
def test_clear(repo):
    u_msg = ChatMessage("b1", "1", "u1", ChatRole.USER, "hi", 0, "2026-08-12T10:00:00.000000+00:00")
    a_msg = ChatMessage("b1", "2", "u1", ChatRole.ASSISTANT, "hello", 0, "2026-08-12T10:00:01.000000+00:00")
    
    repo.save_turn(u_msg, a_msg)
    assert len(repo.list_messages("b1")) == 2
    
    repo.clear("b1")
    assert len(repo.list_messages("b1")) == 0
