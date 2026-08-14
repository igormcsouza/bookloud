import logging

from starlette.responses import JSONResponse

from src.auth.jwt_verifier import CognitoJwtVerifier, InvalidToken
from src.config import settings

logger = logging.getLogger(__name__)

# Module-level instance to cache JWKS across requests
_verifier: CognitoJwtVerifier | None = None

def get_verifier() -> CognitoJwtVerifier:
    global _verifier
    if _verifier is None:
        jwks_url = None
        if settings.aws_endpoint_url:
            jwks_url = f"{settings.aws_endpoint_url.rstrip('/')}/{settings.cognito_user_pool_id}/.well-known/jwks.json"
            
        _verifier = CognitoJwtVerifier(
            region=settings.aws_region,
            user_pool_id=settings.cognito_user_pool_id,
            audience=settings.cognito_client_id,
            jwks_url=jwks_url,
        )
    return _verifier


class FunctionUrlAuthMiddleware:
    """Pure-ASGI. Verifies the Bearer token and writes the claims into
    scope["aws.event"] in API Gateway's own shape, so
    auth/dependencies.py's claims_from_request() and get_current_user()
    are UNCHANGED and every use case below them cannot tell which app it is
    running in. On a missing or invalid token it short-circuits with a 401
    JSON body -- it never falls through to the route, because a route that
    reached get_current_user() with no claims would 401 anyway, just after
    doing work."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if scope.get("path") == "/health":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1")

        if not auth_header.startswith("Bearer "):
            response = JSONResponse(
                {"message": "UNAUTHENTICATED"}, status_code=401
            )
            return await response(scope, receive, send)

        token = auth_header[7:]

        try:
            v = get_verifier()
            claims = v.verify(token)
        except InvalidToken as e:
            logger.warning("FunctionUrlAuthMiddleware rejecting token: %s", str(e))
            response = JSONResponse(
                {"message": "UNAUTHENTICATED"}, status_code=401
            )
            return await response(scope, receive, send)

        # Inject claims into scope["aws.event"] API Gateway style
        aws_event = scope.setdefault("aws.event", {})
        request_context = aws_event.setdefault("requestContext", {})
        authorizer = request_context.setdefault("authorizer", {})
        jwt_dict = authorizer.setdefault("jwt", {})
        jwt_dict["claims"] = claims

        return await self.app(scope, receive, send)
