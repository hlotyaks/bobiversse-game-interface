# Joining the Enshrouded server (new player guide)

Access is private: the game is only reachable to people invited onto our Tailscale network.
There is no public server and no port on the open internet. These steps do **not** give you
access to the host's desktop, SSH, or home network — only the game.

## What you'll do

1. **Get your invite.** The server admin will email you a Tailscale invite to the private
   network. Open it and accept using an account **you own** (Google, Microsoft, GitHub, or
   email). Do not use or ask for the admin's login.
2. **Install Tailscale.** Download from <https://tailscale.com/download> for your computer,
   install with the default options, then open it and **Log in** with the account that
   accepted the invite. (On Windows, restart if it hangs on first sign-in.)
3. **Send your device name for approval.** In the Tailscale app, copy the device name it
   shows and send it to the admin. Confirm the app shows the network name and says
   **Connected**. Wait for the admin to say your device is approved.
4. **Connect in Enshrouded.** Launch the game → **Play** → **Add Server** (server list), then:
   - Address: `100.84.161.38:15636`  (or `bobiverse.tail40344b.ts.net:15636` if names work for you)
   - Password: `<server password — the admin shares this privately>`
   Leave Tailscale running in the background while you play.

If it doesn't connect: make sure the Tailscale app says **Connected**, then send the admin a
screenshot of the app showing your device name and status. Never send passwords, recovery
codes, or keys.

## Copy-paste message to send a new player

> You're invited to our private Enshrouded server. Access is over Tailscale (a private
> network) — nothing is exposed to the public internet.
> 1. I'll email you a Tailscale invite. Open it and accept with your own account.
> 2. Install Tailscale from https://tailscale.com/download, then open it and log in with that
>    same account. (Windows: restart if the first login hangs.)
> 3. Send me the device name the Tailscale app shows, and wait for me to approve it.
> 4. In Enshrouded: Play → Add Server → address `100.84.161.38:15636`, password `<server password — the admin shares this privately>`.
>    Keep Tailscale connected while you play.

## Admin checklist (server owner)

Do these before telling a new player they're ready:

- [ ] **The game is running.** A friend can reach the tailnet immediately, but can only join
      once the Enshrouded instance is provisioned and started (see
      [instance-provisioning.md](instance-provisioning.md)).
- [ ] **Send the invite** from the Tailscale admin console (Machines/Users → invite external
      user) to the player's email.
- [ ] **Approve their device** in the admin console once they report its name.
- [ ] **Scope their access (recommended).** Use an ACL/grant so the player's device can reach
      only UDP `15636` on `bobiverse` — not SSH (TCP 22) or any other host/port. See
      [tailscale-policy.example.hujson](tailscale-policy.example.hujson) and
      [tailscale-publishing.md](tailscale-publishing.md).
- [ ] **Share the connect details** (`100.84.161.38:15636`, password `<server password — the admin shares this privately>`) privately —
      only with invited players. The password is the same for everyone; the tailnet membership
      is the real gate, and anyone who joins with it has in-game Admin rights.
- [ ] **Removing a player:** revoke their device/account in the Tailscale admin console; access
      stops immediately.
