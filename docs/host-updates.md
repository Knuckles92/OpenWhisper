# Unattended updates for a shared speech host

On Omarchy, an updater running under the desktop user's session can call OpenWhisper's D-Bus controls. Continue fetching on your normal schedule and prepare dependencies outside the running installation. Track the revision running in the service separately from the staged revision so a deferred restart stays pending on the next run. When a revision is ready to activate, reserve the host immediately before switching to it and restarting the service.

```sh
gdbus call --session \
  --dest org.openwhisper.OpenWhisper \
  --object-path /org/openwhisper/OpenWhisper \
  --method org.openwhisper.Control.PrepareUpdate
```

The single string in the reply contains JSON. A `reason` means to keep the update pending and retry on the next run. An unreadable reply or a missing method also means to defer a planned restart. A `token` reserves the host for the number of seconds in `expires_in`. Start the restart within that time. If the update is aborted, release the reservation using `org.openwhisper.Control.CancelUpdate` with the token as its string argument. The reservation expires automatically if the updater exits before restarting.

The reservation checks local capture and processing, paired recordings, and active or queued requests as one admission decision. New capture and speech requests receive a temporary refusal after the reservation is granted. Connected clients that lack the recording hook defer updates because the host cannot see their capture state. A disconnected recording holds the host briefly while the client reconnects.

`org.openwhisper.Control.UpdateStatus` on the same object returns JSON with `engine_ready`, `recordings`, `requests`, `untracked_clients`, and `maintenance` while speech sharing is running. After a restart, poll for `engine_ready` before reporting success. Confirmed service outages can follow the updater's existing recovery path.
