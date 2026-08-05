# Local development identity — used when APP_ENV=dev and no EasyAuth headers are present.
#
# Values are wired to the real MACAE tenant/app so that:
#   - get_authenticated_user_details() returns a stable, non-null user_principal_id
#   - get_tenantid() decodes the X-Ms-Client-Principal blob and returns the real tenant_id
#   - OBO flow in auth_utils._dev_acquire_user_token() targets the correct tenant/client
#
# X-Ms-Client-Principal is a base64-encoded JSON with at minimum {"tid": "<tenant_id>"}.
# Tenant / client IDs match _DEV_TENANT_ID / _DEV_CLIENT_ID in auth_utils.py.

sample_user = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en",
    "Content-Type": "application/json",
    "Host": "localhost:8000",
    "Origin": "http://localhost:3000",
    "Referer": "http://localhost:3000/",
    "User-Agent": "Mozilla/5.0 (dev)",
    # ── EasyAuth identity headers ──────────────────────────────────────────
    # A stable local dev user ID — used as Cosmos partition key / user_id everywhere.
    "X-Ms-Client-Principal-Id": "dev-local-user-0000-0000-000000000001",
    "X-Ms-Client-Principal-Name": "dev@local",
    "X-Ms-Client-Principal-Idp": "aad",
    # Base64-encoded JSON: {"tid": "<tenant_id>", "oid": "<user_id>", ...}
    # get_tenantid() decodes this to extract tenant_id = 978d9cc6-...
    "X-Ms-Client-Principal": (
        "eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJSUzI1NiIsICJpc3MiOiAiaHR0cHM6Ly9sb2dpbi5taWNyb3NvZnRvbmxpbmUuY29tLzk3OGQ5Y2M2LTc4NGMtNGM5OC04ZDkwLWE0YTYzNDRhNjVmZi92Mi4wIiwgInRpZCI6ICI5NzhkOWNjNi03ODRjLTRjOTgtOGQ5MC1hNGE2MzQ0YTY1ZmYiLCAib2lkIjogImRldi1sb2NhbC11c2VyLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEiLCAibmFtZSI6ICJEZXYgTG9jYWwgVXNlciIsICJwcmVmZXJyZWRfdXNlcm5hbWUiOiAiZGV2QGxvY2FsIiwgInJvbGVzIjogW119"
    ),
    # Access token is intentionally empty here — auth_utils._dev_acquire_user_token()
    # will mint a real delegated token via DeviceCodeCredential (cached to disk after
    # first login, auto-refreshed for ~90 days). Set MACAE_DEV_OBO_TOKEN env var to
    # override with a pre-acquired token and skip the device-code prompt entirely.
    "X-Ms-Token-Aad-Access-Token": "",
    "X-Ms-Token-Aad-Id-Token": "",
}
