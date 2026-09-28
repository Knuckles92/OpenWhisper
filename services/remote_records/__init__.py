"""Where a paired computer keeps its records: here, on its host, or both.

``host_store`` is the host side: it takes a device's uploads, verifies and
imports them into this computer's own history and meetings, and serves them
back. ``kinds`` turns each kind of record (dictation, meeting) into files and
back. ``sync`` is the client side: an outbox that copies or moves records to
the paired host once they are saved here, and brings them back on request.
"""
