# Ad Management — Usage Guide

## Quick Start

1. **Enable the plugin** — enabled by default; manage it in Admin > Plugins.
2. **Create a zone** — Admin > AI & Content > Ad Management > Zones. For example `homepage-banner`, size 320x120.
3. **Create a placement** — bind it to a zone, choose the type (image / code), and set targeting, schedule, weight and frequency caps.
4. **Embed the ad** — use the `render_ads.html` macro in your page template, or the front-end `ads.js` async rendering.
5. **Verify** — open the page and confirm the impression is counted under Stats.

## Stats

- Real-time impression / click counters plus daily statistics.
- Click details are sampled and stored with a hashed IP (privacy-safe) for click-fraud review.

## Settings

- **default_width** — 320 — default ad width in pixels.
- **default_height** — 0 — `0` means auto height.
- **max_placements** — 50 — maximum number of placements.

## Tips

- Deleting a zone is rejected while placements still reference it.
- Impression reporting is rate-limited to 60 req/min/IP, click reporting to 30 req/min/IP.
- AI agents can manage placements through the `ads/*` tool interfaces.
