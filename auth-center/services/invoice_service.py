#!/usr/bin/env python3
"""
Invoice Service — 电子发票生成
支付成功后自动生成 PDF 发票，存储在 data/invoices/ 目录
依赖：fpdf2 (pip install fpdf2)
"""
import os, sys, random, string
from datetime import datetime

# ── 发票存储目录 ──
INVOICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'invoices')


def _get_seller_info():
    """从 system_config 读取发票商家信息。

    Returns:
        dict: {name, tax_id, address, phone, bank}

    Raises:
        ValueError: seller_tax_id 未配置或仍为掩码占位时抛出，拒绝开票。
    """
    try:
        from models import get_db
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from models import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM system_config WHERE key IN "
            "('seller_name','seller_tax_id','seller_address','seller_phone','seller_bank')"
        ).fetchall()
    cfg = {r['key']: r['value'] for r in rows}

    seller = {
        'name': cfg.get('seller_name', '') or '',
        'tax_id': cfg.get('seller_tax_id', '') or '',
        'address': cfg.get('seller_address', '') or '',
        'phone': cfg.get('seller_phone', '') or '',
        'bank': cfg.get('seller_bank', '') or '',
    }
    if not seller['tax_id'] or 'X' in seller['tax_id'].upper():
        raise ValueError('seller tax id not configured, refuse to issue invoice')
    return seller


def _ensure_dir():
    """确保发票目录存在"""
    os.makedirs(INVOICE_DIR, exist_ok=True)


def _find_chinese_font():
    """查找系统中可用的中文字体"""
    candidates = [
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
        '/usr/share/fonts/droid/DroidSansFallbackFull.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf',
        '/usr/share/fonts/truetype/fonts-japanese-gothic.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # Try to auto-install
    try:
        import subprocess
        subprocess.run(
            ['pip3', 'install', '--break-system-packages', 'fonttools'],
            capture_output=True, timeout=30, stderr=subprocess.DEVNULL
        )
        # Check if any .ttf or .ttc files exist in common dirs
        for root in ['/usr/share/fonts', '/usr/local/share/fonts']:
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    if fn.endswith(('.ttf', '.ttc')):
                        return os.path.join(dirpath, fn)
    except Exception:
        pass
    return None


def new_invoice_no():
    """生成发票号：INV + 日期 + 4位随机"""
    date_part = datetime.now().strftime('%Y%m%d')
    rand_part = ''.join(random.choices(string.digits, k=4))
    return f'INV{date_part}{rand_part}'


def generate_invoice_pdf(order_no, user_name, plan_name, period_text, amount_fen, invoice_no=None):
    """
    生成电子发票 PDF
    返回 (invoice_no, pdf_path) 或 (None, None) 失败
    """
    _ensure_dir()
    if not invoice_no:
        invoice_no = new_invoice_no()

    amount_yuan = amount_fen / 100.0
    pdf_filename = f'{invoice_no}.pdf'
    pdf_path = os.path.join(INVOICE_DIR, pdf_filename)

    # 商家信息必须在 PDF 生成前校验：未配置或仍为掩码时抛错，禁止开票
    seller = _get_seller_info()

    font_path = _find_chinese_font()

    try:
        from fpdf import FPDF

        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.add_page()

        # 注册中文字体（如果找到），否则用 Helvetica（fpdf2 内置）
        if font_path:
            pdf.add_font('zh', '', font_path, uni=True)
            pdf.add_font('zh', 'B', font_path, uni=True)
            font_name = 'zh'
        else:
            font_name = 'Helvetica'

        # 标题
        pdf.set_font(font_name, '', 22)
        pdf.cell(0, 14, 'ELECTRONIC INVOICE', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font(font_name, '', 10)
        pdf.cell(0, 6, 'Electronic Invoice / Dian Zi Fa Piao', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)

        # 分隔线
        pdf.set_draw_color(200, 200, 200)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(4)

        # 发票信息
        pdf.set_font(font_name, '', 10)
        info_items = [
            ('Invoice No.', invoice_no),
            ('Date', datetime.now().strftime('%Y-%m-%d %H:%M')),
            ('Order No.', order_no),
            ('Status', 'Issued'),
        ]
        for label, value in info_items:
            pdf.set_font(font_name, 'B', 10)
            pdf.cell(30, 7, label + ':')
            pdf.set_font(font_name, '', 10)
            pdf.cell(0, 7, value, new_x='LMARGIN', new_y='NEXT')

        pdf.ln(4)

        # Seller / Buyer info
        pdf.set_font(font_name, 'B', 12)
        pdf.cell(0, 8, 'Seller / Xiao Shou Fang', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font(font_name, '', 10)
        pdf.cell(0, 6, f'Name: {seller["name"]}', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 6, f'Tax ID: {seller["tax_id"]}', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 6, f'Address: {seller["address"]}', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(2)

        pdf.set_font(font_name, 'B', 12)
        pdf.cell(0, 8, 'Buyer / Gou Mai Fang', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font(font_name, '', 10)
        pdf.cell(0, 6, f'Name: {user_name}', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)

        # 商品明细
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(2)

        col_w = [60, 30, 30, 30, 40]
        headers = ['Description', 'Period', 'Qty', 'Unit Price', 'Amount']
        pdf.set_font(font_name, 'B', 9)
        for i, h in enumerate(headers):
            pdf.cell(col_w[i], 8, h, border=0)
        pdf.ln()

        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(1)

        pdf.set_font(font_name, '', 9)
        pdf.cell(col_w[0], 7, plan_name)
        pdf.cell(col_w[1], 7, period_text)
        pdf.cell(col_w[2], 7, '1')
        pdf.cell(col_w[3], 7, f'¥{amount_yuan:.2f}', align='R')
        pdf.cell(col_w[4], 7, f'¥{amount_yuan:.2f}', align='R')
        pdf.ln()

        pdf.ln(1)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(2)

        # 总计
        pdf.set_font(font_name, 'B', 12)
        pdf.cell(0, 8, f'Total: ¥{amount_yuan:.2f}', align='R', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(2)

        # 金额大写
        pdf.set_font(font_name, '', 9)
        total_cn = _num2cn(amount_yuan)
        pdf.cell(0, 6, f'Amount in words: {total_cn}', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(8)

        # 底部
        pdf.set_font(font_name, '', 8)
        pdf.set_text_color(128, 128, 128)
        pdf.cell(0, 5, f'Generated by System at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
                 align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 5, 'This is a computer-generated electronic invoice, valid without a physical seal.',
                 align='C', new_x='LMARGIN', new_y='NEXT')

        pdf.output(pdf_path)
        return invoice_no, pdf_filename

    except ImportError:
        print('[Invoice] fpdf2 not installed, creating stub invoice record')
        return invoice_no, ''
    except Exception as e:
        print(f'[Invoice] PDF generation failed: {e}')
        return invoice_no, ''


def _num2cn(n):
    """数字转中文大写（金额用）"""
    digits = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']
    units = ['', 'ten', 'hundred', 'thousand']
    big_units = ['', 'ten thousand', 'hundred million']

    if n == 0:
        return 'zero yuan only'

    yuan = int(n)
    fen = round((n - yuan) * 100)
    result = []

    if yuan > 0:
        parts = []
        i = 0
        while yuan > 0:
            part = yuan % 10000
            if part > 0:
                sub = []
                for j in range(4):
                    d = part % 10
                    if d > 0:
                        sub.insert(0, digits[d] + ' ' + units[j])
                    elif sub and sub[0] != 'zero ':
                        sub.insert(0, 'zero ')
                    part //= 10
                sub_str = ''.join(sub).strip() + ' ' + big_units[i]
                parts.insert(0, sub_str)
            elif parts:
                parts.insert(0, 'zero ')
            i += 1
            yuan //= 10000
        result.append(''.join(parts).strip())

    result.append('yuan')

    if fen > 0:
        if fen < 10:
            result.append(f' zero {digits[fen]} fen')
        else:
            result.append(f' {digits[fen // 10]} {digits[fen % 10]} fen')
    else:
        result.append(' only')

    return ''.join(result)


def create_invoice_record(order_no, user_id, amount_fen, plan_name, period_text, user_name=''):
    """
    创建发票记录 + 生成 PDF
    由 _fulfill_order() 在支付成功后调用
    """
    try:
        from models import get_db
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from models import get_db

    invoice_no = new_invoice_no()
    invoice_no, pdf_filename = generate_invoice_pdf(
        order_no=order_no,
        user_name=user_name or f'User#{user_id}',
        plan_name=plan_name,
        period_text=period_text,
        amount_fen=amount_fen,
        invoice_no=invoice_no,
    )

    with get_db() as conn:
        conn.execute(
            """INSERT INTO invoices (invoice_no, order_no, user_id, amount_fen, amount_yuan,
               plan_name, period_text, pdf_path, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'issued')""",
            (invoice_no, order_no, user_id, amount_fen, amount_fen / 100.0,
             plan_name, period_text, pdf_filename))
        conn.commit()

    print(f'[Invoice] Created {invoice_no} for order {order_no}')
    return invoice_no
