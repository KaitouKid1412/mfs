# Scraping provenance & ToS inventory (F-15)

Status: **AWAITING USER SIGN-OFF** — each row below carries a recommended
decision (`accept-risk` / `replace` / `drop-AMC`); none is final until the
operator signs the sign-off block at the bottom.

Scope: every adapter whose access path is something other than "GET a URL the
AMC publishes in plain HTML". All data scraped is statutory disclosure
material (SEBI-mandated monthly portfolios, factsheet PTR) fetched at low
volume (one run/day, a handful of requests per AMC per month), for private
research — not redistribution. That posture lowers, but does not eliminate,
ToS exposure: several paths below mimic browsers or replay reverse-engineered
client internals, which most site ToS prohibit regardless of intent.

General failure mode shared by all four: these paths are invisible contracts.
The AMC can rotate a key/bundle/host at any redeploy with zero notice; the
pipeline's fail-fast posture means the affected AMC drops out loudly (per-AMC
isolation records the failure, Gate B shows the coverage hole) rather than
silently — that containment is what makes `accept-risk` tenable at all.

---

## 1. ITI — extracted AES key/IV (managers/iti.py:104-105, holdings/iti.py:91-92)

**Access path.** ITI's catalog API (`itiamc.com/jeeth/api/v1/catalog/`)
AES-128-CBC-encrypts every request and response body. The key
(`aar6tzij8o1snaar`) and IV (`0123456789ABCDEF`) were recovered verbatim from
the public Angular bundle's `encryptionProviderService` (they ship to every
browser). Both the factsheet (PTR) and holdings adapters carry their own copy
and replicate the SPA's encrypt/decrypt with `cryptography`.

**What breaks if it rotates.** A new bundle with a new key makes every API
call return garbage/error → both ITI adapters fail per-AMC at the next run
(loud, isolated). Fix = re-extract key/IV from the new `main.<hash>.js`
(~30 min manual work). Both files must be updated together.

**ToS exposure.** Moderate. The "encryption" is obfuscation of a public
catalog (the key ships to every visitor), and the data behind it is
SEBI-mandated disclosure. But replaying extracted client crypto is exactly
the kind of "circumvention of access controls" language site ToS — and, read
aggressively, IT Act s.43 — gestures at. No auth is bypassed (there are no
credentials; everyone gets the same key).

**Recommendation: accept-risk.** Statutory data, no credentialed access
bypassed, loud failure on rotation. Mitigation: keep the two key copies
greppable (`_AES_KEY`) so rotation is one re-extraction; if ITI ever puts the
catalog behind real auth, drop to `drop-AMC` rather than credential-sharing.

- [ ] USER decision: accept-risk / replace / drop-AMC: ____________

---

## 2. Edelweiss — TLS-fingerprint/browser mimicry + AdvisorKhoj URL resolution (managers/edelweiss.py:204-231, holdings/edelweiss.py)

**Access path.** Edelweiss sits behind Akamai bot management that 403s
non-browser TLS fingerprints. The factsheet adapter uses HTTP/2 + a full
Chrome header set (UA, `Sec-Fetch-*`, Origin/Referer) + a homepage TLS warmup
to pass the edge, then calls the SPA's encrypted `getAllDownloads` API
(per-request HmacSHA256 key + AES response decryption, replicated from the
bundle). The holdings adapter additionally cannot reach the listing API at
all (the app layer needs a client-IP-bound HMAC), so it scrapes a THIRD
PARTY — AdvisorKhoj's form-download centre — purely to resolve the
non-guessable timestamped filename, then downloads the bytes from Edelweiss's
own CDN with the Chrome header fingerprint.

**What breaks if it rotates.** (a) Akamai policy tightened to JS-challenge or
real TLS fingerprinting (JA3) → both adapters 403 → loud per-AMC failure;
header mimicry cannot fix that, only a real browser engine could. (b) The
`getAllDownloads` crypto scheme changes → factsheet adapter fails loudly.
(c) AdvisorKhoj changes layout/blocks us or stops mirroring → holdings
discovery fails loudly; bytes provenance is unaffected (we never take data
bytes from AdvisorKhoj, only a URL).

**ToS exposure.** Highest of the four. Deliberately defeating a bot-detection
edge is unambiguous ToS breach territory even for statutory data, and it adds
a second party's ToS (AdvisorKhoj) to the surface. Volume is trivial
(a few requests/month) and the bytes come from the AMC's own CDN, which
keeps data-integrity provenance clean even though access provenance is not.

**Recommendation: accept-risk, flagged for replacement.** This is the
adapter most likely to break AND most exposed. If Edelweiss publishes a
stable direct download index (worth re-checking quarterly), switch
immediately. If Akamai escalates, prefer `drop-AMC` over escalating the
mimicry (no headless-browser arms race — that crosses a line we don't want
to cross for one AMC).

- [ ] USER decision: accept-risk / replace / drop-AMC: ____________

---

## 3. Kotak — prodtest API host (holdings/kotak.py:121; F-13)

**Access path.** Holdings discovery + download go to
`https://vlbapiprodtest.kotakmf.com/kotakapi/portfolio/...` — a host whose
name says TEST, with no freshness/correctness SLA. F-13 investigation
(code/docs only; no network in this pass): this host is what the LIVE
www.kotakmf.com SPA itself is configured to call (`urlProxies.kotakapi` in
the production bundle), so it serves real investor traffic today; the only
production-named alternative (`www.kotakmf.com/api`, a reverse-proxy of the
same service) sits behind a Radware bot challenge and is unusable; any other
production host (`vlbapiprod` etc.) is an unverified guess requiring a live
probe. **F-13 decision: keep the host, document the risk (done in the module
docstring), and verify continuously** — no switch without a verified parity
check.

**What breaks if it rotates.** Kotak repointing the SPA at a real prod host
and decommissioning prodtest → loud per-AMC failure (good). The DANGEROUS
mode is silent divergence: prodtest keeps answering but with stale or
test-fixture data. That is undetectable by the pipeline, which is why the
recurring manual parity check exists: docs/ops/spot_check.md item 4 (one
Kotak scheme-month's top-10 holdings vs Kotak's public disclosure page,
monthly with the ingest cycle).

**Operator verification step (next sanctioned ingest window).** Re-inspect
the SPA bundle's `urlProxies` for a production `kotakapi` host; probe the
obvious `vlbapiprod`/`api` variants; if one answers without the Radware
challenge, verify one scheme-month row-identical against prodtest, then
switch `_API` and record the parity note in the commit message.

**Recommendation: accept-risk with the standing parity check** (the
spot-check item is mandatory, not optional, while `_API` points at prodtest).

- [ ] USER decision: accept-risk / replace / drop-AMC: ____________

---

## 4. JioBlackRock — hardcoded Next.js Server Action id (holdings/jio_blackrock.py:91)

**Access path.** The monthly-disclosure list is fetched by POSTing to the
public disclosure page with `Next-Action:
70f6a730f357954e4483bbdc8988624b3f73cad662` — the Server Action id lifted
from the page bundle's `createServerReference(...)` call. The JioBlackRock
SERVER executes the action (holding its own gated Strapi token) and returns
the document list; the `.xlsx` files come from a public CDN with no auth.
From our side this is an unauthenticated, public endpoint — we never touch
or possess the Strapi credentials.

**What breaks if it rotates.** Next.js regenerates action ids on (some)
redeploys of that page → the POST returns an error/empty flight payload →
loud per-AMC failure. Fix = re-read the new id from the page bundle
(~10 min). Expect this to recur at every site redesign.

**ToS exposure.** Low-moderate. We invoke an endpoint the site itself exposes
unauthenticated to every visitor's browser; no secret is extracted or
replayed (contrast ITI). It is still a non-public interface used outside the
intended client, and the gated-Strapi backend signals the AMC did not intend
direct programmatic access.

**Recommendation: accept-risk.** Cleanest provenance of the four after the
plain-HTML adapters; rotation cost is small and loud. If JioBlackRock ever
exposes the document list in server-rendered HTML or a public API, switch.

- [ ] USER decision: accept-risk / replace / drop-AMC: ____________

---

## Summary table

| adapter(s) | mechanism | rotation blast radius | ToS exposure | recommendation |
|---|---|---|---|---|
| iti (managers + holdings) | AES key/IV extracted from public bundle | both ITI feeds, loud | moderate | accept-risk |
| edelweiss (managers + holdings) | Akamai bot-filter mimicry + encrypted API + AdvisorKhoj URL resolution | both feeds, loud | **high** | accept-risk, flagged for replacement; drop-AMC over escalation |
| kotak (holdings) | prodtest API host (no SLA) | loud if host dies; **silent if data diverges** | low (open host) / data-provenance risk high | accept-risk + mandatory monthly parity check |
| jio_blackrock (holdings) | hardcoded Next.js Server Action id | holdings feed, loud | low-moderate | accept-risk |

## Sign-off

- Operator: ____________  Date: ____________
- Decisions above confirmed (tick each adapter's box) — until then the
  recommendations are provisional and any of these adapters may be disabled
  on request.
