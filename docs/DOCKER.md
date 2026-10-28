# MoonLan in Docker

An image of MoonLan, built from this repository for `linux/amd64` and
`linux/arm64` on every release tag:

| Registry | Image |
|---|---|
| Docker Hub | `neonight/moonlan` |
| GitHub Container Registry | `ghcr.io/neonight-d/moonlan` |

Both carry the same image under the same tags: `0.8.3` (one release),
`0.8` (the latest 0.8.x), `latest` (the latest release). A pre-release
such as `0.9.0-rc1` has its own tag only. Take the second registry when
the first does not answer from your network or turns you away — Docker
Hub limits anonymous pulls, and some networks cannot reach it at all.

## The demo, in one command

~~~bash
docker run --rm -p 8080:8080 -e MOONLAN_DEMO=1 neonight/moonlan
~~~

Open `http://<this machine>:8080/`. A virtual network, nothing polled,
nothing kept after the container goes.

## Running it for real: docker compose

[`docs/deploy/docker-compose.yml`](deploy/docker-compose.yml) is the
example. In a folder of its own:

~~~bash
mkdir -p config data
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/neonight-d/MoonLan/main/docs/deploy/docker-compose.yml
curl -fsSLo config/config.yaml https://raw.githubusercontent.com/neonight-d/MoonLan/main/config.example.yaml
# set the switch addresses and the SNMP community in config/config.yaml
sudo chown -R 1000:1000 data
docker compose up -d
~~~

The network is Docker's ordinary bridge: SNMP to the switches and the
pings go out through NAT, from the address of the machine, so a switch
that answers SNMP to that machine answers the container. `network_mode:
host` is not needed.

### The two volumes

| In the container | What is there |
|---|---|
| `/config` | `config.yaml` (`MOONLAN_CONFIG=/config/config.yaml`), and `tls/` for HTTPS |
| `/data` | the working directory: the database (`db_path: moonlan.db`, relative, lands here) — Settings and personal notifications included — `oui/` — the IEEE register, the console token |

The container runs as `moonlan`, uid and gid 1000: `config/` must be
readable by it and `data/` writable — hence the `chown` above.

Without `config.yaml` the service starts on its defaults, polls nothing,
and says in its log and on the empty map where the file goes:
`/config/config.yaml`. The example to copy is in the image too,
`/app/config.example.yaml`. The container does not copy it into your
volume by itself.

## The console: `docker exec`

The working directory and `MOONLAN_CONFIG` are the container's, so the
commands are the ones of [OPERATIONS.md](OPERATIONS.md) after
`docker exec`:

~~~bash
# the first administrator — sign-in switches on at once
docker exec -it moonlan python -m moonlan.users add <name> --role admin

# the IEEE register of MAC blocks: who made a device (into /data/oui)
docker exec moonlan python -m moonlan.oui update

# diagnostics
docker exec moonlan python -m moonlan.diag --stp
docker exec moonlan python -m moonlan.diag --config
~~~

## HTTPS

As anywhere else ([HTTPS.md](HTTPS.md)): put the certificate and the key
in `config/tls/` and name them in `config/config.yaml`:

~~~yaml
listen:
  port: 8443
  public_url: https://moonlan.example.local:8443
  tls_cert: /config/tls/fullchain.pem
  tls_key: /config/tls/privkey.pem
~~~

and publish that port instead (`"8443:8443"`). The key must be readable
by uid 1000 and by nobody else.

## Health

`docker ps` shows `healthy` once `GET /api/health` answers: the image's
`HEALTHCHECK` asks it with `python -m moonlan.healthcheck`, over http or
https as `config.yaml` says, the service's own certificate accepted.

## Updating

~~~bash
docker compose pull && docker compose up -d
~~~

The database is migrated by the new version as it starts, as with
`git pull` and a restart. `docker stop` sends SIGTERM and the service
closes the database before it exits; `init: true` in the example passes
the signal on.

## Moving an installation from systemd to Docker

1. `sudo systemctl stop moonlan` — a clean stop leaves the database
   whole, its write-ahead log folded in.
2. Copy `config.yaml` into `config/`. Paths in it are the container's
   now: `db_path: moonlan.db` (or leave it out), `tls_cert` and `tls_key`
   under `/config/tls/`, `oui.path` left out.
3. Copy the database (`moonlan.db`, and `moonlan.db-wal` / `-shm` if they
   are there) and `oui/` into `data/`; `sudo chown -R 1000:1000 data`.
   The accounts, the layout, the names and notes and the journal are all
   in that one file.
4. `docker compose up -d`. The same map, names and pins; About shows the
   version and the build of the image.
5. `sudo systemctl disable moonlan` once you are sure.

## Ping in rootless Docker and Podman

MoonLan pings with the system `ping` as an ordinary user. Docker allows
that by default: it opens ICMP sockets to every group of a container
(`net.ipv4.ping_group_range`). Rootless Docker and Podman may not; if
devices never turn green and the node menu's ping says nothing answers,
add one of these to the service in `docker-compose.yml`:

~~~yaml
    sysctls:
      net.ipv4.ping_group_range: "0 2147483647"
~~~

or

~~~yaml
    cap_add:
      - NET_RAW
~~~

`traceroute` sends UDP and needs neither.

## Building the image yourself

~~~bash
docker build --build-arg MOONLAN_BUILD=$(git rev-parse --short HEAD) -t moonlan .
~~~

`MOONLAN_BUILD` is the build About shows: an image has no `.git` to read
it from. Every push to `main` builds and runs the image in CI
(`.github/workflows/docker.yml`, `.github/docker-check.sh`); a release
tag publishes it.
