def recipe_display_title(name):
 """Clean importer-only title artifacts without changing stored recipe payloads."""
 title=str(name or '').strip()
 title=re.sub(r'^\*{2,}\s*ADD\s+CORRECT\b[^*]*\*{2,}\s*','',title,flags=re.I)
 title=re.sub(r'^R\d{1,4}\s+(?=[A-Za-z])','',title,flags=re.I)
 title=re.sub(r'\s*\(\s*RCode\s*\)\s*$','',title,flags=re.I)
 title=re.sub(r'\s+Includes\s+recyclable\s+aluminum\s+tray\s*$','',title,flags=re.I)
 title=re.sub(r'^["“]([^"”]+)["”](?=\s|$)',r'\1',title)
 return title.strip()

