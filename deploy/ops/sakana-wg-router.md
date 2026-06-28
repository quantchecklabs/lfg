# Sakana WireGuard Egress Router

Sakana's API is returning a generic Google edge `403` from this Hetzner Germany
host before normal API auth. This setup routes only `api.sakana.ai` through a
small US VPS via WireGuard, leaving all other traffic unchanged.

## VPS Choice

Use a US VPS that is not this current Hetzner Germany host. A small shared CPU
instance is enough.

Recommended first try: DigitalOcean NYC/SFO, Akamai/Linode Newark/Dallas, or
Vultr Seattle/Chicago/New Jersey. Hetzner Ashburn/Hillsboro may work, but if
Sakana blocks by provider ASN rather than country, another provider is safer.

## Install

Create an Ubuntu/Debian VPS in the US and make sure UDP `51820` is open.

Then from this repo:

```bash
chmod +x deploy/ops/sakana-wg-router.sh
deploy/ops/sakana-wg-router.sh install root@YOUR_US_VPS_IP
deploy/ops/sakana-wg-router.sh test
```

The script:

- installs WireGuard on the VPS and this box
- enables IPv4 forwarding and NAT on the VPS
- creates `/etc/wireguard/wg-sakana.conf` on both sides
- starts `wg-quick@wg-sakana`
- adds a systemd timer that refreshes the route for `api.sakana.ai`
- routes only Sakana API IPs through the tunnel

## Rollback

```bash
deploy/ops/sakana-wg-router.sh down
```

To fully remove local files:

```bash
sudo rm -f /etc/wireguard/wg-sakana.conf
sudo rm -f /usr/local/sbin/wg-sakana-routes
sudo rm -f /etc/systemd/system/wg-sakana-routes.service
sudo rm -f /etc/systemd/system/wg-sakana-routes.timer
sudo systemctl daemon-reload
```
