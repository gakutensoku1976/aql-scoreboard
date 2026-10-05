"""閲覧画面のURLをQRコードにして、A4縦の中央に大きく配置したPDFを作る。

使い方: uv run --no-project --with reportlab python tools/make_qr_pdf.py [URL] [出力先] [タイトル]
"""

import os
import sys

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

URL = sys.argv[1] if len(sys.argv) > 1 else "https://aql-sokuhou.q-koken.com/score/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "print/score_viewer_qr_a4.pdf"
TITLE = sys.argv[3] if len(sys.argv) > 3 else "QLA2026得点表示"
QR_SIZE = 160 * mm  # 余白（クワイエットゾーン）込みの一辺
# タイトルは BIZ UDPゴシック Bold（TTC の 2 番目が UDP 版）
TITLE_FONT = os.path.expanduser("~/.local/share/fonts/BIZ-UDGothicB.ttc")
pdfmetrics.registerFont(TTFont("BIZUDPGothic-Bold", TITLE_FONT, subfontIndex=1))

page_w, page_h = A4
c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle(TITLE)

qr = QrCodeWidget(URL, barLevel="M")
x0, y0, x1, y1 = qr.getBounds()
d = Drawing(QR_SIZE, QR_SIZE, transform=[QR_SIZE / (x1 - x0), 0, 0, QR_SIZE / (y1 - y0), 0, 0])
d.add(qr)
qr_x = (page_w - QR_SIZE) / 2
qr_y = (page_h - QR_SIZE) / 2
renderPDF.draw(d, c, qr_x, qr_y)

# QRコードの上にタイトル
c.setFont("BIZUDPGothic-Bold", 48)
c.drawCentredString(page_w / 2, qr_y + QR_SIZE + 6 * mm, TITLE)

# QRコードの下にURLを文字でも載せる（読み取れない端末で手入力できるように）
c.setFont("Helvetica", 18)
c.drawCentredString(page_w / 2, qr_y - 4 * mm, URL)
c.save()
print(OUT)
