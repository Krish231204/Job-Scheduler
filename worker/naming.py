"""Human-readable worker names.

`os.getpid()` is always 1 for the main process inside a Docker container,
so naming workers "worker-{pid}" made every single one "worker-1" -- useless
for telling them apart on the Workers page. Generate a short adjective-noun
name instead (same idea as Docker's own container naming), which stays
readable in a table and is distinct enough in practice that collisions
don't matter -- the numeric `id` column is still the real unique key.
"""
import random

_ADJECTIVES = [
    "swift", "steady", "quiet", "brisk", "keen", "nimble", "sturdy", "calm",
    "eager", "bold", "tidy", "sharp", "quick", "solid",
]
_NOUNS = [
    "falcon", "otter", "heron", "badger", "lynx", "sparrow", "beetle",
    "marten", "wren", "gecko", "puma", "raven", "hare",
]


def generate_worker_name() -> str:
    return f"worker-{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
