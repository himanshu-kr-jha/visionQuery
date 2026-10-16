# Watches are answered from memory, not from raw frames

Alerts come from Watches evaluated against the same Tracks, Visits and Areas that Questions use, not from a separate live rule pipeline over frames.
This costs a second or two of latency after a Track updates, and buys a single perception pipeline whose output serves both Questions and Alerts, so the two can never drift apart.
