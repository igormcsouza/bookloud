import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from .config import cognito_pool_name, is_prod


class AuthStack(cdk.Stack):
    """Cognito user pool, created now (Phase 0) on purpose: sign-in aliases
    are **immutable after pool creation**, and IMPLEMENTATION_PLAN.md calls
    this out explicitly. Getting it right here means Phase 1 is pure
    application code — no infra change.

    Username + password sign-in (not email-based): email/phone aliases are
    intentionally left off ``SignInAliases``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prod = is_prod(environment)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=cognito_pool_name(environment),
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(username=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=False, mutable=True),
            ),
            account_recovery=(
                cognito.AccountRecovery.EMAIL_ONLY if prod else cognito.AccountRecovery.NONE
            ),
            password_policy=(
                cognito.PasswordPolicy(
                    min_length=12,
                    require_lowercase=True,
                    require_uppercase=True,
                    require_digits=True,
                    require_symbols=True,
                )
                if prod
                else cognito.PasswordPolicy(
                    min_length=8,
                    require_lowercase=False,
                    require_uppercase=False,
                    require_digits=False,
                    require_symbols=False,
                )
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN if prod else cdk.RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            prevent_user_existence_errors=True,
        )

        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(
            self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id
        )
