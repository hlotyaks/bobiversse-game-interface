# Game build updates: a stale build that restarting does not fix

On 2026-10-21 players reported that Enshrouded "needs updating" and that restarting the server did
not help. Both halves were true, for two independent reasons. The server was healthy the whole
time — `HostOnline`, world loaded, ports bound — just serving a build that updated clients refused
to join.

- Installed build: `25988414`, written 2026-10-16 23:44 UTC by the image migration.
- Latest `public` build: `26083076`, published 2026-10-21 ~09:51 UTC.

## Fault 1: steamcmd silently reports "already up to date"

The image checks for updates itself, against `api.steamcmd.net`, and it got the right answer:

    enshrouded-updater INFO - Enshrouded server needs to be updated

It then ran steamcmd, which installed nothing and said:

    Success! App '2278520' already up to date.

This is a race in steamcmd, not in the image's version check. `app_update` can return "already up
to date" before the appinfo request for the app has finished, which only happens when the appinfo
cache is cold. `/home/enshrouded` is not persisted, so the cache is cold on **every** container
start. Upstream [#262](https://github.com/mornedhels/enshrouded-server/issues/262) carries the
`appinfo_log.txt` evidence: `UpdatesJob: apps still needs updates, run again` logged in the same
second `app_update` returned success.

Reproduced here against the pinned digest with a copy of the production `appmanifest_2278520.acf`
in an otherwise empty install directory. Across five runs the outcome was **nondeterministic** —
some runs reported "already up to date", others began a real update — which is what a race looks
like and is the reason a restart sometimes works. The image's oddly quoted
`app_update 2278520 "public public validate"` argument is malformed (the branch and `validate`
arrive as one positional token, so `validate` never applies) but it is *not* the cause: both the
quoted and unquoted forms produced both outcomes.

Two things make the failure silent rather than loud:

- steamcmd exits **0**, so `downloadEnshrouded` counts as a success and the retry path in
  `doUpdate` — remove `steamapps`, download again — never runs.
- The server is then started again on the build it was already running, so nothing in the log
  looks like a failure.

`/opt/enshrouded/current_version` does record the build actually installed, so the next start
re-checks and tries again. That makes every start a fresh coin flip. `UPDATE_CRON` is unset on this
host, so there is no attempt *between* restarts, which is why restarting looked like it never
worked rather than like it worked sometimes.

### Remedy

`enshrouded-force-update` removes the version file and the Steam manifests, so steamcmd cannot
conclude anything from local state and has to treat the install as new:

    docker exec game-enshrouded-primary /usr/local/etc/enshrouded/enshrouded-force-update

It is a supervisord-level operation: the container keeps running, so the systemd unit is untouched.
Check the server is empty first — the image's own player check is broken on this image
([#260](https://github.com/mornedhels/enshrouded-server/issues/260): `python-a2s` 1.3.0 fails to
import on Python 3.14), so it will *not* protect a connected player.

It removes `steamapps`, which is where the Proton prefix lives, so the next start logs
`Proton files are not available!` and regenerates it. That is expected, not a second fault.

Verify the result rather than trusting the log:

    docker exec game-enshrouded-primary grep -m1 buildid \
      /opt/enshrouded/server/steamapps/appmanifest_2278520.acf
    curl -s https://api.steamcmd.net/v1/info/2278520 \
      | jq -r '.data."2278520".depots.branches.public.buildid'

## Fault 2: supervisord could not stop the game, because we dropped CAP_KILL

The forced update installed `26083076` correctly — and then could not restart the server onto it:

    CRIT unknown problem killing enshrouded-server (144):
    PermissionError: [Errno 1] Operation not permitted

Both game images keep supervisord as root and run the game as an unprivileged uid (`10000` here).
The kernel allows a signal across that uid boundary only with `CAP_KILL`, and the capability set
added back after `cap_drop: [ALL]` did not include it:

    CapEff: 00000000000000cb    # CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID

Confirmed directly, with signal 0 so nothing is delivered — root inside the container, against the
game's own pid:

    kill: Operation not permitted

So `supervisorctl stop enshrouded-server` had never been able to work. The consequences:

- The updater's stop → update → start sequence cannot complete. It updated the files on disk and
  left the **old** process serving, then failed to start because the program had parked in
  `UNKNOWN` state.
- `docker compose down` degrades to a SIGKILL. supervisord gets the SIGTERM, cannot pass it on, and
  Docker kills the container after `stop_grace_period` — so every stop was an unclean world save.

**Valheim had the same hole**, silently, since its capability set is the same pattern. It had been
running 3 weeks with no graceful shutdown available.

The cause was in how the set was chosen: it was derived by reading what the image's `bootstrap`
needs to *start* — `groupmod`/`usermod`, `chown -R` — and verified by watching a server come up.
Nothing in that exercise exercised a stop. `CAP_KILL` is now in `cap_add` for both templates, with
tests that assert it for each and assert that nothing broader (`SYS_ADMIN`, `privileged`) was
reached for instead. `CAP_KILL` only reaches processes in the container's own PID namespace.

## Deliberately not done

- **`UPDATE_CRON`.** It would retry the coin flip periodically, but `doUpdate` stops the server
  whenever it believes an update is needed, and the player check that is supposed to prevent that
  is broken (#260). On this host that trades a stale build for players being dropped mid-session.
- **Persisting `/home/enshrouded`** to keep the appinfo cache warm. It would narrow the race, but
  that path also holds the baked `.steam/sdk32`/`sdk64` libraries the image links at build time;
  mounting over it risks the Steamworks initialisation that
  [enshrouded-1.0-outage.md](enshrouded-1.0-outage.md) was about. Not worth it to paper over an
  upstream race that `enshrouded-force-update` resolves deterministically.
