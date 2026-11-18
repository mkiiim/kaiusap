"""Generated handles: `<adjective>-<noun>-<nnnn>`, such as `delicate-haze-4521`, from the word lists below.

A generated name is never one the store has seen: not a handle ever bound (active or released), not one offered
to a session, and not one suggested to a session. Taking a stopped session's handle with `iam` would take its
name from it, so the generator never hands one out."""
import random

from .model import PERSONAS, handles

# Lowercase letters only, at most nine each, so every name fits the handle rule (2-32 characters).
ADJECTIVES = (
    "amber", "ancient", "autumn", "billowing", "bitter", "black", "blue", "bold", "brave", "bright",
    "brisk", "broad", "calm", "clever", "cold", "cool", "crimson", "curly", "damp", "dark",
    "dappled", "deep", "delicate", "dry", "dusty", "eager", "early", "empty", "falling", "fancy",
    "fierce", "floral", "fragrant", "frosty", "gentle", "gifted", "golden", "green", "hidden", "hollow",
    "icy", "jolly", "keen", "late", "lingering", "little", "lively", "long", "lucky", "misty",
    "mellow", "muddy", "nameless", "noble", "odd", "old", "orange", "patient", "plain", "polished",
    "proud", "purple", "quick", "quiet", "rapid", "red", "restless", "rough", "round", "royal",
    "shiny", "shrill", "shy", "silent", "small", "snowy", "soft", "solitary", "sparkling", "spring",
    "square", "steep", "still", "summer", "sunny", "super", "sweet", "swift", "tender", "tiny",
    "twilight", "wandering", "warm", "weathered", "white", "wild", "winter", "wispy", "withered", "young",
)
NOUNS = (
    "bird", "breeze", "brook", "bush", "butterfly", "cherry", "cloud", "darkness", "dawn", "dew",
    "dream", "dust", "feather", "field", "fire", "firefly", "flower", "fog", "forest", "frog",
    "frost", "glade", "glitter", "grass", "hall", "hat", "haze", "heart", "hill", "king",
    "lake", "leaf", "light", "meadow", "moon", "morning", "mountain", "mouse", "night", "paper",
    "pine", "pond", "queen", "rain", "resonance", "river", "sea", "shadow", "shape", "silence",
    "sky", "smoke", "snow", "snowflake", "sound", "star", "sun", "sunset", "surf", "thunder",
    "fern", "tree", "truth", "aspen", "birch", "violet", "voice", "water", "waterfall", "wave",
    "clover", "wind", "wood", "otter", "heron", "maple", "cedar", "willow", "harbor", "canyon",
    "comet", "ember", "garnet", "lantern", "marsh", "orchid", "pebble", "prairie", "quartz", "reef",
    "ridge", "sparrow", "spruce", "summit", "thistle", "tide", "valley", "whisper", "wren", "zephyr",
)

TRIES = 1000


def generate(unavailable=(), rng=None):
    """A name not in `unavailable`. Raises LookupError if none turns up in `TRIES` draws, which with
    about 90 million names means the set is nearly full."""
    rng = rng or random.SystemRandom()
    unavailable = set(unavailable)
    for _ in range(TRIES):
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}-{rng.randint(1000, 9999)}"
        if name not in unavailable:
            return name
    raise LookupError("no free generated name")


def unavailable(store):
    """Every name the store has seen as a handle: bound in `handles.jsonl` (active or released, and any it
    released along the way), and offered or suggested in `sessions/personas.jsonl`."""
    names = set(handles(store))
    for line in store.rows("handles.jsonl"):
        for field in ("handle", "also_released"):
            if isinstance(line.get(field), str):
                names.add(line[field])
    for row in store.rows(PERSONAS):
        for field in ("offered", "suggested", "handle"):
            if isinstance(row.get(field), str):
                names.add(row[field])
    return names


def free(store, rng=None):
    """A generated name the store has never seen as a handle."""
    return generate(unavailable(store), rng)
