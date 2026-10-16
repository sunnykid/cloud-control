"""EC2 제어 API 테스트. 실제 AWS·실습 DB·.env를 사용하지 않는다.

실행: .venv/bin/python -m unittest tests.test_actions -v
"""

import asyncio
import json
import os
import unittest
import uuid
from unittest import mock

from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy.exc import OperationalError

from backend import action_store, actions, aws_service, config
from backend.main import app

TOKEN = "test-token"
ACCOUNT = "111122223333"
ALLOWED = "i-0aaaaaaaaaaaaaaaa"
OTHER_ALLOWED = "i-0bbbbbbbbbbbbbbbb"
NOT_ALLOWED = "i-0ccccccccccccccccc"

TEST_ENV = {
    "AWS_PROFILE": "test",
    "AWS_REGION": "ap-northeast-2",
    "EXPECTED_ACCOUNT_ID": ACCOUNT,
    "ADMIN_API_TOKEN": TOKEN,
    "ALLOWED_INSTANCE_IDS": f" {ALLOWED} , ,{OTHER_ALLOWED}",
    "DB_HOST": "unused",
    "DB_USER": "unused",
    "DB_PASSWORD": "unused",
    "DB_NAME": "unused",
}


def call_api(method, path, body=None, token=TOKEN):
    """httpx 없이 ASGI 앱을 직접 호출한다."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"test"), (b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80),
    }
    messages = []

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                await asyncio.sleep(3600)
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    status = messages[0]["status"]
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(payload)


class FakeStore:
    """action_store 함수 대체. 메모리에 이력을 보관한다."""

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = set()  # 실패시킬 함수 이름

    def _check(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise OperationalError("SELECT 1", {}, Exception("db down"))

    def get_by_request_id(self, request_id):
        self._check("get_by_request_id")
        return next((dict(r) for r in self.rows.values() if r["request_id"] == request_id), None)

    def find_open_action(self, instance_id):
        self._check("find_open_action")
        return next(
            (dict(r) for r in self.rows.values()
             if r["instance_id"] == instance_id and r["status"] in ("REQUESTED", "UNKNOWN")),
            None,
        )

    def create_requested(self, request_id, instance_id, action):
        self._check("create_requested")
        if any(r["request_id"] == request_id for r in self.rows.values()):
            raise action_store.DuplicateRequestId()
        return self.add(request_id, instance_id, action, "REQUESTED")

    def mark_result(self, action_id, status, aws_request_id, message):
        self._check("mark_result")
        row = self.rows[action_id]
        if row["status"] == "REQUESTED":
            row.update(status=status, aws_request_id=aws_request_id, message=message)

    def add(self, request_id, instance_id, action, status):
        row = {"id": str(uuid.uuid4()), "request_id": request_id, "instance_id": instance_id,
               "action": action, "status": status, "aws_request_id": None, "message": None}
        self.rows[row["id"]] = row
        return dict(row)


class ActionApiTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        # 실제 .env를 읽지 않는다.
        no_dotenv = mock.patch.object(config, "load_dotenv", lambda *a, **k: None)
        no_dotenv.start()
        self.addCleanup(no_dotenv.stop)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)

        # DB 저장 함수 대체
        self.store = FakeStore()
        for name in ("get_by_request_id", "find_open_action", "create_requested", "mark_result"):
            p = mock.patch.object(action_store, name, getattr(self.store, name))
            p.start()
            self.addCleanup(p.stop)

        # AWS client 대체
        self.sts = mock.Mock()
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": "arn:aws:iam::x:user/s", "UserId": "u"}
        self.ec2 = mock.Mock()
        self.set_state("stopped")
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"PreviousState": {"Name": "stopped"}, "CurrentState": {"Name": "pending"}}],
            "ResponseMetadata": {"RequestId": "aws-req-1", "HTTPStatusCode": 200},
        }
        self.ec2.stop_instances.return_value = {
            "StoppingInstances": [{"PreviousState": {"Name": "running"}, "CurrentState": {"Name": "stopping"}}],
            "ResponseMetadata": {"RequestId": "aws-req-2", "HTTPStatusCode": 200},
        }
        self.client_configs = []

        def make_client(name, config=None):
            self.client_configs.append((name, config))
            return {"sts": self.sts, "ec2": self.ec2}[name]

        session = mock.Mock()
        session.client.side_effect = make_client
        p = mock.patch.object(aws_service.boto3, "Session", return_value=session)
        self.session_cls = p.start()
        self.addCleanup(p.stop)

    # 도우미
    def set_state(self, state):
        self.ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"InstanceId": ALLOWED, "State": {"Name": state}}]}]
        }

    def mutation_calls(self):
        return self.ec2.start_instances.call_count + self.ec2.stop_instances.call_count

    def post(self, instance_id=ALLOWED, action="start", request_id=None, token=TOKEN, body=None):
        if body is None:
            body = {"action": action, "request_id": request_id or str(uuid.uuid4())}
        return call_api("POST", f"/api/ec2/{instance_id}/actions", body, token)

    # 인증
    def test_invalid_token_makes_no_calls(self):
        for token in (None, "wrong", ""):
            status, body = self.post(token=token)
            self.assertEqual(status, 401)
            self.assertEqual(body["detail"]["code"], "APP_AUTH_REQUIRED")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.store.calls, [])
        self.session_cls.assert_not_called()

    # 입력 검증
    def test_extra_field_and_bad_input_are_422(self):
        rid = str(uuid.uuid4())
        cases = [
            {"action": "start", "request_id": rid, "region": "us-east-1"},
            {"action": "start", "request_id": "not-a-uuid"},
            {"action": "terminate", "request_id": rid},
            {"action": "start"},
        ]
        for body in cases:
            status, resp = self.post(body=body)
            self.assertEqual(status, 422, body)
            self.assertEqual(resp["detail"]["code"], "VALIDATION_ERROR")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.store.calls, [])

    # 허용 목록
    def test_not_allowed_instance_is_403_before_any_call(self):
        status, body = self.post(instance_id=NOT_ALLOWED)
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.store.calls, [])
        self.session_cls.assert_not_called()

    def test_empty_allowlist_rejects_control(self):
        os.environ["ALLOWED_INSTANCE_IDS"] = " , "
        config.get_settings.cache_clear()
        status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "INSTANCE_CONTROL_DISABLED")
        self.assertEqual(self.mutation_calls(), 0)

    # 정상 접수와 재전송
    def test_accepted_returns_202(self):
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 202)
        self.assertEqual(set(body), {"action_id", "request_id", "status"})
        self.assertEqual(body["request_id"], rid)
        self.assertEqual(body["status"], "ACCEPTED")
        row = self.store.rows[body["action_id"]]
        self.assertEqual((row["status"], row["aws_request_id"]), ("ACCEPTED", "aws-req-1"))
        self.ec2.start_instances.assert_called_once_with(InstanceIds=[ALLOWED])
        # 변경 호출은 재시도 1회 제한 client를 사용한다.
        control_configs = [c for n, c in self.client_configs if n == "ec2" and c is aws_service.CONTROL_CONFIG]
        self.assertEqual(len(control_configs), 1)
        self.assertEqual(aws_service.CONTROL_CONFIG.retries, {"total_max_attempts": 1})

    def test_stop_requires_running(self):
        self.set_state("running")
        status, body = self.post(action="stop")
        self.assertEqual(status, 202)
        self.ec2.stop_instances.assert_called_once_with(InstanceIds=[ALLOWED])

    def test_same_key_resend_executes_once(self):
        rid = str(uuid.uuid4())
        first_status, first = self.post(request_id=rid)
        # 재전송 시점에는 상태가 바뀌었어도 중복 판정이 먼저다.
        self.set_state("pending")
        second_status, second = self.post(request_id=rid)
        self.assertEqual((first_status, second_status), (202, 200))
        self.assertEqual(first, second)
        self.assertEqual(self.mutation_calls(), 1)

    def test_same_key_different_content_is_409(self):
        rid = str(uuid.uuid4())
        self.post(request_id=rid)
        status, body = self.post(request_id=rid, action="stop")
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["code"], "IDEMPOTENCY_CONFLICT")
        status, body = self.post(request_id=rid, instance_id=OTHER_ALLOWED)
        self.assertEqual(status, 409)
        self.assertEqual(self.mutation_calls(), 1)

    def test_unique_conflict_on_insert_returns_existing(self):
        rid = str(uuid.uuid4())
        real_get = self.store.get_by_request_id
        seen = []

        def racing_get(request_id):
            # 첫 조회 직후 다른 경로가 같은 키를 먼저 저장한 상황을 흉내 낸다.
            result = real_get(request_id)
            if not seen:
                seen.append(1)
                self.store.add(rid, ALLOWED, "start", "ACCEPTED")
            return result

        with mock.patch.object(action_store, "get_by_request_id", racing_get):
            status, body = self.post(request_id=rid)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(self.mutation_calls(), 0)

    # 상태·계정
    def test_wrong_state_is_409(self):
        for state in ("running", "pending", "stopping"):
            self.set_state(state)
            status, body = self.post(action="start")
            self.assertEqual(status, 409, state)
            self.assertEqual(body["detail"]["code"], "INVALID_INSTANCE_STATE")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.store.rows, {})

    def test_account_mismatch_is_403(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999", "Arn": "a", "UserId": "u"}
        status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "AWS_ACCOUNT_MISMATCH")
        self.assertEqual(self.mutation_calls(), 0)
        self.assertEqual(self.store.rows, {})

    # DB 실패
    def test_db_failure_makes_no_mutation(self):
        for failing in ("get_by_request_id", "find_open_action", "create_requested"):
            self.store.fail = {failing}
            status, body = self.post()
            self.assertEqual(status, 503, failing)
            self.assertEqual(body["detail"]["code"], "DB_UNAVAILABLE")
        self.assertEqual(self.mutation_calls(), 0)

    def test_db_update_failure_after_aws_keeps_requested(self):
        self.store.fail = {"mark_result"}
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "DB_UPDATE_FAILED")
        self.assertEqual([r["status"] for r in self.store.rows.values()], ["REQUESTED"])
        self.store.fail = set()
        # 같은 키는 기존 REQUESTED를 돌려주고, 새 키는 차단된다. AWS는 다시 호출하지 않는다.
        self.assertEqual(self.post(request_id=rid)[1]["status"], "REQUESTED")
        self.assertEqual(self.post()[0], 409)
        self.assertEqual(self.mutation_calls(), 1)

    # 결과 불확실·거절
    def test_timeout_records_unknown_without_retry(self):
        self.ec2.start_instances.side_effect = ReadTimeoutError(endpoint_url="https://ec2")
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "AWS_RESULT_UNKNOWN")
        self.assertEqual(body["detail"]["request_id"], rid)
        self.assertEqual([r["status"] for r in self.store.rows.values()], ["UNKNOWN"])
        self.assertEqual(self.mutation_calls(), 1)
        # 같은 키 재전송은 UNKNOWN을 돌려주고 추가 호출이 없다.
        status, body = self.post(request_id=rid)
        self.assertEqual((status, body["status"]), (200, "UNKNOWN"))
        self.assertEqual(self.mutation_calls(), 1)

    def test_explicit_rejection_records_failed(self):
        self.ec2.start_instances.side_effect = ClientError(
            {"Error": {"Code": "IncorrectInstanceState"}, "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": "r"}},
            "StartInstances",
        )
        status, body = self.post()
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["status"], "FAILED")
        self.assertEqual([r["status"] for r in self.store.rows.values()], ["FAILED"])
        # FAILED는 확정 결과이므로 새 요청은 막지 않는다.
        self.ec2.start_instances.side_effect = None
        self.assertEqual(self.post()[0], 202)

    def test_existing_open_action_blocks_new_request(self):
        for open_status in ("REQUESTED", "UNKNOWN"):
            self.store.rows.clear()
            self.store.add(str(uuid.uuid4()), ALLOWED, "start", open_status)
            status, body = self.post()
            self.assertEqual(status, 409, open_status)
            self.assertEqual(body["detail"]["code"], "ACTION_IN_PROGRESS")
        self.assertEqual(self.mutation_calls(), 0)

    # lock
    def test_mutation_runs_inside_single_global_lock(self):
        lock_before = actions.CONTROL_LOCK
        held = []
        self.ec2.start_instances.side_effect = lambda **kw: held.append(actions.CONTROL_LOCK.locked()) or {
            "StartingInstances": [{}], "ResponseMetadata": {"RequestId": "r"}}
        self.post()
        self.assertEqual(held, [True])
        self.assertIs(actions.CONTROL_LOCK, lock_before)
        self.assertFalse(actions.CONTROL_LOCK.locked())


if __name__ == "__main__":
    unittest.main()
