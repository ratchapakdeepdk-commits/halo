from collections import defaultdict


def select(entries, project=None, tag=None, start=None, end=None) -> list:
    return [e for e in entries
            if (project is None or e.project == project.lower())
            and (tag is None or e.has_tag(tag))
            and (start is None or e.day >= start)
            and (end is None or e.day <= end)]


def _sorted(d: dict) -> dict:
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))


def totals(entries, by: str = "project") -> dict:
    out = defaultdict(int)
    for e in entries:
        for key in (e.tags if by == "tag" else (e.project,)):
            out[key] += e.minutes
    return _sorted(out)


def weekly(entries) -> dict:
    out = defaultdict(lambda: defaultdict(int))
    for e in entries:
        y, w, _ = e.day.isocalendar()
        out[f"{y}-W{w:02d}"][e.project] += e.minutes
    return {k: dict(v) for k, v in sorted(out.items())}
