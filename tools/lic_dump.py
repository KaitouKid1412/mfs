"""Throwaway inspection tool for LIC factsheet PDF."""
import pdfplumber, logging, sys, re
logging.getLogger('pdfminer').setLevel(logging.ERROR)

PDF = 'data/raw/factsheets/lic/2026-04.pdf'

def dump_first_lines():
    with pdfplumber.open(PDF) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            first = lines[0] if lines else ''
            print(f'p{i+1}: {first[:120]}')

def dump_pages(indices):
    with pdfplumber.open(PDF) as pdf:
        for i in indices:
            if i >= len(pdf.pages):
                continue
            text = pdf.pages[i].extract_text() or ''
            print(f'\n========== PAGE {i+1} ==========')
            print(text[:3000])

def search_term(term):
    with pdfplumber.open(PDF) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            if term.lower() in text.lower():
                # find surrounding context
                idx = text.lower().find(term.lower())
                start = max(0, idx - 80)
                end = min(len(text), idx + 200)
                print(f'p{i+1}: ...{text[start:end]}...')

def dump_last_lines(indices):
    with pdfplumber.open(PDF) as pdf:
        for i in indices:
            text = pdf.pages[i].extract_text() or ''
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            print(f'--- page {i+1} (last 15 lines) ---')
            for ln in lines[-15:]:
                print(repr(ln[:150]))
            print()


def dump_words(page_idx):
    with pdfplumber.open(PDF) as pdf:
        page = pdf.pages[page_idx]
        words = page.extract_words(use_text_flow=True)
        print(f'--- page {page_idx+1}: {len(words)} words, w={page.width:.1f} h={page.height:.1f} ---')
        for w in words[:200]:
            print(f"  x0={w['x0']:.1f} top={w['top']:.1f} '{w['text']}'")


def dump_top_words(indices, max_top=80):
    with pdfplumber.open(PDF) as pdf:
        for i in indices:
            page = pdf.pages[i]
            words = page.extract_words(use_text_flow=True)
            print(f'--- page {i+1} top words (top<{max_top}) ---')
            for w in words:
                if w['top'] < max_top:
                    print(f"  x0={w['x0']:.1f} top={w['top']:.1f} '{w['text']}'")
            print()


def dump_chars_in_box(idx, x0=0, x1=612, y0=0, y1=864):
    with pdfplumber.open(PDF) as pdf:
        page = pdf.pages[idx]
        chars = [c for c in page.chars if x0 <= c['x0'] <= x1 and y0 <= c['top'] <= y1]
        chars.sort(key=lambda c: (c['top'], c['x0']))
        print(f'--- page {idx+1}: {len(chars)} chars in box ---')
        for c in chars:
            print(f"  x0={c['x0']:.1f} top={c['top']:.1f} '{c.get('text','?')}' size={c.get('size','?')}")


def parse_toc():
    with pdfplumber.open(PDF) as pdf:
        text = pdf.pages[2].extract_text() or ''
        TOC_RE = re.compile(
            r'^\s*(\d+)\.\s+(LIC\s+MF\s+.+?)\s+\.+\s+(\d+)\s*$',
            re.MULTILINE,
        )
        for m in TOC_RE.finditer(text):
            print(f'sr={m.group(1)} name="{m.group(2)}" page={m.group(3)}')


def find_licmf_in_pages(start, end):
    with pdfplumber.open(PDF) as pdf:
        for i in range(start, end):
            text = pdf.pages[i].extract_text() or ''
            matches = re.findall(r'LIC\s*MF\s+[A-Za-z][\w\s&\-,\.]{2,60}?(?:Fund|FOF|ETF|Saver)', text, re.IGNORECASE)
            uniq = []
            for m in matches:
                if m not in uniq:
                    uniq.append(m)
            if uniq:
                print(f'p{i+1}: {uniq[:5]}')
            else:
                print(f'p{i+1}: NONE')


def find_big_fonts(idx):
    with pdfplumber.open(PDF) as pdf:
        page = pdf.pages[idx]
        # Find big sized chars
        by_size = {}
        for c in page.chars:
            s = round(c.get('size', 0), 1)
            by_size.setdefault(s, []).append(c)
        for s in sorted(by_size.keys(), reverse=True)[:5]:
            print(f'\nfont size {s}: {len(by_size[s])} chars')
            txt = ''.join(c.get('text', '?') for c in by_size[s])
            print(f'  text: {txt[:300]!r}')


def dump_topchars(idx, max_top=80):
    with pdfplumber.open(PDF) as pdf:
        page = pdf.pages[idx]
        chars = [c for c in page.chars if c['top'] < max_top]
        chars.sort(key=lambda c: (c['top'], c['x0']))
        print(f'--- page {idx+1}: {len(chars)} chars with top<{max_top} ---')
        for c in chars:
            print(f"  x0={c['x0']:.1f} top={c['top']:.1f} '{c.get('text','?')}' font={c.get('fontname','')}")


def dump_images(indices):
    with pdfplumber.open(PDF) as pdf:
        for i in indices:
            page = pdf.pages[i]
            print(f'--- page {i+1}: w={page.width:.1f} h={page.height:.1f} ---')
            print(f'  images: {len(page.images)}')
            for im in page.images[:10]:
                print(f"    x0={im['x0']:.1f} top={im['top']:.1f} x1={im['x1']:.1f} bottom={im['bottom']:.1f} w={im['width']:.1f} h={im['height']:.1f}")
            print(f'  chars: {len(page.chars)}')
            # show chars at top of page
            top_chars = [c for c in page.chars if c['top'] < 80]
            print(f'  top chars: {len(top_chars)}')


def find_turnover():
    with pdfplumber.open(PDF) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            if re.search(r'turnover|ptr', text, re.IGNORECASE):
                idx = re.search(r'turnover|ptr', text, re.IGNORECASE).start()
                print(f'\np{i+1}: ...{text[max(0,idx-100):idx+300]}...')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'first'
    if cmd == 'first':
        dump_first_lines()
    elif cmd == 'pages':
        dump_pages([int(x) - 1 for x in sys.argv[2:]])
    elif cmd == 'last':
        dump_last_lines([int(x) - 1 for x in sys.argv[2:]])
    elif cmd == 'words':
        dump_words(int(sys.argv[2]) - 1)
    elif cmd == 'topwords':
        dump_top_words([int(x) - 1 for x in sys.argv[2:]])
    elif cmd == 'images':
        dump_images([int(x) - 1 for x in sys.argv[2:]])
    elif cmd == 'topchars':
        dump_topchars(int(sys.argv[2]) - 1, max_top=int(sys.argv[3]) if len(sys.argv) > 3 else 80)
    elif cmd == 'bigfont':
        find_big_fonts(int(sys.argv[2]) - 1)
    elif cmd == 'toc':
        parse_toc()
    elif cmd == 'find_licmf':
        s = int(sys.argv[2]) - 1
        e = int(sys.argv[3])
        find_licmf_in_pages(s, e)
    elif cmd == 'search':
        search_term(sys.argv[2])
    elif cmd == 'turnover':
        find_turnover()
