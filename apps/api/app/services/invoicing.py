"""Renders and stores the immutable PDF invoice for one wallet recharge —
see .claude/specs/21-invoicing.md. generate_invoice_for_transaction is only
ever called from services/payments.py::handle_payment_captured, right after
a wallet credit succeeds; a failure here must never roll back or block that
credit (the caller wraps this in a broad try/except for exactly that
reason), so every failure mode here should be allowed to raise rather than
silently swallowed internally — the caller is what decides not to propagate.
"""
import io
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.user import User
from app.models.wallet_transaction import WalletTransaction
from app.services import billing, profile_service, storage_service

logger = logging.getLogger(__name__)

# Fixed, platform-wide issuer details — not per-user, not versioned via a DB
# table (unlike PricingRate); if these ever change, historical invoices
# still show what was true at issue time because they're snapshotted onto
# the Invoice row at generation time, never re-derived afterward.
DASHR_ISSUER_NAME = "DASHR Material Handling Solutions (OPC) Private Limited"
DASHR_ISSUER_GST_NO = "06AAMCD5859M1ZX"
DASHR_ISSUER_ADDRESS = "1185P, Near Arora Properties, Sector 46, Gurugram, Haryana 122001, India"
SERVICE_DESCRIPTION = "Cardex Recharge - For Visiting Card Parsing,Enrichment and Scoring"

# Placeholder copy pending legal review — confirm the exact wording with
# DASHR before this ships, per .claude/specs/21-invoicing.md.
DEFAULT_TERMS_AND_CONDITIONS = (
    "1. Recharged balance is prepaid and non-refundable/non-withdrawable "
    "from the website; it may only be spent on visiting card parsing, "
    "enrichment, and scoring actions on the DASHR AI platform.\n"
    "2. This invoice is issued on receipt of payment in full — no "
    "further amount is due against it.\n"
    "3. For a refund or withdrawal request, contact DASHR customer "
    "support; there is no self-serve refund flow.\n"
    "4. Subject to Gurugram jurisdiction only."
)

_LOGO_PATH = Path(__file__).resolve().parents[4] / "assets" / "brand" / "dashr-logo.jpg"

# reportlab's base-14 fonts (Helvetica et al.) have no glyph for ₹ (U+20B9)
# — it renders as a missing-glyph box. DejaVu Sans does, and is bundled
# here rather than relied on from the host OS, so PDF rendering doesn't
# depend on which fonts happen to be installed in a given container.
_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_REGULAR = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_fonts_registered = False


def _ensure_fonts_registered() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(_FONTS_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(_FONTS_DIR / "DejaVuSans-Bold.ttf")))
    # Lets Paragraph markup's <b>...</b> resolve to the bold TTF instead of
    # falling back to Helvetica-Bold (which has no ₹ glyph) — reportlab only
    # auto-derives bold/italic faces for its built-in font families.
    pdfmetrics.registerFontFamily(
        _FONT_REGULAR, normal=_FONT_REGULAR, bold=_FONT_BOLD, italic=_FONT_REGULAR, boldItalic=_FONT_BOLD
    )
    _fonts_registered = True


def generate_invoice_for_transaction(db: Session, transaction: WalletTransaction) -> Invoice:
    """Idempotent on wallet_transaction_id: a redelivered webhook for an
    already-invoiced recharge is a silent no-op, matching
    handle_payment_captured's own idempotency on razorpay_order_id. The
    SELECT below is check-then-act, not atomic — the UNIQUE constraint on
    invoices.wallet_transaction_id is the backstop for a genuine race (see
    the IntegrityError handling at the bottom)."""
    existing = db.scalar(
        select(Invoice).where(Invoice.wallet_transaction_id == transaction.wallet_transaction_id)
    )
    if existing is not None:
        logger.info(
            "Invoice already exists for wallet_transaction_id=%s — no-op",
            transaction.wallet_transaction_id,
        )
        return existing

    if transaction.transaction_type != "recharge_credit":
        # Defensive — the only real caller today is handle_payment_captured,
        # but this keeps the invariant enforced in code, not just by
        # convention (CLAUDE.md: never invoice anything but a recharge).
        raise ValueError(
            f"Cannot generate an invoice for transaction_type={transaction.transaction_type!r} "
            f"(wallet_transaction_id={transaction.wallet_transaction_id})"
        )

    user = db.get(User, transaction.user_id)
    profile = profile_service.get_or_empty_profile(db, user)

    # A GST-registered buyer is billed under their registered company name,
    # not the individual user's own name — GST invoices are issued to the
    # registered entity, and its name must match the GSTIN shown alongside
    # it. Falls back to the user's own name if gst_no is set but
    # company_name was never filled in, so bill_to_name is never blank.
    # Without a GST No. on file, the buyer isn't presented as a registered
    # business, so the individual user's own name is used as before.
    bill_to_name = (profile.company_name or user.name or "") if profile.gst_no else (user.name or "")

    taxable_value_inr = transaction.amount_inr
    cgst_amount_inr, sgst_amount_inr, total_inr = billing.compute_gst(taxable_value_inr)

    sequence_value = db.scalar(select(func.nextval("invoice_number_seq")))
    invoice_number = f"DASHR-INV-{sequence_value:06d}"

    # Computed explicitly here (not left to the Invoice model's server_default
    # now()) so the exact same value is used both on the row and in the PDF
    # itself — the two must always agree, and re-reading the row's own
    # issued_at after commit would insert a needless round trip for it.
    issued_at = datetime.now(timezone.utc)

    pdf_bytes = _render_pdf(
        invoice_number=invoice_number,
        issued_at=issued_at,
        taxable_value_inr=taxable_value_inr,
        sac_code=billing.SAC_CODE,
        cgst_rate_percent=billing.CGST_RATE_PERCENT,
        sgst_rate_percent=billing.SGST_RATE_PERCENT,
        cgst_amount_inr=cgst_amount_inr,
        sgst_amount_inr=sgst_amount_inr,
        total_inr=total_inr,
        bill_to_name=bill_to_name,
        bill_to_gst_no=profile.gst_no,
        bill_to_billing_address=profile.billing_address,
        issuer_name=DASHR_ISSUER_NAME,
        issuer_gst_no=DASHR_ISSUER_GST_NO,
        issuer_address=DASHR_ISSUER_ADDRESS,
        service_description=SERVICE_DESCRIPTION,
        terms_and_conditions=DEFAULT_TERMS_AND_CONDITIONS,
    )

    # Upload before any DB write: if S3 is unreachable, nothing is written
    # to `invoices` at all, so a later retry of this same function is a
    # genuine "generate again" rather than a row whose pdf_storage_key
    # points at nothing.
    pdf_storage_key = f"invoices/{user.user_id}/{invoice_number}.pdf"
    storage_service.upload_file(pdf_storage_key, pdf_bytes, "application/pdf")

    invoice = Invoice(
        user_id=user.user_id,
        org_id=user.org_id,
        wallet_transaction_id=transaction.wallet_transaction_id,
        invoice_number=invoice_number,
        sac_code=billing.SAC_CODE,
        taxable_value_inr=taxable_value_inr,
        cgst_rate_percent=billing.CGST_RATE_PERCENT,
        sgst_rate_percent=billing.SGST_RATE_PERCENT,
        cgst_amount_inr=cgst_amount_inr,
        sgst_amount_inr=sgst_amount_inr,
        total_inr=total_inr,
        service_description=SERVICE_DESCRIPTION,
        bill_to_name=bill_to_name,
        bill_to_gst_no=profile.gst_no,
        bill_to_billing_address=profile.billing_address,
        issuer_name=DASHR_ISSUER_NAME,
        issuer_gst_no=DASHR_ISSUER_GST_NO,
        issuer_address=DASHR_ISSUER_ADDRESS,
        terms_and_conditions=DEFAULT_TERMS_AND_CONDITIONS,
        pdf_storage_key=pdf_storage_key,
        issued_at=issued_at,
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent call for the same wallet_transaction
        # (unique constraint) — fall back to the row the other insert just
        # created, mirroring billing._get_or_create_wallet's race handling.
        # The PDF this call uploaded under pdf_storage_key is now orphaned
        # (no row ever points at it) — clean it up rather than leaving it in
        # storage forever under a burned invoice_number.
        db.rollback()
        storage_service.delete_file(pdf_storage_key)
        return db.scalar(
            select(Invoice).where(
                Invoice.wallet_transaction_id == transaction.wallet_transaction_id
            )
        )
    db.refresh(invoice)
    return invoice


def _load_logo_flowable(max_width_mm: float, max_height_mm: float) -> RLImage | None:
    """dashr-logo.jpg is a CMYK JPEG (per assets/brand/README.md) — some
    renderers show shifted colors if fed directly, so it's converted to RGB
    in memory first. Returns None (logo silently omitted) if the file isn't
    present, rather than failing invoice generation over a missing asset."""
    if not _LOGO_PATH.exists():
        logger.warning("Logo not found at %s — invoice will omit it", _LOGO_PATH)
        return None
    with PILImage.open(_LOGO_PATH) as img:
        rgb = img.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG")
        buffer.seek(0)
        width_px, height_px = rgb.size

    max_width_pt = max_width_mm * mm
    max_height_pt = max_height_mm * mm
    scale = min(max_width_pt / width_px, max_height_pt / height_px)
    return RLImage(buffer, width=width_px * scale, height=height_px * scale)


def _render_pdf(
    *,
    invoice_number: str,
    issued_at: datetime,
    taxable_value_inr: Decimal,
    sac_code: str,
    cgst_rate_percent: Decimal,
    sgst_rate_percent: Decimal,
    cgst_amount_inr: Decimal,
    sgst_amount_inr: Decimal,
    total_inr: Decimal,
    bill_to_name: str,
    bill_to_gst_no: str | None,
    bill_to_billing_address: str | None,
    issuer_name: str,
    issuer_gst_no: str,
    issuer_address: str,
    service_description: str,
    terms_and_conditions: str,
) -> bytes:
    """Platypus (SimpleDocTemplate + Table/Paragraph/Image flowables), not
    the low-level canvas API: the layout below is a sequence of discrete
    structured blocks (a two-column header, a meta row, a bill-to block, a
    single-row item table, a totals table, a numbered-list T&C paragraph, a
    footer line) with variable-length content (an address that may wrap to
    2 lines vs. 1, T&C text that could grow) — exactly what Platypus's
    automatic flow handles, instead of hand-computing y-offsets.

    Visual style (outer border box, boxed/shaded section headers, ruled
    dividers) intentionally matches DASHR's existing Zoho-style tax invoice
    template — but content stays what was already decided for this
    prepaid, non-physical-goods receipt: no Bank Details section, no Ship
    To / Place of Supply / Terms / Due Date rows, no IGST line, no Balance
    Due line, no "Authorized Signature" block or scanned signature image,
    no "Powered by Zoho" (or any) footer branding. Do not re-add any of
    these by copying more of the generic reference template than intended.
    """
    _ensure_fonts_registered()

    BORDER_COLOR = colors.HexColor("#999999")
    HEADER_BG = colors.HexColor("#f2f2f2")
    PAGE_MARGIN = 16 * mm
    CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN

    # Named so a column-width tweak is a one-line edit — each is used both
    # in its own table's colWidths and in a sibling column's "whatever's
    # left" remainder calc below, and previously had to be kept in sync by
    # hand in two places.
    HEADER_LOGO_WIDTH = 42 * mm
    HEADER_TITLE_WIDTH = 45 * mm
    ITEM_COL_NUM = 10 * mm
    ITEM_COL_SAC = 20 * mm
    ITEM_COL_QTY = 15 * mm
    ITEM_COL_RATE = 25 * mm
    ITEM_COL_AMOUNT = 24 * mm
    TOTALS_LABEL_WIDTH = 50 * mm
    TOTALS_VALUE_WIDTH = 37 * mm

    styles = getSampleStyleSheet()
    normal = ParagraphStyle("Normal", parent=styles["Normal"], fontName=_FONT_REGULAR, fontSize=9.5, leading=13)
    bold = ParagraphStyle("Bold", parent=normal, fontName=_FONT_BOLD)
    title_style = ParagraphStyle(
        "Title", parent=normal, fontName=_FONT_BOLD, fontSize=22, alignment=2
    )
    section_label = ParagraphStyle("SectionLabel", parent=bold, fontSize=9.5)
    heading = ParagraphStyle("Heading", parent=bold, fontSize=11, spaceBefore=10, spaceAfter=4)
    small_italic = ParagraphStyle("SmallItalic", parent=normal, fontName=_FONT_REGULAR, fontSize=8)

    def _draw_outer_border(canvas, doc_):
        canvas.saveState()
        canvas.setStrokeColor(BORDER_COLOR)
        canvas.setLineWidth(1)
        canvas.rect(
            PAGE_MARGIN - 4 * mm,
            PAGE_MARGIN - 4 * mm,
            A4[0] - 2 * (PAGE_MARGIN - 4 * mm),
            A4[1] - 2 * (PAGE_MARGIN - 4 * mm),
        )
        canvas.restoreState()

    def _boxed(padding: float = 6, extra: list | None = None) -> TableStyle:
        """The BOX/padding boilerplate shared by every boxed section below
        (header, meta, bill-to, subject) — a border-weight or padding
        change only needs to happen here, not in four separate call sites.
        `extra` appends section-specific rules (e.g. a shaded header row,
        an internal divider) on top of the shared base."""
        return TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, BORDER_COLOR),
                ("TOPPADDING", (0, 0), (-1, -1), padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
            + (extra or [])
        )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
    )
    story = []

    # --- Header: logo | issuer block | "INVOICE" title, boxed ---
    logo = _load_logo_flowable(max_width_mm=38, max_height_mm=18)
    issuer_block = Paragraph(
        f"<b>{issuer_name}</b><br/>{issuer_address}<br/>GSTIN {issuer_gst_no}",
        normal,
    )
    header_table = Table(
        [[logo or "", issuer_block, Paragraph("INVOICE", title_style)]],
        colWidths=[HEADER_LOGO_WIDTH, CONTENT_WIDTH - HEADER_LOGO_WIDTH - HEADER_TITLE_WIDTH, HEADER_TITLE_WIDTH],
    )
    header_table.setStyle(
        _boxed(
            padding=8,
            extra=[
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ],
        )
    )
    story.append(header_table)

    # --- Meta row: Invoice # / Invoice Date, boxed ---
    meta_table = Table(
        [[
            Paragraph(f"<b>#</b> : {invoice_number}", normal),
            Paragraph(f"<b>Invoice Date</b> : {issued_at.strftime('%d/%m/%Y')}", normal),
        ]],
        colWidths=[CONTENT_WIDTH / 2, CONTENT_WIDTH / 2],
    )
    meta_table.setStyle(_boxed(extra=[("LINEAFTER", (0, 0), (0, 0), 1, BORDER_COLOR)]))
    story.append(meta_table)

    # --- Bill To, boxed with a shaded header bar ---
    # bill_to_* is freeform, user-editable SellerProfile/User.name data —
    # unlike issuer_*/service_description/terms_and_conditions (fixed DASHR
    # constants), it must be XML-escaped before going into Paragraph markup.
    # Unescaped, a value containing a stray recognized tag (e.g. an address
    # with a bare "<b>") throws during doc.build() and permanently breaks
    # invoice generation for that user, since the caller's broad except
    # swallows it silently rather than ever retrying.
    bill_to_lines = [_xml_escape(bill_to_name)]
    if bill_to_billing_address:
        bill_to_lines.append(_xml_escape(bill_to_billing_address))
    if bill_to_gst_no:
        bill_to_lines.append(f"GSTIN {_xml_escape(bill_to_gst_no)}")
    bill_to_table = Table(
        [
            [Paragraph("Bill To", section_label)],
            [Paragraph("<br/>".join(bill_to_lines), normal)],
        ],
        colWidths=[CONTENT_WIDTH],
    )
    bill_to_table.setStyle(
        _boxed(
            extra=[
                ("LINEBELOW", (0, 0), (0, 0), 1, BORDER_COLOR),
                ("BACKGROUND", (0, 0), (0, 0), HEADER_BG),
            ]
        )
    )
    story.append(bill_to_table)

    # --- Subject, boxed ---
    subject_table = Table(
        [[Paragraph(f"<b>Subject :</b> {service_description}", normal)]],
        colWidths=[CONTENT_WIDTH],
    )
    subject_table.setStyle(_boxed())
    story.append(subject_table)
    story.append(Spacer(1, 5 * mm))

    # --- Item table ---
    # Description is wrapped in a Paragraph, not a plain string, so it wraps
    # to multiple lines within its column instead of overflowing into the
    # SAC/Qty columns next to it — the service description is long enough
    # to need it.
    item_col_widths = [
        ITEM_COL_NUM,
        CONTENT_WIDTH - ITEM_COL_NUM - ITEM_COL_SAC - ITEM_COL_QTY - ITEM_COL_RATE - ITEM_COL_AMOUNT,
        ITEM_COL_SAC,
        ITEM_COL_QTY,
        ITEM_COL_RATE,
        ITEM_COL_AMOUNT,
    ]
    item_table = Table(
        [
            ["#", "Item & Description", "SAC", "Qty", "Rate", "Amount"],
            [
                "1",
                Paragraph(service_description, normal),
                sac_code,
                "1",
                f"{taxable_value_inr:,.2f}",
                f"{taxable_value_inr:,.2f}",
            ],
        ],
        colWidths=item_col_widths,
    )
    item_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
                ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
                ("GRID", (0, 0), (-1, -1), 1, BORDER_COLOR),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(item_table)
    story.append(Spacer(1, 6 * mm))

    # --- Totals (right-aligned, unboxed — matches the reference's plain
    # totals block rather than the bordered sections above it) ---
    totals_table = Table(
        [
            ["", "Sub Total", f"{taxable_value_inr:,.2f}"],
            ["", f"CGST @ {cgst_rate_percent}%", f"{cgst_amount_inr:,.2f}"],
            ["", f"SGST @ {sgst_rate_percent}%", f"{sgst_amount_inr:,.2f}"],
            ["", "Total Paid", f"₹{total_inr:,.2f}"],
        ],
        colWidths=[CONTENT_WIDTH - TOTALS_LABEL_WIDTH - TOTALS_VALUE_WIDTH, TOTALS_LABEL_WIDTH, TOTALS_VALUE_WIDTH],
    )
    totals_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (1, -1), (-1, -1), _FONT_BOLD),
                ("LINEABOVE", (1, -1), (-1, -1), 1, colors.black),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(totals_table)
    story.append(Spacer(1, 8 * mm))

    # --- Terms & Conditions ---
    story.append(Paragraph("Terms &amp; Conditions", heading))
    for line in terms_and_conditions.split("\n"):
        if line.strip():
            story.append(Paragraph(line, normal))
    story.append(Spacer(1, 10 * mm))

    # --- Footer ---
    story.append(
        Paragraph("This is a computer generated invoice and does not require a signature.", small_italic)
    )

    doc.build(story, onFirstPage=_draw_outer_border, onLaterPages=_draw_outer_border)
    return buffer.getvalue()
