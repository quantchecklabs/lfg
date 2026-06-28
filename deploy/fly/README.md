# Fly.io

Fly is a good fit for a private `lfg` deployment because it has persistent
volumes and private networking. The checked-in `fly.toml` intentionally does not
define a public `http_service`; expose the UI through Fly private networking,
WireGuard, Tailscale, or a local proxy rather than publishing an unauthenticated
control plane.

## Deploy

Publish the bundled release first:

```bash
scripts/release.sh v0.1.0
```

Then deploy:

```bash
fly apps create lfg
fly volumes create lfg_data --size 1 --region sjc
fly deploy
```

For local access during testing:

```bash
fly proxy 8766:8766
```

Then open `http://127.0.0.1:8766`.

## Notes

- Set provider keys as Fly secrets, for example `fly secrets set ANTHROPIC_API_KEY=...`.
- The Dockerfile installs the published bundled release, which is required while
  the Vibes SDK path is not live for source installs.
- Authenticate agent CLIs inside the machine if you need CLI-backed sessions.
- Keep public services disabled unless you add authentication in front of `lfg`.
