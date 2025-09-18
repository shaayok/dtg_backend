# pip install reportlab requests
import io, requests
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import smtplib
from reportlab.platypus import Image
from reportlab.lib.units import inch
import requests
import io
import os
from dotenv import load_dotenv
load_dotenv()

def _fetch_logo_bytes(url: str):
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return io.BytesIO(r.content)   # <- return BytesIO stream
    except Exception as e:
        print("Logo fetch error:", e)
        return None

def _money(x):
    try: return "${:,.2f}".format(float(x or 0))
    except: return "$0.00"

def _fetch_logo(url: str):
    try:
        r = requests.get(url, timeout=5); r.raise_for_status()
        return ImageReader(io.BytesIO(r.content))
    except Exception:
        return None

def build_quote_pdf_bytes(data: dict) -> bytes:
    """
    Expected data:
    {
      "account_name": str,                   # e.g., "Amazon LAX9"
      "name": str,                           # quote number e.g., "SQ-20250818-011742"
      "status": str,                         # e.g., "Open"
      "shipping_address": str,               # multiline or single line
      "lines": [                             # items
        {"description": str, "name": str, "price": number, "qty": number}
      ],
      "logo_url": str (optional),
      "quote_date": str (optional),          # else today
      "notes": str (optional)
    }
    """
    buf = io.BytesIO()
    left = right = 0.6 * inch
    top = 0.6 * inch
    bottom = 0.7 * inch

    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=left, rightMargin=right, topMargin=top, bottomMargin=bottom
    )
    avail = doc.width

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="HRight", fontSize=22, alignment=2))
    styles.add(ParagraphStyle(name="SmallGray", fontSize=9, textColor=colors.grey))
    styles.add(ParagraphStyle(name="Tight", leading=14))
    story = []

    # --- Header (logo + quote info) ---
    #logo_stream = _fetch_logo_bytes(data.get("logo_url") or "https://i.ibb.co/hvF4nWd/dtg-logo.png")
    logo_path = "DTG_Logo_Black.png"
    logo = Image(logo_path, width=2.1*inch, height=0.65*inch)

    quote_no = data.get("name") or "QUOTE"
    quote_dt = data.get("quote_date") or datetime.utcnow().strftime("%B %d, %Y")

    header = Table(
        [[logo,
          [Paragraph("Quotation", styles["HRight"]),
           Spacer(1, 4),
           Paragraph(f"<b>{quote_no}</b>", styles["Normal"]),
           Paragraph(quote_dt, styles["SmallGray"])]
         ]],
        colWidths=[0.5*avail, 0.5*avail]
    )
    header.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("ALIGN",(1,0),(1,0),"RIGHT"),
        ("BOTTOMPADDING",(0,0),(-1,-1),0),
    ]))
    story += [header, Spacer(1, 6)]

    story += [Paragraph("35 Upton Dr<br/>Wilmington, MA 01887<br/>978-532-0444", styles["Tight"]),
              Spacer(1, 12)]

    # --- Summary (3 cols): Customer | Status | Ship To ---
    customer = data.get("account_name","")
    status   = data.get("status","")
    ship_to  = (data.get("shipping_address") or "")

    summary = Table(
        [["Customer", "Status", "Ship To"],
         [customer, status, Paragraph(ship_to, styles["Tight"])]],
        colWidths=[avail*0.22, avail*0.18, avail*0.60],
        repeatRows=1
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.black),
        ("TEXTCOLOR",(0,0),(-1,0), colors.white),
        ("GRID",(0,0),(-1,-1), 1, colors.black),
        ("VALIGN",(0,0),(-1,-1), "TOP"),
    ]))
    story += [summary, Spacer(1, 10)]

    # --- Items table (5 cols): Description | Part # | QTY | Unit | Ext ---
    lines = data.get("lines") or []
    rows = [["Description", "Part #", "QTY", "Unit Price", "Ext Price"]]
    total = 0.0
    for it in lines:
        qty   = float(it.get("qty", 0) or 0)
        price = float(it.get("price", 0) or 0)
        ext   = qty * price
        total += ext
        rows.append([
            Paragraph(it.get("description",""), styles["Tight"]),
            it.get("name",""),
            f"{qty:.0f}" if qty.is_integer() else f"{qty:g}",
            _money(price),
            _money(ext),
        ])

    rows.append(["", "", "", "Total", _money(total)])
    # proportions (sum to 1.0): Description widest
    widths = [avail*0.48, avail*0.20, avail*0.08, avail*0.12, avail*0.12]

    items_tbl = Table(rows, colWidths=widths, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.black),
        ("TEXTCOLOR",(0,0),(-1,0), colors.white),
        ("GRID",(0,0),(-1,-1), 1, colors.black),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("ALIGN",(2,1),(-1,-2),"RIGHT"),
        ("ALIGN",(2,0),(2,-1),"CENTER"),         # QTY header center
        ("FONTNAME",(-2,-1),(-1,-1),"Helvetica-Bold"),
        ("SPAN",(0,-1),(2,-1)),                  # span blanks left of Total
    ]))
    story += [items_tbl, Spacer(1, 12)]

    # --- Notes (optional) ---
    notes = (data.get("notes") or "").replace("\n","<br/>")
    if notes:
        story += [Paragraph("<b>Notes:</b>", styles["Normal"])]
        notes_tbl = Table([[Paragraph(notes, styles["Tight"])]], colWidths=[avail])
        notes_tbl.setStyle(TableStyle([("GRID",(0,0),(-1,-1),1,colors.black)]))
        story += [notes_tbl]

    # --- Footer with page numbers ---
    def _footer(canvas, doc_):

        # --- metadata (set once per file, not per page) ---
        canvas.setAuthor("DTG")   # what you want to appear instead of anonymous
        canvas.setTitle(data.get("name", "Quotation"))
        canvas.setSubject("Customer Quotation")
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(doc_.pagesize[0] - right, 0.45*inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf.read()

def send_test_email_with_pdf(sample):

    pdf_bytes = build_quote_pdf_bytes(sample)

    gmail_user = os.getenv('GMAIL_USER')
    gmail_app_password = os.getenv('GMAIL_APP_PASSWORD')
    # Get both email recipients
    email_to_1 = os.getenv('EMAIL_TO')
    email_to_2 = 'sayaksamaddar@virtualemployee.com'  # Second email recipient
    email_to_3 = 'amazon-portal-activit-aaaaq74u3hzgbxwefmrhystcaa@the-dtg.slack.com'
    recipients = [email for email in [email_to_1, email_to_2, email_to_3] if email.strip()]
    to_email = ', '.join(recipients)
    if not all([gmail_user, gmail_app_password, to_email]):
        raise RuntimeError("Set GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO env vars.")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Your DTG Quote {sample['name']}"
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(
        f"""<html><body>
        <p>Hello {sample.get('first_name', '')},</p>
        <p>This quote was generated by our test portal and is not valid.<br>To get an accurate quote please email <b>sales@dtgpower.com</b><br>We will let you know when the portal is live and ready for quotes. </p>
        <p>Please find attached your quote <b>{sample['name']}</b>.</p>
        <p>Thanks,<br/>DTG</p>
        </body></html>""", "html"
    ))

    attach = MIMEApplication(pdf_bytes, _subtype="pdf")
    attach.add_header("Content-Disposition", "attachment", filename=f"{sample['name']}.pdf")
    msg.attach(attach)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)

    print("Sent test email with PDF to", to_email)

# Run once for a quick test:
if __name__ == "__main__":
    sample = {
    "account_name": "Amazon LAX9",
    "lines": [{
        "description": 'CART.PS - Definitive Technology Problem Solver Cart, 18" footprint with additional shelf and printer tray for Zebra label printer, 5" locking casters, front handle, lift, DEFINITIVE Battery Controller including Inverter.',
        "name": "DTG-PS-001-16DTG.",
        "price": 2183.0,
        "qty": 3.0
    }],
    "name": "SQ-20250818-011742",
    "shipping_address": "Amazon.com Services, Inc. (LAX9) 10247 Bellegrave",
    "status": "Open"
    }

    send_test_email_with_pdf(sample)


def send_contact_created_email(data):
    gmail_user = os.getenv('GMAIL_USER')
    gmail_app_password = os.getenv('GMAIL_APP_PASSWORD')

    email_to_1 = os.getenv('EMAIL_TO')
    email_to_2 = 'sayaksamaddar@virtualemployee.com'  # Second email recipient
    email_to_3 = 'amazon-portal-activit-aaaaq74u3hzgbxwefmrhystcaa@the-dtg.slack.com'
    recipients = [email for email in [email_to_1, email_to_2, email_to_3] if email.strip()]
    to_email = ', '.join(recipients)

    subject = f"New Salesforce Contact: {data.get('firstName','')} {data.get('lastName','')}"

    html = f"""
    <html>
    <body>
        <h2>New Salesforce Contact Created</h2>
        <p><b>Name:</b> {data.get('firstName','')} {data.get('lastName','')}</p>
        <p><b>Email:</b> {data.get('email','')}</p>
        <p>
            <a href="https://definitivetechnologygroup--testbox.sandbox.lightning.force.com/lightning/r/Contact/{data.get('contactId','')}/view"
               style="background:#4CAF50;color:white;padding:8px 15px;text-decoration:none;border-radius:6px;">
               View in Salesforce
            </a>
        </p>
        <p style="color:#888;">This is an automated notification.</p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)


def send_account_address_changed_email(data):
    gmail_user = os.getenv('GMAIL_USER')
    gmail_app_password = os.getenv('GMAIL_APP_PASSWORD')

    email_to_1 = os.getenv('EMAIL_TO')
    email_to_2 = 'sayaksamaddar@virtualemployee.com'  # Second email recipient
    email_to_3 = 'amazon-portal-activit-aaaaq74u3hzgbxwefmrhystcaa@the-dtg.slack.com'
    recipients = [email for email in [email_to_1, email_to_2, email_to_3] if email.strip()]
    to_email = ', '.join(recipients)

    subject = f"Salesforce Account Address Changed: {data.get('accountName','Unknown Account')}"

    html = f"""
    <html>
    <body>
        <h2>Salesforce Account Address Updated</h2>
        <p><b>Account:</b> {data.get('accountName','')}</p>
        <p><b>New Address:</b><br>
            {data.get('street','')}<br>
            {data.get('city','')}, {data.get('state','')} {data.get('postal_code','')}<br>
            {data.get('country','')}
        </p>
        <p>
            <a href="https://definitivetechnologygroup--testbox.sandbox.lightning.force.com/lightning/r/Account/{data.get('accountId','')}/view"
               style="background:#2196F3;color:white;padding:8px 15px;text-decoration:none;border-radius:6px;">
               View Account in Salesforce
            </a>
        </p>
        <p style="color:#888;">This is an automated notification.</p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)


def send_account_request_email(data):
    gmail_user = os.getenv('GMAIL_USER')
    gmail_app_password = os.getenv('GMAIL_APP_PASSWORD')

    email_to_1 = os.getenv('EMAIL_TO')
    email_to_2 = 'sayaksamaddar@virtualemployee.com'  # Second email recipient
    email_to_3 = 'amazon-portal-activit-aaaaq74u3hzgbxwefmrhystcaa@the-dtg.slack.com'
    recipients = [email for email in [email_to_1, email_to_2, email_to_3] if email.strip()]
    to_email = ', '.join(recipients)

    subject = f"Account addition request by {data.get('email','Unknown user')}"

    html = f"""
    <html>
    <body>
        <h2>Account Addition Request</h2>
        <p><b>Account:</b> {data.get('otherAccounts','')}</p>
        <p><b>Requested by:</b> {data.get('email','')}</p>
        <p style="color:#888;">This is an automated notification.</p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)
