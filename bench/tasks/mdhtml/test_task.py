import unittest
from mdhtml import render


class T(unittest.TestCase):
    def test_headings_hr(self):
        self.assertEqual(render("# Title\n\n### Sub ###"), "<h1>Title</h1>\n<h3>Sub</h3>")
        self.assertEqual(render("#nope"), "<p>#nope</p>")
        self.assertEqual(render("####### seven"), "<p>####### seven</p>")
        self.assertEqual(render("a\n\n---\n\n***\n\n___"), "<p>a</p>\n<hr>\n<hr>\n<hr>")

    def test_paragraphs(self):
        self.assertEqual(render("one\ntwo  \n\n\nthree\n"), "<p>one\ntwo</p>\n<p>three</p>")
        self.assertEqual(render(""), "")
        self.assertEqual(render("para\n# Head"), "<p>para</p>\n<h1>Head</h1>")

    def test_escape(self):
        self.assertEqual(render('a < b & "c" > d'), "<p>a &lt; b &amp; &quot;c&quot; &gt; d</p>")
        self.assertEqual(render(r"\*not em\* and \\ \# \[x\]"), r"<p>*not em* and \ # [x]</p>")

    def test_inline(self):
        self.assertEqual(render("**b** and __b__ and *i* and _i_"),
                         "<p><strong>b</strong> and <strong>b</strong> and <em>i</em> and <em>i</em></p>")
        self.assertEqual(render("**bold *nested* text**"), "<p><strong>bold <em>nested</em> text</strong></p>")
        self.assertEqual(render("use `a*b*c <x>` here"), "<p>use <code>a*b*c &lt;x&gt;</code> here</p>")
        self.assertEqual(render("2 * 3 = 6 and a lone *star"), "<p>2 * 3 = 6 and a lone *star</p>")
        self.assertEqual(render("see [the **docs**](http://x.io/a?b=1&c=2)"),
                         '<p>see <a href="http://x.io/a?b=1&amp;c=2">the <strong>docs</strong></a></p>')

    def test_code_fence(self):
        md = "```python\nif a < b:\n    print(\"*x*\")\n\n```\nafter"
        self.assertEqual(render(md), '<pre><code class="language-python">if a &lt; b:\n'
                         '    print(&quot;*x*&quot;)\n</code></pre>\n<p>after</p>')
        self.assertEqual(render("```\nx\n"), "<pre><code>x</code></pre>")
        self.assertEqual(render("```\n# not head\n```"), "<pre><code># not head</code></pre>")

    def test_lists(self):
        self.assertEqual(render("- a\n* **b**\n+ c"), "<ul>\n<li>a</li>\n<li><strong>b</strong></li>\n<li>c</li>\n</ul>")
        self.assertEqual(render("1. x\n2. y"), "<ol>\n<li>x</li>\n<li>y</li>\n</ol>")
        self.assertEqual(render("3. x\n4. y"), '<ol start="3">\n<li>x</li>\n<li>y</li>\n</ol>')
        self.assertEqual(render("- one\n  more\n- two\n\ntext"),
                         "<ul>\n<li>one more</li>\n<li>two</li>\n</ul>\n<p>text</p>")
        self.assertEqual(render("- a\n1. b"), "<ul>\n<li>a</li>\n</ul>\n<ol>\n<li>b</li>\n</ol>")
        self.assertEqual(render("-not a list"), "<p>-not a list</p>")

    def test_blockquote(self):
        self.assertEqual(render("> quote *here*\n>\n> - item"),
                         "<blockquote>\n<p>quote <em>here</em></p>\n<ul>\n<li>item</li>\n</ul>\n</blockquote>")
        self.assertEqual(render(">> deep"), "<blockquote>\n<blockquote>\n<p>deep</p>\n</blockquote>\n</blockquote>")

    def test_document(self):
        md = "# Doc\n\nIntro with `code`.\n\n> note\n\n1. first\n2. second\n\n```sh\nls -l\n```"
        self.assertEqual(render(md), "<h1>Doc</h1>\n<p>Intro with <code>code</code>.</p>\n"
                         "<blockquote>\n<p>note</p>\n</blockquote>\n<ol>\n<li>first</li>\n<li>second</li>\n</ol>\n"
                         '<pre><code class="language-sh">ls -l</code></pre>')


if __name__ == "__main__":
    unittest.main()
