---
name: add-anistream-telegram-provider
description: Add, update, or repair a media catalogue provider in AniStream Telegram, including strict URL detection, anonymous cross-provider search attribution, structured season and language variants, episode and embed extraction, provider registration, Telegram callback navigation, Mini App playback, resolver integration, SSRF-safe networking, media-gateway compatibility, and regression tests. Use whenever an agent is asked to support a new streaming site or adapt an existing provider after a site change in the AniStream Telegram repository.
---

# Add an AniStream Telegram Provider

Implement site-specific behavior behind AniStream's neutral provider contract.
Preserve the private Search, Continue Watching, Telegram navigation, source
preflight, authenticated Mini App, per-episode history, and media-gateway
workflows. Do not add Local or Download features.

## Inspect the contract first

Read these files before editing:

- `src/anistream/models.py`
- `src/anistream/providers/base.py`
- `src/anistream/providers/registry.py`
- `src/anistream/providers/__init__.py`
- one complete provider such as `src/anistream/providers/anime_sama.py`
- `src/anistream/resolvers/base.py`, `registry.py`, and `hosts.py`
- `src/anistream/services/source_planner.py` and `media_probe.py`
- `src/anistream/utils/http.py`
- `src/anistream_telegram/core.py`, `bot.py`, `web.py`, `media.py`,
  `security.py`, and `database.py`
- focused provider, resolver, HTTP, bot-flow, web, media, and security tests

Check the worktree before changing files. Preserve unrelated user changes,
ignored runtime state, and production configuration.

## Keep provider boundaries strict

Implement the four `Provider` methods in a dedicated module under
`src/anistream/providers/`:

1. `matches(url)` must validate parsed exact hostnames or intentional
   subdomains plus the provider's catalogue path. Reject lookalike suffixes.
2. `search(query)` must return `SearchResult` values with the stable provider
   ID, display name, title, and canonical catalogue URL.
3. `variants(url)` must return every playable season/language pair as a
   separate `CatalogueVariant`.
4. `catalogue(url)` must return one language-specific `Catalogue` with
   contiguous episode numbers and ordered embed candidates.

Also declare concise `content_types` and `search_languages` tuples on the
provider class. These values power the anonymous two-row capability summary in
Telegram Settings and Search. Use broad user-facing labels such as `Movies`,
`Series`, `Anime`, `French`, or `English`; never put the real provider name,
domain, internal language code, or marketing copy in these fields.

Keep HTML, JavaScript, API, route, and language-code knowledge inside the
provider. Do not add provider names, route shapes, or language codes to the
Telegram bot, web routes, history, database, player, or planner.

Treat every provider response and extracted URL as untrusted even when it comes
from a known site.

## Model languages without global assumptions

Use provider-owned language identifiers:

```python
language = MediaLanguage(code="provider-native-code", label="Human label")
variant = CatalogueVariant(
    name="Season 1 - Human label",
    url=language_specific_url,
    season="Season 1",
    language=language,
)
```

Apply these invariants:

- Keep the provider's normalized language code stable.
- Use a concise label suitable for Telegram buttons and Mini App metadata.
- Return one variant per season/language pair; never merge languages.
- Preserve the selected `MediaLanguage` in the resulting `Catalogue`.
- Keep direct season/language URLs on that exact language.
- Follow provider relationship metadata for separate season pages.
- Keep the current page usable if optional related-season discovery fails.
- Discover variants from provider metadata when possible.
- Do not introduce a global language enum.

History identity includes provider, catalogue URL, season, and language.
Changing canonical URLs can split existing Continue Watching entries, so make
canonicalization deterministic.

## Register and expose the provider

Export the provider and instantiate it in `default_providers()` in
`src/anistream/providers/__init__.py`. Every registered provider participates
in Telegram Search by default. Each Telegram user can disable individual
providers under **Settings → Manage providers**; the provider registry must
honor that per-user selection before starting any upstream search work.

Use a stable lowercase `provider.id` and keep the real provider display name
available internally for diagnostics. The Telegram product must never expose
that name to users. `CoreService` assigns stable anonymous aliases
(`Provider 1`, `Provider 2`, and so on) from the explicit
`default_providers()` registration order. New providers automatically receive
the next alias. Append new providers and never reorder existing registrations,
because that order is a user-facing compatibility contract. Update the alias
regression test when appending a provider. Do not hardcode provider IDs, names,
or alias numbers in `BotHandlers`.

Every Telegram button that combines a title with an alias must reserve room for
the complete `Provider N` suffix. Truncate only the title with an ellipsis.
A new provider should not require provider-specific conditional code in
`CoreService`, `BotHandlers`, `WebRoutes`, or the Mini App.
Its anonymous Settings and Search summaries must come entirely from the
provider's generic capability metadata.

If provider-specific configuration is genuinely required:

- add narrowly named environment fields through `Config`;
- document them in `.env.example` and the README;
- scope cookies to exact provider catalogue hosts;
- never hardcode credentials, tokens, cookies, captured responses, or paths;
- add a regression test proving cookies cannot reach embed or media hosts.

## Preserve Telegram navigation

The bot stores provider payloads server-side in user-bound
`EphemeralSelection` records. Keep Telegram `callback_data` short and opaque;
never serialize catalogue URLs, titles, JSON, credentials, or media URLs into a
button.

Provider additions must preserve:

- visible anonymous `Provider N` attribution in Search, Continue Watching,
  Completed, and management views without leaking the real provider name;
- title truncation that always preserves the complete provider alias;
- a Back action from variants to search results;
- a Back action from episodes to variants;
- paginated episodes when the catalogue is large;
- user binding and expiry for every selection;
- validation of indices and episode numbers after loading stored state;
- private-chat and whitelist middleware behavior;
- the single-use Web App launch-ticket flow.

Do not bypass the bot and expose a raw provider URL to the Mini App.

## Handle embed hosts separately

Provider code should expose ordered embed URLs, not duplicate resolution logic.
If a host is unsupported:

1. Add a focused resolver under `src/anistream/resolvers/`.
2. Match parsed hostnames exactly.
3. Register it in `default_resolvers()`.
4. Return `ResolvedMedia` with required Referer, Origin, and User-Agent headers.
5. Test it with mocked responses.

Do not weaken probing or accept arbitrary HTML as playable media. Keep direct
media matching limited to explicit MP4/HLS paths extracted from provider data.

## Preserve both SSRF boundaries

All provider, resolver, and probe requests must use the shared
`anistream.utils.http.HttpClient`. Never call raw Requests, urllib3, aiohttp, or
system proxy APIs from provider code.

The synchronous client must continue to:

- accept only absolute HTTP(S) URLs on ports 80 and 443;
- reject URL credentials;
- resolve every hop once and reject the entire answer set if any IP is
  non-global;
- connect sockets only to the validated numeric IP set;
- retain the original hostname for HTTP Host, TLS SNI, and certificate checks;
- disable environment and explicit proxies;
- revalidate and repin every redirect;
- bound non-streaming response sizes;
- restrict sensitive cookies to exact configured provider hosts.

The final Mini App media path has a separate boundary:

- validate the resolved master URL in `WebRoutes`;
- keep media requests behind authenticated playback IDs;
- use `SafeResolver` and `TCPConnector` for DNS-pinned media connections;
- keep DNS caching disabled;
- validate each explicit redirect and HLS child resource;
- preserve encrypted playback-bound HLS tokens;
- sanitize upstream headers;
- keep private, link-local, reserved, and unsafe-port destinations blocked.

`MEDIA_ALLOWED_HOSTS` applies only to final media/CDN hosts. When introducing a
provider, record every legitimate master, rendition, segment, audio, subtitle,
and key hostname observed during sanitized smoke checks. Do not broaden or
disable the IP controls when the list is incomplete.

## Add deterministic coverage

Use mocked HTTP responses or small sanitized inline fixtures. Never make the
automated suite depend on a live provider or commit captured pages.

Cover at least:

- accepted canonical/alternate domains and rejected lookalikes;
- empty search, stable anonymous provider attribution, and long-title
  truncation that preserves the alias;
- anonymous provider capability metadata, default-enabled behavior, per-user
  enable/disable persistence, and exclusion before upstream search;
- root catalogue discovery and direct deep links;
- separate related seasons, including optional partial failures;
- every supported language mapping and label;
- distinct variants for each season/language pair;
- selected variant language equality with catalogue language;
- contiguous episode numbering and missing-player alignment;
- unusable embed filtering;
- HTTP failures, malformed responses, and empty catalogues;
- registration through `default_providers()`;
- Search payload serialization through the neutral core;
- Telegram result, variant, episode, pagination, and Back-button behavior when
  provider output changes shape;
- any new resolver and required request headers;
- private/mixed DNS rejection, DNS pinning, redirects, and cookie isolation for
  new networking behavior;
- acceptance by `public_url_parts()` and the media gateway;
- watch-history isolation between provider/language/season variants.

Run from the repository root:

```text
python -m pytest -q
python -m compileall -q src tests
node --check web/src/main.js
npm --prefix web run build
```

Optionally run one sanitized live Search → variants → catalogue → prepare-media
smoke check after deterministic tests pass. Report live results separately
because provider behavior changes. Never print or persist cookies, launch
tickets, session tokens, cast grants, or raw authenticated pages.

## Update documentation

Update:

- the Supported providers table and provider notes;
- resolver-host coverage when adding a host;
- requirements for any new dependency;
- environment documentation for any private provider setting;
- media/CDN allowlist guidance;
- test coverage summary.

Keep the README structure aligned with AniStream CLI while documenting only the
Telegram watch-only product.

## Definition of done

Confirm all of the following:

- Link detection accepts only the intended provider URLs.
- Search results visibly identify the anonymous `Provider N` choice without
  exposing the provider's real name.
- Every season/language variant opens the matching catalogue.
- Bot callbacks remain short, opaque, user-bound, expiring, and navigable.
- Episode lists remain contiguous and paginate correctly.
- Watch receives ordered candidates and retains automatic fallback.
- The Mini App receives only authenticated same-origin media routes.
- Provider requests retain DNS pinning, TLS verification, cookie isolation,
  redirect checks, response bounds, and proxy blocking.
- Final media delivery retains SafeResolver, HLS token, and whitelist controls.
- Continue Watching remains isolated by user/provider/catalogue/language and
  handles rewatches without regression.
- Existing providers and the full offline suite pass.
- A sanitized live smoke check, if performed, succeeds through the real shared
  client.
- Documentation lists the provider and any configuration or allowlist impact.
- No runtime databases, environment files, cookies, tokens, media, captured
  pages, hardcoded personal paths, or temporary validation artifacts are
  staged.
