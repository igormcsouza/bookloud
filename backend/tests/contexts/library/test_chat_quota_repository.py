import boto3
import pytest
from moto import mock_aws

from src.contexts.library.infrastructure.dynamodb_chat_quota_repository import DynamoDbChatQuotaRepository


@pytest.fixture
def table_name():
    return "test-quota-table"

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
    return DynamoDbChatQuotaRepository(table_name=table_name)


def test_increment_and_check(repo):
    user_id = "u1"
    date = "2026-08-12"
    
    assert repo.get_count(user_id, date) == 0
    
    # 1st time
    assert repo.increment_and_check(user_id, date, 2) is True
    assert repo.get_count(user_id, date) == 1
    
    # 2nd time
    assert repo.increment_and_check(user_id, date, 2) is True
    assert repo.get_count(user_id, date) == 2
    
    # 3rd time - should fail
    assert repo.increment_and_check(user_id, date, 2) is False
    assert repo.get_count(user_id, date) == 2
    
    # different user/date
    assert repo.increment_and_check("u2", date, 2) is True
    assert repo.increment_and_check(user_id, "2026-08-13", 2) is True
