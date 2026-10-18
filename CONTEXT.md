# VisionQuery

A closed, single-owner system that watches every camera an owner has and answers questions about what those cameras saw in the past.

## Language

**Deployment**:
One owner's complete, self-contained installation: its cameras, its footage, and everything derived from them.
Every question is asked of exactly one Deployment and may draw on any camera in it.
_Avoid_: tenant, customer, account, instance

**Owner**:
The single party a Deployment belongs to, such as a company or a household.
There is exactly one per Deployment, so ownership is never a field on anything inside it.
_Avoid_: customer, tenant, organization

**Operator**:
A named person who uses a Deployment on the Owner's behalf, as either a viewer or an investigator.
Viewers ask and follow; only investigators make Assertions.
_Avoid_: user, account, guard, admin

**Assertion**:
Something an investigator declares true, which the Deployment never overrides: a confirmed or rejected Link, an Adjacency, or a Case.
Always records which Operator made it and when.
_Avoid_: override, manual input, annotation, label

**Withdrawal**:
An investigator taking back an Assertion, their own or another's.
Recorded alongside the Assertion it withdraws, never erasing it.
_Avoid_: undo, delete, revert, retract

**Visual Data**:
Anything from which what a camera saw could be reconstructed or recognized: frames, Footage, crops, and embeddings derived from them.
Never leaves its Deployment, whatever else is allowed to.
_Avoid_: media, imagery, sensitive data

**Reasoner**:
The language model that turns a Question into searches over the Deployment's memory and turns what those searches find into an Answer.
Local by default; the Owner may opt into a remote one, which then sees text and structured facts but never Visual Data.
_Avoid_: LLM, agent, brain, assistant

## Memory

**Footage**:
The recorded video from a camera, the only thing that can never be regenerated once lost.
Kept on a rolling budget: when space runs out, the oldest Footage not in a Case goes first.
_Avoid_: recording, video, archive, clip (a clip is a span of Footage)

**Keyframe**:
A still crop of a person chosen from a Track to represent how they looked.
Outlives the Footage it came from, so a Track can still be shown after its Footage has expired.
_Avoid_: thumbnail, snapshot, crop, best shot

**Description**:
A model's account, in words, of what happens in a span of Footage, made only when a Question needs it and kept for reuse.
An interpretation, never an observation: always presented as what the model says, and replaceable by a better model's account.
_Avoid_: caption, summary, narration, scene description

**Case**:
An investigator's saved investigation: the Anchors, Visits, spans of Footage and notes they chose to keep together.
Everything in a Case is exempt from expiry until the Case is released.
_Avoid_: incident, investigation, bookmark, folder, evidence

## Place

**Camera**:
What films for a Deployment, known by a name unique within it.
Either pulled, reached by the Deployment at its URL, or pushed, publishing its video to the Deployment at a path of its own.
A pulled Camera's URL is secret in its user, password, path and query, because cameras put their credentials in any of them; only its scheme, host and port are shown.
A pushed Camera publishes with credentials of its own, a user and a password chosen for it, of which the Deployment keeps only a hash, and which let it publish to its path and nowhere else.
A removed Camera's name is free for another, but its Footage and Tracks still say which Camera saw them.
_Avoid_: stream, channel, feed, device, source

**Area**:
A named place the Owner refers to, such as "the loading dock" or "gate 2", seen by one or more cameras.
How a Question's "where" is turned into which cameras to look at.
_Avoid_: zone, region, location, scope

**Adjacency**:
A path a person can walk between two cameras' views without passing any other camera, with the range of times that walk takes.
Two cameras whose views overlap are adjacent with a walking time of zero.
Drawn roughly by an investigator, then refined from confirmed Links; the Deployment may propose new ones, which an investigator accepts or declines.
_Avoid_: edge, connection, transition, neighbour, route

## Asking

**Question**:
Something an Operator asks the Deployment about the past, in their own words.
_Avoid_: query, prompt, search, request

**Answer**:
The Deployment's reply to a Question: statements, each resting on Evidence the memory holds, together with the Gaps that limit them.
A statement with nothing to rest on is not part of an Answer.
_Avoid_: response, result, reply, summary

**Evidence**:
What a statement in an Answer rests on: a Track, a Link, a Description, or a span of Footage.
Always something the Operator can open and check.
_Avoid_: source, citation, proof, reference

**Gap**:
A stretch of place and time the Deployment could not have seen: a camera offline, Footage expired, an Area with no camera, or a person lost between cameras.
Stated in every Answer it bears on, so that "nothing happened" is never confused with "nothing was seen".
_Avoid_: blind spot, outage, missing data, coverage hole

**Candidate**:
A Track offered as a possible answer to "which one did you mean?", found from a description, a photo, or similarity to another Track.
A suggestion only; nothing is followed from a Candidate.
_Avoid_: match, result, hit, suggestion

**Anchor**:
The Track an Operator has confirmed is the person they mean.
Following always starts from an Anchor, whether it was clicked in Footage directly or chosen from Candidates.
_Avoid_: seed, query track, target, selection

**Watch**:
A standing Question the Owner wants answered whenever it becomes true, such as "anyone in the server room after 10pm".
Asked of the same memory as every other Question, never of raw frames.
_Avoid_: rule, alert rule, trigger, monitor, use case

**Alert**:
One occasion on which a Watch became true, pointing at the Tracks and Footage that made it so.
_Avoid_: alarm, event, notification, incident

## Identity

**Track**:
One person continuously seen by one camera, from when that camera first sees them to when it loses them.
Part of the memory from its first moment: while the camera still sees the person the Track is open and keeps growing, and once lost it is closed.
An observation, never revised: if the camera lost someone and picked them up again, that is two Tracks.
_Avoid_: tracklet, detection, trajectory, object

**Kind**:
What a Track is of, such as a person or a vehicle.
Every Track has exactly one; the first version follows only people.
_Avoid_: class, category, type, label

**Visit**:
The Deployment's current belief that a set of Tracks, across cameras, are one person during one continuous stay.
Anonymous, known only by appearance, and revisable: Tracks may be linked into it, unlinked from it, or two Visits merged, without any Track changing.
_Avoid_: journey, trip, path, global track, identity

**Link**:
A belief about whether two Tracks are the same person; Visits are made of the positive ones.
Inferred by the Deployment (only when confident), or confirmed or rejected by an investigator as an Assertion.
Assertions are final to the Deployment: inference never re-creates a rejected Link nor splits a confirmed one; only a Withdrawal undoes them.
_Avoid_: match, association, edge, hop

**Rejection**:
A Link an investigator has declared false: these two Tracks are not the same person.
As much a fact as a confirmation, and kept like one.
_Avoid_: unlink, negative match, veto

**Contradiction**:
Asserted Links that cannot all be true at once, such as A and B confirmed, B and C confirmed, but A and C rejected.
Surfaced to investigators to settle; never resolved by the Deployment.
_Avoid_: conflict, inconsistency, error

**Following**:
Extending outward from an Anchor's Visit, across cameras and forward and backward in time, to find Tracks the confident Links missed.
What it finds are proposed Links until an investigator confirms them.
_Avoid_: tracing, tracking, pursuit, search

**Person**:
A durable identity that holds across days, attached to Visits when there is evidence strong enough to carry identity beyond clothing and appearance.
Not part of the first version; Visits never depend on one existing.
_Avoid_: subject, individual, identity, profile
