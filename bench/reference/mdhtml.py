import re

ESCAPABLE = "\\`*_[]()#"
HEADING = re.compile(r"(#{1,6}) +(.*?)[ #]*$")
HR = re.compile(r"(-{3,}|\*{3,}|_{3,})$")
UL = re.compile(r"[-*+] +(.*)$")
OL = re.compile(r"(\d+)\. +(.*)$")


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def inline(text):
    saved, out, i = [], [], 0

    def keep(html):
        saved.append(html)
        return f"\x00{len(saved) - 1}\x00"

    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in ESCAPABLE:
            out.append(keep(_esc(text[i + 1])))
            i += 2
        elif c == "`" and "`" in text[i + 1:]:
            j = text.index("`", i + 1)
            out.append(keep(f"<code>{_esc(text[i + 1:j])}</code>"))
            i = j + 1
        else:
            out.append(c)
            i += 1
    s = _esc("".join(out))
    s = re.sub(r"\[([^\]]*)\]\(([^)\s]*)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<strong>\2</strong>", s)
    s = re.sub(r"(\*|_)(?=\S)(.+?)(?<=\S)\1", r"<em>\2</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], s)


def _starts_block(line):
    s = line.strip()
    return (s.startswith("```") or s.startswith(">") or HEADING.match(s) or HR.match(s)
            or UL.match(s) or OL.match(s))


def render(md):
    lines = (md[:-1] if md.endswith("\n") else md).split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
        elif s.startswith("```"):
            lang = s[3:].strip()
            body, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_esc(chr(10).join(body))}</code></pre>")
        elif s.startswith(">"):
            inner = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q = lines[i].strip()[1:]
                inner.append(q[1:] if q.startswith(" ") else q)
                i += 1
            out.append(f"<blockquote>\n{render(chr(10).join(inner))}\n</blockquote>")
        elif HEADING.match(s):
            m = HEADING.match(s)
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            i += 1
        elif HR.match(s):
            out.append("<hr>")
            i += 1
        elif UL.match(s) or OL.match(s):
            pat = UL if UL.match(s) else OL
            first = OL.match(s)
            items = []
            while i < len(lines) and lines[i].strip():
                t = lines[i].strip()
                m = pat.match(t)
                if m:
                    items.append(m.groups()[-1])
                elif _starts_block(t):
                    break
                else:
                    items[-1] += " " + t
                i += 1
            tag = "ul" if pat is UL else "ol"
            start = f' start="{int(first.group(1))}"' if first and int(first.group(1)) != 1 else ""
            lis = "\n".join(f"<li>{inline(x)}</li>" for x in items)
            out.append(f"<{tag}{start}>\n{lis}\n</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not (para and _starts_block(lines[i])):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(chr(10).join(para))}</p>")
    return "\n".join(out)
