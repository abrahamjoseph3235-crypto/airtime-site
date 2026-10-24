"""Phone-first Airtime Reward site with server-side Google Form eligibility checks."""
import io
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
import mimetypes
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from PIL import Image, UnidentifiedImageError


load_dotenv()
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PROD = APP_ENV == "production"
IS_STAGING = APP_ENV == "staging"
SECRET = os.getenv("SECRET_KEY")
if (IS_PROD or IS_STAGING) and not SECRET:
    raise RuntimeError("SECRET_KEY must be set in staging and production")
if (IS_PROD or IS_STAGING) and not os.getenv("ADMIN_REVIEW_PASSWORD", "").strip():
    raise RuntimeError("ADMIN_REVIEW_PASSWORD must be set in staging and production to protect the private review dashboard.")
SECRET = SECRET or secrets.token_hex(32)

db_url = os.getenv("DATABASE_URL", "sqlite:///dev.db")
# Use psycopg 3 explicitly for all PostgreSQL URLs; it is the driver installed by requirements.txt.
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
if (IS_PROD or IS_STAGING) and not db_url.startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("Staging and production require a managed PostgreSQL DATABASE_URL; local SQLite is not durable.")
EVIDENCE_STORAGE = os.getenv("EVIDENCE_STORAGE", "local").strip().lower()
CF_ACCESS_TEAM_DOMAIN = os.getenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").strip("/")
CF_ACCESS_AUD = os.getenv("CLOUDFLARE_ACCESS_AUD", "").strip()
if (IS_PROD or IS_STAGING) and EVIDENCE_STORAGE != "r2":
    raise RuntimeError("Staging and production require EVIDENCE_STORAGE=r2 for durable screenshot storage.")
if (IS_PROD or IS_STAGING) and not all(os.getenv(name, "").strip() for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")):
    raise RuntimeError("Staging and production require R2 account, access key, secret key, and bucket settings.")
if IS_PROD and (not CF_ACCESS_TEAM_DOMAIN or not CF_ACCESS_AUD):
    raise RuntimeError("Production requires CLOUDFLARE_ACCESS_TEAM_DOMAIN and CLOUDFLARE_ACCESS_AUD to protect reviewer routes.")
if EVIDENCE_STORAGE not in ("local", "r2"):
    raise RuntimeError("EVIDENCE_STORAGE must be 'local' or 'r2'.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET,
    SQLALCHEMY_DATABASE_URI=db_url,
    SESSION_COOKIE_SECURE=(IS_PROD or IS_STAGING),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,  # Three screenshots, maximum 12 MiB total
    EVIDENCE_FOLDER=os.path.join(app.instance_path, "evidence"),
)
if db_url.startswith(("postgresql://", "postgresql+psycopg://")):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address, app=app, default_limits=["100 per hour"],
    storage_uri=os.getenv("REDIS_URL") or "memory://"
)
log = logging.getLogger("airtime")
logging.basicConfig(level=logging.INFO)

AMOUNT_KOBO = int(float(os.getenv("AIRTIME_AMOUNT_NAIRA", "100")) * 100)
NETWORKS = ["MTN", "Airtel", "Glo", "9mobile"]
EXPECTED_REFERRAL = "Kelechi Ndubueze"
MIN_FORM_TIME_SECONDS = 90
FORM_VIEW_URL = os.getenv(
    "GOOGLE_FORM_VIEW_URL",
    "https://docs.google.com/forms/d/e/1FAIpQLSemqQxGTKy6PRR5SA3UXAdlk8a0gi25oXUAUhENRTsPlMSpMw/viewform"
).strip()
PHONE_ENTRY_ID = os.getenv("GOOGLE_FORM_PHONE_ENTRY_ID", "551363144").strip()
REFERRAL_ENTRY_ID = os.getenv("GOOGLE_FORM_REFERRAL_ENTRY_ID", "1296999881").strip()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def norm_phone(raw):
    """Normalize Nigerian mobile numbers to 080XXXXXXXX format."""
    d = re.sub(r"[\s\-()]", "", raw or "")
    d = re.sub(r"^(\+?234)", "0", d)
    return d if re.fullmatch(r"0[789][01]\d{8}", d) else None


def has_legacy_claim(phone):
    """Keep pre-update claim history as a duplicate guard without altering old tables."""
    if "claim" not in set(inspect(db.engine).get_table_names()):
        return False
    return db.session.execute(
        text("SELECT 1 FROM claim WHERE phone = :phone LIMIT 1"), {"phone": phone}
    ).first() is not None


class RewardParticipant(db.Model):
    __tablename__ = "reward_participants"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(11), unique=True, nullable=False)
    google_response_id = db.Column(db.String(128), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    claim = db.relationship("RewardClaim", uselist=False, backref="participant")


class RewardClaim(db.Model):
    __tablename__ = "reward_claims"
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(
        db.Integer, db.ForeignKey("reward_participants.id"), unique=True, nullable=False
    )
    # Unique constraint prevents a phone number from receiving more than one reward.
    phone = db.Column(db.String(11), unique=True, nullable=False)
    network = db.Column(db.String(10), nullable=False)
    amount_kobo = db.Column(db.BigInteger, nullable=False)
    status = db.Column(db.String(12), default="processing", nullable=False)
    idempotency_key = db.Column(db.String(36), unique=True, nullable=False)
    provider_reference = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class EvidenceSubmission(db.Model):
    __tablename__ = "evidence_submissions"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(11), unique=True, nullable=False, index=True)
    network = db.Column(db.String(10), nullable=False)
    # Legacy compatibility: stores the referral image filename when no phone screenshot is required.
    phone_image = db.Column(db.String(255), nullable=False)
    referral_image = db.Column(db.String(255), nullable=False)
    confirmation_image = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    reviewer_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    reviewed_at = db.Column(db.DateTime)


def _r2_client():
    """Create an S3-compatible Cloudflare R2 client only when R2 storage is selected."""
    import boto3
    account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
    access_key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.getenv("R2_BUCKET_NAME", "").strip()
    if not all((account_id, access_key, secret_key, bucket)):
        raise RuntimeError("R2 storage requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME.")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def _save_evidence(upload, filename, ext):
    """Store evidence locally for development or in private R2 for production."""
    if EVIDENCE_STORAGE == "r2":
        upload.stream.seek(0)
        _r2_client().put_object(
            Bucket=os.environ["R2_BUCKET_NAME"],
            Key=filename,
            Body=upload.stream,
            ContentType={".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[ext],
        )
    else:
        os.makedirs(app.config["EVIDENCE_FOLDER"], exist_ok=True)
        upload.save(os.path.join(app.config["EVIDENCE_FOLDER"], filename))
    return filename


def _delete_evidence(filename):
    if EVIDENCE_STORAGE == "r2":
        _r2_client().delete_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=filename)
    else:
        try:
            os.remove(os.path.join(app.config["EVIDENCE_FOLDER"], filename))
        except OSError:
            pass


def _read_evidence(filename):
    if EVIDENCE_STORAGE == "r2":
        response = _r2_client().get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=filename)
        return response["Body"].read()
    with open(os.path.join(app.config["EVIDENCE_FOLDER"], filename), "rb") as handle:
        return handle.read()


def _verify_cloudflare_access():
    """Validate Cloudflare Access when configured; reviewer login still requires its password."""
    if not CF_ACCESS_TEAM_DOMAIN and not CF_ACCESS_AUD:
        # Staging may initially use the app's strong password login while Access is configured.
        # Production startup explicitly requires both values.
        return
    if not (CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD):
        abort(403)
    assertion = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if not assertion:
        abort(403)
    try:
        import jwt
        jwks_client = jwt.PyJWKClient(
            f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=300,
        )
        signing_key = jwks_client.get_signing_key_from_jwt(assertion)
        jwt.decode(
            assertion,
            signing_key.key,
            algorithms=["RS256"],
            audience=CF_ACCESS_AUD,
            issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}",
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception:
        log.warning("Cloudflare Access token validation failed for a reviewer request.")
        abort(403)


@app.before_request
def protect_reviewer_routes_with_cloudflare_access():
    if request.path == "/review" or request.path.startswith("/review/"):
        _verify_cloudflare_access()


with app.app_context():
    db.create_all()
    if EVIDENCE_STORAGE == "local":
        os.makedirs(app.config["EVIDENCE_FOLDER"], exist_ok=True)


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-src https://docs.google.com; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cache-Control"] = "no-store"
    if IS_PROD:
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resp


@app.route("/", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def index():
    if request.method == "POST":
        phone = norm_phone(request.form.get("phone"))
        if not phone:
            return render_template(
                "index.html", amount=AMOUNT_KOBO // 100,
                error="Enter a valid Nigerian mobile number."
            ), 400
        session.clear()
        session["pending_phone"] = phone
        return redirect(url_for("application_form"))
    return render_template("index.html", amount=AMOUNT_KOBO // 100)


@app.get("/form")
def application_form():
    phone = session.get("pending_phone")
    if not phone:
        return redirect(url_for("index"))
    # Prefill the form with the one phone number the user entered on this site.
    query = urlencode({
        "embedded": "true",
        "usp": "pp_url",
        f"entry.{PHONE_ENTRY_ID}": phone,
    })
    form_url = f"{FORM_VIEW_URL}?{query}"
    return render_template(
        "form.html", form_url=form_url, phone=phone
    )


def _valid_image_upload(upload):
    """Verify a real PNG/JPEG/WebP image and enforce a 4 MiB per-file limit."""
    if not upload or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    allowed = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
    if ext not in allowed:
        return None
    data = upload.stream.read(4 * 1024 * 1024 + 1)
    upload.stream.seek(0)
    if len(data) > 4 * 1024 * 1024:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != allowed[ext]:
                return None
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return ext


def _admin_logged_in():
    return session.get("review_admin") is True


@app.post("/confirm-submission")
@limiter.limit("10 per hour")
def confirm_form_submission():
    if not session.get("pending_phone"):
        return redirect(url_for("index"))
    # This is the user's declaration, not an API-verified Google submission.
    if request.form.get("confirm_read") != "yes":
        return redirect(url_for("application_form"))
    session["submission_confirmed"] = True
    return redirect(url_for("evidence_upload"))


@app.route("/evidence", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def evidence_upload():
    phone = session.get("pending_phone")
    if not phone or not session.get("submission_confirmed"):
        return redirect(url_for("application_form"))
    # Preserve duplicate protection across both new submissions and old claim tables.
    if has_legacy_claim(phone) or RewardClaim.query.filter_by(phone=phone).first():
        return render_template(
            "message.html", title="Phone number already used",
            text="This phone number appears in existing reward history and cannot submit another reward claim."
        ), 409
    existing = EvidenceSubmission.query.filter_by(phone=phone).first()
    if existing:
        return render_template("evidence_status.html", submission=existing, phone=phone)
    if request.method == "POST":
        network = request.form.get("network", "")
        if network not in NETWORKS:
            return render_template("evidence_upload.html", phone=phone, error="Choose a valid mobile network."), 400
        fields = [
            ("referral_screenshot", "referral"),
            ("confirmation_screenshot", "confirmation"),
        ]
        checked = []
        for field, label in fields:
            upload = request.files.get(field)
            ext = _valid_image_upload(upload)
            if not ext:
                return render_template(
                    "evidence_upload.html", phone=phone,
                    error=f"Upload a valid PNG, JPG, or WebP screenshot for the {label} evidence (maximum 4 MB each)."
                ), 400
            checked.append((label, upload, ext))
        saved = {}
        try:
            for label, upload, ext in checked:
                filename = f"{uuid.uuid4().hex}{ext}"
                _save_evidence(upload, filename, ext)
                saved[label] = filename
            record = EvidenceSubmission(
                phone=phone,
                network=network,
                # phone_image is a legacy non-null column; reuse the referral filename without requiring a phone screenshot.
                phone_image=saved["referral"],
                referral_image=saved["referral"],
                confirmation_image=saved["confirmation"],
                status="pending",
            )
            db.session.add(record)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            for filename in saved.values():
                try:
                    _delete_evidence(filename)
                except Exception:
                    log.exception("Could not clean up orphaned evidence object %s", filename)
            existing = EvidenceSubmission.query.filter_by(phone=phone).first()
            if existing:
                return render_template("evidence_status.html", submission=existing, phone=phone)
            return render_template("evidence_upload.html", phone=phone, error="We could not save this submission. Please try again."), 500
        except Exception:
            db.session.rollback()
            for filename in saved.values():
                try:
                    _delete_evidence(filename)
                except Exception:
                    log.exception("Could not clean up orphaned evidence object %s", filename)
            log.exception("Evidence upload failed.")
            return render_template("evidence_upload.html", phone=phone, error="Upload failed. Please try again."), 500
        session.pop("submission_confirmed", None)
        return render_template("evidence_status.html", submission=record, phone=phone)
    return render_template("evidence_upload.html", phone=phone)


@app.route("/review", methods=["GET", "POST"])
def review_dashboard():
    admin_password = os.getenv("ADMIN_REVIEW_PASSWORD", "")
    if not admin_password:
        return render_template(
            "message.html", title="Review dashboard not configured",
            text="Set ADMIN_REVIEW_PASSWORD in the local environment before using the private review dashboard."
        ), 503
    if request.method == "POST" and not _admin_logged_in():
        supplied = request.form.get("password", "")
        if secrets.compare_digest(supplied, admin_password):
            session["review_admin"] = True
            session.permanent = True
            return redirect(url_for("review_dashboard"))
        return render_template("review_login.html", error="Incorrect password."), 401
    if not _admin_logged_in():
        return render_template("review_login.html")
    submissions = EvidenceSubmission.query.order_by(EvidenceSubmission.created_at.desc()).all()
    total_phone_numbers = EvidenceSubmission.query.count()
    eligible_phone_numbers = EvidenceSubmission.query.filter(
        EvidenceSubmission.status.in_(["approved", "reward_sent"])
    ).count()
    pending_count = EvidenceSubmission.query.filter_by(status="pending").count()
    return render_template(
        "review_dashboard.html",
        submissions=submissions,
        total_phone_numbers=total_phone_numbers,
        eligible_phone_numbers=eligible_phone_numbers,
        pending_count=pending_count,
    )


@app.post("/review/logout")
def review_logout():
    session.pop("review_admin", None)
    return redirect(url_for("review_dashboard"))


@app.post("/review/<int:submission_id>/<decision>")
def review_decision(submission_id, decision):
    if not _admin_logged_in():
        abort(403)
    if decision not in ("approve", "reject", "mark-sent"):
        abort(404)
    record = db.session.get(EvidenceSubmission, submission_id)
    if record is None:
        abort(404)
    if decision == "mark-sent":
        if record.status != "approved":
            return redirect(url_for("review_dashboard"))
    elif record.status != "pending":
        return redirect(url_for("review_dashboard"))
    if decision == "mark-sent":
        if record.status != "approved":
            return redirect(url_for("review_dashboard"))
        record.status = "reward_sent"
    else:
        if record.status != "pending":
            return redirect(url_for("review_dashboard"))
        record.status = "approved" if decision == "approve" else "rejected"
    record.reviewer_note = (request.form.get("reviewer_note") or "").strip()[:500] or record.reviewer_note
    record.reviewed_at = utcnow()
    db.session.commit()
    return redirect(url_for("review_dashboard"))


@app.get("/review/<int:submission_id>/image/<kind>")
def review_evidence_image(submission_id, kind):
    if not _admin_logged_in():
        abort(403)
    record = db.session.get(EvidenceSubmission, submission_id)
    if record is None:
        abort(404)
    filename = {
        "phone": record.phone_image,
        "referral": record.referral_image,
        "confirmation": record.confirmation_image,
    }.get(kind)
    if not filename:
        abort(404)
    try:
        payload = _read_evidence(filename)
    except Exception:
        log.exception("Evidence object retrieval failed for submission %s", submission_id)
        abort(404)
    return send_file(
        io.BytesIO(payload),
        mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        as_attachment=True,
        download_name=f"submission-{submission_id}-{kind}{os.path.splitext(filename)[1]}",
        max_age=0,
    )


@app.post("/eligibility")
@limiter.limit("10 per hour")
def check_eligibility():
    if session.get("submission_confirmed") and session.get("pending_phone"):
        return redirect(url_for("evidence_upload"))
    return redirect(url_for("application_form"))


@app.route("/claim", methods=["GET", "POST"])
def claim():
    return redirect(url_for("index"))


@app.get("/status")
def status():
    return redirect(url_for("index"))


@app.errorhandler(429)
def too_many(_):
    return render_template(
        "message.html", title="Too many attempts",
        text="Please wait a while before trying again."
    ), 429


@app.errorhandler(400)
def bad_request(_):
    return render_template(
        "message.html", title="Request expired",
        text="Your session or form token expired. Go back and try again."
    ), 400
