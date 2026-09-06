"""Generate a professional RTL Word document (.docx) containing deployment instructions for the professor."""

from __future__ import annotations

from pathlib import Path
import docx
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = PROJECT_ROOT / "راهنمای_اجرای_پروژه_برای_استاد.docx"

# Color Palette
COLOR_PRIMARY = RGBColor(0x1E, 0x3A, 0x8A)      # Deep Navy #1E3A8A
COLOR_SECONDARY = RGBColor(0x2B, 0x6F, 0x93)    # Teal-Blue #2B6F93
COLOR_TEXT_DARK = RGBColor(0x1F, 0x29, 0x37)    # Charcoal #1F2937
COLOR_MUTED = RGBColor(0x4B, 0x55, 0x63)        # Gray #4B5563
COLOR_CODE_GREEN = RGBColor(0x04, 0x78, 0x57)   # Green #047857
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)

HEX_PRIMARY = "1E3A8A"
HEX_SECONDARY = "2B6F93"
HEX_BG_LIGHT = "F8FAFC"
HEX_BG_CALLOUT = "F0F9FF"
HEX_BORDER = "CBD5E1"
HEX_CODE_BG = "0F172A"                          # Dark slate for terminal boxes


def set_p_rtl(p: docx.text.paragraph.Paragraph, align: WD_ALIGN_PARAGRAPH = WD_ALIGN_PARAGRAPH.RIGHT) -> None:
    """Set RTL direction and alignment on paragraph."""
    p.alignment = align
    pPr = p._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    bidi.set(qn("w:val"), "1")
    pPr.append(bidi)


def set_run_font(
    run: docx.text.run.Run,
    *,
    font_name: str = "Tahoma",
    size_pt: float = 11,
    bold: bool = False,
    italic: bool = False,
    color: RGBColor | None = None,
    is_code: bool = False,
) -> None:
    """Configure font, size, language, and bidirectional properties on a run."""
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    if color:
        run.font.color.rgb = color

    rPr = run._r.get_or_add_rPr()
    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"), "Consolas" if is_code else font_name)
    rFonts.set(qn("w:hAnsi"), "Consolas" if is_code else font_name)
    rFonts.set(qn("w:cs"), font_name)
    rPr.append(rFonts)

    if not is_code:
        rtl = OxmlElement("w:rtl")
        rtl.set(qn("w:val"), "1")
        rPr.append(rtl)


def add_heading(doc: Document, text: str, level: int = 1) -> docx.text.paragraph.Paragraph:
    """Add a beautifully styled RTL heading."""
    p = doc.add_paragraph()
    set_p_rtl(p, WD_ALIGN_PARAGRAPH.RIGHT)
    p.paragraph_format.space_before = Pt(14 if level == 1 else 10)
    p.paragraph_format.space_after = Pt(4)

    run = p.add_run(text)
    if level == 1:
        set_run_font(run, font_name="Tahoma", size_pt=14, bold=True, color=COLOR_PRIMARY)
    elif level == 2:
        set_run_font(run, font_name="Tahoma", size_pt=12, bold=True, color=COLOR_SECONDARY)
    else:
        set_run_font(run, font_name="Tahoma", size_pt=11, bold=True, color=COLOR_TEXT_DARK)
    return p


def add_persian_paragraph(
    doc: Document,
    text: str,
    *,
    bold: bool = False,
    size_pt: float = 10.5,
    space_after: float = 4,
    color: RGBColor | None = COLOR_TEXT_DARK,
) -> docx.text.paragraph.Paragraph:
    """Add a standard RTL Persian paragraph."""
    p = doc.add_paragraph()
    set_p_rtl(p, WD_ALIGN_PARAGRAPH.RIGHT)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.25

    run = p.add_run(text)
    set_run_font(run, font_name="Tahoma", size_pt=size_pt, bold=bold, color=color)
    return p


def add_code_box(doc: Document, code_lines: list[str], comment: str = "") -> None:
    """Add a dark terminal-style code snippet box (LTR)."""
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.cell(0, 0)
    cell.width = Inches(6.5)

    # Set background and border
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{HEX_CODE_BG}"/>')
    cell._tc.get_or_add_tcPr().append(shd)

    borders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'  <w:top w:val="none"/>'
        f'  <w:left w:val="single" w:sz="24" w:space="0" w:color="{HEX_SECONDARY}"/>'
        f'  <w:bottom w:val="none"/>'
        f'  <w:right w:val="none"/>'
        f'</w:tcBorders>'
    )
    cell._tc.get_or_add_tcPr().append(borders)

    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.line_spacing = 1.15

    for i, line in enumerate(code_lines):
        run = p.add_run(line + ("\n" if i < len(code_lines) - 1 else ""))
        if line.startswith("#"):
            set_run_font(run, font_name="Consolas", size_pt=9.5, color=RGBColor(0x94, 0xA3, 0xB8), is_code=True)
        else:
            set_run_font(run, font_name="Consolas", size_pt=10, bold=True, color=RGBColor(0x38, 0xBD, 0xF8), is_code=True)

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_callout(doc: Document, title: str, text: str, border_color: str = HEX_SECONDARY) -> None:
    """Add an elegant RTL callout card."""
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.cell(0, 0)
    cell.width = Inches(6.5)

    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{HEX_BG_CALLOUT}"/>')
    cell._tc.get_or_add_tcPr().append(shd)

    borders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'  <w:top w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'  <w:left w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'  <w:bottom w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'  <w:right w:val="single" w:sz="24" w:space="0" w:color="{border_color}"/>'
        f'</w:tcBorders>'
    )
    cell._tc.get_or_add_tcPr().append(borders)

    p = cell.paragraphs[0]
    set_p_rtl(p, WD_ALIGN_PARAGRAPH.RIGHT)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(2)
    run_t = p.add_run(f"📌 {title}\n")
    set_run_font(run_t, font_name="Tahoma", size_pt=10.5, bold=True, color=COLOR_PRIMARY)

    run_b = p.add_run(text)
    set_run_font(run_b, font_name="Tahoma", size_pt=9.5, color=COLOR_TEXT_DARK)

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def build_document() -> None:
    """Construct the complete Word document."""
    doc = Document()

    # Set 1-inch margins
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # 1. Main Header Title
    title_p = doc.add_paragraph()
    set_p_rtl(title_p, WD_ALIGN_PARAGRAPH.CENTER)
    title_p.paragraph_format.space_after = Pt(2)
    run_title = title_p.add_run("راهنمای استقرار و اجرای سامانه تفسیر بالینی واریانت‌ها")
    set_run_font(run_title, font_name="Tahoma", size_pt=17, bold=True, color=COLOR_PRIMARY)

    sub_p = doc.add_paragraph()
    set_p_rtl(sub_p, WD_ALIGN_PARAGRAPH.CENTER)
    sub_p.paragraph_format.space_after = Pt(12)
    run_sub = sub_p.add_run("Clinical Variant Interpretation Web Application — Deployment Guide\nویژه استاد محترم راهنما جهت راه‌اندازی سریع با داکر بر روی سرور یا سیستم محلی")
    set_run_font(run_sub, font_name="Tahoma", size_pt=10, italic=True, color=COLOR_MUTED)

    # Horizontal divider
    divider = doc.add_paragraph()
    set_p_rtl(divider, WD_ALIGN_PARAGRAPH.CENTER)
    divider.paragraph_format.space_after = Pt(8)
    run_div = divider.add_run("―" * 45)
    set_run_font(run_div, font_name="Tahoma", size_pt=9, color=RGBColor(0xCB, 0xD5, 0xE1))

    # Greeting & Project Summary
    add_persian_paragraph(
        doc,
        "سلام و احترام استاد گرامی، وقت شما بخیر؛",
        bold=True,
        size_pt=11,
        color=COLOR_PRIMARY,
    )
    add_persian_paragraph(
        doc,
        "پروژه وب‌اپلیکیشن تفسیر بالینی واریانت‌های ژنتیکی به صورت کامل داکرایز (Dockerized) و آماده استقرار شده است. دسترسی جنابعالی به عنوان Collaborator بر روی ریپازیتوری گیت‌هاب فعال گردیده و تمام کدهای بک‌اند، فرانت‌اند، قالب‌های گزارش و فایل‌های پیکربندی داکر در دسترس شماست.",
    )

    # Callout with Repository Links
    add_callout(
        doc,
        "مشخصات دسترسی به مخزن و سامانه",
        "• آدرس مخزن در گیت‌هاب: https://github.com/parhamKIY/CLINCAL_VARIANT_WEB_APPLICATION\n"
        "• دسترسی: شما به عنوان همکار (Collaborator) به پروژه دعوت شده‌اید.\n"
        "• پورت پیش‌فرض سامانه: 8501 (قابل دسترسی در مرورگر پس از اجرا)\n"
        "• فایل تنظیمات و کلیدها (.env): به صورت جداگانه در پیام برای شما ضمیمه شده است.",
    )

    # Method 1: Direct Server Deployment
    add_heading(doc, "روش اول: کلون و اجرای مستقیم بر روی سرور لینوکس (پیشنهادی)", level=1)
    add_persian_paragraph(
        doc,
        "در صورتی که به ترمینال سرور دسترسی دارید، این روش سریع‌ترین و تمیزترین حالت است و در ۳ گام ساده انجام می‌گیرد:",
    )

    add_persian_paragraph(doc, "گام ۱: کلون کردن مخزن و ورود به پوشه پروژه", bold=True, size_pt=10.5)
    add_code_box(
        doc,
        [
            "git clone https://github.com/parhamKIY/CLINCAL_VARIANT_WEB_APPLICATION.git",
            "cd CLINCAL_VARIANT_WEB_APPLICATION",
        ],
    )

    add_persian_paragraph(
        doc,
        "گام ۲: قرار دادن فایل تنظیمات (.env)",
        bold=True,
        size_pt=10.5,
    )
    add_persian_paragraph(
        doc,
        "فایل .env ارسالی را مستقیماً داخل همین پوشه اصلی قرار دهید (می‌توانید با نرم‌افزارهای انتقال فایل نظیر WinSCP یا FileZilla فایل را درگ کنید، یا در صورت تمایل فایل را با دستور nano .env باز کرده و محتوای ارسالی را پیست نمایید).",
    )

    add_persian_paragraph(doc, "گام ۳: اجرای تک‌دستوری برنامه با Docker Compose", bold=True, size_pt=10.5)
    add_code_box(
        doc,
        [
            "# ساخت ایمیج و اجرای برنامه در پس‌زمینه",
            "docker compose up -d --build",
        ],
    )
    add_persian_paragraph(
        doc,
        "داکر به طور خودکار تمام وابستگی‌های سیستمی و پایتونی را داخل کانتینر ایزوله نصب کرده، پایگاه داده را مقداردهی اولیه نموده و وب‌سایت را بالا می‌آورد.",
        size_pt=10,
        color=COLOR_MUTED,
    )

    # Method 2: Local Download then Transfer
    add_heading(doc, "روش دوم: دانلود روی سیستم شخصی و انتقال به سرور", level=1)
    add_persian_paragraph(
        doc,
        "اگر مایلید ابتدا کدهای پروژه را روی سیستم خودتان داشته باشید و سپس به سرور منتقل نمایید:",
    )
    add_persian_paragraph(
        doc,
        "۱. دریافت پروژه: وارد لینک گیت‌هاب شوید، روی دکمه سبز رنگ Code کلیک کرده و گزینه Download ZIP را انتخاب کنید (یا دستور git clone را روی سیستم شخصی خود بزنید).\n"
        "۲. قرار دادن فایل تنظیمات: فایل فشرده را اکسترکت کنید و فایل .env ارسالی را مستقیماً داخل پوشه اصلی پروژه (کنار فایل‌های Dockerfile و app.py) پیست نمایید.\n"
        "۳. انتقال پوشه به سرور: کل پوشه پروژه را با نرم‌افزار WinSCP یا دستور scp به سرور منتقل کنید:\n"
        "    scp -r CLINCAL_VARIANT_WEB_APPLICATION user@server_ip:/path/to/destination\n"
        "۴. اجرا در سرور: در ترمینال سرور وارد پوشه شده و دستور زیر را اجرا فرمایید:",
    )
    add_code_box(
        doc,
        [
            "cd CLINCAL_VARIANT_WEB_APPLICATION",
            "docker compose up -d --build",
        ],
    )

    # Web Access
    add_heading(doc, "دسترسی به وب‌سایت و بررسی وضعیت سلامت", level=1)
    add_persian_paragraph(
        doc,
        "پس از اجرای کانتینر، سامانه بر روی پورت 8501 سرور فعال خواهد بود. کافی است در مرورگر آدرس زیر را باز فرمایید:",
    )
    add_code_box(doc, ["http://<آدرس_IP_سرور>:8501"])

    add_persian_paragraph(
        doc,
        "نکته فایروال سرور: در صورتی که صفحه وب باز نشد، اطمینان حاصل فرمایید که پورت 8501 در فایروال سرور باز باشد (در اوبونتو با دستور: sudo ufw allow 8501/tcp).",
        size_pt=9.5,
        color=COLOR_MUTED,
    )

    # Command Cheat-sheet Table
    add_heading(doc, "دستورات مدیریتی متداول (جدول کاربردی)", level=1)

    table = doc.add_table(rows=6, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    headers = ["دستور لینوکس (ترمینال)", "شرح عملکرد"]
    for j, h in enumerate(headers):
        cell = table.cell(0, j)
        shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{HEX_PRIMARY}"/>')
        cell._tc.get_or_add_tcPr().append(shd)
        p = cell.paragraphs[0]
        set_p_rtl(p, WD_ALIGN_PARAGRAPH.CENTER)
        r = p.add_run(h)
        set_run_font(r, font_name="Tahoma", size_pt=10, bold=True, color=COLOR_WHITE)

    rows_data = [
        ("docker compose up -d", "شروع اجرای برنامه در پس‌زمینه (بدون بیلد مجدد)"),
        ("docker compose logs -f", "مشاهده زنده لاگ‌های خروجی و عیب‌یابی"),
        ("docker compose ps", "بررسی وضعیت اجرای کانتینر و وضعیت سلامت (Healthcheck)"),
        ("docker compose restart", "راه‌اندازی مجدد برنامه"),
        ("docker compose down", "توقف برنامه (اطلاعات دیتابیس و گزارش‌ها محفوظ می‌ماند)"),
    ]

    for i, (cmd, desc) in enumerate(rows_data, start=1):
        # Column 0: Code
        cell_cmd = table.cell(i, 0)
        cell_cmd.width = Inches(2.8)
        p_cmd = cell_cmd.paragraphs[0]
        p_cmd.alignment = WD_ALIGN_PARAGRAPH.LEFT
        r_cmd = p_cmd.add_run(cmd)
        set_run_font(r_cmd, font_name="Consolas", size_pt=9.5, bold=True, color=COLOR_PRIMARY, is_code=True)

        # Column 1: Description
        cell_desc = table.cell(i, 1)
        cell_desc.width = Inches(3.7)
        p_desc = cell_desc.paragraphs[0]
        set_p_rtl(p_desc, WD_ALIGN_PARAGRAPH.RIGHT)
        r_desc = p_desc.add_run(desc)
        set_run_font(r_desc, font_name="Tahoma", size_pt=9.5, color=COLOR_TEXT_DARK)

        if i % 2 == 1:
            shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{HEX_BG_LIGHT}"/>')
            cell_cmd._tc.get_or_add_tcPr().append(shd)
            shd2 = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{HEX_BG_LIGHT}"/>')
            cell_desc._tc.get_or_add_tcPr().append(shd2)

    # Architectural & Security Highlights
    add_heading(doc, "نکات فنی، پایداری داده‌ها و امنیت", level=1)
    add_persian_paragraph(
        doc,
        "• پایداری پایگاه داده و گزارش‌ها (Persistent Volumes): پوشه storage شامل دیتابیس تحلیلی SQLite، کش شواهد بیوانفورماتیک و گزارش‌های نهایی Word تولید شده است که به عنوان Volume به سرور متصل است تا در اثر ریستارت یا آپدیت کانتینر، داده‌ها حذف نشوند.\n"
        "• بسته‌های آنتولوژی HPO: فایل‌های آنتولوژی HPO (با حجم ۱۱۳ مگابایت) داخل پوشه data در مخزن گیت‌هاب قرار دارند و پس از کلون، برنامه در اولین اجرا نیاز به دانلود سنگین اینترنتی نخواهد داشت.\n"
        "• امنیت چندلایه‌ای و مهار تزریق پرامپت: ورودی شرح بالینی مجهز به ماژول‌های اعتبارسنجی عبارات، فیلتر داده‌های هویتی بیمار (PHI Redaction) و سد دفاعی چندلایه‌ای در برابر دستکاری پرامپت است.\n"
        "• راهنمای استقرار پیشرفته (DEPLOYMENT.md): برای جزئیات فنی‌تر، نحوه تنظیم Nginx به عنوان Reverse Proxy با دامنه دانشگاهی و SSL، فایل DEPLOYMENT.md در ریشه پروژه در دسترس است.",
        size_pt=9.5,
        color=COLOR_TEXT_DARK,
    )

    # Sign-off
    add_persian_paragraph(doc, "با احترام و آرزوی سلامتی\nپرهام کیومرثی", bold=True, size_pt=10.5, color=COLOR_PRIMARY, space_after=12)

    doc.save(OUTPUT_FILE)
    print("Document successfully created:", OUTPUT_FILE.name.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    build_document()
