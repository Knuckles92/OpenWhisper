# Backup and restore

Open **Settings → Backup & restore**, under **Models & storage**. Backups are local files; creating or restoring one does not require internet access.

## Create a backup

Choose **Create backup…** and save the `.owbackup` file to a local folder or removable drive. **Include saved recordings** is enabled by default. The page shows the last backup's date, location, and size, and reports failures without publishing an incomplete archive.

Backups include the SQLite database, application settings, and, when selected, locally retained dictation and meeting recordings. Text-only backups preserve database content but remove audio references; restoring one replaces existing local recordings too. Bring records kept only on a paired host back to this computer before backing up if you need them included.

Speech models, downloaded runtimes, temporary remote caches, API keys, device pairing credentials, and private host keys are excluded. Restored settings disable sharing and MCP services, select local record storage, and require devices to be paired again. Credentials already stored in the destination computer's operating-system keyring remain there.

Archives contain private transcripts and may contain recordings. They are **not encrypted**; choose a private destination. A removable drive or a different disk also protects against loss of the computer's main disk.

The app waits for recording, transcription, and meeting processing to finish before starting. It copies on a worker, pauses local record transfers, waits for active incoming host record requests to finish, and verifies file checksums. While the host is backing up, new record requests receive a retryable busy response. Clients keep their local copies and resume after the pause; the pause does not use up a record's failure retry limit. If an active request cannot finish within 30 seconds, backup stops and transfers resume. SQLite's *online backup API* creates a consistent database snapshot while the app is open. Here, “online” means the database can remain open; it does not mean uploading anything.

## Restore

1. Choose **Choose backup…** and inspect its date, app version, size, recording status, and any warnings.
2. Choose **Restore and restart** and confirm replacement of local data.
3. OpenWhisper validates and stages the contents, closes its data writers, then restarts. It applies the restore before opening settings or SQLite.

Restore replaces local settings, history, meeting data, and recordings with the archive's contents. This is a replacement, not a merge. Current owned data is retained under `.openwhisper-restore/previous-<date>-<id>` in the application data directory; its location appears on the backup page after restart. The recovery copy is kept separately from automatic backup retention.

After restoring a host, pair each client again, then open **Settings → Remote engine → Paired computers → Recover stored records…** on the host. Choose the old computer's record group and its new pairing, then choose **Recover records**. This grants that pairing access to the old records alongside anything it has already saved under its new pairing. Computer names alone never grant access. Repeat for each old record group that belongs to that client. The client can then refresh History or Past Meetings, or bring the records back. Removing the pairing and keeping its records makes those groups available for recovery again.

Other OpenWhisper windows and History API processes using this data directory must be closed before the restart can apply a restore. If another process is still running, startup explains the conflict and leaves the restore pending. Close the other process and start OpenWhisper again.

An interrupted file swap is journaled so the next start can recover before opening the database. Validation or staging failure leaves current data in place. If restart itself fails, start OpenWhisper manually; the staged restore remains pending.

## Automatic backups

Automatic backups are **Off** by default. Choose **Daily** or **Weekly**, an existing destination folder, and how many automatic archives to retain, then save those settings. Automatic backups include recordings and run only while OpenWhisper is open and idle. Missed backups wait until the app is available; a disconnected drive does not cause a fallback to a different location.

The calendar marks the latest backup with a solid dot and planned automatic backups with an outlined dot. Select a date to see its status, use the arrows to browse months, or choose **Today** to return to the current date. Planned dates follow the saved schedule; save any frequency changes with **Save schedule**. Dates can shift when the app is closed or busy. The calendar shows the latest saved backup, rather than a full archive history.

Retention deletes only archives this installation recorded as successful automatic backups in that destination. It does not delete manual backups or unrelated files. A failed backup never causes retention to run.

## Verification

The backup tests use temporary data directories. Coverage includes portable restore, text-only restore, checksums and unsafe archive paths, cancellation, interrupted swaps, exclusive startup leases, activity guards, record-transfer pauses, automatic retention, and both desktop Settings styles. Packaged restart and removable-drive behavior still need release testing on each supported operating system.
