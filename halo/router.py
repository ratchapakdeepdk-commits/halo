"""Cheap keyword router for shell use (`halo auto`). Deliberately crude: it runs before any
model is called, so it must cost nothing. Inside a frontier agent the agent itself decides
what to delegate; this is only for people typing at a terminal."""
import re

# Work that needs hands (edit files, run commands) -> frontier agent.
AGENT_WORDS = (
    "fix edit refactor implement debug deploy commit push merge install create write "
    "แก้ไฟล์ แก้โค้ด แก้บั๊ก เขียนโค้ด เขียนไฟล์ สร้างไฟล์ รันให้ ติดตั้ง ทดสอบให้ เทสให้ เพิ่มฟีเจอร์"
).split()
# Judgement-heavy work -> frontier agent.
HARD_WORDS = (
    "why prove design architecture compare trade-off tradeoff decide recommend analyze analyse "
    "ช่วยคิด ทำไม พิสูจน์ ออกแบบ วางแผน เปรียบเทียบ วิเคราะห์ ข้อดีข้อเสีย ตัดสินใจ สถาปัตยกรรม"
).split()
PATH_RE = re.compile(r"(?:^|\s)(?:\./|~/|/)\S+|\.(?:py|js|ts|sh|toml|json|md|rs|go|c|cpp)\b")
WORD_RE = re.compile(r"[a-z][a-z\-]*")


def route(prompt: str, has_input: bool = False) -> tuple[str, str]:
    """Return ("local"|"frontier", reason)."""
    if has_input:
        return "local", "has file/stdin input — digest locally so it never enters frontier context"
    low = prompt.lower()
    words = set(WORD_RE.findall(low))
    for w in AGENT_WORDS:
        if (w in words) if w.isascii() else (w in low):
            return "frontier", f"needs tools ('{w}')"
    if PATH_RE.search(prompt):
        return "frontier", "refers to a file path"
    for w in HARD_WORDS:
        if (w in words) if w.isascii() else (w in low):
            return "frontier", f"needs judgement ('{w}')"
    if len(prompt) > 300:
        return "frontier", f"long prompt ({len(prompt)} chars)"
    return "local", "short, self-contained question"
