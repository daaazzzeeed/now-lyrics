"""Подмножество шрифта в woff2. Короткоживущий подпроцесс, чтобы fontTools не жил в памяти сервера.

    python -m lyrics_tv.font_worker SRC.otf OUT.woff2 CHARS.txt
"""
import os
import sys


def main(src: str, out: str, chars_file: str) -> None:
    from fontTools import subset

    with open(chars_file, encoding="utf-8") as f:
        chars = f.read()
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]       # вертикальные формы, лигатуры и пр. — пусть остаются
    opts.name_IDs = [0, 1, 2, 3, 4, 5, 6]
    opts.notdef_outline = True
    font = subset.load_font(src, opts)
    sub = subset.Subsetter(opts)
    sub.populate(text=chars)
    sub.subset(font)
    tmp = out + ".part"
    subset.save_font(font, tmp, opts)
    os.replace(tmp, out)


if __name__ == "__main__":
    main(*sys.argv[1:4])
