"""Boto3 조회 로직과 AWS 오류 분류."""

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    CredentialRetrievalError,
    HTTPClientError,
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)

from backend.config import Settings

CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AllAccessDisabled",
}
LOGIN_REQUIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "InvalidToken",
    "RequestExpired",
    "AuthFailure",
    "UnrecognizedClientException",
}


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _access_denied() -> AwsServiceError:
    return AwsServiceError(403, "AWS_ACCESS_DENIED", "AWS 권한이 부족합니다.")


def _login_required() -> AwsServiceError:
    return AwsServiceError(
        503,
        "AWS_LOGIN_REQUIRED",
        "AWS 로그인이 만료되었거나 갱신할 수 없습니다. aws login --profile <프로필>을 다시 실행하세요.",
    )


def _connection_error() -> AwsServiceError:
    return AwsServiceError(503, "AWS_CONNECTION_ERROR", "AWS와 통신하지 못했습니다. 네트워크를 확인하세요.")


def translate_error(exc: Exception) -> AwsServiceError:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED_CODES:
            return _access_denied()
        if code in LOGIN_REQUIRED_CODES:
            return _login_required()
        return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청이 실패했습니다.")
    if isinstance(
        exc,
        (
            LoginError,
            SSOError,
            TokenRetrievalError,
            NoCredentialsError,
            PartialCredentialsError,
            CredentialRetrievalError,
            ProfileNotFound,
        ),
    ):
        return _login_required()
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return _connection_error()
    return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청이 실패했습니다.")


class AwsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            # 요청마다 새 세션을 만들어 aws login 재실행 결과를 바로 반영한다.
            self.session = boto3.Session(
                profile_name=settings.aws_profile,
                region_name=settings.aws_region,
            )
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc

    def _client(self, name: str):
        return self.session.client(name, config=CLIENT_CONFIG)

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc

    def get_context(self) -> dict:
        identity = self._call(self._client("sts").get_caller_identity)
        account_id = identity["Account"]
        if account_id != self.settings.expected_account_id:
            raise AwsServiceError(
                403,
                "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다.",
            )
        return {
            "account_id": account_id,
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
        }

    def _paginate(self, client, operation: str, result_key: str, **kwargs) -> list:
        def run():
            items = []
            for page in client.get_paginator(operation).paginate(**kwargs):
                items.extend(page.get(result_key, []))
            return items

        return self._call(run)

    def list_ec2_instances(self) -> list[dict]:
        reservations = self._paginate(self._client("ec2"), "describe_instances", "Reservations")
        items = []
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                items.append(
                    {
                        "instance_id": inst["InstanceId"],
                        "name": tags.get("Name") or inst["InstanceId"],
                        "state": inst.get("State", {}).get("Name"),
                        "instance_type": inst.get("InstanceType"),
                        "private_ip": inst.get("PrivateIpAddress"),
                        "public_ip": inst.get("PublicIpAddress"),
                    }
                )
        return items

    def list_s3_buckets(self) -> list[dict]:
        buckets = self._paginate(self._client("s3"), "list_buckets", "Buckets")
        return [{"name": b["Name"]} for b in buckets]

    def list_security_groups(self) -> list[dict]:
        groups = self._paginate(self._client("ec2"), "describe_security_groups", "SecurityGroups")
        return [
            {
                "GroupId": g["GroupId"],
                "GroupName": g["GroupName"],
                "IpPermissions": g.get("IpPermissions", []),
            }
            for g in groups
        ]
