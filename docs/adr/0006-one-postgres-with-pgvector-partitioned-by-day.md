# One Postgres, with pgvector, partitioned by day

All memory (Tracks, Keyframe embeddings, Links, Assertions, Gaps, Cases) lives in one PostgreSQL database with TimescaleDB hypertables and pgvector, partitioned by day with a vector index per partition.
Every search is bounded by time before similarity, so per-day indexes stay small, and retention is dropping partitions.
A dedicated vector database was rejected because keeping two stores consistent through inserts, expiry, Case holds and Withdrawals would let Evidence point at things the other store had already lost.
Embedding search sits behind one narrow module so a Deployment that outgrows pgvector can swap it out without a rewrite.
