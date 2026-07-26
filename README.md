# AniStream Telegram

AniStream Telegram is a private, whitelist-only Telegram bot and Mini App for
searching multiple AniStream providers and watching through an authenticated
web player. It intentionally contains no Local or Download workflow.

The bot reuses AniStream's provider-neutral catalogue, resolver and source
planning design. Playback is adapted for the web: MP4 byte ranges and HLS
playlists are relayed through a same-origin media gateway so provider headers
never have to be exposed to browser code.

## User flow

1. An allowed Telegram user opens the bot in a private chat.
2. The bot displays **Search**, **Continue Watching**, and **Help**.
3. Search queries every registered provider concurrently.
4. The user chooses the provider result, season, language and episode.
5. The bot creates a short-lived, one-time launch ticket.
6. A Telegram Web App button opens the Mini App.
7. The backend validates signed Telegram `initData`, its age, the whitelist,
   and the ticket owner.
8. The Mini App streams through the authenticated media gateway and saves
   per-episode progress.

## Security model

- Access defaults to denied.
- Telegram usernames are never authorization identifiers; signed 64-bit
  Telegram user IDs are.
- Bot updates, callbacks, Mini App authentication, API calls and media requests
  all re-check authorization at their boundary.
- Launch tickets are random, single use, user-bound and expire after two
  minutes.
- Web sessions are opaque, stored as SHA-256 hashes, and delivered only through
  `HttpOnly` cookies.
- Every mutating API call requires an origin check and CSRF token.
- HLS resource URLs are encrypted and bound to one playback session.
- Upstream requests reject URL credentials, non-HTTP schemes, unexpected
  ports, private/link-local/reserved IPs and unvalidated redirect destinations.
- SQLAlchemy parameterizes database access and the frontend never renders
  provider text as HTML.
- The production container runs as a non-root user with a read-only filesystem,
  no Linux capabilities and an internal-only database network.

`MEDIA_ALLOWED_HOSTS` can add a strict provider/CDN hostname allowlist. When it
is empty, arbitrary public hosts may be used, but private and reserved networks
remain blocked. A strict list is recommended once real provider CDN domains
have been inventoried on the VPS.

## Requirements

- Python 3.11+
- Node.js 22+ to build the Mini App
- A Telegram bot token from BotFather
- An HTTPS domain for Telegram Web Apps
- PostgreSQL for the production Compose deployment

SQLite is supported for local development.

## Local development

Create an environment file:

```powershell
Copy-Item .env.example .env
```

Generate secrets instead of keeping the sample values:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Build the frontend:

```powershell
Set-Location web
npm install
npm run build
Set-Location ..
```

Install Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

For local backend work, use `RUN_MODE=polling`, `COOKIE_SECURE=false`, and a
local `PUBLIC_BASE_URL`. Telegram itself will only open a production Web App
from an HTTPS URL, so a real HTTPS tunnel or staging domain is required for
end-to-end Mini App testing.

Start the service:

```powershell
.\start-local.ps1
```

The launcher loads the ignored `.env` file into the process and creates the
local SQLite data directory when needed. Stop it with `Ctrl+C`.

## Whitelist administration

IDs in `TELEGRAM_ALLOWED_USERS` are inserted at startup. Runtime administration
is also available from the VPS shell:

```text
anistream-admin allow 123456789
anistream-admin deny 123456789
anistream-admin list
```

There is deliberately no Telegram command for changing the whitelist.

## BotFather configuration

1. Create the bot and save its token outside Git.
2. Set the bot's domain to the exact HTTPS `PUBLIC_BASE_URL` domain.
3. Keep the bot in private-chat mode for this application.
4. Start the service; webhook mode registers
   `https://your-domain/telegram/webhook` with a secret header.
5. Send `/start` from a whitelisted Telegram account.

The actual watch button is an inline Web App button. Do not replace it with a
reply-keyboard Web App button because the server requires signed user
information in `initData`.

## VPS deployment

Point the domain's A record at the VPS. Copy `.env.example` to `.env` and set:

```text
ANISTREAM_DOMAIN=watch.example.com
PUBLIC_BASE_URL=https://watch.example.com
POSTGRES_PASSWORD=<long random password>
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_ALLOWED_USERS=<comma-separated numeric IDs>
WEBHOOK_SECRET=<random letters/digits/_/->
SESSION_SECRET=<at least 32 random bytes>
```

Then deploy:

```text
docker compose build
docker compose up -d
docker compose ps
```

Only ports 80 and 443 need to be public. Restrict SSH to trusted source
addresses where possible. The application and PostgreSQL services are not
published as host ports.

## Adding providers

Providers live under `src/anistream/providers/` and implement:

- `matches(url)`
- `search(query)`
- `variants(url)`
- `catalogue(url)`

Register the provider in `default_providers()`. Provider output must stay in the
neutral `SearchResult`, `CatalogueVariant`, `Catalogue`, `Episode`, and
`EmbedCandidate` models.

Resolvers live under `src/anistream/resolvers/`. Register new embed hosts in
`default_resolvers()` and return `ResolvedMedia` with the required `Referer`,
`Origin`, and `User-Agent` headers.

For a server deployment, also add contract tests and update the outbound
hostname inventory. Never add a generic resolver that can fetch user-supplied
URLs.

## Tests

```text
python -m pytest
python -m compileall -q src tests
cd web
npm run build
```

The included tests cover Telegram signature validation, expiry, ticket replay,
whitelist revocation, per-episode positions, the rewatch progression
regression, opaque media-token binding and private-network blocking.

## Important limitations

- Provider and embed-host behavior can change without notice.
- The VPS relays video traffic and therefore consumes its bandwidth.
- `MEDIA_ALLOWED_HOSTS` should be tightened from observed production domains.
- Android, iOS and Desktop Telegram playback still require real-client testing.
- This project does not host media. Operators remain responsible for provider
  terms and applicable laws.
