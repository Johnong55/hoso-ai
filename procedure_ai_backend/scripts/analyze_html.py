# scripts/analyze_html.py
import sys
sys.stdout.reconfigure(encoding='utf-8')
from bs4 import BeautifulSoup

# ── Procedure page ─────────────────────────────────────────
print("\n" + "="*60)
print("PROCEDURE PAGE ANALYSIS")
print("="*60)

with open('scripts/debug_procedure.html', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

print("\n=== H1 tags ===")
for el in soup.find_all('h1'):
    print(f"  class={el.get('class')} text={el.get_text(strip=True)[:120]!r}")

print("\n=== H2 tags ===")
for el in soup.find_all('h2'):
    print(f"  class={el.get('class')} text={el.get_text(strip=True)[:80]!r}")

print("\n=== H3 tags ===")
for el in soup.find_all('h3'):
    print(f"  class={el.get('class')} text={el.get_text(strip=True)[:80]!r}")

print("\n=== Elements with tthc/procedure/detail/title in class ===")
seen = set()
for el in soup.find_all(class_=True):
    classes = ' '.join(el.get('class', []))
    if any(k in classes for k in ['tthc', 'procedure', 'detail', 'title']):
        text = el.get_text(strip=True)[:80]
        key = (el.name, classes)
        if text and key not in seen:
            seen.add(key)
            print(f"  <{el.name} class={classes!r}> {text!r}")

print("\n=== Tables (first row) ===")
for i, table in enumerate(soup.find_all('table')):
    first_row = table.find('tr')
    if first_row:
        row_text = first_row.get_text(' ', strip=True)[:100]
        print(f"  Table[{i}] class={table.get('class')} row={row_text!r}")

print("\n=== Text containing key labels ===")
keywords = ['Linh vuc', 'Co quan', 'Thoi han', 'Le phi', 'Trinh tu',
            'Thanh phan', 'Can cu phap', 'Ket qua',
            'Lĩnh vực', 'Cơ quan', 'Thời hạn', 'Lệ phí']
for el in soup.find_all(string=True):
    text = el.strip()
    if any(k.lower() in text.lower() for k in keywords) and len(text) < 60:
        parent = el.parent
        gp = parent.parent if parent else None
        if parent and parent.name not in ['script', 'style']:
            print(f"  {text!r}")
            print(f"    parent: <{parent.name} class={parent.get('class')}>")
            if gp:
                print(f"    gp:     <{gp.name} class={gp.get('class')}>")
