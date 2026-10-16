# Identity is layered: Track, Visit, Person

A Track is an unrevisable single-camera observation; a Visit is a revisable belief that Tracks across cameras are one person during one stay; a Person is a durable cross-day identity attached to Visits.
The first version builds Tracks and Visits from appearance alone and does not build Persons.
Face recognition and enrolment were deliberately kept out of the base layer: on CCTV, faces are missing exactly where following matters most (backs, distance, low light), so making them the identity key would break Following.

## Consequences

Re-identification can be improved or replaced by changing Links, without touching any Track.
