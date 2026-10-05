from dataclasses import dataclass
from datetime import date, time


@dataclass(frozen=True)
class Entry:
    day: date
    start: time
    end: time
    project: str
    text: str
    tags: tuple = ()

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)

    def has_tag(self, tag: str) -> bool:
        return tag.lower() in self.tags
