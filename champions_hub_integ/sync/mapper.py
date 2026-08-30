import json
import frappe
from frappe.utils import flt, today, getdate


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
    bundle = row.get("bundle")
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
    product = course or bundle
    if product:
        item_code = _upsert_item(product)

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
    """Champions Hub accounting base currency (SAR). Not the local ERPNext Company currency."""
    return settings.get("base_currency") or "SAR"


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


def _lookup_api_exchange_rate(from_currency, to_currency, date):
    """Return a rate previously seeded from Champions Hub API enrollments."""
    rows = frappe.get_all(
        "Currency Exchange",
        filters={
            "from_currency": from_currency,
            "to_currency": to_currency,
            "date": ("<=", date),
            "for_selling": 1,
        },
        fields=["exchange_rate"],
        order_by="date desc",
        limit=1,
    )
    return flt(rows[0].exchange_rate) if rows else 0.0


def _seed_api_exchange_rates(amounts, payment, base_currency, date):
    """Persist FX pairs from the enrollment API payload for cross-rate lookups."""
    if not amounts:
        return

    currency = amounts.get("currency")
    quoted_base = amounts.get("quoted_base_currency")
    applied = flt(amounts.get("applied_exchange_rate"))

    # Price quoted in SAR: applied converts SAR -> payment currency.
    if quoted_base == base_currency and applied and currency and currency != base_currency:
        _ensure_currency_exchange(currency, base_currency, date, flt(1 / applied))

    # Paid in SAR: applied converts quoted_base -> SAR.
    if currency == base_currency and quoted_base and quoted_base != base_currency and applied:
        _ensure_currency_exchange(quoted_base, base_currency, date, applied)

    if not payment:
        return

    settlement_currency = payment.get("settlement_currency")
    settlement_rate = flt(payment.get("settlement_exchange_rate"))
    if not currency or not settlement_currency or not settlement_rate:
        return

    # Stripe settlement: e.g. 1 SAR = 0.266 USD → USD -> SAR = 1 / 0.266.
    if currency == base_currency and settlement_currency != base_currency:
        _ensure_currency_exchange(settlement_currency, base_currency, date, flt(1 / settlement_rate))


def _rate_from_api_amounts(currency, base_currency, amounts, date, payment=None):
    """
    Derive payment currency -> base currency (SAR) from enrollment API fields only.
    applied_exchange_rate converts quoted_base_currency -> payment currency.
    """
    if not amounts or not currency:
        return 0.0
    if currency == base_currency:
        return 1.0

    quoted_base = amounts.get("quoted_base_currency")
    applied = flt(amounts.get("applied_exchange_rate"))

    # Checkout quoted in SAR.
    if quoted_base == base_currency and applied:
        return flt(1 / applied)

    if not quoted_base:
        return _lookup_api_exchange_rate(currency, base_currency, date)

    # Same checkout and quote currency (USD/USD, EUR/EUR, ...).
    if currency == quoted_base:
        if quoted_base == base_currency:
            return 1.0
        return _lookup_api_exchange_rate(currency, base_currency, date)

    payment_to_quoted = 0.0
    if applied:
        payment_to_quoted = flt(1 / applied)
    else:
        gross_minor = flt(amounts.get("gross_minor"))
        quoted_minor = flt(amounts.get("quoted_base_amount_minor"))
        if gross_minor and quoted_minor:
            payment_to_quoted = quoted_minor / gross_minor

    if not payment_to_quoted:
        return _lookup_api_exchange_rate(currency, base_currency, date)

    if quoted_base == base_currency:
        return payment_to_quoted

    quoted_to_base = _lookup_api_exchange_rate(quoted_base, base_currency, date)
    if quoted_to_base:
        return payment_to_quoted * quoted_to_base

    return 0.0


def _resolve_conversion_rate(currency, posting_date, settings, amounts=None, payment=None):
    """
    Return base-currency (SAR) conversion rate for `currency` using API data only.
    """
    company_currency = _company_currency(settings)
    _ensure_currency(currency)

    if not currency or currency == company_currency:
        return 1.0

    date = getdate(posting_date or today())
    _seed_api_exchange_rates(amounts, payment, company_currency, date)
    rate = _rate_from_api_amounts(
        currency, company_currency, amounts, date, payment=payment
    )

    if not rate:
        frappe.throw(
            (
                f"No exchange rate for {currency} → {company_currency} on {date}. "
                f"The enrollment API did not include enough FX data "
                f"(amounts.applied_exchange_rate / quoted_base_currency)."
            ),
            title="Missing Exchange Rate",
        )

    _ensure_currency_exchange(currency, company_currency, date, rate)
    return rate


def _ensure_currency_exchange(from_currency, to_currency, date, rate):
    """Insert a Currency Exchange row when missing (first API rate wins)."""
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


def _default_customer_group(customer_type="Individual"):
    """Return a leaf Customer Group (ERPNext rejects group nodes on Customer)."""
    selling_group = frappe.db.get_single_value("Selling Settings", "customer_group")
    if selling_group and not frappe.db.get_value("Customer Group", selling_group, "is_group"):
        return selling_group

    preferred = "Commercial" if customer_type == "Company" else "Individual"
    if frappe.db.exists("Customer Group", preferred) and not frappe.db.get_value(
        "Customer Group", preferred, "is_group"
    ):
        return preferred

    group = frappe.db.get_value(
        "Customer Group",
        {"is_group": 0},
        "name",
        order_by="name asc",
    )
    if not group:
        frappe.throw(
            "No non-group Customer Group found. Create one under Selling > Customer Group.",
            title="Missing Customer Group",
        )
    return group


def _default_territory():
    """Return a leaf Territory (ERPNext rejects group nodes on Customer)."""
    selling_territory = frappe.db.get_single_value("Selling Settings", "territory")
    if selling_territory and not frappe.db.get_value("Territory", selling_territory, "is_group"):
        return selling_territory

    territory = frappe.db.get_value(
        "Territory",
        {"is_group": 0},
        "name",
        order_by="name asc",
    )
    if not territory:
        frappe.throw(
            "No non-group Territory found. Create one under Selling > Territory.",
            title="Missing Territory",
        )
    return territory


def _resolve_country(country_code):
    if not country_code:
        return None
    code = str(country_code).strip().upper()
    country = frappe.db.get_value("Country", {"code": code}, "name")
    if country:
        return country
    if frappe.db.exists("Country", country_code):
        return country_code
    return None


def _address_city(address_text, country_code):
    if address_text and isinstance(address_text, str):
        parts = [part.strip() for part in address_text.split(",") if part.strip()]
        if len(parts) >= 2 and parts[-1].replace(" ", "").isdigit():
            return parts[-2]
        if parts:
            return parts[-1]
    return country_code or "-"


def _billing_contact_fields(student, billing):
    """Extract email, phone, and address line from API billing (supports dict or string address)."""
    email = student.get("email")
    phone = billing.get("phone")
    address_text = billing.get("address")

    if isinstance(address_text, dict):
        email = address_text.get("email") or email
        phone = address_text.get("phone") or phone
        address_text = (
            address_text.get("line1")
            or address_text.get("address_line1")
            or address_text.get("address")
        )

    if address_text is not None and not isinstance(address_text, str):
        address_text = str(address_text)

    return email, phone, address_text


def _upsert_customer_address(customer_name, student, billing):
    """Create or update the primary billing Address with email and phone."""
    email, phone, address_text = _billing_contact_fields(student, billing)
    country = _resolve_country(billing.get("country"))

    if not any([email, phone, address_text, country]):
        return

    existing = frappe.db.sql(
        """
        SELECT a.name
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name
        WHERE dl.link_doctype = 'Customer'
            AND dl.link_name = %s
            AND IFNULL(a.is_primary_address, 0) = 1
        ORDER BY a.modified DESC
        LIMIT 1
        """,
        customer_name,
    )

    if existing:
        doc = frappe.get_doc("Address", existing[0][0])
    else:
        doc = frappe.new_doc("Address")
        doc.address_type = "Billing"
        doc.is_primary_address = 1
        doc.is_shipping_address = 1
        doc.address_title = frappe.db.get_value("Customer", customer_name, "customer_name")
        doc.append("links", {"link_doctype": "Customer", "link_name": customer_name})

    if address_text:
        doc.address_line1 = address_text
    elif not doc.address_line1:
        doc.address_line1 = doc.address_title or "-"

    doc.city = _address_city(address_text, billing.get("country")) or doc.address_line1
    if country:
        doc.country = country
    elif not doc.country:
        doc.country = frappe.db.get_single_value("Global Defaults", "country") or "Libya"

    if email:
        doc.email_id = email
    if phone:
        doc.phone = phone

    doc.flags.ignore_permissions = True
    doc.save() if existing else doc.insert()


def _upsert_customer(student, billing, settings):
    """Create or update a Customer keyed on student.user_id."""
    user_id = student["user_id"]
    existing = frappe.db.get_value("Customer", {"champions_hub_user_id": user_id}, "name")

    customer_name_value = billing.get("company") or student.get("name") or student["email"]
    customer_type = "Company" if billing.get("company") else "Individual"
    customer_group = _default_customer_group(customer_type)
    territory = _default_territory()

    if existing:
        doc = frappe.get_doc("Customer", existing)
        doc.customer_name = customer_name_value
        doc.customer_type = customer_type
        if not doc.customer_group or frappe.db.get_value("Customer Group", doc.customer_group, "is_group"):
            doc.customer_group = customer_group
        if not doc.territory or frappe.db.get_value("Territory", doc.territory, "is_group"):
            doc.territory = territory
        doc.save(ignore_permissions=True)
        _upsert_customer_address(doc.name, student, billing)
        return doc.name

    doc = frappe.new_doc("Customer")
    doc.customer_name = customer_name_value
    doc.customer_type = customer_type
    doc.customer_group = customer_group
    doc.territory = territory
    doc.champions_hub_user_id = user_id
    doc.save(ignore_permissions=True)
    _upsert_customer_address(doc.name, student, billing)
    return doc.name


def _product_item_name(product):
    return product.get("title") or product.get("title_localized") or product["id"]


def _find_item_by_name(item_name):
    """Reuse an existing course item when the same product title was synced before."""
    return frappe.db.get_value(
        "Item",
        {"item_name": item_name, "item_group": "Courses"},
        "name",
        order_by="creation asc",
    )


def _upsert_item(product):
    """Create or update an Item keyed on product id; reuse by title to avoid duplicates."""
    item_code = product["id"]
    item_name = _product_item_name(product)

    if frappe.db.exists("Item", item_code):
        doc = frappe.get_doc("Item", item_code)
        if doc.item_name != item_name:
            doc.item_name = item_name
            doc.save(ignore_permissions=True)
        return item_code

    existing_by_name = _find_item_by_name(item_name)
    if existing_by_name:
        return existing_by_name

    if not frappe.db.exists("Item Group", "Courses"):
        ig = frappe.new_doc("Item Group")
        ig.item_group_name = "Courses"
        ig.parent_item_group = "All Item Groups"
        ig.save(ignore_permissions=True)

    doc = frappe.new_doc("Item")
    doc.item_code = item_code
    doc.item_name = item_name
    doc.item_group = "Courses"
    doc.is_stock_item = 0
    doc.save(ignore_permissions=True)
    return item_code


def _upsert_sales_invoice(
    source_id, customer, item_code, posting_date, currency, conversion_rate,
    invoice_total, discount, outstanding, due_date, settings, accounts
):
    """Create or update a draft Sales Invoice keyed on source_id."""
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
    return sinv.name


def _upsert_payment_entry(
    source_id, customer, amount_paid, currency, conversion_rate, posting_date,
    gateway, reference, sinv_name, settings, accounts
):
    """Create a draft Payment Entry linked to the Sales Invoice."""
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
    """Create a draft Credit Note (return Sales Invoice) for refunds/chargebacks."""
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
