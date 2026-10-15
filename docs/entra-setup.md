# Entra ID setup

The API validates Entra ID v2.0 access tokens issued for one app registration in our tenant.
The web app signs users in with MSAL (SPA flow) and calls the API with that token. Roles come
from Entra groups and/or app roles. No client secret is needed.

## 1. Create the app registration

1. Entra admin center → **App registrations** → **New registration**.
2. Name: `ai-cost-tracker`. Supported account types: **Accounts in this organizational
   directory only** (single tenant).
3. Redirect URI: platform **Single-page application (SPA)**, URI
   `https://costs.promptshields.com/` (add `http://localhost:3000/` for local dev).
4. Register. Note the **Application (client) ID** and **Directory (tenant) ID**.

## 2. Issue v2.0 tokens and expose an API scope

1. **Manifest**: set `"requestedAccessTokenVersion": 2` (`accessTokenAcceptedVersion` in the
   legacy manifest). The API only accepts the v2.0 issuer
   `https://login.microsoftonline.com/<tenant>/v2.0`.
2. **Expose an API** → set the Application ID URI to `api://<client-id>` → **Add a scope**
   `access_as_user` (admins and users can consent).
3. The web app requests `api://<client-id>/access_as_user`. Tokens carry `aud` = the client
   ID; `api://<client-id>` is accepted too.

## 3. Emit group IDs in the token

1. **Token configuration** → **Add groups claim** → choose **Groups assigned to the
   application** (recommended) or **Security groups**. Under *Access*, keep **Group ID**.
2. If you chose *Groups assigned to the application*: **Enterprise applications** →
   `ai-cost-tracker` → **Users and groups** → add the allow-listed and role groups.

Users in more than ~200 groups hit the **groups overage**: Entra omits `groups` and points to
Microsoft Graph instead. The API does not call Graph; it answers 403 with a message saying to
emit only assigned groups or use app roles. "Groups assigned to the application" avoids this.

## 4. Optional: app roles

Instead of (or as well as) group mapping, **App roles** → create roles with *Allowed member
types* = Users/Groups and **values** exactly `owner`, `admin`, `analyst`, `viewer`. Assign
users or groups in **Enterprise applications** → **Users and groups**. They arrive in the
`roles` claim. `owner` implies every other role.

## 5. Configure the API

| Variable | Value |
|---|---|
| `ACT_ENTRA_TENANT_ID` | Directory (tenant) ID. Tokens must have a matching `tid`. |
| `ACT_ENTRA_CLIENT_ID` | Application (client) ID. |
| `ACT_ALLOWED_GROUP_IDS` | JSON list of group object IDs allowed to sign in, e.g. `["<id>"]`. Empty = anyone in the tenant. |
| `ACT_ROLE_GROUPS` | JSON map role → group IDs, e.g. `{"admin": ["<id>"], "viewer": ["<id>"]}`. |
| `ACT_ENVIRONMENT` | `production` in deployed environments. |
| `ACT_AUTH_DISABLED` | Local dev only. `true` skips token checks and acts as an owner; refused unless `ACT_ENVIRONMENT=local`. |

A user's roles are the union of app roles in the token and roles whose groups they are in.
A signed-in user with no role can call only endpoints that do not require one.
