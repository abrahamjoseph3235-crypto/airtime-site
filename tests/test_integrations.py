import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers import (
    AirtimeResult, GoogleFormsVerifier, MockAirtime, MockVerifier,
    VtuAgentAirtime, normalize_nigerian_phone, verify_vtuagent_webhook,
)


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode()
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self._body


class FakeRequest:
    def __init__(self, body):
        self.body = body
    def execute(self):
        return self.body


class FakeGoogleFormsResource:
    def __init__(self, form, responses):
        self.form = form
        self.response_pages = responses
    def get(self, **kwargs):
        return FakeRequest(self.form)
    def responses(self):
        return self
    def list(self, **kwargs):
        return FakeRequest(self.response_pages.pop(0))


class FakeGoogleService:
    def __init__(self, form, responses):
        self.resource = FakeGoogleFormsResource(form, responses)
    def forms(self):
        return self.resource


def make_fake_google_verifier(responses):
    form = {"items": [
        {"title": "Respondent Phone Number",
         "questionItem": {"question": {"questionId": "phone-question"}}},
        {"title": "Referral",
         "questionItem": {"question": {"questionId": "referral-question"}}},
    ]}
    verifier = GoogleFormsVerifier.__new__(GoogleFormsVerifier)
    verifier._service = FakeGoogleService(form, responses)
    verifier._form_id = "test-form"
    verifier._phone_title = "Respondent Phone Number"
    verifier._referral_title = "Referral"
    verifier._question_ids = None
    return verifier


def test_phone_normalization():
    assert normalize_nigerian_phone(" +234 803 000 0001 ") == "08030000001"
    assert normalize_nigerian_phone("0803-000-0001") == "08030000001"
    assert normalize_nigerian_phone("123") is None


def test_mock_verifier_requires_phone_and_full_referral_name():
    verifier = MockVerifier()
    assert verifier.find_eligible_response("08030000001") == "mock-eligible-1"
    assert verifier.find_eligible_response("08030000002") is None
    assert verifier.find_eligible_response("08030000003") is None


def test_google_verifier_matches_phone_and_referral():
    verifier = make_fake_google_verifier([{
        "responses": [{
            "responseId": "eligible-response",
            "answers": {
                "phone-question": {"textAnswers": {"answers": [{"value": "+2348030000001"}]}},
                "referral-question": {"textAnswers": {"answers": [{"value": "  Kelechi Ndubueze "}]}}
            }
        }]
    }])
    assert verifier.find_eligible_response("08030000001") == "eligible-response"


def test_google_verifier_rejects_wrong_referral():
    verifier = make_fake_google_verifier([{
        "responses": [{
            "responseId": "wrong-referral",
            "answers": {
                "phone-question": {"textAnswers": {"answers": [{"value": "08030000001"}]}},
                "referral-question": {"textAnswers": {"answers": [{"value": "Someone Else"}]}}
            }
        }]
    }])
    assert verifier.find_eligible_response("08030000001") is None


def test_google_verifier_paginates_responses():
    verifier = make_fake_google_verifier([
        {"responses": [], "nextPageToken": "next"},
        {"responses": [{
            "responseId": "page-two",
            "answers": {
                "phone-question": {"textAnswers": {"answers": [{"value": "08030000001"}]}},
                "referral-question": {"textAnswers": {"answers": [{"value": "Kelechi Ndubueze"}]}}
            }
        }]}
    ])
    assert verifier.find_eligible_response("08030000001") == "page-two"


def test_mock_airtime_is_idempotent():
    provider = MockAirtime()
    first = provider.send("08031234567", "MTN", 10000, "same-ref")
    second = provider.send("08031234567", "MTN", 10000, "same-ref")
    assert first == second
    assert first.status == "successful"


def test_vtuagent_disabled_by_default_never_calls_network():
    called = []
    provider = VtuAgentAirtime(api_key="dummy", allow_requests=False,
                               opener=lambda *a, **k: called.append(True))
    try:
        provider.send("08031234567", "MTN", 10000, "test-ref")
    except RuntimeError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("Expected disabled provider to refuse request")
    assert called == []


def test_vtuagent_purchase_payload_and_success_mapping():
    captured = {}
    def fake_open(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["payload"] = json.loads(req.data.decode())
        return FakeResponse({"status": "successful", "reference": "test-ref"}, 200)
    provider = VtuAgentAirtime(api_key="test-key", base_url="https://sandbox.invalid",
                               allow_requests=True, opener=fake_open)
    result = provider.send("08031234567", "MTN", 12550, "test-ref")
    assert result == AirtimeResult("successful", "test-ref")
    assert captured["url"] == "https://sandbox.invalid/v1/airtime/purchase"
    assert captured["payload"] == {
        "network": "mtn", "phone": "08031234567", "amount": "125.5", "request_ref": "test-ref"
    }
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_vtuagent_pending_is_not_treated_as_success():
    captured = []
    def fake_open(req, timeout):
        captured.append(req.full_url)
        return FakeResponse({"status": "pending", "reference": "ref-2"}, 202)
    provider = VtuAgentAirtime(api_key="test-key", base_url="https://sandbox.invalid",
                               allow_requests=True, opener=fake_open)
    assert provider.send("08031234567", "Airtel", 10000, "ref-2") == AirtimeResult("unknown", "ref-2")
    assert provider.status("ref-2") == AirtimeResult("unknown", "ref-2")
    assert captured[-1].endswith("/v1/transaction/status")


def test_webhook_signature_check():
    key, reference = "unit-test-key", "claim-123"
    payload = {"reference": reference,
               "hash": hashlib.sha256(f"{key}:{reference}".encode()).hexdigest()}
    assert verify_vtuagent_webhook(payload, key)
    payload["hash"] = "0" * 64
    assert not verify_vtuagent_webhook(payload, key)


def test_unknown_network_is_rejected():
    provider = VtuAgentAirtime(api_key="test-key", allow_requests=True, opener=lambda *a, **k: None)
    try:
        provider.send("08031234567", "UnknownNet", 10000, "ref")
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown network should be rejected")
