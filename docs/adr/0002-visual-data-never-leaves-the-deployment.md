# Visual Data never leaves the Deployment; the Reasoner is pluggable

Frames, Footage, crops and embeddings never leave the Deployment, in any mode, for any feature.
The Reasoner sits behind a pluggable interface and defaults to a model running inside the Deployment.
An Owner may opt into a remote Reasoner, which then receives only Question text and structured facts.
Any future proposal to send Visual Data to a cloud model, including for "better descriptions", is a change to this decision, not an implementation detail.
