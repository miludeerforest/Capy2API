# Capy2API for CLIProxyAPI (experimental)

Community, **unofficial** provider plugin for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 8.x. It maps OpenAI Chat Completions requests to the **documented Capy Thread API**, not the Capy website's private endpoints.

## Features and limits

- ✅ CLIProxyAPI C-ABI plugin (Linux `.so`) with model registration and credential parser.
- ✅ Import a Capy **official API key** and existing project ID; static keys do not auto-refresh.
- ✅ `POST /threads`, `GET /threads/{id}`, `GET /threads/{id}/messages` bridged through CPA host HTTP transport.
- ✅ Text-only `/v1/chat/completions`, including multi-turn messages flattened into a new Capy thread.
- ⚠️ Streaming is **buffered SSE**. The current plugin waits for the full agent answer before sending chunks.
- ❌ No Capy web-session cookie login, browser OAuth, token theft, or bypass of subscription checks. Capy does not document a third-party OAuth flow.
- ❌ No image/audio, tool/function calling, Responses-native state, or actual model token accounting.
- ❌ Capy cloud machine time and API usage may **cost credits**, even for models labeled free in the UI. Do not assume free API routing.
- ⚠️ This prototype has **not** been tested against a live Capy account or a running CLIProxyAPI instance. It has only been compiled and unit-tested offline.

## Build (Linux)

You need Go 1.21+ and a C compiler (`gcc`). Inside `plugin/`:

```bash
CGO_ENABLED=1 go build -buildmode=c-shared -o ../capy.so .
```

> CLIProxyAPI official releases may be compiled without CGO. Use a CPA binary with native dynamic plugin support or rebuild CPA with CGO and the plugin loader enabled. Native plugins are trusted code and run in-process.

Copy `capy.so` to CPA's plugin directory (`plugins/linux/amd64/capy.so` or `plugins/capy.so`) and add the config below. Restart CPA.

```yaml
plugins:
  enabled: true
  dir: plugins
  configs:
    capy:
      enabled: true
      priority: 20
```

## Authentication: import API key, not OAuth

1. Sign into `https://capy.ai` in your browser and create a personal API key at **Settings → API**.
2. Find an accessible project ID via `GET https://api.capy.ai/api/v1/projects` (see the snippet below).
3. Create an auth file `~/.cli-proxy-api/capy-main.json` using `examples/capy-main.json` as a template, substituting your own secret and project ID. Protect with `chmod 600`.
4. Confirm CPA Management API lists the Capy credential and model IDs. CPA accepts `/v1/chat/completions` from downstream clients including New API.

```bash
curl -fsS https://api.capy.ai/api/v1/projects \
  -H "Authorization: Bearer $CAPY_API_KEY"
```

**Never paste your API key into chat or commit your filled-in credential file to Git.** The official Capy API uses `Authorization: Bearer capy_...`, not cookies.

## Models

| CPA model | Capy upstream model ID |
| --- | --- |
| `capy-deepseek-v4.1-flash` | `deepseek/deepseek-v4.1-flash-1017` |
| `capy-glm-5.3-flash` | `zai/glm-5.3-flash` |

First check your account's actual model permissions with `GET https://api.capy.ai/api/v1/models`. A name appearing in an API model list does not guarantee free usage or that a specific account may execute it.

## New API

Once your CLIProxyAPI `/v1/models` shows `capy-*` models, add an **OpenAI-compatible** channel in your New API with the existing CLIProxyAPI base URL and client API key. No Capy secret should be entered in New API.

## Tests

```bash
cd plugin && go test ./...
```

Unit tests mock the Capy API at the HTTP bridge boundary. Live end-to-end testing still requires your credentials and billing confirmation.

## Official documentation

- Capy API: https://docs.capy.ai/api-reference/overview
- Create threads: https://docs.capy.ai/api-reference/threads/create-thread
- List messages: https://docs.capy.ai/api-reference/messages/list-thread-messages
- CLIProxyAPI plugin ABI: https://github.com/router-for-me/CLIProxyAPI/tree/main/examples/plugin/simple

## Design note

This implementation explicitly does **not** emulate a raw DeepSeek/GLM API. Capy is an agent that runs work in a project on a machine. Thus latency, semantics, cloud costs, and response shape differ from a pure completion API. Agent instructions and tools are not carried over from OpenAI's function-calling contract.
