import json
import frappe
from frappe.utils import flt, today, getdate
from erpnext.setup.utils import get_exchange_rate


GATEWAY_MODE_MAP = {
    "stripe": "Credit Card",
    "xpay": "Bank Transfer",
    "instapay": "Bank Transfer",
    "vodafone_cash": "Cash",
    "manual_transfer": "Bank Transfer",
}


def upsert_enrollment(row, settings):
    """Process a single enrollment row from the API into ERPNext documents."""
    source_id = row["id"]
    status = row["status"]
    amounts = row["amounts"]
    student = row["student"]
    billing = row.get("billing") or {}
    course = row.get("course")
    payment = row.get("payment") or {}
    installment = row.get("installment")
    refund = row.get("refund")
    dispute = row.get("dispute")

    currency = amounts["currency"]
    amount_paid = flt(amounts["amount_paid_minor"]) / 100
    wallet_applied = flt(amounts.get("wallet_amount_applied_minor") or 0) / 100
    discount = flt(amounts.get("discount_amount_minor") or 0) / 100
    gross = flt(amounts.get("gross_minor") or 0) / 100
    if not gross:
        gross = amount_paid + wallet_applied + discount

    # Invoice the agreed installment total, otherwise the pre-discount gross.
    # Discount is applied separately only when it is less than the line total.
    if installment:
        invoice_total = flt(installment["agreed_price_minor"]) / 100
        discount = 0  # already reflected in agreed_price
        outstanding = flt(installment["remaining_minor"]) / 100
    else:
        invoice_total = gross
        outstanding = 0

    posting_date = row.get("enrolled_at") or row["created_at"]
    if posting_date and "T" in posting_date:
        posting_date = posting_date[:10]

    conversion_rate = _resolve_conversion_rate(
        currency=currency,
        posting_date=posting_date,
        settings=settings,
        amounts=amounts,
        payment=payment,
    )

    accounts = _resolve_accounts(settings)

    # --- Customer ---
    customer_name = _upsert_customer(student, billing, settings)

    # --- Item ---
    item_code = None
    if course:
        item_code = _upsert_item(course)

    # --- Sales Invoice ---
    sinv_name = None
    if status in ("active", "refunded", "chargeback") and item_code:
        sinv_name = _upsert_sales_invoice(
            source_id=source_id,
            customer=customer_name,
            item_code=item_code,
            posting_date=posting_date,
            currency=currency,
            conversion_rate=conversion_rate,
            invoice_total=invoice_total,
            discount=discount,
            outstanding=outstanding,
            due_date=installment["due_date"] if installment else None,
            settings=settings,
            accounts=accounts,
        )

    # --- Payment Entry ---
    pe_name = None
    if status in ("active", "refunded", "chargeback") and amount_paid > 0 and sinv_name:
        pe_name = _upsert_payment_entry(
            source_id=source_id,
            customer=customer_name,
            amount_paid=amount_paid,
            currency=currency,
            conversion_rate=conversion_rate,
            posting_date=posting_date,
            gateway=payment.get("gateway"),
            reference=payment.get("reference"),
            sinv_name=sinv_name,
            settings=settings,
            accounts=accounts,
        )

    # --- Fee Journal Entry ---
    fee_minor = payment.get("fee_minor")
    if fee_minor and fee_minor > 0 and payment.get("gateway") == "stripe":
        fee_currency = payment.get("settlement_currency") or currency
        fee_rate = _resolve_conversion_rate(
            currency=fee_currency,
            posting_date=posting_date,
            settings=settings,
            amounts=amounts,
            payment=payment,
        )
        _upsert_fee_journal(
            source_id=source_id,
            fee=fee_minor / 100,
            currency=fee_currency,
            conversion_rate=fee_rate,
            posting_date=posting_date,
            settings=settings,
            accounts=accounts,
        )

    # --- Credit Note ---
    cn_name = None
    if status == "refunded" and refund and refund.get("amount_minor"):
        cn_name = _upsert_credit_note(
            source_id=source_id,
            customer=customer_name,
            item_code=item_code,
            amount=refund["amount_minor"] / 100,
            currency=currency,
            conversion_rate=conversion_rate,
            posting_date=refund.get("refunded_at", posting_date),
            sinv_name=sinv_name,
            settings=settings,
            accounts=accounts,
        )
    elif status == "chargeback" and dispute and dispute.get("amount_minor"):
        dispute_currency = dispute.get("currency") or currency
        dispute_rate = _resolve_conversion_rate(
            currency=dispute_currency,
            posting_date=posting_date,
            settings=settings,
            amounts=amounts,
            payment=payment,
        )
        cn_name = _upsert_credit_note(
            source_id=source_id,
            customer=customer_name,
            item_code=item_code,
            amount=dispute["amount_minor"] / 100,
            currency=dispute_currency,
            conversion_rate=dispute_rate,
            posting_date=dispute.get("disputed_at", posting_date),
            sinv_name=sinv_name,
            settings=settings,
            accounts=accounts,
        )

    # --- Log ---
    _upsert_log(
        source_id=source_id,
        status=status,
        posting_date=posting_date,
        currency=currency,
        amount_paid=amount_paid,
        invoice_total=invoice_total,
        outstanding=outstanding,
        due_date=installment["due_date"] if installment else None,
        customer=customer_name,
        item_code=item_code,
        sinv_name=sinv_name,
        pe_name=pe_name,
        cn_name=cn_name,
        raw=row,
    )


def _company_currency(settings):
    return frappe.get_cached_value("Company", settings.default_company, "default_currency")


def _resolve_accounts(settings):
    """
    Resolve income / receivable / payment accounts.
    Falls back to company defaults when Settings are misconfigured
    (e.g. income set to a receivable, receivable set to Cash).
    """
    company = settings.default_company

    income = settings.income_account
    if not _is_income_account(income):
        income = frappe.db.get_value(
            "Account",
            {"company": company, "root_type": "Income", "is_group": 0, "disabled": 0},
            "name",
            order_by="name asc",
        ) or frappe.db.get_value("Company", company, "default_income_account")

    receivable = settings.receivable_account
    if not _is_receivable_account(receivable):
        receivable = (
            frappe.db.get_value(
                "Account",
                {
                    "company": company,
                    "account_type": "Receivable",
                    "is_group": 0,
                    "disabled": 0,
                },
                "name",
            )
            or frappe.db.get_value("Company", company, "default_receivable_account")
        )

    cost_center = settings.cost_center or frappe.db.get_value(
        "Company", company, "cost_center"
    )

    payment_accounts = {}
    for gateway in ("stripe", "xpay", "instapay", "vodafone_cash", "manual_transfer"):
        field = f"payment_account_{gateway}"
        acc = getattr(settings, field, None)
        if not acc or not _is_bank_or_cash_account(acc):
            acc = (
                frappe.db.get_value(
                    "Account",
                    {
                        "company": company,
                        "account_type": ("in", ["Bank", "Cash"]),
                        "is_group": 0,
                        "disabled": 0,
                    },
                    "name",
                )
                or receivable
            )
        payment_accounts[gateway] = acc

    fee_expense = settings.fee_expense_account
    if not fee_expense or frappe.db.get_value("Account", fee_expense, "root_type") != "Expense":
        fee_expense = frappe.db.get_value(
            "Account",
            {"company": company, "root_type": "Expense", "is_group": 0, "disabled": 0},
            "name",
        )

    return frappe._dict(
        income=income,
        receivable=receivable,
        cost_center=cost_center,
        payment_accounts=payment_accounts,
        fee_expense=fee_expense,
    )


def _is_income_account(account):
    if not account or not frappe.db.exists("Account", account):
        return False
    root = frappe.db.get_value("Account", account, "root_type")
    return root == "Income"


def _is_receivable_account(account):
    if not account or not frappe.db.exists("Account", account):
        return False
    return frappe.db.get_value("Account", account, "account_type") == "Receivable"


def _is_bank_or_cash_account(account):
    if not account or not frappe.db.exists("Account", account):
        return False
    return frappe.db.get_value("Account", account, "account_type") in ("Bank", "Cash")


def _ensure_currency(currency):
    """Enable the Currency master if it exists; create a stub if missing."""
    if not currency:
        return
    if frappe.db.exists("Currency", currency):
        if not frappe.db.get_value("Currency", currency, "enabled"):
            frappe.db.set_value("Currency", currency, "enabled", 1)
        return
    doc = frappe.new_doc("Currency")
    doc.currency_name = currency
    doc.enabled = 1
    doc.insert(ignore_permissions=True)


def _resolve_conversion_rate(currency, posting_date, settings, amounts=None, payment=None):
    """
    Return company-currency conversion rate for `currency`.
    Creates a Currency Exchange row so ERPNext never hits Frankfurter for LYD pairs.
    """
    company_currency = _company_currency(settings)
    _ensure_currency(currency)

    if not currency or currency == company_currency:
        return 1.0

    date = getdate(posting_date or today())
    rate = 0.0

    # 1) Existing Currency Exchange for this date (or older)
    rows = frappe.get_all(
        "Currency Exchange",
        filters={
            "from_currency": currency,
            "to_currency": company_currency,
            "date": ("<=", date),
            "for_selling": 1,
        },
        fields=["exchange_rate"],
        order_by="date desc",
        limit=1,
    )
    rate = flt(rows[0].exchange_rate) if rows else 0.0

    # 2) API checkout rate when the quote was in company currency
    if not rate and amounts:
        quoted_base = amounts.get("quoted_base_currency")
        applied = flt(amounts.get("applied_exchange_rate"))
        if quoted_base == company_currency and applied:
            # applied_exchange_rate converts quoted_base -> amounts.currency
            # We need currency -> company_currency, so invert.
            rate = flt(1 / applied)

    # 3) Stripe settlement rate when settlement is company currency
    if not rate and payment:
        settlement_currency = payment.get("settlement_currency")
        settlement_rate = flt(payment.get("settlement_exchange_rate"))
        if settlement_currency == company_currency and settlement_rate:
            rate = settlement_rate

    # 4) Fallback rates configured in Champions Hub Settings
    if not rate:
        for row in settings.get("exchange_rates") or []:
            if row.currency == currency and flt(row.exchange_rate) > 0:
                rate = flt(row.exchange_rate)
                break

    # 5) Skip Frankfurter for unsupported pairs (e.g. * → LYD) — it only spam-logs 404s
    # Prefer Settings / Currency Exchange. Do not call get_exchange_rate unless company
    # currency is a Frankfurter-supported one and no local rate exists.
    if not rate:
        company_currency_ok = company_currency not in ("LYD",)
        if company_currency_ok:
            try:
                rate = flt(
                    get_exchange_rate(currency, company_currency, date, args="for_selling")
                )
            except Exception:
                rate = 0.0

    if not rate:
        frappe.throw(
            (
                f"No exchange rate for {currency} → {company_currency} on {date}. "
                f"Add a row under Champions Hub Settings → Exchange Rates, "
                f"or create a Currency Exchange record."
            ),
            title="Missing Exchange Rate",
        )

    _ensure_currency_exchange(currency, company_currency, date, rate)
    return rate


def _ensure_currency_exchange(from_currency, to_currency, date, rate):
    """Upsert a Currency Exchange so ERPNext validation does not call Frankfurter."""
    existing = frappe.db.get_value(
        "Currency Exchange",
        {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "date": date,
        },
        "name",
    )
    if existing:
        if flt(frappe.db.get_value("Currency Exchange", existing, "exchange_rate")) != flt(rate):
            frappe.db.set_value("Currency Exchange", existing, "exchange_rate", rate)
        return existing

    doc = frappe.new_doc("Currency Exchange")
    doc.from_currency = from_currency
    doc.to_currency = to_currency
    doc.date = date
    doc.exchange_rate = rate
    doc.for_selling = 1
    doc.for_buying = 1
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_customer(student, billing, settings):
    """Create or update a Customer keyed on student.user_id."""
    user_id = student["user_id"]
    existing = frappe.db.get_value("Customer", {"champions_hub_user_id": user_id}, "name")

    customer_name_value = billing.get("company") or student.get("name") or student["email"]
    customer_type = "Company" if billing.get("company") else "Individual"

    if existing:
        doc = frappe.get_doc("Customer", existing)
        doc.customer_name = customer_name_value
        doc.customer_type = customer_type
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.new_doc("Customer")
    doc.customer_name = customer_name_value
    doc.customer_type = customer_type
    doc.customer_group = "All Customer Groups"
    doc.territory = "All Territories"
    doc.champions_hub_user_id = user_id
    doc.save(ignore_permissions=True)
    return doc.name


def _upsert_item(course):
    """Create or update an Item keyed on course.id."""
    item_code = course["id"]
    if frappe.db.exists("Item", item_code):
        doc = frappe.get_doc("Item", item_code)
        doc.item_name = course.get("title") or course.get("title_localized") or item_code
        doc.save(ignore_permissions=True)
        return item_code

    if not frappe.db.exists("Item Group", "Courses"):
        ig = frappe.new_doc("Item Group")
        ig.item_group_name = "Courses"
        ig.parent_item_group = "All Item Groups"
        ig.save(ignore_permissions=True)

    doc = frappe.new_doc("Item")
    doc.item_code = item_code
    doc.item_name = course.get("title") or course.get("title_localized") or item_code
    doc.item_group = "Courses"
    doc.is_stock_item = 0
    doc.save(ignore_permissions=True)
    return item_code


def _upsert_sales_invoice(
    source_id, customer, item_code, posting_date, currency, conversion_rate,
    invoice_total, discount, outstanding, due_date, settings, accounts
):
    """Create or update a submitted Sales Invoice keyed on source_id."""
    existing = frappe.db.get_value(
        "Sales Invoice", {"champions_enrollment_id": source_id}, "name"
    )

    if existing:
        return existing

    # Net line when discount would exceed gross (defensive)
    line_rate = flt(invoice_total)
    discount_amount = flt(discount)
    if discount_amount >= line_rate:
        # Bad/odd payload — keep net total, skip SI-level discount
        discount_amount = 0

    sinv = frappe.new_doc("Sales Invoice")
    sinv.customer = customer
    sinv.posting_date = posting_date or today()
    sinv.due_date = due_date or posting_date or today()
    sinv.currency = currency
    sinv.conversion_rate = conversion_rate
    sinv.company = settings.default_company
    sinv.debit_to = accounts.receivable
    sinv.champions_enrollment_id = source_id
    sinv.set_posting_time = 1
    sinv.ignore_pricing_rule = 1

    sinv.append("items", {
        "item_code": item_code,
        "qty": 1,
        "rate": line_rate,
        "income_account": accounts.income,
        "cost_center": accounts.cost_center,
    })

    if discount_amount > 0:
        sinv.apply_discount_on = "Grand Total"
        sinv.discount_amount = discount_amount

    sinv.insert(ignore_permissions=True)
    sinv.submit()
    return sinv.name


def _upsert_payment_entry(
    source_id, customer, amount_paid, currency, conversion_rate, posting_date,
    gateway, reference, sinv_name, settings, accounts
):
    """Create a Payment Entry linked to the Sales Invoice."""
    existing = frappe.db.get_value(
        "Payment Entry", {"champions_enrollment_id": source_id}, "name"
    )
    if existing:
        return existing

    gateway = gateway or "manual_transfer"
    paid_to = accounts.payment_accounts.get(gateway) or accounts.payment_accounts.get("stripe")
    paid_from = accounts.receivable
    company_currency = _company_currency(settings)

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.party_type = "Customer"
    pe.party = customer
    pe.posting_date = posting_date or today()
    pe.company = settings.default_company
    pe.paid_from = paid_from
    pe.paid_to = paid_to
    pe.paid_from_account_currency = frappe.db.get_value("Account", paid_from, "account_currency") or company_currency
    pe.paid_to_account_currency = frappe.db.get_value("Account", paid_to, "account_currency") or company_currency
    pe.paid_amount = amount_paid
    pe.received_amount = amount_paid
    pe.source_exchange_rate = conversion_rate if pe.paid_from_account_currency != company_currency else 1
    pe.target_exchange_rate = conversion_rate if pe.paid_to_account_currency != company_currency else 1
    # When party account is company currency but invoice is foreign, PE party currency follows invoice
    pe.party_account = paid_from
    pe.reference_no = reference or source_id
    pe.reference_date = posting_date or today()
    pe.mode_of_payment = GATEWAY_MODE_MAP.get(gateway, "Bank Transfer")
    pe.champions_enrollment_id = source_id

    # Allocate in invoice currency
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": sinv_name,
        "allocated_amount": amount_paid,
    })

    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name


def _upsert_fee_journal(source_id, fee, currency, conversion_rate, posting_date, settings, accounts):
    """Create a Journal Entry for gateway processing fees."""
    je_key = f"FEE-{source_id}"
    if frappe.db.exists("Journal Entry", {"cheque_no": je_key}):
        return

    if not accounts.fee_expense:
        return

    je = frappe.new_doc("Journal Entry")
    je.posting_date = posting_date or today()
    je.company = settings.default_company
    je.cheque_no = je_key
    je.cheque_date = posting_date or today()
    je.multi_currency = 1 if currency != _company_currency(settings) else 0
    je.user_remark = f"Champions Hub gateway fee for enrollment {source_id}"

    gateway_account = accounts.payment_accounts.get("stripe")
    je.append("accounts", {
        "account": accounts.fee_expense,
        "debit_in_account_currency": fee,
        "exchange_rate": conversion_rate,
        "cost_center": accounts.cost_center,
    })
    je.append("accounts", {
        "account": gateway_account,
        "credit_in_account_currency": fee,
        "exchange_rate": conversion_rate,
    })

    je.insert(ignore_permissions=True)
    je.submit()


def _upsert_credit_note(
    source_id, customer, item_code, amount, currency, conversion_rate,
    posting_date, sinv_name, settings, accounts
):
    """Create a Credit Note (return Sales Invoice) for refunds/chargebacks."""
    cn_key = f"CN-{source_id}"
    existing = frappe.db.get_value(
        "Sales Invoice", {"champions_enrollment_id": cn_key}, "name"
    )
    if existing:
        return existing

    if posting_date and "T" in str(posting_date):
        posting_date = str(posting_date)[:10]

    cn = frappe.new_doc("Sales Invoice")
    cn.customer = customer
    cn.posting_date = posting_date or today()
    cn.company = settings.default_company
    cn.currency = currency
    cn.conversion_rate = conversion_rate
    cn.is_return = 1
    cn.return_against = sinv_name
    cn.debit_to = accounts.receivable
    cn.champions_enrollment_id = cn_key
    cn.set_posting_time = 1

    cn.append("items", {
        "item_code": item_code,
        "qty": -1,
        "rate": amount,
        "income_account": accounts.income,
        "cost_center": accounts.cost_center,
    })

    cn.insert(ignore_permissions=True)
    cn.submit()
    return cn.name


def _upsert_log(
    source_id, status, posting_date, currency, amount_paid,
    invoice_total, outstanding, due_date, customer, item_code,
    sinv_name, pe_name, cn_name, raw, sync_status="Success", error_message=None
):
    """Create or update the Champions Enrollment Log."""
    existing = frappe.db.exists("Champions Enrollment Log", source_id)

    if existing:
        doc = frappe.get_doc("Champions Enrollment Log", source_id)
    else:
        doc = frappe.new_doc("Champions Enrollment Log")
        doc.source_id = source_id

    doc.status = status
    doc.sync_status = sync_status
    doc.error_message = error_message
    doc.posting_date = posting_date
    doc.currency = currency
    doc.amount_paid = amount_paid
    doc.invoice_total = invoice_total
    doc.outstanding = outstanding
    doc.due_date = due_date
    doc.customer = customer
    doc.customer_name_field = frappe.db.get_value("Customer", customer, "customer_name") if customer else None
    doc.item_code = item_code
    doc.sales_invoice = sinv_name
    doc.payment_entry = pe_name
    doc.credit_note = cn_name
    doc.raw_payload = json.dumps(raw, ensure_ascii=False, default=str)
    doc.save(ignore_permissions=True)


def log_enrollment_error(row, error_message):
    """Write a failed sync attempt to the enrollment log."""
    amounts = (row or {}).get("amounts") or {}
    installment = (row or {}).get("installment")
    posting_date = (row or {}).get("enrolled_at") or (row or {}).get("created_at")
    if posting_date and "T" in str(posting_date):
        posting_date = str(posting_date)[:10]

    _upsert_log(
        source_id=row["id"],
        status=row.get("status"),
        posting_date=posting_date,
        currency=amounts.get("currency"),
        amount_paid=(amounts.get("amount_paid_minor") or 0) / 100,
        invoice_total=(
            (installment["agreed_price_minor"] / 100)
            if installment
            else (amounts.get("amount_paid_minor") or 0) / 100
        ),
        outstanding=(installment["remaining_minor"] / 100) if installment else 0,
        due_date=installment["due_date"] if installment else None,
        customer=None,
        item_code=None,
        sinv_name=None,
        pe_name=None,
        cn_name=None,
        raw=row,
        sync_status="Error",
        error_message=error_message[:1000] if error_message else None,
    )
