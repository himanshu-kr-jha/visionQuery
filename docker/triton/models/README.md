# Triton's model repository

Mounted read-only into Triton at `/models` by the `gpu` profile of `compose.yaml`.
It holds no models yet: the person detector, ReID and CLIP models are chosen in the M2a spike and added in M2, each as a directory of its own (`<model>/config.pbtxt` and `<model>/<version>/`).
Weights are never committed (see `.gitignore`).
