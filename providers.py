"""Google Forms eligibility verification and safe airtime provider adapters."""
import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def normalize_nigerian_phone(raw):
    value = re.sub(r"[\s\-()]", "", raw or "")
    value = re.sub(r"^(\+?234)", "0", value)
    return value if re.fullmatch(r"0[789][01]\d{8}", value) else None


class MockVerifier:
    """Local-only fake form records. No Google API requests are made."""
    def __init__(self, path=None):
        source = Path(path) if path else Path(__file__).resolve().with_name("mock_responses.json")
        with source.open(encoding="utf-8") as handle:
            self._responses = json.load(handle)

    def find_eligible_response(self, phone):
        wanted = normalize_nigerian_phone(phone)
        if not wanted:
            return None
        for response in self._responses:
            response_phone = normalize_nigerian_phone(response.get("phone", ""))
            referral = str(response.get("referral", "")).strip()
            if response_phone == wanted and referral == "Kelechi Ndubueze":
                return response.get("responseId")
        return None


class GoogleFormsVerifier:
    """Read-only verification of submitted phone and referral answers."""
    SCOPES = [
        "https://www.googleapis.com/auth/forms.body.readonly",
        "https://www.googleapis.com/auth/forms.responses.readonly",
    ]

    def __init__(self, form_id, key_file,
                 phone_title="Respondent Phone Number", referral_title="Referral"):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            key_file, scopes=self.SCOPES
        )
        self._service = build("forms", "v1", credentials=credentials, cache_discovery=False)
        self._form_id = form_id
        self._phone_title = phone_title
        self._referral_title = referral_title
        self._question_ids = None

    def _get_question_ids(self):
        if self._question_ids is not None:
            return self._question_ids

        form = self._service.forms().get(formId=self._form_id).execute()
        found = {}
        for item in form.get("items", []):
            title = str(item.get("title", "")).strip().casefold()
            question = item.get("questionItem", {}).get("question", {})
            question_id = question.get("questionId")
            if title and question_id:
                found[title] = question_id
            # Also handle question groups, if the form is later reorganized.
            group = item.get("questionGroupItem", {})
            for grouped in group.get("questions", []):
                grouped_id = grouped.get("questionId")
                grouped_title = str(grouped.get("title", item.get("title", ""))).strip().casefold()
                if grouped_id and grouped_title:
                    found[grouped_title] = grouped_id

        phone_id = found.get(self._phone_title.strip().casefold())
        referral_id = found.get(self._referral_title.strip().casefold())
        if not phone_id or not referral_id:
            raise RuntimeError(
                "Google Form is missing the expected phone or referral question. Check question titles."
            )
        self._question_ids = {"phone": phone_id, "referral": referral_id}
        return self._question_ids

    @staticmethod
    def _answer_value(response, question_id):
        answer = response.get("answers", {}).get(question_id, {})
        text_answers = answer.get("textAnswers", {}).get("answers", [])
        if not text_answers:
            return None
        value = text_answers[0].get("value")
        return value.strip() if isinstance(value, str) else None

    def find_eligible_response(self, phone):
        wanted_phone = normalize_nigerian_phone(phone)
        if not wanted_phone:
            return None
        question_ids = self._get_question_ids()
        token = None
        while True:
            page = self._service.forms().responses().list(
                formId=self._form_id, pageSize=500, pageToken=token
            ).execute()
            for response in page.get("responses", []):
                form_phone = normalize_nigerian_phone(
                    self._answer_value(response, question_ids["phone"]) or ""
                )
                referral = (self._answer_value(response, question_ids["referral"]) or "").strip()
                if form_phone == wanted_phone and referral == "Kelechi Ndubueze":
                    response_id = response.get("responseId")
                    if response_id:
                        return response_id
            token = page.get("nextPageToken")
            if not token:
                return None


def get_verifier():
    mode = os.getenv("VERIFIER", "mock").strip().lower()
    if mode == "google":
        form_id = os.getenv("GOOGLE_FORM_ID", "").strip()
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json").strip()
        key_path = Path(key_file) if key_file else Path()
        if key_file and not key_path.is_absolute():
            key_path = Path(__file__).resolve().parent / key_path
        if not form_id or not key_file or not key_path.is_file():
            raise RuntimeError(
                "Google verification requires GOOGLE_FORM_ID and an accessible service-account JSON file."
            )
        return GoogleFormsVerifier(form_id, str(key_path.resolve()))
    if mode == "mock":
        return MockVerifier()
    raise RuntimeError("Unknown VERIFIER. Use mock for local tests or google for authorized form lookup.")


@dataclass
class AirtimeResult:
    status: str  # successful | failed | unknown
    reference: str | None = None


class MockAirtime:
    """Deterministic local test adapter; it never calls a real provider."""
    def __init__(self):
        self._seen = {}

    def send(self, phone, network, amount_kobo, idempotency_key):
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]
        if phone.endswith("0000"):
            result = AirtimeResult("failed")
        elif phone.endswith("9999"):
            raise TimeoutError("simulated provider timeout")
        else:
            result = AirtimeResult("successful", "MOCK-" + uuid.uuid4().hex[:10])
        self._seen[idempotency_key] = result
        return result

    def status(self, idempotency_key):
        return self._seen.get(idempotency_key, AirtimeResult("unknown"))


class VtuAgentAirtime:
    """Preliminary VTUAgent adapter. Network access is disabled by default."""
    NETWORK_MAP = {
        "MTN": "mtn", "Airtel": "airtel", "Glo": "glo", "9mobile": "etisalat",
        "mtn": "mtn", "airtel": "airtel", "glo": "glo", "9mobile": "etisalat",
    }

    def __init__(self, api_key=None, base_url=None, allow_requests=None, opener=None, timeout=8):
        self.api_key = api_key if api_key is not None else os.getenv("VTUAGENT_API_KEY", "")
        self.base_url = (base_url if base_url is not None else os.getenv(
            "AIRTIME_VTUAGENT_BASE_URL", "https://api.vtuagent.com"
        )).rstrip("/")
        if allow_requests is None:
            allow_requests = os.getenv("AIRTIME_VTUAGENT_ALLOW_REQUESTS", "false").lower() == "true"
        self.allow_requests = bool(allow_requests)
        self.opener = opener or urlopen
        self.timeout = timeout

    def _post(self, path, payload):
        if not self.allow_requests:
            raise RuntimeError("VTUAgent requests are disabled.")
        if not self.api_key:
            raise RuntimeError("VTUAGENT_API_KEY is not configured.")
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                http_status = response.status
        except HTTPError as exc:
            raw, http_status = exc.read(), exc.code
            if http_status not in (202, 424):
                raise RuntimeError(f"VTUAgent returned HTTP {http_status}.") from None
        except (TimeoutError, URLError):
            raise TimeoutError("VTUAgent outcome is uncertain; reconcile status before retrying.") from None
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("VTUAgent returned an unreadable response.") from None
        return http_status, body

    @staticmethod
    def _result(http_status, body):
        state = str(body.get("status", "")).lower()
        if http_status == 202 or state == "pending":
            return AirtimeResult("unknown", body.get("reference"))
        if http_status == 424 or state in ("failed", "error"):
            return AirtimeResult("failed", body.get("reference"))
        if http_status == 200 and state == "successful":
            return AirtimeResult("successful", body.get("reference"))
        return AirtimeResult("unknown", body.get("reference"))

    def send(self, phone, network, amount_kobo, idempotency_key):
        if network not in self.NETWORK_MAP:
            raise ValueError("Unsupported mobile network.")
        if not phone or not idempotency_key or len(idempotency_key) > 50:
            raise ValueError("A valid phone and unique request reference are required.")
        payload = {
            "network": self.NETWORK_MAP[network],
            "phone": phone,
            "amount": f"{amount_kobo / 100:.2f}".rstrip("0").rstrip("."),
            "request_ref": idempotency_key,
        }
        http_status, body = self._post("/v1/airtime/purchase", payload)
        return self._result(http_status, body)

    def status(self, idempotency_key):
        if not idempotency_key:
            raise ValueError("A request reference is required.")
        http_status, body = self._post(
            "/v1/transaction/status", {"request_ref": idempotency_key}
        )
        return self._result(http_status, body)


def verify_vtuagent_webhook(payload, api_key):
    """Preliminary helper; confirm the exact signature scheme with VTUAgent before use."""
    reference = payload.get("reference")
    supplied = payload.get("hash")
    if not isinstance(reference, str) or not isinstance(supplied, str) or not api_key:
        return False
    expected = hashlib.sha256(f"{api_key}:{reference}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, supplied)


def get_airtime_provider():
    provider = os.getenv("AIRTIME_PROVIDER", "mock").lower()
    if provider == "vtuagent":
        return VtuAgentAirtime()
    if provider == "mock":
        return MockAirtime()
    raise RuntimeError("Unknown AIRTIME_PROVIDER. Use mock or vtuagent.")
