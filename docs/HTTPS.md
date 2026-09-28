# HTTPS and the public address

Since v0.7.6 MoonLan can serve HTTPS itself or stand behind a reverse
proxy that does, and people can sign in with a hardware key (a
passkey). Both start from one setting: the address people open the map
at.

## The public address

~~~yaml
listen:
  public_url: https://example.local:8443
~~~

A scheme and a host, a port if it is not the default one — no path.
From it MoonLan takes:

- the origin it checks changing requests and passkey responses
  against — the one it holds to be right, not the one a request says;
- the passkey's relying-party id: the host name, without the port;
- whether the connection is meant to be protected.

**It has to be a name, not an IP address.** A passkey is bound to a
host name; with `https://10.0.0.5:8443` signing in with a key is off,
and the log says why. Unset, everything works as it did before v0.7.6
and signing in with a key is off.

A map opened at another address than this one — by IP, by another
name — still works with a password and TOTP. Signing in with a key is
then greyed out, with a link to the right address.

## MoonLan serving HTTPS itself

### A certificate from your own authority (mkcert)

Browsers accept a certificate only from an authority they trust, and
signing in with a key refuses a page with a certificate error even
after "proceed anyway". On a LAN the practical authority is your own:
[mkcert](https://github.com/FiloSottile/mkcert), or the internal CA of
a Windows domain. With mkcert, on any machine:

~~~bash
mkcert -install                        # creates the root (once)
mkcert example.local                   # example.local.pem + example.local-key.pem
mkcert -CAROOT                         # where rootCA.pem is
~~~

Copy the two files to the MoonLan machine, readable by the service's
user only:

~~~bash
sudo install -o moonlan -m 600 example.local-key.pem /opt/moonlan/tls/
sudo install -o moonlan -m 644 example.local.pem /opt/moonlan/tls/
~~~

~~~yaml
listen:
  host: 0.0.0.0
  port: 8443
  public_url: https://example.local:8443
  tls_cert: /opt/moonlan/tls/example.local.pem
  tls_key: /opt/moonlan/tls/example.local-key.pem
  http_redirect_port: 8080          # optional: http:// -> https://
~~~

`python run.py` checks both files before it starts: a missing file, a
file it cannot read or a key that does not belong to the certificate is
one line and an exit. At startup the log says whom the certificate
names and until when, warns if it does not name the host of
`public_url` (browsers would refuse it), if it ends within 30 days, and
if the key is readable by anybody but its owner.

**The end of a certificate.** mkcert issues certificates for about two
years. Two weeks before the end the `tls_cert_expiring` alarm is raised
(checked once a day). The certificate is read at startup: replace the
files **and restart MoonLan**; the alarm clears at the start of a
process serving one that is not about to end.

**`listen.http_redirect_port`** answers every request with a redirect
to the same path at `public_url` — somebody who types `http://` out of
habit lands on the page where signing in with a key works. It is a
temporary redirect on purpose: a permanent one is cached by the browser
and would outlive HTTPS if it is ever switched off.

### Trusting the root on the machines that open the map

The root is `rootCA.pem` from `mkcert -CAROOT` (or your domain CA).
Install it on every machine where the map is opened — administrators
at least, since they are the ones signing in with keys.

- **Windows**, one machine (an administrator's command prompt):

  ~~~text
  certutil -addstore root rootCA.pem
  ~~~

  In a domain: a Group Policy — Computer Configuration → Policies →
  Windows Settings → Security Settings → Public Key Policies → Trusted
  Root Certification Authorities → Import. Edge and Chrome use the
  Windows store.
- **Linux** (Debian, Ubuntu):

  ~~~bash
  sudo cp rootCA.pem /usr/local/share/ca-certificates/moonlan-root.crt
  sudo update-ca-certificates
  ~~~

  Chrome and Chromium on Linux keep their own store as well; add it
  there with `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n moonlan -i rootCA.pem`
  (package `libnss3-tools`), or `mkcert -install` does both.
- **Firefox** has its own store on every system: Settings → Privacy &
  Security → Certificates → View Certificates → Authorities → Import,
  or `security.enterprise_roots.enabled = true` to make it trust the
  system's roots (the default on Windows since Firefox 120).

## Behind a reverse proxy (nginx, Caddy)

The proxy holds the TLS; MoonLan speaks plain HTTP to it on the local
machine. Without `listen.trusted_proxies` this breaks two things:

- every changing request (a pin, a cleared alarm, a password) is
  refused with `bad_origin`: the browser sends
  `Origin: https://example.local`, and MoonLan sees the request arrive
  over `http`;
- the delay and the lock after wrong passwords are kept per client
  address, and every client is the proxy: five wrong passwords from
  one person delay everybody.

~~~yaml
listen:
  host: 127.0.0.1          # only the proxy may reach MoonLan
  port: 8080
  public_url: https://example.local
  trusted_proxies: [127.0.0.1]
~~~

`run.py` hands the list to uvicorn, which then takes the client's
address from `X-Forwarded-For` and the scheme from `X-Forwarded-Proto`
— from those addresses only. From anybody else the headers are
ignored; with the list empty they are ignored altogether.

**Listen on 127.0.0.1.** A MoonLan listening on `0.0.0.0` behind a proxy
can be reached around the proxy, over plain HTTP.

### nginx

~~~nginx
server {
    listen 443 ssl;
    server_name example.local;
    ssl_certificate     /etc/nginx/tls/example.local.pem;
    ssl_certificate_key /etc/nginx/tls/example.local-key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name example.local;
    return 307 https://$host$request_uri;
}
~~~

### Caddy

~~~text
example.local {
    tls /etc/caddy/tls/example.local.pem /etc/caddy/tls/example.local-key.pem
    reverse_proxy 127.0.0.1:8080
}
~~~

Caddy sends `X-Forwarded-For` and `X-Forwarded-Proto` and keeps `Host`
by itself.

## What changes once the map is on HTTPS

- **The session cookie** becomes `__Host-moonlan_session`, with
  `Secure`: the browser sends it over HTTPS only, and takes it only for
  this exact host — a neighbouring subdomain cannot plant one. A
  session opened over plain HTTP is not carried over: everybody signs
  in once more after the switch.
- **No password over plain HTTP.** With `listen.public_url` on https,
  a password (signing in, changing it, binding TOTP) that arrives over
  plain HTTP is refused, and the form links to the protected address.
  Whoever opened the service's port directly would otherwise go on
  sending the password in the clear although HTTPS is there. A map
  opened by an address with a session already in hand keeps working;
  without `public_url`, plain HTTP works exactly as before.
- **The header** stops saying the connection is not protected.
- **The log** says at startup how the map reaches people: HTTPS served
  by MoonLan, HTTPS at a proxy, or plain HTTP. An https `public_url`
  with neither `tls_cert` nor `trusted_proxies` is called out: every
  request would arrive as plain HTTP and every password be refused.

### HSTS — off unless asked for

~~~yaml
listen:
  hsts_max_age: 31536000   # a year; 0 (the default) — not sent
~~~

`Strict-Transport-Security` makes a browser that has seen it refuse
plain `http://` to this host name for `max-age` seconds — it stops the
one plain request a typed `http://` would make. It is off by default
because it cannot be taken back: if the certificate is ever let lapse,
or HTTPS switched off, a browser that remembers it cannot open the map
at all — not even with a warning to click through — until the time
runs out. Switch it on once HTTPS has run for a while and the renewal
of the certificate is in somebody's calendar; start with a short age
(`600`) and raise it later. It is sent on HTTPS answers only.
