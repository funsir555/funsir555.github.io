# Railway License Service

This directory contains a standalone license service for your exam app.

## 1. Local setup

PowerShell:

```powershell
cd C:\Users\funsir\Desktop\模拟考试系统\license_service
.\bootstrap_local.ps1
```

Edit `.env` and set:

- `ADMIN_TOKEN`
- `LICENSE_SIGNING_SECRET`
- `LICENSE_DB_PATH`

Then run:

```powershell
.\run_local.ps1
```

Health check:

```text
http://127.0.0.1:8000/health
```

## 2. Railway deployment

1. Push this directory to GitHub.
2. In Railway, create a new project from the GitHub repo.
3. Set service root to `license_service` if the repo contains other files.
4. Add environment variables:
   - `ADMIN_TOKEN`
   - `LICENSE_SIGNING_SECRET`
   - `LICENSE_DB_PATH=/data/license.db`
5. Add a Railway Volume and mount it to `/data`.
6. Enable Public Networking and generate a Railway domain.

## 3. API overview

Public endpoints:

- `GET /health`
- `POST /api/license/activate`
- `POST /api/license/validate`

Admin endpoints, require header `X-Admin-Token`:

- `POST /api/admin/licenses`
- `GET /api/admin/licenses`
- `POST /api/admin/licenses/{license_key}/disable`
- `POST /api/admin/licenses/{license_key}/enable`
- `GET /api/admin/licenses/{license_key}/activations`
- `POST /api/admin/licenses/{license_key}/unbind`

## 4. Sample requests

Create a license:

```http
POST /api/admin/licenses
X-Admin-Token: your-admin-token
Content-Type: application/json

{
  "license_key": "MID-TEST-001",
  "max_devices": 2,
  "note": "first test key"
}
```

Activate:

```http
POST /api/license/activate
Content-Type: application/json

{
  "license_key": "MID-TEST-001",
  "device_fingerprint": "PC-001",
  "device_name": "office-pc"
}
```

Validate:

```http
POST /api/license/validate
Content-Type: application/json

{
  "license_key": "MID-TEST-001",
  "device_fingerprint": "PC-001"
}
```

## 5. Notes

- The license service is isolated from the main exam app to avoid regressions.
- SQLite data persists only if Railway Volume is mounted.
- `LICENSE_SIGNING_SECRET` is reserved for later client-side cache verification.
