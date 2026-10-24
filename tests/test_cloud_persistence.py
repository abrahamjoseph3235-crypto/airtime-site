import io
import os
from types import SimpleNamespace

import pytest

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["ADMIN_REVIEW_PASSWORD"] = "test-review-password"
os.environ["RATELIMIT_ENABLED"] = "false"

import app as app_module


class FakeObjectBody:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"].read()

    def get_object(self, **kwargs):
        return {"Body": FakeObjectBody(self.objects[(kwargs["Bucket"], kwargs["Key"])])}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def test_r2_storage_round_trip_and_delete(monkeypatch):
    client = FakeR2()
    monkeypatch.setattr(app_module, "EVIDENCE_STORAGE", "r2")
    monkeypatch.setattr(app_module, "_r2_client", lambda: client)
    monkeypatch.setenv("R2_BUCKET_NAME", "private-test-bucket")

    upload = SimpleNamespace(stream=io.BytesIO(b"png-bytes"))
    app_module._save_evidence(upload, "test-object.png", ".png")
    assert client.put_calls[0]["ContentType"] == "image/png"
    assert app_module._read_evidence("test-object.png") == b"png-bytes"

    app_module._delete_evidence("test-object.png")
    assert ("private-test-bucket", "test-object.png") not in client.objects


def test_reviewer_route_requires_validated_access_header_when_configured(monkeypatch):
    monkeypatch.setattr(app_module, "CF_ACCESS_TEAM_DOMAIN", "example.cloudflareaccess.com")
    monkeypatch.setattr(app_module, "CF_ACCESS_AUD", "test-audience")
    client = app_module.app.test_client()
    response = client.get("/review")
    assert response.status_code == 403


def test_local_reviewer_route_still_works_without_access_configuration(monkeypatch):
    monkeypatch.setattr(app_module, "CF_ACCESS_TEAM_DOMAIN", "")
    monkeypatch.setattr(app_module, "CF_ACCESS_AUD", "")
    monkeypatch.setenv("ADMIN_REVIEW_PASSWORD", "test-review-password")
    response = app_module.app.test_client().get("/review")
    assert response.status_code == 200

