# Sample diagnostic output

Every file here is the output of one `python -m moonlan.diag` report,
run against a live network **through `--anonymize`** and never edited
afterwards. They are here for two reasons: so the shape of each report
is visible without access to switches, and so there is something to
compare a bug report against.

Addresses come from RFC 5737 (`198.51.100.0/24`), MAC addresses from
RFC 7042 (`00:00:5e:00:53:xx`), and names are the ones the flag
assigns (`switch-N`, `host-NN`). All seven files were produced in one
run, so a device carries the same replacement in every one of them and
they read as a single network.

| File | Command |
|------|---------|
| `config.txt` | `diag --config` |
| `topology.txt` | `diag --topology` |
| `hosts.txt` | `diag --hosts` |
| `stp.txt` | `diag --stp` |
| `loop.txt` | `diag --loop` |
| `port.txt` | `diag --port <switch>` |
| `walk.txt` | `diag --walk <switch> <oid>` |

**Attaching one of these to an issue:** run the same command with
`--anonymize`. Without it, a `diag` report is a complete map of your
network — every address, every MAC address, every host name, and the
port labels somebody typed into the switches. `moonlan/anonymize.py`
documents exactly what the flag rewrites and the one thing it cannot:
a bare word in a port description is not recognisable as a name, so
skim the output before you send it.
