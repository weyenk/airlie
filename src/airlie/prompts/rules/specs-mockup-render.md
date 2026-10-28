---
kind: rule
roles: [design]
fields: [max_chars]
governs:
  specs.mockup.page: one-self-contained-page
  specs.mockup.network: none
  specs.mockup.script: none
  specs.mockup.reply: one-html-fence
why: |
  What design is told to draw one mockup option (D4, D13 of the specs phase, P4, 2026-10-25). The page is
  served to the app and shown to a person, never executed by the line: one self-contained HTML file with
  inline CSS only, no script, no link, no import, nothing fetched, images only as data: URIs. The line
  rejects a page that breaks this (`check_page`) and sends it back once with the findings. One page per
  call because design's max_tokens holds one page, not three; the size cap is {max_chars} characters.
---
Draw ONE page for the direction below: a static mockup a person looks at, not a working app. It must be a single self-contained HTML document with a <style> element holding all the CSS. No <script>, no <link>, no @import, no <iframe>, no event handler attributes, no URL of any kind (not even a font or an icon); an image may be a data: URI. Use realistic sample content, not lorem ipsum. Keep the whole page under {max_chars} characters. Reply with the page in one ```html fence and nothing else of length.
