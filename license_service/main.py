import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


DEFAULT_DB_PATH = "/data/license.db"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat()


def normalize_key(value: str) -> str:
    return value.strip().upper()


def get_database_path() -> str:
    return os.getenv("LICENSE_DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH


def get_admin_token() -> str:
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ADMIN_TOKEN is required")
    return token


def get_signing_secret() -> str:
    secret = os.getenv("LICENSE_SIGNING_SECRET", "").strip()
    if not secret:
        raise RuntimeError("LICENSE_SIGNING_SECRET is required")
    return secret


def get_conn() -> sqlite3.Connection:
    db_path = Path(get_database_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS license_keys (
          license_key TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'active',
          max_devices INTEGER NOT NULL DEFAULT 1,
          expires_at TEXT,
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS license_activations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          license_key TEXT NOT NULL,
          device_fingerprint TEXT NOT NULL,
          device_name TEXT NOT NULL DEFAULT '',
          first_activated_at TEXT NOT NULL,
          last_validated_at TEXT NOT NULL,
          UNIQUE(license_key, device_fingerprint)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_license_activations_license_key
        ON license_activations(license_key)
        """
    )
    conn.commit()
    conn.close()


def parse_optional_datetime(raw: Optional[str]) -> Optional[datetime]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_expired(expires_at: Optional[str]) -> bool:
    expires_dt = parse_optional_datetime(expires_at)
    return expires_dt is not None and expires_dt <= utc_now()


def require_admin_token(x_admin_token: str) -> None:
    if not x_admin_token or not secrets.compare_digest(x_admin_token, get_admin_token()):
        raise HTTPException(status_code=401, detail="invalid admin token")


def build_license_signature(
    license_key: str,
    device_fingerprint: str,
    status: str,
    expires_at: Optional[str],
) -> str:
    payload = "|".join(
        [
            normalize_key(license_key),
            device_fingerprint.strip(),
            status.strip(),
            (expires_at or "").strip(),
        ]
    )
    return hmac.new(
        get_signing_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def fetch_license_or_404(conn: sqlite3.Connection, license_key: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT license_key, status, max_devices, expires_at, note, created_at, updated_at
        FROM license_keys
        WHERE license_key = ?
        """,
        (normalize_key(license_key),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="license not found")
    return row


def ensure_license_usable(row: sqlite3.Row) -> None:
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="license disabled")
    if is_expired(row["expires_at"]):
        raise HTTPException(status_code=403, detail="license expired")


def license_payload(row: sqlite3.Row, *, device_fingerprint: str) -> dict:
    status = "active"
    signature = build_license_signature(
        row["license_key"],
        device_fingerprint,
        status,
        row["expires_at"],
    )
    return {
        "licenseKey": row["license_key"],
        "status": status,
        "maxDevices": row["max_devices"],
        "expiresAt": row["expires_at"],
        "signature": signature,
        "issuedAt": utc_now_text(),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Exam License Service", version="0.1.0", lifespan=lifespan)


class CreateLicenseRequest(BaseModel):
    license_key: str = Field(min_length=4, max_length=128)
    max_devices: int = Field(default=1, ge=1, le=100)
    expires_at: Optional[str] = None
    note: str = Field(default="", max_length=500)


class ToggleLicenseRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class UnbindRequest(BaseModel):
    device_fingerprint: str = Field(min_length=3, max_length=256)


class ActivateRequest(BaseModel):
    license_key: str = Field(min_length=4, max_length=128)
    device_fingerprint: str = Field(min_length=3, max_length=256)
    device_name: str = Field(default="", max_length=256)


class ValidateRequest(BaseModel):
    license_key: str = Field(min_length=4, max_length=128)
    device_fingerprint: str = Field(min_length=3, max_length=256)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "exam-license-service",
        "timestamp": utc_now_text(),
    }


@app.post("/api/admin/licenses")
def create_license(
    body: CreateLicenseRequest,
    x_admin_token: str = Header(default=""),
) -> dict:
    require_admin_token(x_admin_token)
    expires_at = parse_optional_datetime(body.expires_at)
    normalized_key = normalize_key(body.license_key)
    now = utc_now_text()

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO license_keys(
          license_key, status, max_devices, expires_at, note, created_at, updated_at
        ) VALUES (?, 'active', ?, ?, ?, ?, ?)
        ON CONFLICT(license_key) DO UPDATE SET
          status = 'active',
          max_devices = excluded.max_devices,
          expires_at = excluded.expires_at,
          note = excluded.note,
          updated_at = excluded.updated_at
        """,
        (
            normalized_key,
            body.max_devices,
            expires_at.isoformat() if expires_at else None,
            body.note.strip(),
            now,
            now,
        ),
    )
    conn.commit()
    row = fetch_license_or_404(conn, normalized_key)
    conn.close()
    return {
        "ok": True,
        "license": {
            "licenseKey": row["license_key"],
            "status": row["status"],
            "maxDevices": row["max_devices"],
            "expiresAt": row["expires_at"],
            "note": row["note"],
        },
    }


@app.get("/api/admin/licenses")
def list_licenses(x_admin_token: str = Header(default="")) -> dict:
    require_admin_token(x_admin_token)
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
          lk.license_key,
          lk.status,
          lk.max_devices,
          lk.expires_at,
          lk.note,
          lk.created_at,
          lk.updated_at,
          COUNT(la.id) AS bound_devices
        FROM license_keys lk
        LEFT JOIN license_activations la
          ON la.license_key = lk.license_key
        GROUP BY
          lk.license_key, lk.status, lk.max_devices, lk.expires_at,
          lk.note, lk.created_at, lk.updated_at
        ORDER BY lk.created_at DESC, lk.license_key ASC
        """
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "licenses": [
            {
                "licenseKey": row["license_key"],
                "status": row["status"],
                "maxDevices": row["max_devices"],
                "boundDevices": row["bound_devices"],
                "expiresAt": row["expires_at"],
                "note": row["note"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ],
    }


@app.post("/api/admin/licenses/{license_key}/disable")
def disable_license(
    license_key: str,
    body: ToggleLicenseRequest,
    x_admin_token: str = Header(default=""),
) -> dict:
    require_admin_token(x_admin_token)
    conn = get_conn()
    fetch_license_or_404(conn, license_key)
    conn.execute(
        """
        UPDATE license_keys
        SET status = 'disabled', note = ?, updated_at = ?
        WHERE license_key = ?
        """,
        (body.note.strip(), utc_now_text(), normalize_key(license_key)),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/admin/licenses/{license_key}/enable")
def enable_license(
    license_key: str,
    body: ToggleLicenseRequest,
    x_admin_token: str = Header(default=""),
) -> dict:
    require_admin_token(x_admin_token)
    conn = get_conn()
    fetch_license_or_404(conn, license_key)
    conn.execute(
        """
        UPDATE license_keys
        SET status = 'active', note = ?, updated_at = ?
        WHERE license_key = ?
        """,
        (body.note.strip(), utc_now_text(), normalize_key(license_key)),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/admin/licenses/{license_key}/activations")
def list_activations(license_key: str, x_admin_token: str = Header(default="")) -> dict:
    require_admin_token(x_admin_token)
    conn = get_conn()
    fetch_license_or_404(conn, license_key)
    rows = conn.execute(
        """
        SELECT id, device_fingerprint, device_name, first_activated_at, last_validated_at
        FROM license_activations
        WHERE license_key = ?
        ORDER BY first_activated_at ASC, id ASC
        """,
        (normalize_key(license_key),),
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "activations": [
            {
                "id": row["id"],
                "deviceFingerprint": row["device_fingerprint"],
                "deviceName": row["device_name"],
                "firstActivatedAt": row["first_activated_at"],
                "lastValidatedAt": row["last_validated_at"],
            }
            for row in rows
        ],
    }


@app.post("/api/admin/licenses/{license_key}/unbind")
def unbind_device(
    license_key: str,
    body: UnbindRequest,
    x_admin_token: str = Header(default=""),
) -> dict:
    require_admin_token(x_admin_token)
    conn = get_conn()
    fetch_license_or_404(conn, license_key)
    conn.execute(
        """
        DELETE FROM license_activations
        WHERE license_key = ? AND device_fingerprint = ?
        """,
        (normalize_key(license_key), body.device_fingerprint.strip()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/license/activate")
def activate_license(body: ActivateRequest) -> dict:
    conn = get_conn()
    row = fetch_license_or_404(conn, body.license_key)
    ensure_license_usable(row)

    normalized_key = row["license_key"]
    fingerprint = body.device_fingerprint.strip()
    existing = conn.execute(
        """
        SELECT id
        FROM license_activations
        WHERE license_key = ? AND device_fingerprint = ?
        """,
        (normalized_key, fingerprint),
    ).fetchone()

    now = utc_now_text()
    if existing is None:
        bound_count = conn.execute(
            """
            SELECT COUNT(*) AS count_value
            FROM license_activations
            WHERE license_key = ?
            """,
            (normalized_key,),
        ).fetchone()["count_value"]
        if bound_count >= row["max_devices"]:
            conn.close()
            raise HTTPException(status_code=403, detail="device limit reached")
        conn.execute(
            """
            INSERT INTO license_activations(
              license_key, device_fingerprint, device_name, first_activated_at, last_validated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (normalized_key, fingerprint, body.device_name.strip(), now, now),
        )
    else:
        conn.execute(
            """
            UPDATE license_activations
            SET device_name = ?, last_validated_at = ?
            WHERE id = ?
            """,
            (body.device_name.strip(), now, existing["id"]),
        )
    conn.commit()
    payload = license_payload(row, device_fingerprint=fingerprint)
    conn.close()
    return {
        "ok": True,
        "activated": True,
        "license": payload,
    }


@app.post("/api/license/validate")
def validate_license(body: ValidateRequest) -> dict:
    conn = get_conn()
    row = fetch_license_or_404(conn, body.license_key)
    ensure_license_usable(row)

    normalized_key = row["license_key"]
    fingerprint = body.device_fingerprint.strip()
    activation = conn.execute(
        """
        SELECT id
        FROM license_activations
        WHERE license_key = ? AND device_fingerprint = ?
        """,
        (normalized_key, fingerprint),
    ).fetchone()
    if activation is None:
        conn.close()
        raise HTTPException(status_code=403, detail="device not bound")

    conn.execute(
        """
        UPDATE license_activations
        SET last_validated_at = ?
        WHERE id = ?
        """,
        (utc_now_text(), activation["id"]),
    )
    conn.commit()
    payload = license_payload(row, device_fingerprint=fingerprint)
    conn.close()
    return {
        "ok": True,
        "valid": True,
        "license": payload,
    }
