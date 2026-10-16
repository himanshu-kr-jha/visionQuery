# Sized for 16 to 64 cameras on one box

A Deployment targets 16 to 64 cameras on one or two GPUs, and is developed on a single 24 GB GPU.
There is no message broker (no Kafka, NATS or Redis streams), no distributed vector store and no multi-node orchestration.
Workers lease their cameras so they could spread across machines later, but nothing is built for that until a real Deployment needs it.
