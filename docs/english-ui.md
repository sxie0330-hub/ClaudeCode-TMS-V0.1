# English interface

Users switch with **EN / 中文** at the top right (also on the sign-in page). The choice is a cookie (`tms_lang`), kept for a year.

How it works: pages are written in Chinese. When the cookie says `en`, `core.i18n.LanguageMiddleware` activates Django's
English and translates the HTML (and JSON) on the way out, using `core/data/i18n_en.json`:

- `exact`: Chinese string → English.
- `patterns`: Chinese with `{}` where a value is filled in at runtime; English may use `{0}`, `{1}` to reorder. Captured values
  are translated too.
- Text not in the dictionary (mostly what people typed in Chinese) is left exactly as written.
- `<script>`, `<style>`, `<pre>` and the contents of `<textarea>` are never touched.

## After adding or changing Chinese text in the code

1. `python3 scripts/i18n_extract.py . /tmp/strings.json` lists every Chinese string the code can show.
2. Translate the new ones (keep `{}` placeholders) and add them, or add hand-written entries to `core/data/i18n_extra.json`.
3. Rebuild `core/data/i18n_en.json` with `scripts/i18n_merge.py` (see the script header), or edit it directly.

Pages with long sentences split by template logic can use `{% if en %}…{% else %}…{% endif %}` (`en` is in every template context).
