"""AWS Lambda entrypoint for the Bookloud backend.

Wraps the **same** FastAPI ``app`` used by local dev and the tests with
Mangum, so every route works unchanged behind API Gateway. No Lambda-only
app is forked.

Deployed as a container image with ``CMD ["lambda_function.handler"]``.
"""

from mangum import Mangum

from src.main import app

handler = Mangum(app)
