"""
Documents: QR codes, PDF invoices and the data used by emails.
Uses only reportlab (and Pillow, which reportlab already needs).
"""
import io
import os

from reportlab.graphics.barcode import qrencoder
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

INK = colors.HexColor("#12292e")
TEAL = colors.HexColor("#1d5a60")
BRASS = colors.HexColor("#b88a3b")
MIST = colors.HexColor("#eef2f0")
LINE = colors.HexColor("#d5ddda")
GOOD = colors.HexColor("#2f7d4f")


# ------------------------------------------------------------------
# QR codes
# ------------------------------------------------------------------
def _qr(text):
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    return code


def qr_svg(text, size=160):
    """QR code as an SVG string (used on web pages)."""
    code = _qr(text)
    n = code.getModuleCount()
    cells = []
    for r in range(n):
        for c in range(n):
            if code.isDark(r, c):
                cells.append("M{} {}h1v1h-1z".format(c + 4, r + 4))
    total = n + 8  # 4 module quiet zone on every side
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {t} {t}" '
            'width="{s}" height="{s}" role="img" aria-label="QR code">'
            '<rect width="{t}" height="{t}" fill="#fff"/>'
            '<path d="{d}" fill="#12292e"/></svg>').format(
        t=total, s=size, d="".join(cells))


def qr_png(text, scale=6):
    """QR code as PNG bytes (used inside emails)."""
    from PIL import Image as PILImage
    code = _qr(text)
    n = code.getModuleCount()
    total = (n + 8) * scale
    img = PILImage.new("RGB", (total, total), "white")
    pixels = img.load()
    for r in range(n):
        for c in range(n):
            if code.isDark(r, c):
                x0, y0 = (c + 4) * scale, (r + 4) * scale
                for dy in range(scale):
                    for dx in range(scale):
                        pixels[x0 + dx, y0 + dy] = (18, 41, 46)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _qr_flowable(text, size):
    return Image(io.BytesIO(qr_png(text, scale=8)), width=size, height=size)


# ------------------------------------------------------------------
# PDF invoice
# ------------------------------------------------------------------
def _money(value, currency):
    return "{} {:,}".format(currency, value).strip()


def build_invoice_pdf(bill, hotel_name, currency, logo_path=None):
    """Return the invoice as PDF bytes. `bill` comes from app.get_bill()."""
    b = bill["booking"]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm,
                            rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm,
                            title=bill.get("title", "INVOICE").title() + " " + b["booking_id"],
                            author=hotel_name)

    small = ParagraphStyle("small", fontName="Helvetica", fontSize=8.5,
                           textColor=colors.HexColor("#5b6e6b"), leading=11)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5,
                          textColor=INK, leading=13)
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20,
                           textColor=INK, leading=24)
    invoice_word = ParagraphStyle("inv", fontName="Helvetica-Bold",
                                  fontSize=22, textColor=BRASS, leading=26,
                                  alignment=2)
    right = ParagraphStyle("right", parent=body, alignment=2)

    story = []

    # ---- header: logo + hotel name | INVOICE ----
    left_cells = []
    if logo_path and os.path.exists(logo_path):
        try:
            iw, ih = ImageReader(logo_path).getSize()
            height = 16 * mm
            left_cells.append(Image(logo_path, width=height * iw / ih,
                                    height=height))
            left_cells.append(Spacer(1, 2 * mm))
        except Exception:
            pass
    left_cells.append(Paragraph(hotel_name, title))
    right_cells = [Paragraph(bill.get("title", "INVOICE"), invoice_word),
                   Paragraph("No. {}-{}".format(bill.get("prefix", "INV"), b["booking_id"]), right),
                   Paragraph("Date: {}".format(bill["issued"]), right)]
    header = Table([[left_cells, right_cells]], colWidths=[100 * mm, 74 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 4 * mm)]

    rule = Table([[""]], colWidths=[174 * mm], rowHeights=[1.2 * mm])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BRASS)]))
    story += [rule, Spacer(1, 6 * mm)]

    # ---- guest and stay details ----
    guest_lines = ["<b>Billed to</b>", b["name"], b.get("phone") or "",
                   b.get("email") or ""]
    stay_lines = ["<b>Stay details</b>",
                  "Booking: {}".format(b["booking_id"]),
                  "Room {} ({})".format(b["room_no"], b["room_type"]),
                  "{} to {} ({} night{})".format(
                      b["check_in"], b["check_out"], b["nights"],
                      "" if int(b["nights"]) == 1 else "s")]
    info = Table([[Paragraph("<br/>".join(x for x in guest_lines if x), body),
                   Paragraph("<br/>".join(stay_lines), body)]],
                 colWidths=[87 * mm, 87 * mm])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [info, Spacer(1, 7 * mm)]

    # ---- itemised table ----
    rows = [["Description", "Qty", "Unit price", "Amount"]]
    for line in bill["lines"]:
        rows.append([Paragraph(line["description"], body), str(line["qty"]),
                     _money(line["unit"], currency),
                     _money(line["amount"], currency)])
    table = Table(rows, colWidths=[84 * mm, 16 * mm, 36 * mm, 38 * mm],
                  repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
    ]
    for i in range(2, len(rows), 2):
        style.append(("BACKGROUND", (0, i), (-1, i), MIST))
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 5 * mm)]

    # ---- totals ----
    totals = [["Total", _money(bill["total"], currency)],
              ["Paid", _money(bill["paid"], currency)],
              ["Balance due", _money(bill["due"], currency)]]
    for p in bill["payments"]:
        totals.insert(-1, ["  {} ({})".format(p["method"], p["created_at"][:10]),
                           _money(p["amount"], currency)])
    t_style = [("ALIGN", (1, 0), (1, -1), "RIGHT"),
               ("FONTSIZE", (0, 0), (-1, -1), 9.5),
               ("TEXTCOLOR", (0, 0), (-1, -1), INK),
               ("TOPPADDING", (0, 0), (-1, -1), 3),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
               ("FONTSIZE", (0, 0), (-1, 0), 12),
               ("LINEBELOW", (0, 0), (-1, 0), 1, INK),
               ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
               ("LINEABOVE", (0, -1), (-1, -1), 1, INK)]
    tot = Table(totals, colWidths=[44 * mm, 36 * mm])
    tot.setStyle(TableStyle(t_style))

    status_text = "PAID IN FULL" if bill["due"] == 0 else "PAYMENT DUE"
    status_color = GOOD if bill["due"] == 0 else BRASS
    status = Table([[status_text]], colWidths=[44 * mm], rowHeights=[10 * mm])
    status.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, status_color),
        ("TEXTCOLOR", (0, 0), (-1, -1), status_color),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))

    lower = Table([[[status], tot]], colWidths=[94 * mm, 80 * mm])
    lower.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    story += [lower, Spacer(1, 10 * mm)]

    # ---- QR and thank-you ----
    qr_block = [_qr_flowable("HOTEL:" + b["booking_id"], 28 * mm),
                Paragraph("Booking " + b["booking_id"], small)]
    thanks = [Paragraph("Thank you for staying with us.", bold),
              Spacer(1, 1.5 * mm),
              Paragraph("Please keep this invoice for your records. The QR "
                        "code holds your booking number for quick check-in.",
                        small)]
    foot = Table([[qr_block, thanks]], colWidths=[40 * mm, 134 * mm])
    foot.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(foot)

    doc.build(story)
    return buffer.getvalue()
