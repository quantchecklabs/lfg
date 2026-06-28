# Render

Render can run the shared Docker image via `render.yaml`. Treat this as a demo
or private-network deployment unless you add authentication in front of `lfg`.

## Deploy

Publish the bundled release first:

```bash
scripts/release.sh v0.1.0
```

Then:

1. Create a new Blueprint from this repository.
2. Render will read `render.yaml`.
3. Add optional secrets such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
   `ELEVENLABS_API_KEY`.
4. If you need private access, pair it with Render's Tailscale subnet router
   template and put the Tailscale auth key on that router service.

The service mounts `/data` for `lfg` data and scanned repos.

The Dockerfile installs the published bundled release, which is required while
the Vibes SDK path is not live for source installs.
