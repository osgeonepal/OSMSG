# osmsg leaderboard frontend

Static single-page leaderboard for the osmsg API. No build step: plain HTML, CSS, and JavaScript.
Tailwind, lucide, and flatpickr load from a CDN at runtime.

## Files

- `index.html` , page markup and CDN includes.
- `app.js` , query flow, state, rendering, CSV export.
- `charts.js` , the trending-hashtags and editors lists.
- `style.css` , all styling (light and dark).
- `sw.js` , service worker (Workbox): network-first for the app shell and API, long-lived CDN cache.
- `manifest.webmanifest` , PWA manifest.
- `tailwind.config.js` , Tailwind CDN configuration.

## API origin

`app.js` reads the API base from `window.OSMSG_API_BASE`, falling back to `window.location.origin`.
In production Caddy serves this site and reverse-proxies `/api/*`, `/health`, `/docs`, and `/schema`
to the API on the same origin, so no per-deploy edit is needed.

## Local preview

Serve the directory with any static server and point it at a running API:

```sh
python -m http.server -d frontend 8080
```

Open <http://localhost:8080>. Set `window.OSMSG_API_BASE` in the console or a small inline script if
the API runs on a different origin.

## Deployment

The production Caddy service bind-mounts this directory read-only at `/srv/leaderboard`
(`infra/docker-compose.yml`). See `docs/infra.md` for the full deployment.
