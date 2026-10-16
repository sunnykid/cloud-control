# Cloud Control

학생 본인의 AWS 계정에서 다음 세 가지를 웹으로 수행하는 **학습용 앱**입니다.

1. **조회**: 계정·EC2·S3 버킷·서울 리전 보안 그룹·제어 이력
2. **지정 EC2 제어**: 서버 허용 목록에 있는 인스턴스의 시작·중지만
3. **보안 그룹 규칙 점검**: 실제 EC2에 연결된 보안 그룹에서 인터넷 전체에 열린 SSH(22)·RDP(3389) 규칙 찾기

## 범위

- **단일 프로세스, localhost 전용**입니다. `127.0.0.1:8001`에서 uvicorn 프로세스 하나로 실행합니다. 전역 lock이 한 프로세스 안에서만 동작하므로 여러 워커나 여러 서버로 실행하지 않습니다.
- 리전은 서울(`ap-northeast-2`)만 사용합니다. 다른 값이면 서버가 시작되지 않습니다.
- 사용자는 실습하는 학생 한 명입니다. 사용자 계정·권한 관리는 없습니다.

### 지원하지 않는 기능

| 구분 | 지원하지 않는 것 |
|---|---|
| 생성·삭제 | EC2·S3·보안 그룹 생성/삭제, EC2 종료(terminate) |
| 제어 | 시작·중지 외의 작업(재부팅, 강제 중지 등), 허용 목록 밖 인스턴스 제어 |
| 자동 수정 | 보안 그룹 규칙 추가·삭제·수정, 점검 결과에 따른 자동 조치 |
| 자동화 | 예약 실행, 자동 재시도, 상태 자동 폴링 |
| 배포·운영 | 외부 배포, 0.0.0.0 바인딩, 다중 프로세스, HTTPS |
| 인증 | 아이디·비밀번호 로그인, JWT 발급, 사용자 관리 |
| 저장 | 보안 점검 결과 저장(점검은 매번 새로 조회) |

## 실행 순서

### 1. 프로젝트 폴더와 가상환경 (WSL Ubuntu)

프로젝트는 WSL 리눅스 파일 시스템에 둡니다(`/mnt/c/...` 아래가 아님).

```bash
cd ~/vibe-lab/cloud-control
python3 -m venv .venv            # 처음 한 번
.venv/bin/pip install -r requirements.txt
```

### 2. MariaDB 시작

```bash
sudo systemctl start mariadb      # systemd가 없는 WSL이면: sudo service mariadb start
sudo systemctl status mariadb --no-pager
```

DB와 사용자는 처음 한 번 만듭니다(`sudo mariadb`에서 실행, 암호는 직접 정해서 입력).

```sql
CREATE DATABASE cloud_control CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'cloud_control'@'localhost' IDENTIFIED BY '<직접 정한 암호>';
GRANT ALL PRIVILEGES ON cloud_control.* TO 'cloud_control'@'localhost';
```

### 3. 환경변수 파일 `.env`

`.env.example`을 복사한 뒤 편집기로 값을 채웁니다. `.env`는 `.gitignore`에 포함되어 Git에 올라가지 않습니다. 실제 값은 채팅·문서·스크린샷에 넣지 마세요.

```bash
cp .env.example .env
```

| 변수 | 용도 |
|---|---|
| `AWS_PROFILE` | `aws login`에 사용한 AWS CLI 프로필 이름 |
| `AWS_REGION` | 사용할 리전. `ap-northeast-2`만 허용 |
| `EXPECTED_ACCOUNT_ID` | 본인 AWS 계정 ID(12자리). 로그인한 계정이 이와 다르면 모든 AWS 요청을 403으로 차단 |
| `ADMIN_API_TOKEN` | 웹 화면에서 입력하는 앱 토큰. 없거나 비어 있으면 서버가 시작되지 않음 |
| `ALLOWED_INSTANCE_IDS` | 시작·중지를 허용할 EC2 인스턴스 ID 목록(쉼표 구분) |
| `DB_HOST`, `DB_PORT` | MariaDB 주소와 포트(`DB_PORT` 생략 시 3306) |
| `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MariaDB 사용자, 암호, 데이터베이스 이름 |

앱 토큰은 직접 만든 긴 임의 문자열을 씁니다. 다음 명령은 화면에 출력하지 않고 `.env`에 바로 추가합니다(예시 줄은 지웁니다).

```bash
echo "ADMIN_API_TOKEN=$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
```

### 4. 테이블 생성

```bash
.venv/bin/python -m backend.init_db
```

없는 테이블만 만들고, 기존 테이블과 데이터는 지우거나 바꾸지 않습니다. 이전 버전으로 만든 `action_logs`에 `request_id` UNIQUE 제약이 없으면 실패하면서 직접 실행할 `ALTER TABLE` 문을 보여 줍니다.

### 5. AWS 로그인

```bash
aws login --profile lab
aws sts get-caller-identity --profile lab   # 계정이 EXPECTED_ACCOUNT_ID와 같은지 확인
```

학생 본인의 관리자 권한 IAM 사용자로 받은 **임시 자격증명**을 사용합니다. 만료되면 앱이 `AWS_LOGIN_REQUIRED`를 표시하므로 이 명령을 다시 실행합니다(서버 재시작은 필요 없음).

### 6. 서버 실행

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

`.env`는 이 서버가 시작할 때 읽습니다. `.env`를 바꾸면 서버를 재시작해야 반영됩니다.

### 7. Windows 브라우저 접속

Windows의 브라우저에서 **http://localhost:8001/** 을 엽니다(WSL2의 localhost 전달 기능 사용). 접속되지 않으면 WSL 안에서 `curl -I http://127.0.0.1:8001/`로 서버가 떠 있는지 먼저 확인하고, Windows의 `%UserProfile%\.wslconfig`에서 `localhostForwarding`이 꺼져 있지 않은지 확인합니다. 외부에서 접속하도록 `--host 0.0.0.0`으로 바꾸지 마세요.

화면에서 앱 토큰을 입력하고 [연결]을 누릅니다.

## 앱 토큰

- **로그인 후 자동 발급되는 JWT가 아닙니다.** `.env`의 `ADMIN_API_TOKEN`에 직접 넣어 둔 **실습용 고정 문자열**입니다. 만료 시간과 사용자 구분이 없습니다.
- 서버는 모든 `/api` 요청의 `Authorization: Bearer <토큰>`을 이 값과 비교합니다. 정적 화면(`/`)은 토큰 입력을 위해 인증 없이 열립니다.
- 브라우저는 토큰을 JS 메모리에만 보관합니다. URL·localStorage에 저장하지 않으므로 페이지를 새로고침하거나 [로그아웃]하면 다시 입력해야 합니다.
- 토큰을 바꾸려면 `.env`를 수정하고 서버를 재시작합니다.
- 앱 토큰 오류(401 `APP_AUTH_REQUIRED`)와 AWS 로그인 만료(503 `AWS_LOGIN_REQUIRED`)는 다릅니다. 앞의 경우는 토큰 입력 화면으로 돌아가고, 뒤의 경우는 화면 상단에 `aws login` 안내가 나옵니다.

## EC2 제어

### 허용 목록 (`ALLOWED_INSTANCE_IDS`)

- 시작·중지는 이 목록에 있는 인스턴스만 가능합니다. 목록은 **`.env`에서만 편집**하고, 바꾼 뒤 **서버를 재시작**해야 반영됩니다. 화면이나 API로는 바꿀 수 없습니다.
- 목록이 비어 있으면 모든 시작·중지를 거절합니다. 조회와 보안 점검은 그대로 됩니다.
- **목록에서 ID를 지워도 EC2가 삭제되지 않습니다.** 이 앱에서 그 인스턴스를 제어할 수 없게 될 뿐이며, 인스턴스와 기존 실행 이력은 그대로 남습니다.
- 허용 목록은 시작·중지 제한일 뿐이고, 조회나 보안 점검 대상을 거르지 않습니다.

### 요청 흐름

1. EC2 탭에서 허용 대상이 `stopped`면 [시작], `running`이면 [중지]만 활성화됩니다. `pending`·`stopping` 같은 전환 상태에서는 둘 다 비활성화됩니다.
2. 확인창에 계정·리전·이름·ID·작업이 표시됩니다. [취소]하면 아무 요청도 보내지 않습니다. [승인]하면 요청 키(UUID)를 만들어 보냅니다.
3. 서버는 요청 키 중복, 같은 인스턴스의 미확정 이력, 계정, 최신 EC2 상태를 확인한 뒤 이력에 `REQUESTED`를 기록하고, 그다음에 AWS를 **한 번만** 호출합니다. 자동 재시도, 완료 대기, 상태 폴링은 하지 않습니다.
4. 같은 요청 키를 다시 보내면 AWS를 다시 호출하지 않고 기존 결과를 돌려줍니다.

### 이력 상태

| 상태 | 의미 |
|---|---|
| `ACCEPTED` | AWS가 시작·중지 **요청을 접수**함 |
| `FAILED` | AWS가 명시적으로 거절했거나, 자격증명 문제로 요청이 전송되지 않음 |
| `UNKNOWN` | 타임아웃·연결 끊김 등으로 AWS 응답을 받지 못해 처리 여부를 모름 |
| `REQUESTED` | AWS 호출 전에 기록한 상태. 이 상태로 남았다면 결과를 이력에 반영하지 못한 것 |

**`ACCEPTED`는 완료가 아닙니다.** AWS가 요청을 받았다는 뜻일 뿐, 인스턴스는 그 뒤 `pending → running` 또는 `stopping → stopped`로 바뀌며 이 과정이 실패할 수도 있습니다. 화면에는 'AWS 요청 접수 — 현재 상태는 새로고침으로 확인'이 표시되며, 실제 상태는 [EC2·이력 새로고침]이나 AWS 콘솔로 확인합니다.

### UNKNOWN·REQUESTED 대응

이 두 상태가 남은 인스턴스는 **새 시작·중지가 409 `ACTION_IN_PROGRESS`로 차단**됩니다. 실제로는 처리됐는데 다시 요청하는 일을 막기 위해서입니다. 앱은 이 상태를 자동으로 고치지 않습니다.

1. 응답을 받지 못한 직후라면 화면의 [같은 요청 키로 다시 보내기]를 눌러 서버에 남은 결과를 확인합니다. 새 키로 다시 요청하지 않습니다.
2. AWS 콘솔이나 CLI에서 인스턴스의 실제 상태를 확인합니다.
   ```bash
   aws ec2 describe-instances --profile lab --region ap-northeast-2 --instance-ids <인스턴스 ID> \
     --query 'Reservations[].Instances[].State.Name'
   ```
3. 결과에 맞게 이력을 직접 정리합니다. 요청이 반영됐으면 `ACCEPTED`, 반영되지 않았으면 `FAILED`로 바꿉니다.
   ```sql
   UPDATE cloud_control.action_logs
   SET status = 'ACCEPTED', message = '콘솔에서 확인'
   WHERE request_id = '<요청 키>' AND status IN ('UNKNOWN', 'REQUESTED');
   ```

대시보드의 '결과 미확정 제어' 숫자는 최근 50건 이력 중 이 두 상태의 개수입니다. 그보다 오래된 미확정 이력은 숫자에 잡히지 않지만 서버는 여전히 차단합니다.

## 보안 그룹 점검

- **대상**: 상태가 pending·running·stopping·stopped인 EC2의 **모든 네트워크 인터페이스(ENI)에 실제로 연결된 보안 그룹**만 점검합니다. 어떤 EC2에도 연결되지 않은 그룹은 점검하지 않습니다(보안 그룹 탭에는 보입니다).
- **기준**: 인바운드 규칙 중 `0.0.0.0/0` 또는 `::/0`에서 TCP 22나 3389(포트 범위에 포함되는 경우와 전체 프로토콜 `-1` 포함)를 허용하는 규칙입니다.
- **건수**: 그룹의 규칙 × 공개 CIDR 조합당 1건입니다. 한 규칙이 22와 3389를 모두 포함해도 1건이고, 여러 인스턴스가 같은 그룹을 써도 늘지 않습니다. 표에서는 연결된 인스턴스·ENI마다 행을 펼쳐 보여 줍니다.
- [점검 실행]을 누를 때마다 AWS에서 새로 조회합니다. 결과는 저장하지 않습니다.
- **규칙 기반 점검이며 실제 침해 여부 판정이 아닙니다.** 네트워크 ACL, 다른 보안 그룹·prefix list를 출처로 한 규칙, 22·3389 이외 포트는 판단하지 않습니다. 자동 수정도 하지 않습니다.

### 결과 구분

| 화면 표시 | 의미 |
|---|---|
| 공개 관리 포트 규칙: 검토 필요 (n건) | 연결 그룹을 모두 점검했고 기준에 맞는 규칙을 찾음 |
| 이번 점검 조건에 해당하는 규칙 없음 | 연결 그룹이 1개 이상 있고 모두 점검했으며, 기준에 맞는 규칙이 없음 |
| 점검 대상 EC2 연결 보안 그룹 없음 | 점검할 EC2가 없거나 연결된 그룹이 0개라 점검할 대상 자체가 없음 |
| 점검 실패: [코드] … | 조회가 하나라도 실패함. **'규칙 없음'으로 해석하면 안 됨** |

점검 실패 코드: `AWS_ACCESS_DENIED`(403, 권한 부족), `AWS_LOGIN_REQUIRED`(503, AWS 로그인 만료), `AWS_CONNECTION_ERROR`(503, 통신 실패), `AWS_SCAN_INCOMPLETE`(503, 조회 중 그룹이 삭제됐거나 ENI·그룹 정보가 누락됨). 실패하면 이전 결과도 지우고 오류만 보여 줍니다.

## 화면 구성

| 탭 | 내용 |
|---|---|
| 대시보드 | 계정·리전·호출자 ARN, EC2(전체·running·stopped)·S3·보안 그룹·결과 미확정 제어 수. 조회에 실패한 항목은 '확인 불가' |
| EC2 | 인스턴스 목록, 상태 필터(이미 조회한 목록만 거름), 최근 제어 결과, 시작·중지 |
| S3 | 계정의 버킷 이름 |
| 보안 그룹 | 서울 리전 보안 그룹의 인바운드 규칙 |
| 실행 이력 | MariaDB `action_logs` 최신 50건 |
| 보안 점검 | 위 점검 결과 |

## 오류 코드

모든 오류 본문은 `{"detail": {"code": "...", "message": "..."}}` 형식입니다.

| 코드 | HTTP | 의미 |
|---|---|---|
| `APP_AUTH_REQUIRED` | 401 | 앱 토큰 누락·불일치 |
| `AWS_LOGIN_REQUIRED` | 503 | AWS 임시 자격증명 만료·갱신 불가 → `aws login --profile lab` |
| `AWS_ACCESS_DENIED` | 403 | AWS 권한 부족 |
| `AWS_ACCOUNT_MISMATCH` | 403 | 로그인한 계정 ≠ `EXPECTED_ACCOUNT_ID` |
| `AWS_CONNECTION_ERROR` | 503 | AWS 통신 실패 |
| `AWS_SCAN_INCOMPLETE` | 503 | 보안 점검 대상 불일치·정보 누락 |
| `INSTANCE_NOT_ALLOWED` | 403 | 허용 목록 밖 인스턴스 |
| `INSTANCE_CONTROL_DISABLED` | 403 | 허용 목록이 비어 있음 |
| `INVALID_INSTANCE_STATE` | 409 | start는 stopped, stop은 running에서만 가능 |
| `ACTION_IN_PROGRESS` | 409 | 같은 인스턴스에 REQUESTED·UNKNOWN 이력이 있음 |
| `IDEMPOTENCY_CONFLICT` | 409 | 같은 요청 키를 다른 대상·작업에 사용 |
| `AWS_RESULT_UNKNOWN` | 503 | AWS 응답을 받지 못함(UNKNOWN 기록) |
| `DB_UNAVAILABLE` | 503 | DB 실패. AWS 변경 요청은 보내지 않음 |
| `DB_UPDATE_FAILED` | 503 | AWS 호출 후 결과 기록 실패. 이력이 REQUESTED로 남음 |
| `VALIDATION_ERROR` | 422 | 요청 형식 오류(추가 필드, 잘못된 UUID 등) |

## API

모든 `/api` 경로에 `Authorization: Bearer <앱 토큰>`이 필요합니다.

| 메서드 · 경로 | 응답 |
|---|---|
| `GET /api/context` | `account_id`, `caller_arn`, `region` |
| `GET /api/ec2` | `items[{instance_id, name, state, instance_type, private_ip, public_ip, controllable}]`, `count` |
| `GET /api/s3` | `items[{name}]`, `count` |
| `GET /api/security-groups` | `items[{GroupId, GroupName, IpPermissions}]`, `count` |
| `GET /api/actions` | 최신 50건 `items`, `count` |
| `POST /api/ec2/{instance_id}/actions` | 본문 `{action: "start"\|"stop", request_id: UUID}`. 최초 접수 202, 같은 요청 재전송 200 |
| `GET /api/security-checks` | `findings`, `checked_groups`, `checked_instance_count`, `checked_group_count`, `finding_count`, `checked_at` |

터미널에서 확인할 때는 `.venv/bin/python check_api.py /api/context`처럼 실행합니다. 토큰은 화면에 표시되지 않게 입력받습니다.

## 검증 현황

`PLAN.md`의 원칙대로, 실제 환경에서 확인한 것과 아닌 것을 나눕니다. 직접 확인할 때마다 이 표를 갱신합니다.

### 실제 환경에서 직접 확인함

| 단계 | 내용 |
|---|---|
| A | WSL에서 `aws login --profile lab`, CLI 조회 |
| B | 조회 API(`/api/context`, `/api/ec2`, `/api/s3`, `/api/security-groups`)와 웹 화면 |

### 단위 테스트·모의 응답으로만 확인함 (실제 AWS·MariaDB 미검증)

| 단계 | 확인한 범위 |
|---|---|
| C | 토큰 누락·잘못된 scheme·불일치 → 401, AWS 호출 없음, 허용 목록 파싱, 정적 화면 무인증 |
| D | `tests/test_actions.py` 17개: 401·허용 밖 ID·DB 실패 시 변경 호출 0회, 같은 키 재전송 시 누적 1회, 타임아웃 → UNKNOWN 및 추가 호출 0회, REQUESTED·UNKNOWN 차단, 422, 409, 403 |
| D | `/api/actions` 응답 형식과 최신 50건 정렬(메모리 SQLite), `init_db` 실패 메시지에 암호 미포함 |
| E | 모의 EC2 응답으로 연결 그룹 수집·중복 제거·100개 묶음 조회·대상 불일치 처리·건수 계산 |
| F | 화면 JS 문법 검사와 요소 ID 대조 |

### 아직 확인하지 않음

- 실제 MariaDB에서 `init_db`, 이력 저장·조회
- 실제 EC2 시작·중지와 `ACCEPTED` 기록, 새로고침 후 상태 변화
- 확인창 취소 시 이력이 생기지 않는지, 응답 유실 시 같은 키 재전송 화면
- 실제 보안 그룹 점검 결과와 AWS 콘솔·CLI 대조
- Windows 브라우저에서 C~F 화면 전체 동작
- AWS 로그인 만료 시 안내 배너

## 콘솔·CLI 대조 방법

앱 화면의 값과 아래 읽기 전용 명령의 결과가 같아야 합니다.

```bash
P="--profile lab --region ap-northeast-2"

# 대시보드: 계정·호출자 ARN
aws sts get-caller-identity --profile lab

# EC2 목록과 수(전체/running/stopped)
aws ec2 describe-instances $P --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType,PrivateIpAddress,PublicIpAddress,Tags[?Key==`Name`]|[0].Value]' --output table
aws ec2 describe-instances $P --query 'length(Reservations[].Instances[])'
aws ec2 describe-instances $P --filters Name=instance-state-name,Values=running --query 'length(Reservations[].Instances[])'
aws ec2 describe-instances $P --filters Name=instance-state-name,Values=stopped --query 'length(Reservations[].Instances[])'

# S3 버킷 수, 보안 그룹 수
aws s3api list-buckets --profile lab --query 'length(Buckets)'
aws ec2 describe-security-groups $P --query 'length(SecurityGroups)'

# 보안 점검: checked_instance_count
aws ec2 describe-instances $P --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'length(Reservations[].Instances[])'

# 보안 점검: checked_group_count와 그룹 ID
aws ec2 describe-instances $P --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].NetworkInterfaces[].Groups[].GroupId' --output text | tr '\t' '\n' | sort -u

# 보안 점검: 각 그룹의 공개 인바운드 규칙
aws ec2 describe-security-groups $P --group-ids <그룹 ID> \
  --query 'SecurityGroups[].IpPermissions[?contains(IpRanges[].CidrIp, `0.0.0.0/0`) || contains(Ipv6Ranges[].CidrIpv6, `::/0`)]'
```

실행 이력은 MariaDB에서 대조합니다.

```sql
SELECT status, COUNT(*) FROM cloud_control.action_logs GROUP BY status;
```

## 테스트

실제 AWS·MariaDB·`.env`를 사용하지 않습니다.

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## 파일 구조

```
backend/
  config.py          런타임 설정(.env는 서버 실행 시에만 읽음)
  auth.py            앱 토큰 인증, EC2 제어 허용 목록 검사
  aws_service.py     Boto3 조회, AWS 오류 분류, 연결 보안 그룹 점검
  security_check.py  inspect_group: 공개 관리 포트 규칙 판정
  actions.py         EC2 시작·중지(전역 lock, 중복 방지, 결과 기록)
  action_store.py    action_logs 저장 함수
  db.py, models.py   SQLAlchemy 엔진·세션, action_logs 모델
  init_db.py         없는 테이블만 생성
  main.py            FastAPI 경로(API 먼저, frontend 정적 파일은 마지막)
frontend/            index.html, app.js, style.css
tests/test_actions.py
check_api.py         터미널용 API 확인 도구
```
