# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A content backend for a live-wallpaper mobile app, deployed on Vercel at `https://livewallpaper-gamma.vercel.app`. There is no build step, no `package.json`, no tests, and no `vercel.json` — Vercel serves the repo's static files directly and auto-detects `api/` as serverless functions.

- `video/*.mp4` — the wallpapers themselves (tracked in git)
- `thumb/*.webp` — one thumbnail per video, same basename
- `banner/*.webp` — promo banners for featured categories
- `3d/<category>/<n>/` — parallax (3D) wallpapers, one folder per wallpaper (e.g. `3d/anime/1/`), each holding three same-size JPGs (1536×2752):
  - `image.jpg` — full composed image (foreground + background)
  - `image_background.jpg` — background layer only (subject removed/filled)
  - `image_depth.jpg` — grayscale depth map, **white = near, black = far**
  Referenced from `parallax_data.json`; `image.jpg` doubles as the thumbnail.
- `wallpaper_data.json` — the single source of truth the app reads
- `parallax_data.json` — source of truth for parallax wallpapers (separate categories and ids)
- `api/[key].js` and `api/parallax/[key].js` — thin dynamic routes; both delegate to the shared handler `api/_lib/serveConfig.js` (`_`-prefixed paths under `api/` are not deployed as functions)

## API

- `GET /api/<key>` returns `wallpaper_data[key]` (`banner_config`, `category_config`, or `wallpaper_config`); unknown key → 404 listing available keys.
- `GET /api/wallpaper_config?categoryId=N` returns only that category's array.
- `GET /api/parallax/<key>` does the same over `parallax_data.json` (`category_config`, `wallpaper_config`, also supports `?categoryId=N`).
- Responses set CORS `*` and `Cache-Control: s-maxage=300, stale-while-revalidate=86400`, so data changes can take up to ~5 min to show through the CDN.
- The JSON is `require`d at build time — editing it requires a redeploy (push to `main`).
- Comments and error messages are in Vietnamese; keep that style.

Local run (if needed): `vercel dev` (Vercel CLI), then hit `http://localhost:3000/api/category_config`.

## `wallpaper_data.json` structure and conventions

- `category_config`: `{ id, nameId, sort }` — `sort` controls display order (99 = unordered tail).
- `banner_config`: `{ categoryId, sort, url }` — each banner links to a category.
- `wallpaper_config`: object keyed by category id **as a string** (`"1"`…`"15"`), each value an array of `{ categoryId, id, isDark, isPremium, thumb, url }`.
- Wallpaper `id` = `categoryId * 1000 + n` (e.g. 4003 is the 3rd Christmas wallpaper).
- Asset files are named `<prefix>_<NN>` with the same `NN` in `video/` and `thumb/`. The prefix is usually the lowercased category name, except category 15 "New Year" uses `year_`.
- `url`/`thumb`/banner `url` are absolute URLs on `https://livewallpaper-gamma.vercel.app/...`.

### Adding a wallpaper

1. Add `video/<prefix>_<NN>.mp4` and `thumb/<prefix>_<NN>.webp`.
2. Append an entry to `wallpaper_config["<categoryId>"]` with the next id and absolute URLs.

Adding a category means updating `category_config`, adding a new key in `wallpaper_config`, and optionally a banner in `banner_config`.

Quick consistency check (every referenced file exists):

```sh
python3 -c "import json,os;d=json.load(open('wallpaper_data.json'));p=lambda u:u.split('.vercel.app/')[-1];[print('MISSING',p(u)) for v in d['wallpaper_config'].values() for w in v for u in (w['url'],w['thumb']) if not os.path.exists(p(u))];[print('MISSING',p(b['url'])) for b in d['banner_config'] if not os.path.exists(p(b['url']))]"
```

## Parallax data (`parallax_data.json`)

- Own `category_config` list — parallax category ids are independent of video category ids.
- `wallpaper_config["<categoryId>"]` entries: `{ categoryId, id, isDark, isPremium, image, background, depth }` (absolute URLs to the 3 files in `3d/<category>/<n>/`). No separate thumb.
- Parallax `id` = `100000 + categoryId * 1000 + n` (e.g. 101001 = `3d/anime/1`) so it never collides with video ids.
