"""HW6 P2: count tokens in data/shop_handbook.md with tiktoken and estimate handbook-only input cost."""
import json
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parent.parent
text = (ROOT / "data" / "shop_handbook.md").read_text(encoding="utf-8")
prices = json.loads((ROOT / "data" / "prices.json").read_text(encoding="utf-8"))["gpt-6-luna"]

counts = {name: len(tiktoken.get_encoding(name).encode(text)) for name in ("o200k_base", "cl100k_base")}
tokens = counts["o200k_base"]

uncached_1 = tokens / 1_000_000 * prices["input_per_1m"]
cached_1 = tokens / 1_000_000 * prices["cached_input_per_1m"]
write_1 = tokens / 1_000_000 * prices["cache_write_per_1m"]

print(json.dumps({
    "chars": len(text),
    "tiktoken_version": tiktoken.__version__,
    "counts": counts,
    "prices": prices,
    "uncached_1": uncached_1,
    "uncached_1000": uncached_1 * 1000,
    "cached_1": cached_1,
    "cached_1000": cached_1 * 1000,
    "cache_write_1": write_1,
    "write_then_999_cached": write_1 + cached_1 * 999,
}, indent=2))
