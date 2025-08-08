# Window TinyLFU in python
# https://arxiv.org/pdf/1512.00727 - about TinyLFU cache Admission Policy
# https://highscalability.com/design-of-a-modern-cache/ - about window TinyLFU
# https://github.com/ben-manes/caffeine/wiki/Design#eviction-policy

from .windowed_tiny_lfu import WindowedTinyLFU, WindowedTinyLFUTTL

__all__ = ["WindowedTinyLFU", "WindowedTinyLFUTTL"]
