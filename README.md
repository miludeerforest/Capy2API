# Capy2API — Capy free-model bridge for CLIProxyAPI

> **Status: research / not operational.** This repository targets the **free models shown in the Capy web/app model selector**, not the metered Capy Agent API. No verified implementation of the free-model route or Capy account login exists yet. Do not deploy this as a working provider.

## Target

Expose eligible Capy free-model access (for example, DeepSeek V4.1 Flash and GLM-5.3-Flash, if available for the account) to self-hosted [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), then optionally to New API.

Intended flow:

```text
New API / OpenAI client
       |
CLIProxyAPI 8.x provider plugin
       |
Capy account authentication (supported, consented flow — TBD)
       |
Capy free model route (availability, terms and protocol — TBD)
```

## Verified facts

- CLIProxyAPI 8.x provides a C-ABI dynamic plugin mechanism with model registration, auth records, and executors. See [official example](https://github.com/router-for-me/CLIProxyAPI/tree/main/examples/plugin/simple).
- Capy's **documented** API lives at `https://api.capy.ai/api/v1` and authenticates with a key created at **Settings → API**.
- The documented Capy API creates project-bound agent threads, not OpenAI chat completions. API access and the free/ad-supported options shown in the Capy UI **must not be assumed to have the same entitlement**.
- The Capy API documentation does **not** establish a third-party OAuth flow for authenticating a CLIProxyAPI plugin to the ad-supported free-model routing.

## Required validation before implementation

1. Establish whether Capy explicitly permits external application access to the free/ad-supported model selection, including ads or other user-facing eligibility conditions.
2. Establish a supported Capy account authorization mechanism for a local third-party client (OAuth, device flow, or other documented method). No credential capture or hidden session scraping.
3. With a dedicated test account, verify model selection, reply transport, rate limits, error handling, regional availability, and billing against Capy-provided documentation or authorized integration.
4. Once supported, implement and test the CLIProxyAPI provider plugin; then verify its models through `/v1/models` and actual Chat Completions via a New API compatible channel.

## Separate official API prototype

An experimental local **official API key → Capy Thread API** prototype was prepared, but it is **not** the free-model-login plugin described here. It is intentionally not represented as the finished solution or published as a working Capy free router.

## Security

Never commit Capy credentials, cookies, authorization headers, JWTs, HAR traces with sessions, or API keys. Do not bypass advertisements, access controls, quotas, or regional restrictions.

## References

- [Capy REST API](https://docs.capy.ai/api-reference/overview)
- [Capy Thread creation](https://docs.capy.ai/api-reference/threads/create-thread)
- [CLIProxyAPI plugin examples](https://github.com/router-for-me/CLIProxyAPI/tree/main/examples/plugin/simple)
