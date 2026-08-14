import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.auth.jwt_verifier import CognitoJwtVerifier, InvalidToken


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    
    # Get JWK
    numbers = public_key.public_numbers()
    def to_base64url(val):
        length = (val.bit_length() + 7) // 8
        b = val.to_bytes(length, byteorder="big")
        import base64
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": to_base64url(numbers.n),
        "e": to_base64url(numbers.e),
    }
    
    return private_pem, jwk


@pytest.fixture
def verifier(rsa_keypair):
    _, jwk = rsa_keypair
    jwks_json = json.dumps({"keys": [jwk]}).encode("utf-8")
    
    return CognitoJwtVerifier(
        region="us-east-1",
        user_pool_id="us-east-1_XXXXX",
        audience="client_123",
        fetch=lambda url: jwks_json,
    )


def make_token(private_pem, **kwargs):
    payload = {
        "sub": "user_1",
        "aud": "client_123",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX",
        "token_use": "id",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    payload.update(kwargs.get("payload_overrides", {}))
    
    headers = {"kid": "test-kid"}
    headers.update(kwargs.get("header_overrides", {}))
    
    alg = kwargs.get("alg", "RS256")
    key = private_pem if alg != "none" else ""
    
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def test_valid_token(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem)
    claims = verifier.verify(token)
    assert claims["sub"] == "user_1"
    assert claims["token_use"] == "id"


def test_missing_kid(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, header_overrides={"kid": None})
    with pytest.raises(InvalidToken, match="Missing kid"):
        verifier.verify(token)


def test_unknown_kid(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, header_overrides={"kid": "wrong-kid"})
    with pytest.raises(InvalidToken, match="Unknown kid"):
        verifier.verify(token)


def test_expired_token(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    exp = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    token = make_token(private_pem, payload_overrides={"exp": exp})
    with pytest.raises(InvalidToken, match="Token expired"):
        verifier.verify(token)


def test_invalid_audience(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, payload_overrides={"aud": "wrong-client"})
    with pytest.raises(InvalidToken, match="Invalid audience"):
        verifier.verify(token)


def test_invalid_issuer(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, payload_overrides={"iss": "https://wrong"})
    with pytest.raises(InvalidToken, match="Invalid issuer"):
        verifier.verify(token)


def test_token_use_access(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, payload_overrides={"token_use": "access"})
    with pytest.raises(InvalidToken, match="Invalid token_use"):
        verifier.verify(token)


def test_alg_none(verifier, rsa_keypair):
    private_pem, _ = rsa_keypair
    token = make_token(private_pem, alg="none")
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_jwks_rate_limiting(monkeypatch, rsa_keypair):
    private_pem, jwk = rsa_keypair
    
    fetch_count = 0
    def tracking_fetch(url):
        nonlocal fetch_count
        fetch_count += 1
        return json.dumps({"keys": [jwk]}).encode("utf-8")
        
    verifier = CognitoJwtVerifier(
        region="us-east-1",
        user_pool_id="us-east-1_XXXXX",
        audience="client_123",
        fetch=tracking_fetch,
    )
    
    # First fetch (cache miss, kid not known)
    token = make_token(private_pem, header_overrides={"kid": "wrong-kid-1"})
    with pytest.raises(InvalidToken):
        verifier.verify(token)
    assert fetch_count == 1
    
    # Second fetch immediately (should hit cooldown, no new fetch)
    token2 = make_token(private_pem, header_overrides={"kid": "wrong-kid-2"})
    with pytest.raises(InvalidToken):
        verifier.verify(token2)
    assert fetch_count == 1  # Blocked by cooldown
    
    # Fast forward time 301 seconds
    monkeypatch.setattr(time, "monotonic", lambda: time.time() + 301)
    with pytest.raises(InvalidToken):
        verifier.verify(token2)
    assert fetch_count == 2
