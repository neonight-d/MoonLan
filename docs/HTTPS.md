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
