# Known Issues

Open problems that are understood but not yet fixed. Close an entry by deleting it in
the same commit that fixes it.

---

## #1 — iOS Safari "Add to Home Screen" creates a bookmark, not a standalone web app

**Status:** open · **Reported:** 2026-08-17 · **Area:** offline briefing (ADR 0003, slice 3)

### Symptom

On an iPad, Safari → Share → Add to Home Screen produces what behaves like a plain URL
bookmark rather than an installed standalone web app.

This matters because ADR 0003 §9 makes **Safari + Add to Home Screen the daily offline
path**. The self-contained bundle (`GET /bundle/<run_id>`) was tested at the same time and
**works**, so there is a functioning offline route today — the convenient one is the part
that is broken.

### What has NOT been established yet

**Standalone install and offline caching are two different things, and only the first is
confirmed broken.** Service workers run in ordinary Safari *tabs* too, so the briefing may
already work offline without the Home Screen icon. Nobody has checked. Establish this
first — it decides whether this is a cosmetic packaging bug or a functional one:

1. Open the briefing in a normal Safari tab. Does the header chip reach `✓ Offline ready`?
2. With the chip green, turn on airplane mode and reload that tab. Does the full briefing
   render, including the basemap and both DOC pane documents?

If both pass, the offline requirement is already met in Safari and this issue is only
about packaging. If the chip never turns green, the real problem is service worker
registration, not Add to Home Screen, and this entry is mistitled.

### Confirmed defect found while investigating (fix regardless)

`static/app.webmanifest` declares `"start_url": "/"`, but `/` redirects to `/upload`
(`app.py:840-842`). So even once standalone install works, tapping the icon **while
online** lands on the upload form rather than a briefing. Offline it happens to work,
because the service worker intercepts the navigation and redirects to the cached run
(`navigationFetch` in `static/sw.js`) — the online path has no such handling.

There is no single correct `start_url`, because the briefing URL is run-scoped
(`/map?r=<run_id>&g=1`) and changes with every upload. Options:

- point `start_url` at a new server route that 302s to the most recent completed run
- keep `/` but make it redirect to the latest run when one exists, upload form otherwise
- have the client rewrite the manifest's `start_url` at runtime via a blob URL

### Candidate causes for the standalone failure (none verified)

- The manifest never loaded or failed to parse on the device — the most likely cause, and
  the easiest to check. Safari also caches manifests aggressively, so a visit made
  *before* the manifest existed can stick.
- `apple-mobile-web-app-capable` is deprecated in favour of the manifest's `display`
  member. Both are present in `index.html`, so this should not be it, but it is worth
  ruling out.
- The page was added from a URL carrying `?r=…&g=…`; iOS prefers the manifest's
  `start_url`, and behaviour when that manifest is unreachable is not well specified.

### Diagnostics to run next

- On the iPad, open `<host>/static/app.webmanifest` directly. It must return HTTP 200 with
  `Content-Type: application/manifest+json` and valid JSON.
- Connect the iPad to macOS Safari's Web Inspector (Develop menu) and check the console
  for manifest parse errors, and Application → Service Workers for a live registration.
- Confirm the site is HTTPS. Service workers and install prompts both require a secure
  origin; `localhost` is exempt but a bare-IP or HTTP host is not.

### Blocker for reproducing

The URL recorded in `CLAUDE.md` — `https://web-production-2ec19.up.railway.app` — returns
Railway's own `404 Application not found` for every path, which is a platform-level "no app
at this hostname", not an application error. Either the deployment URL has changed or the
service is down. **The current URL is needed before any of the above can be checked**, and
`CLAUDE.md` should be corrected once it is known.

### Workaround in the meantime

Use the `⬇ Bundle` button. It downloads the whole briefing as one self-contained HTML file
that needs no server, no service worker and no install — verified working on the device.

### If it needs to be switched off entirely

Set `SW_KILL_SWITCH = True` in `app.py` and deploy. Every registered service worker then
deletes its caches and unregisters itself. See `static/sw-killswitch.js`.
