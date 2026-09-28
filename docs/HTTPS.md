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
