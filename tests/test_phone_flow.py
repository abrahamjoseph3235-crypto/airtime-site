import io
import os
import sys
import tempfile
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate tests from live integrations and the user's normal database.
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["VERIFIER"] = "mock"
os.environ["AIRTIME_PROVIDER"] = "mock"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["ADMIN_REVIEW_PASSWORD"] = "test-review-password"
os.environ["AIRTIME_VTUAGENT_ALLOW_REQUESTS"] = "false"

from app import app, db, EvidenceSubmission, RewardClaim, limiter

TEST_UPLOAD_DIR = tempfile.mkdtemp(prefix="airtime-evidence-tests-")
app.config["EVIDENCE_FOLDER"] = TEST_UPLOAD_DIR
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
app.config["RATELIMIT_ENABLED"] = False
limiter.enabled = False

# Small signature fixtures for route tests; production uploads are restricted by
# extension, signature, request size, and a per-file size limit.
_png_buffer = io.BytesIO()
Image.new("RGB", (1, 1), (255, 255, 255)).save(_png_buffer, format="PNG")
PNG_FIXTURE = _png_buffer.getvalue()


def setup_module():
    with app.app_context():
        db.drop_all()
        db.create_all()


def setup_function(_function):
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()


def teardown_module():
    with app.app_context():
        db.session.remove()
        db.drop_all()


def start_phone_flow(client, phone="08030000001"):
    response = client.post("/", data={"phone": phone})
    assert response.status_code == 302
    return client.get("/form")


def confirm_submission(client):
    response = client.post("/confirm-submission", data={"confirm_read": "yes"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/evidence")


def upload_evidence(client, phone="08030000001"):
    return client.post(
        "/evidence",
        data={
            "network": "MTN",
            "referral_screenshot": (io.BytesIO(PNG_FIXTURE), "referrer.png"),
            "confirmation_screenshot": (io.BytesIO(PNG_FIXTURE), "confirmation.png"),
        },
        content_type="multipart/form-data",
    )


def test_health_check_returns_ok():
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.data == b"ok"


def test_phone_is_entered_once_and_prefilled_into_form():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b'name="phone"' in response.data
    assert b'name="email"' not in response.data

    response = start_phone_flow(client)
    assert response.status_code == 200
    assert b"entry.551363144=08030000001" in response.data
    # Referrer is intentionally not prefilled: the user must select it.
    assert b"entry.1296999881=Kelechi Ndubueze" not in response.data
    assert b"selected referrer" in response.data.lower()


def test_upload_page_is_locked_until_user_confirms_submission():
    client = app.test_client()
    start_phone_flow(client)
    response = client.get("/evidence")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/form")

    response = client.post("/confirm-submission", data={"confirm_read": "yes"})
    assert response.status_code == 302
    assert client.get("/evidence").status_code == 200


def test_two_required_screenshots_create_pending_manual_review():
    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    response = upload_evidence(client)
    assert response.status_code == 200
    assert b"Evidence submitted" in response.data
    assert b"Pending" in response.data

    with app.app_context():
        record = EvidenceSubmission.query.filter_by(phone="08030000001").one()
        assert record.status == "pending"
        assert record.network == "MTN"
        assert os.path.exists(os.path.join(TEST_UPLOAD_DIR, record.phone_image))
        assert os.path.exists(os.path.join(TEST_UPLOAD_DIR, record.referral_image))
        assert os.path.exists(os.path.join(TEST_UPLOAD_DIR, record.confirmation_image))
        assert RewardClaim.query.count() == 0


def test_missing_screenshot_is_rejected():
    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    response = client.post(
        "/evidence",
        data={
            "network": "MTN",
            "phone_screenshot": (io.BytesIO(PNG_FIXTURE), "phone.png"),
            "referral_screenshot": (io.BytesIO(PNG_FIXTURE), "referrer.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert b"confirmation evidence" in response.data.lower()


def test_non_image_file_is_rejected():
    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    response = client.post(
        "/evidence",
        data={
            "network": "MTN",
            "referral_screenshot": (io.BytesIO(b"not-an-image"), "referrer.png"),
            "confirmation_screenshot": (io.BytesIO(PNG_FIXTURE), "confirmation.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_duplicate_phone_cannot_create_second_submission():
    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    assert upload_evidence(client).status_code == 200

    second_client = app.test_client()
    start_phone_flow(second_client)
    confirm_submission(second_client)
    response = upload_evidence(second_client)
    assert response.status_code == 200
    with app.app_context():
        assert EvidenceSubmission.query.filter_by(phone="08030000001").count() == 1


def test_private_review_requires_login_and_approval_is_manual_only():
    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    upload_response = upload_evidence(client)
    assert upload_response.status_code == 200, upload_response.data.decode("utf-8", errors="replace")

    assert client.get("/review").status_code == 200
    response = client.post("/review", data={"password": "wrong"})
    assert response.status_code == 401
    response = client.post("/review", data={"password": "test-review-password"})
    assert response.status_code == 302
    response = client.get("/review")
    assert response.status_code == 200
    assert b"Unique phone numbers submitted" in response.data
    assert b"Eligible phone numbers" in response.data
    assert b"Awaiting review" in response.data
    assert b"Download phone screenshot" not in response.data
    assert b"Eligible phone numbers</p><p class=\"stat-number\">0" in response.data

    with app.app_context():
        record = EvidenceSubmission.query.filter_by(phone="08030000001").one()
        submission_id = record.id
    response = client.post(
        f"/review/{submission_id}/approve",
        data={"reviewer_note": "Evidence checked"},
    )
    assert response.status_code == 302
    with app.app_context():
        record = db.session.get(EvidenceSubmission, submission_id)
        assert record.status == "approved"
        assert record.reviewer_note == "Evidence checked"
        assert RewardClaim.query.count() == 0

    dashboard_after_approval = client.get("/review")
    assert b"Eligible phone numbers</p><p class=\"stat-number\">1" in dashboard_after_approval.data

    response = client.post(
        f"/review/{submission_id}/mark-sent",
        data={"reviewer_note": "Manually sent outside the website"},
    )
    assert response.status_code == 302
    with app.app_context():
        record = db.session.get(EvidenceSubmission, submission_id)
        assert record.status == "reward_sent"
        assert record.reviewer_note == "Manually sent outside the website"
        assert RewardClaim.query.count() == 0


def test_legacy_claim_history_blocks_repeat_evidence():
    from sqlalchemy import text

    with app.app_context():
        db.session.execute(text("CREATE TABLE claim (id INTEGER PRIMARY KEY, phone VARCHAR(11) NOT NULL)"))
        db.session.execute(
            text("INSERT INTO claim (phone) VALUES (:phone)"),
            {"phone": "08030000001"},
        )
        db.session.commit()

    client = app.test_client()
    start_phone_flow(client)
    confirm_submission(client)
    response = client.get("/evidence")
    assert response.status_code == 409
    assert b"already used" in response.data.lower()

    with app.app_context():
        db.session.execute(text("DROP TABLE claim"))
        db.session.commit()


def test_review_evidence_files_are_not_public():
    client = app.test_client()
    response = client.get("/review/1/image/phone")
    assert response.status_code == 403


def test_automated_claim_route_is_disabled():
    client = app.test_client()
    response = client.get("/claim")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
