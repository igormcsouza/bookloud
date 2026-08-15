import json
import time
import urllib.request
from typing import Callable

import jwt


class InvalidToken(Exception):
    """Never carries the token, a claim value, or a key. Its str() is one of a
    small closed set of reasons, safe to log."""
    pass


class CognitoJwtVerifier:
    def __init__(
        self,
        *,
        region: str,
        user_pool_id: str,
        audience: str,
        jwks_url: str | None = None,
        verify_issuer: bool = True,
        fetch: Callable[[str], bytes] | None = None
    ) -> None:
        self._region = region
        self._user_pool_id = user_pool_id
        self._audience = audience
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._jwks_url = jwks_url or f"{self._issuer}/.well-known/jwks.json"
        self._verify_issuer = verify_issuer
        self._fetch = fetch or self._default_fetch
        
        self._jwks: dict | None = None
        self._last_fetch_time: float = -float('inf')

    def _default_fetch(self, url: str) -> bytes:
        with urllib.request.urlopen(url) as response:
            return response.read()

    def _get_jwk(self, kid: str) -> dict | None:
        if self._jwks:
            for key in self._jwks.get("keys", []):
                if key.get("kid") == kid:
                    return key
                    
        # Refetch if not found, subject to 300s cooldown
        now = time.monotonic()
        if now - self._last_fetch_time > 300:
            try:
                data = self._fetch(self._jwks_url)
                self._jwks = json.loads(data)
                self._last_fetch_time = now
            except Exception as e:
                raise InvalidToken("JWKS fetch failed") from e
                
            for key in self._jwks.get("keys", []):
                if key.get("kid") == kid:
                    return key
                    
        return None

    def verify(self, token: str) -> dict:
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("Invalid header")

        kid = unverified_header.get("kid")
        if not kid:
            raise InvalidToken("Missing kid")

        jwk = self._get_jwk(kid)
        if not jwk:
            raise InvalidToken("Unknown kid")

        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
            
            options = {"verify_exp": True, "leeway": 0, "verify_iss": self._verify_issuer}
            
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer if self._verify_issuer else None,
                options=options,
            )
            
            if claims.get("token_use") != "id":
                raise InvalidToken("Invalid token_use")
                
            return claims
            
        except jwt.ExpiredSignatureError:
            raise InvalidToken("Token expired")
        except jwt.InvalidAudienceError:
            raise InvalidToken("Invalid audience")
        except jwt.InvalidIssuerError:
            raise InvalidToken("Invalid issuer")
        except jwt.PyJWTError:
            # signature validation failed, wrong format, etc
            raise InvalidToken("Signature verification failed")
