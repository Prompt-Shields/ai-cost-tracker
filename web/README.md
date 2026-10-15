# AI Cost Tracker web

Standalone Next.js (App Router) front end for the AI Cost Tracker API. Signs users in with
Entra ID (MSAL, redirect flow), then shows an overview of AI spend: KPI tiles, a stacked daily
cost chart by model and a by-model table, for the last 7, 30 or 90 days.

## Run locally

```sh
cd web
npm ci
cp .env.example .env.local   # defaults: API on :8000, auth disabled
npm run dev                  # http://localhost:3000
```

With `NEXT_PUBLIC_AUTH_DISABLED=true` the API must run with `ACT_AUTH_DISABLED=true` and
`ACT_ENVIRONMENT=local` (and allow CORS from the web origin). `act seed-demo` gives you data.

## Configuration

All variables are `NEXT_PUBLIC_*`, so Next.js inlines them into the client bundle **at build
time**; changing them needs a rebuild (pass them as Docker build args).

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | API base URL |
| `NEXT_PUBLIC_AUTH_DISABLED` | unset (auth on) | `"true"` skips sign-in and sends no token (local dev only) |
| `NEXT_PUBLIC_ENTRA_CLIENT_ID` | – | Client ID of the web (SPA) app registration |
| `NEXT_PUBLIC_ENTRA_TENANT_ID` | – | Entra tenant ID |
| `NEXT_PUBLIC_API_SCOPE` | – | API scope, e.g. `api://<api-client-id>/access_as_user` |

Entra setup: register the web app as a **single-page application** with the app's origin as
redirect URI, and grant it delegated permission to the API's exposed scope. Tokens are
acquired silently per API call; if interaction is required the app redirects to sign in.

## Scripts

- `npm run dev` – dev server
- `npm run build` / `npm start` – production build (standalone output) and server
- `npm run typecheck` – `tsc --noEmit`

## Docker

```sh
docker build -t act-web \
  --build-arg NEXT_PUBLIC_API_URL=https://api.example.com \
  --build-arg NEXT_PUBLIC_ENTRA_CLIENT_ID=... \
  --build-arg NEXT_PUBLIC_ENTRA_TENANT_ID=... \
  --build-arg NEXT_PUBLIC_API_SCOPE=api://.../access_as_user \
  web
docker run -p 3000:3000 act-web
```

## Layout

- `app/` – root layout and the Overview route
- `components/` – `AuthProvider` (MSAL / auth-disabled session + API client), `Header`,
  `Overview`, `DailyCostChart`
- `lib/api.ts` – typed client for `/api/v1/me`, `/summary`, `/costs/daily`, `/deployments`
- `lib/series.ts` – pivots daily rows into chart series (top models get a colour, the rest
  fold into "Other")
