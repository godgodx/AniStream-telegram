<div align="center">

# AniStream Telegram

**Discover and watch anime, movies, and series through a private Telegram bot and a secure Mini App.**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Telegram Mini App](https://img.shields.io/badge/Telegram-Mini%20App-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/webapps)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Providers: 2](https://img.shields.io/badge/providers-2-7c3aed)](#supported-providers)

</div>

AniStream Telegram is a provider-driven, whitelist-only Telegram bot. It
searches several catalogues, resolves working video sources, and opens an
authenticated web player inside Telegram. Its provider-neutral core is
deliberately separated from site-specific extraction logic so new providers and
embed hosts can be added without redesigning the bot or Mini App.

This edition is watch-only. It intentionally contains no Local or Download
workflow.

> [!IMPORTANT]
> AniStream Telegram does not host, mirror, or redistribute media. It only
> processes links exposed by configured third-party providers. You are
> responsible for complying with provider terms and the laws applicable in
> your location.

## Features

- **Private whitelist access** — every functional bot action, callback, Mini
  App session, API request, media request, and cast grant is bound to an allowed
  numeric Telegram user ID. The only public command is `/id`, which reveals
  only the sender's own ID for access requests.
- **Clean native Telegram navigation** — Search, Continue Watching, Help,
  paginated episodes, anonymous provider attribution, and breadcrumb-style Back
  buttons use styled inline keyboards that replace the current panel instead
  of flooding the conversation with new messages.
- **Multi-provider search** — query every registered catalogue concurrently and
  expose stable `Provider 1`, `Provider 2`, and subsequent aliases without
  revealing catalogue names in the Telegram interface.
- **Structured seasons and languages** — providers expose one neutral variant
  per season/language pair without global language assumptions.
- **Source preflight planning** — resolve and probe ordered candidates before
  playback, then fall back to another supported source when necessary.
- **Secure Mini App playback** — one-time launch tickets create authenticated
  `HttpOnly` sessions; raw provider headers and media URLs never reach normal
  frontend code.
- **MP4 and HLS gateway** — relay byte ranges, playlists, segments, encryption
  keys, and required upstream headers through the same protected origin.
- **Reliable watch history** — save a distinct position for every episode,
  preserve forward continuation during rewatches, and resume the last genuinely
  interrupted episode. Finished series move to a clearly labelled Completed
  section, while any active or completed entry can be restarted from episode 1.
  Users can also remove an entry and its saved positions through a confirmed
  private action.
- **Series controls** — move to the previous or next episode and automatically
  start the next episode after normal completion.
- **TV playback** — expose Google Cast, AirPlay, or Remote Playback when the
  current Telegram client or external browser supports it.
- **Hardened outbound networking** — block credentials, unsafe ports,
  private/link-local/reserved IPs, DNS rebinding, unsafe redirects, environment
  proxies, oversized provider responses, and unvalidated media destinations.
- **Production isolation** — run as a non-root container with a read-only
  filesystem, dropped Linux capabilities, HTTPS termination, and an
  internal-only PostgreSQL network.

## Supported providers

| Provider | Search | Seasons/languages | Movies | Series | Watch |
| --- | :---: | :---: | :---: | :---: | :---: |
| [Anime-Sama](https://anime-sama.to/) | Yes | Yes | — | Yes | Yes |
| [French Stream](https://french-stream.one/) | Yes | Yes | Yes | Yes | Yes |

Anime-Sama exposes provider-native variants such as VF and VOSTFR. French
Stream exposes movie and series variants including French/VF, VOSTFR,
TrueFrench/VFF, VFQ, and VO/VOSTENG when supplied by the selected title.

These integration names are documented for maintainers, but the Telegram
interface exposes only stable aliases based on registration order:
`Provider 1`, `Provider 2`, and so on. Long titles are shortened before the
alias, so the provider choice always remains visible.

The resolver layer recognizes direct media plus embeds served through
Embed4me, Sendvid, Sibnet, Vidmoly, Vidzy, OneUpload, Uqload, Smoothpre,
Movearnpre, Mivalyo, and Dingtezuni. Third-party availability can change
without notice.

## Requirements

| Dependency | Purpose | Required for |
| --- | --- | --- |
| [Python 3.11+](https://www.python.org/downloads/) | Bot, API, provider core, media gateway | Everything |
| [Node.js 22+](https://nodejs.org/) | Build the static Mini App assets | Build |
| Telegram bot token | Bot authentication through BotFather | Everything |
| Public HTTPS domain | Telegram Web App, webhook, cookies, casting | Production |
| Docker with Compose | Isolated application, PostgreSQL, and Caddy services | Production |

PostgreSQL is used by the production Compose deployment. SQLite remains
available to developers and automated tests.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/godgodx/AniStream-telegram.git
cd AniStream-telegram
```

### 2. Configure production secrets

Copy `.env.example` to `.env` and replace every sample value:

```text
ANISTREAM_DOMAIN=watch.example.com
PUBLIC_BASE_URL=https://watch.example.com
POSTGRES_PASSWORD=<long-random-password>
TELEGRAM_BOT_TOKEN=<BotFather-token>
TELEGRAM_ALLOWED_USERS=<comma-separated-numeric-IDs>
WEBHOOK_SECRET=<random-letters-digits-underscore-hyphen>
SESSION_SECRET=<at-least-32-random-bytes>
```

Generate independent secrets with a trusted password manager or:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Never reuse the Telegram token as another application secret and never commit
`.env`.

### 3. Configure BotFather

1. Create the bot and store its token outside Git.
2. Set the bot domain to the exact `PUBLIC_BASE_URL` domain.
3. Keep the bot in private-chat mode for this application.
4. Send `/start` from a whitelisted Telegram account after deployment.

The Watch action must remain an inline Web App button. The server depends on
signed Telegram `initData` and does not accept a reply-keyboard replacement.

### 4. Deploy

Point the domain's A/AAAA records at the VPS, then run:

```bash
docker compose build
docker compose up -d
docker compose ps
```

Caddy obtains and renews HTTPS certificates automatically. Only ports 80 and
443 need to be public. The application and PostgreSQL services are not
published as host ports.

## How it works

The main menu keeps the watch flow intentionally small:

1. **Continue Watching** — reopen the last interrupted episode and position;
2. **Search** — discover a title across every registered provider;
3. **Help** — explain access and playback behavior.

```text
Whitelisted Telegram user
          |
          +--> Continue Watching -> per-user history -> launch exact episode
          |
          `--> Search -> concurrent provider search
                         |
                         v
                provider result -> season/language -> episode
                         |
                         v
                resolve embeds -> probe media -> choose working route
                         |
                         v
                one-time launch ticket -> signed Telegram Mini App session
                         |
                         v
                authenticated MP4/HLS gateway -> browser or supported TV
                         |
                         `--> save per-episode progress and continuation
```

### Search

Search runs across all registered providers concurrently. Results include the
provider's stable anonymous alias, so adding more sites never makes the choices
ambiguous or reveals catalogue names to Telegram users. Selections are stored
server-side under short-lived opaque IDs instead of being serialized into
Telegram callback data.

Season and language variants lead to a paginated episode keyboard. Every level
includes a Back action so an accidental selection can be corrected without
starting over.

### Continue Watching

History is isolated by Telegram user, provider, catalogue URL, season, and
language. The data model stores:

- the forward continuation episode;
- the last selected or interrupted episode;
- a separate position and duration for every episode;
- completion state for episodes and seasons.

Skipping forward does not erase older positions. Rewatching an earlier episode
does not move the main continuation backwards. If that rewatch is interrupted,
Continue Watching resumes it; after it finishes, continuation returns to the
furthest valid forward point.

### Watch

Before opening the Mini App, the core resolves supported embeds and probes the
media response. The backend creates a random single-use launch ticket bound to
the Telegram user and selected catalogue. The Mini App exchanges signed
Telegram `initData` plus that ticket for an opaque `HttpOnly` web session.

The player requests only same-origin gateway URLs. The server supplies required
Referer, Origin, and User-Agent headers upstream, rewrites HLS resources into
encrypted playback-bound URLs, validates redirects, and relays ranges without
exposing provider credentials.

Progress is saved periodically, on pause, when Telegram hides the Mini App, and
when the page closes. Series expose previous/next controls and advance
automatically after the current episode finishes.

### TV playback

The Cast button chooses the best mechanism available:

- Google Cast in supported Cast-enabled browsers;
- AirPlay when WebKit exposes its native playback-target picker;
- the standard Remote Playback API when implemented by the client.

Google Cast receives a short-lived bearer URL bound to one playback and one
whitelisted Telegram user instead of receiving the Telegram session cookie.
Removing that user from the whitelist immediately invalidates the grant.

Device discovery, codecs, Telegram WebView support, and television behavior
remain platform-dependent. With Google's Default Media Receiver, remote
progress and automatic remote episode changes require the sender Mini App to
remain open. A custom registered Cast receiver would be required for autonomous
server-side progression after the sender closes.

## Whitelist and configuration

Bootstrap allowed users with `TELEGRAM_ALLOWED_USERS`. Runtime administration
is available only from the trusted VPS shell:

```text
anistream-admin allow 123456789
anistream-admin deny 123456789
anistream-admin list
```

A prospective user can send `/id` to the bot in a private chat before being
whitelisted. The bot returns only that user's numeric Telegram ID and a native
copy button. Send the supplied number to the administrator, who can then run
`anistream-admin allow <id>` on the VPS.

`/id` does not add anyone to the whitelist, create a launch ticket, expose the
menu, or authorize the Mini App. There is deliberately no Telegram command for
modifying the whitelist.

Important configuration:

| Variable | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Canonical HTTPS Mini App and webhook origin |
| `TELEGRAM_ALLOWED_USERS` | Initial numeric Telegram ID whitelist |
| `SESSION_TTL_SECONDS` | Authenticated Mini App session lifetime |
| `PLAYBACK_TTL_SECONDS` | Media session lifetime |
| `MAX_STREAMS_PER_USER` | Concurrent gateway stream limit |
| `MEDIA_ALLOWED_HOSTS` | Optional strict media/CDN hostname allowlist |
| `ANIME_SAMA_USER_AGENT` | Optional provider-specific browser user agent |
| `ANIME_SAMA_CF_CLEARANCE` | Optional private Cloudflare clearance value |

`MEDIA_ALLOWED_HOSTS` applies to final resolved media and HLS resource hosts,
not provider catalogue pages. When empty, public media hosts are accepted while
private and reserved addresses remain blocked. After representative production
plays, inventory every master, rendition, key, and segment hostname before
enforcing a strict list.

Runtime databases, environment files, cookies, generated frontend assets,
virtual environments, and caches are ignored by Git. Treat `.env` and the
database as private.

## Architecture

```text
src/
|-- anistream/
|   |-- providers/       Site search, variants, and episode discovery
|   |-- resolvers/       Embed URLs converted into playable media sources
|   |-- services/        Source planning and media probing
|   |-- models.py        Provider-neutral domain models
|   `-- utils/http.py    Bounded, DNS-pinned provider HTTP client
`-- anistream_telegram/
    |-- bot.py           Telegram menus, callbacks, breadcrumbs, launch buttons
    |-- core.py          Async bridge to the provider-neutral AniStream core
    |-- database.py      Whitelist, tickets, sessions, playback, watch history
    |-- web.py           Telegram auth, CSRF-protected API, Mini App routes
    |-- media.py         DNS-pinned MP4/HLS gateway and cast delivery
    `-- security.py      Telegram signatures, origins, URLs, tokens, headers

web/
|-- index.html           Mini App player shell
|-- src/                 Player, episode navigation, progress, casting, styles
`-- dist/                Generated production assets

deploy/Caddyfile         HTTPS reverse proxy
compose.yaml             App, PostgreSQL, and Caddy isolation
```

Provider and resolver registries keep Telegram, history, and playback
independent from any single website. Provider-owned language metadata travels
through the neutral core as a stable code and user-facing label.

### Adding a provider

1. Implement the four-method provider interface under
   `src/anistream/providers/`.
2. Return one `CatalogueVariant` per season/language pair.
3. Register the provider in `default_providers()`.
4. Reuse the neutral search, catalogue, episode, language, and embed models.
5. Add deterministic parsing, URL-detection, language, registration, bot-flow,
   HTTP, resolver, and media-security tests.
6. Document the provider and update the observed outbound media-host inventory.

> [!TIP]
> AI coding agents should invoke
> [`$add-anistream-telegram-provider`](.agents/skills/add-anistream-telegram-provider/SKILL.md)
> before integrating or repairing a provider. The repository skill defines the
> provider, Telegram navigation, resolver, SSRF, gateway, testing, and
> documentation contract.

### Adding an embed host

1. Implement a focused resolver under `src/anistream/resolvers/`.
2. Match parsed hostnames exactly; never use broad attacker-controlled
   substring matching.
3. Register it in `default_resolvers()`.
4. Return `ResolvedMedia` with the required Referer, Origin, and User-Agent.
5. Add mocked resolution and probe coverage.
6. Keep all fetches on the shared DNS-pinned HTTP client and final media
   delivery behind the authenticated gateway.

## Security model

- Access defaults to denied and uses signed numeric Telegram IDs.
- Launch tickets are random, one-time, user-bound, and short-lived.
- Web sessions and cast tokens are stored only as SHA-256 hashes.
- Cookies are `HttpOnly`, `Secure`, `SameSite=None`, and `__Host-` scoped in
  production so Telegram's embedded cross-site WebView can send them only over
  HTTPS.
- Mutating API requests require both an origin check and a CSRF token.
- Provider text is inserted with `textContent`, never interpreted as HTML.
- SQLAlchemy parameterizes database operations.
- Provider and resolver HTTP connections pin validated public DNS answers to
  the socket while preserving TLS SNI and certificate validation.
- Environment proxies are disabled on SSRF-sensitive clients.
- The final aiohttp gateway validates and pins every media and redirect
  destination independently.
- HLS child resources use encrypted, playback-bound, expiring URLs.
- Removing a user from the whitelist revokes sessions and cast access at the
  next authorization boundary.
- Unlisted users can invoke only the exact `/id` command in a private chat;
  callbacks, other commands, launch tickets, and Mini App authentication remain
  blocked.
- Raw launch, media, and cast tokens are excluded from application access logs.
- Production containers run with a read-only filesystem, no-new-privileges,
  dropped capabilities, and a private database network.

Security controls reduce risk but cannot make third-party provider behavior
trustworthy or guarantee absolute security. Rotate exposed credentials
immediately, keep dependencies and the VPS patched, restrict SSH and outbound
networking, and review rejected-host telemetry.

## Development

Install the project with development dependencies:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Build the Mini App:

```bash
cd web
npm install
npm run build
cd ..
```

Run the verification suite:

```bash
python -m pytest -q
python -m compileall -q src tests
node --check web/src/main.js
npm --prefix web run build
```

Automated tests use mocked providers and local application boundaries. Optional
live provider smoke checks must be reported separately because third-party
availability changes and tests must never require cookies or live page
captures.

## Troubleshooting

**Telegram opens the bot but not the player**

Confirm that `PUBLIC_BASE_URL` is HTTPS, the BotFather domain matches it
exactly, the user is whitelisted, and Caddy can reach the application service.

**A provider returns no results or no working source**

Provider and embed pages change independently. Retry later, update AniStream,
and include only sanitized errors when reporting a regression. Never publish
cookies, tokens, database contents, or `.env`.

**Playback works on the phone but not the TV**

The television may reject the codec or HLS variant, or Telegram may not expose
device discovery. Try a compatible external browser on the same network and
keep the sender open for Default Media Receiver progression.

**Strict `MEDIA_ALLOWED_HOSTS` blocks a stream**

Inventory the rejected hostname and decide whether it is a legitimate provider
CDN. Add only the exact domain or required parent suffix; never disable private
IP validation.

## Contributing

Issues and focused pull requests are welcome. Keep site-specific behavior
inside providers and resolvers, preserve Telegram authorization boundaries, add
regression coverage, and never commit runtime databases, generated secrets,
captured pages, cookies, personal paths, or media files.

## License

AniStream Telegram is available under the [MIT License](LICENSE).
