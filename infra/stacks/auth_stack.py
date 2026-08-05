import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from .config import cognito_pool_name, is_prod


class AuthStack(cdk.Stack):
    """Cognito user pool, created in Phase 0 on purpose: sign-in aliases
    are **immutable after pool creation**, and IMPLEMENTATION_PLAN.md calls
    this out explicitly. Getting it right there meant Phase 1 is pure
    application code plus this one trigger.

    Username + password sign-in (not email-based): email/phone aliases are
    intentionally left off ``SignInAliases``.

    Phase 1 adds a PreSignUp Lambda trigger: the pool has no auto-verified
    attributes (no email channel to deliver a confirmation code), so a
    self-signed-up user would otherwise land in ``UNCONFIRMED`` with no way
    out. The trigger auto-confirms every ``PreSignUp_SignUp`` event
    deterministically, with no email dependency. Adding ``LambdaConfig`` to
    an existing pool is an in-place CloudFormation update, not a replacement
    -- verified via ``cdk diff`` (see PLANS/phase-1.md §2.2).

    Post-plan revision (PLANS/phase-1.md §11): user provisioning is
    admin-only, not self-signup. ``self_sign_up_enabled=False`` makes
    Cognito itself reject every public ``SignUp`` call
    (``NotAuthorizedException``), before the PreSignUp trigger would ever
    fire. The trigger stays wired -- harmless dead code, kept in case
    self-signup is revisited -- rather than deleted. Flipping
    ``self_sign_up_enabled`` only changes ``AdminCreateUserConfig.
    AllowAdminCreateUserOnly``, which CloudFormation updates in place (no
    replacement); verified via template diff, same as the PreSignUp trigger
    addition above.
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

        # Guarding on triggerSource === 'PreSignUp_SignUp' means admin-created
        # users (PreSignUp_AdminCreateUser, none exist in this phase, but kept
        # for future-proofing) are unaffected.
        pre_signup_fn = lambda_.Function(
            self,
            "PreSignUpAutoConfirm",
            runtime=lambda_.Runtime.NODEJS_22_X,
            handler="index.handler",
            timeout=cdk.Duration.seconds(5),
            code=lambda_.Code.from_inline(
                "exports.handler = async (event) => {"
                "  if (event.triggerSource === 'PreSignUp_SignUp') {"
                "    event.response.autoConfirmUser = true;"
                "  }"
                "  return event;"
                "};"
            ),
        )

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=cognito_pool_name(environment),
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            lambda_triggers=cognito.UserPoolTriggers(pre_sign_up=pre_signup_fn),
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
