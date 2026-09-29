# One shared password for every device in the fleet

**Type:** security · **Priority:** high · **Area:** bench tooling + deployed fleet
**Raised:** 2026-09-14, from the kela-fob-03 site survey
**Related:** `eyalm/no-hardcoded-passwords` (the code half, done)

## What the problem is

Every device the bench provisions gets the *same* admin password. One value
across OTD500s, RUTM08s, TSW202s and PLANET switches; a second value across
Raythink cameras. Not per site, not per device — per *product family*, fleet
wide.

That makes one disclosure a fleet compromise. There is no blast radius to
contain, no way to revoke access to a single unit, and no way to answer "which
devices could this person reach" with anything other than "all of them".

It also makes rotation so expensive that it never happens: changing the
password means touching every deployed device *and* every bench station in one
coordinated operation. Which is why the value in use today is the value from
the first commit that introduced it.

## How it was found, and why it is not theoretical

The shared values were committed in plaintext — `Kelasys123!` in 29 files
(including source defaults in `bench_core`, four tool CLIs, and the
rugged-operator ISO builder) and `Kelafield123!` in 4. Both were removed from
the working tree in `eyalm/no-hardcoded-passwords`, but they remain in git
history across years of commits on `main` and ~15 branches, so history
rewriting is not a realistic remediation.

Confirmed live during the survey, not inferred:

- **Both cameras at kela-fob-03** were read using `Kelafield123!` taken
  straight out of the committed `raythink.config.example.json`.
- **The router at kela-fob-03** authenticated on `Kelasys123!` on both SSH
  (root) and the REST API (admin).

So the committed value is the working credential on deployed production
hardware. Anyone with repo access, now or historically, has admin on the fleet.

A separate observation that sharpens it: the TSW202 at `192.168.88.118` at that
site *refused* the shared password and still carries its factory DHCP hostname,
meaning it was never provisioned. So the fleet is not even uniformly on the
shared password — there is no single answer to "what is the credential for this
device", which is its own operational problem.

## What to do instead

Generate a password per device at provisioning time and store it in a secret
manager. Concretely:

1. **Generate on the fly.** The bench already changes the password as a
   pipeline step; it should mint a random one per unit instead of applying a
   constant. No shared value to leak, and a compromise is scoped to one device.
2. **Store it keyed by serial.** Vault or Bitwarden Secrets Manager, whichever
   ops prefers. Stations authenticate with a per-station token, so a lost
   station token is revocable on its own.
3. **Reuse the retention path that already exists.** `bench_core`'s factory
   password store (TEC-845) already ships per-unit passwords to bench-central
   keyed by serial, for exactly this reason — a factory-reset device has to stay
   reachable. Provisioned passwords are the same shape and can follow the same
   queue.
4. **Give operators a read path.** Whatever replaces the shared password, an
   operator standing in front of a device needs its credential in one step, or
   the shared password will grow back informally.

## Scope and ordering

- The code half is done: no bench tool can supply a password from source any
  more. It resolves from the station's gitignored config or
  `$KELA_NEW_PASSWORD` and refuses otherwise.
- **Rotation of already-deployed devices is the unfinished half, and nothing
  above fixes it.** It needs an owner, a maintenance window, and a decision on
  whether to rotate straight to per-device passwords (preferable — rotating
  once to another shared value means doing this twice).
- Retrofitting per-device passwords onto already-deployed units is the same
  operation as rotating them, so these should be one piece of work rather than
  two.

## Worth doing alongside

- A secret scanner (`gitleaks` or `detect-secrets`) in CI. Cheap, and it stops
  the next one at the commit rather than at a site survey.
- Audit the other Magos sites: the two radars at kela-fob-03 are still on the
  *vendor* default (`admin`/`password`), which is a different instance of the
  same class of problem and is not addressed by any of the above.
