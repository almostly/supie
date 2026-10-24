# Performance and reliability

supie runs your code on one of two Python runtimes. MicroPython is the default. These figures come from hosted Supabase in eu-west-1, where nearly every request got a fresh worker:

| | bundle | cold boot | cold request, end to end | memory |
|---|---|---|---|---|
| MicroPython | 0.2 MB | 19 ms | 0.18 s | 31 MB |
| Pyodide, from a snapshot | 12 MB | 300 ms | 0.74 s | 103 MB |

Handling a request costs about the same as TypeScript. Heavy computation does not: a Python loop runs 45 to 170 times slower than the same loop in TypeScript, and hosted functions get 2 s of CPU per request. supie fits request logic, validation, scoring and glue code. Batch work belongs on a container service.

The [prototype](../prototype/) has the full measurements.

## Reliability

A one-hour soak on hosted Supabase (eu-west-1, MicroPython, 2026-10-22), from a client in Europe: steady traffic of one inline scoring request and one queued application per second, a burst of 30 of each every 5 minutes, and one queued application in ten sent twice as a client retry.

![An hour of traffic replayed in under a minute: requests flow through the edge function and the queue, and every application ends decided once or in the dead letters](https://github.com/almostly/supie/releases/download/0.1.0/supie-reliability-replay.gif)

| | requests | succeeded | p50 | p90 | p99 |
|---|---|---|---|---|---|
| inline scoring | 3,916 | 99.95% | 0.31 s | 2.2 s | 3.8 s |
| queued application | 4,258 | 99.77% | 0.53 s | 2.8 s | 4.3 s |

- **No server errors.** All 12 failures were connection-level (a reset or an early end of the TLS stream), none an HTTP 5xx from the function.
- **Nothing lost, nothing doubled.** Every one of 3,916 applications was accepted after at most three client retries, and each ended exactly once: 3,837 decided once, 79 in the dead letters as designed. The 334 duplicate sends and 10 retries added no messages.
- **The tail is in the network.** One request in ten took over 2 s, and a few over 10 s, in steady traffic outside the bursts. The client opened a new connection per request, and an inline score and a queued send made in the same second were slow together far more often than chance, though separate workers handled them. A later probe from the same client of Supabase Auth's health endpoint, with no edge function involved, showed the same tail, all of it in the TCP handshake: p90 1.45 s to connect, against about 0.1 s for the server to answer. Reuse connections where you can, and use timeouts and retries, which the queue's send keys make safe.
